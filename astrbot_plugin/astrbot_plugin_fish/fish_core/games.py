# -*- coding: utf-8 -*-
"""群文字游戏状态机：数字炸弹 / 掷骰子 / 抽签 / 抛硬币。

纯本地判定，零模型调用，也不碰 astrbot——这样 selfcheck 能直接跑，出问题也
不用先怀疑是模型抽风。

状态仍然是「按群存内存」：进程一重启，进行中的炸弹就没了；这是有意的取舍，
群聊游戏丢了最多被骂一句「炸弹呢」，为它落盘不值得。

移植自 qq-deepseek-bot/games.py，对外三个入口的名字和签名都没动：
get_group_game / handle_quick_game_commands / process_number_bomb。

比旧实现多做的三件事，都是旧版留下的坑：
1. 旧版只在「本群有人说话」时才检查超时，一个群开了炸弹然后集体沉默，
   这条状态就会一直躺在 active_games 里直到进程重启；现在加 sweep_expired()
   做全表清扫，process_number_bomb 每次进来先扫一遍。
2. 旧版 t.isdigit() 放过了上标数字（"²"），isdigit() 为 True 但 int() 直接抛
   ValueError，一条闲聊就能把整轮消息处理打断；现在先判 ASCII 再转。
3. 旧版第 103 行那段「超过 5 分钟自动清理」是死代码：前面的过期回收已经把
   同样条件处理过了，走到那里 game 必然没过期。现在只留一处。
"""

from __future__ import annotations

import random
import re
import time
from typing import Dict, Optional, Tuple

# 群游戏状态管理: group_id -> game_state
# 形如 {"game": "bomb", "target": 42, "low": 1, "high": 100,
#       "start_time": 123.0, "last_active": 123.0, "guesses": 0}
active_games: Dict[int, dict] = {}

# 猜拳/掷骰/抽签这类无状态玩法共用的随机源。测试里直接替换成固定种子即可复现。
_RNG = random.Random()

# 多久没人正经猜一次就回收。只统计「有效指令」，群友闲聊不续命——
# 否则一个冷群聊两天，炸弹还能一直挂着。
IDLE_TIMEOUT_SEC = 300

_BOMB_START = ("数字炸弹", "开炸弹", "开数字炸弹", "/bomb")
_BOMB_STOP = ("退出数字炸弹", "结束数字炸弹", "结束炸弹", "/stopbomb")


def get_group_game(group_id: int) -> Optional[dict]:
    """拿本群进行中的游戏状态，没有就 None。

    返回的是内存里的活对象（和旧版一致），调用方只读，别改 low/high，
    改了状态机的不变式 low < target < high 就破了。
    """
    return active_games.get(int(group_id))


def sweep_expired(now: Optional[float] = None) -> int:
    """清掉所有群里已经超时的炸弹，返回清掉的个数。

    旧版没有这个：超时只在「该群下一条消息」到达时才检查，一个开了炸弹就
    再没人说话的群，状态会永久占着内存（进程不重启就不释放）。调用方可以
    挂个定时器周期性调它；不挂也不会错，process_number_bomb 每次都会先扫。
    """
    now = time.time() if now is None else now
    dead = [
        gid for gid, g in active_games.items()
        if g.get("game") == "bomb" and now - g.get("last_active", 0.0) > IDLE_TIMEOUT_SEC
    ]
    for gid in dead:
        active_games.pop(gid, None)
    return len(dead)


def _parse_guess(text: str) -> Optional[int]:
    """把一条消息读成数字，不是纯数字返回 None。

    坑：str.isdigit() 对上标/圈号（²、①）也返回 True，而 int() 会抛
    ValueError——旧版直接 len(t) <= 7 后 int(t)，群友随手一个 ² 就能炸掉
    整条消息处理。所以这里先限 ASCII，再兜一层异常。
    """
    if not text or len(text) > 7 or not text.isascii() or not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # 理论上到不了，留着当保险
        return None


def handle_quick_game_commands(text: str) -> Optional[str]:
    """处理掷骰子、抽签、投硬币等单次免状态游戏，命中返回一句话，否则 None。"""
    t = text.strip()

    # 1. 掷骰子 / roll
    # 支持: roll, roll 100, 掷骰子, /roll, 摇骰子
    m_roll = re.match(r"^(?:/)?(?:roll|摇骰子|掷骰子|丢骰子)(?:\s+(\d+))?$", t, re.IGNORECASE)
    if m_roll:
        _digits = m_roll.group(1) or ""
        # 超过七位不静默改成 100：用户喊「roll 12345678」，回一句「1~100」等于答非所问
        if len(_digits) > 7:
            return "数字太大啦，最多七位（最大 1000000）"
        # Python 3.11 起 int() 超过 4300 位直接抛 ValueError（含超长数字串的闲聊会让
        # 整条消息处理中断），长度已经先拦住了
        max_val = int(_digits) if _digits else 100
        if max_val < 1:
            max_val = 6
        elif max_val > 1000000:
            max_val = 1000000
        val = _RNG.randint(1, max_val)
        return f"骰子滚出 {val} 点（1~{max_val}）"

    # 2. 抛硬币
    if t in ("抛硬币", "扔硬币", "硬币", "/coin"):
        res = "正面（字）" if _RNG.random() < 0.5 else "反面（花）"
        return f"硬币落地，是【{res}】"

    # 3. 抽签 / 今日运势
    if t in ("抽签", "求签", "今日运势", "/draw"):
        lots = [
            ("大吉", "今天吃大白饭都不会发胖，想抽卡直接出金！"),
            ("中吉", "平平淡淡才是真，摸摸肥鱼头能转运～"),
            ("小吉", "有些小惊喜在路上，适合今天把任务清一清！"),
            ("末吉", "稳健行事，水群小心被大肥鱼怼哦～"),
            ("大凶", "危！今天出门看路，千万不要碰十连抽！"),
            ("万事如意", "大肥鱼专属庇护，今天你是群里最靓的仔！"),
        ]
        lot, desc = _RNG.choice(lots)
        return f"抽到【{lot}】：\n「{desc}」"

    return None


def process_number_bomb(group_id: int, text: str, user_id: int,
                        sender_nick: str) -> Tuple[bool, Optional[str]]:
    """处理群内数字炸弹逻辑，返回 (is_handled, reply_text)。

    user_id 目前不参与判定（旧版就没用），留着是为了调用方签名一致，
    将来要加「同一个人不能连猜」时直接拿它。
    """
    # 群号统一成 int：调用方有的地方传 str(gid)，键类型不一致会在同一个群里
    # 开出两局炸弹，各自记各自的区间
    group_id = int(group_id)
    t = text.strip()
    now = time.time()

    # 先做全表清扫，再取本群状态：别的群在聊，也顺带把冷掉的炸弹收了
    sweep_expired(now)
    game = active_games.get(group_id)

    # 发起新游戏
    if t in _BOMB_START:
        if game and game.get("game") == "bomb":
            return True, f"游戏还在进行，当前范围 [{game['low']} ~ {game['high']}]，继续猜"

        target = _RNG.randint(2, 99)
        active_games[group_id] = {
            "game": "bomb",
            "target": target,
            "low": 1,
            "high": 100,
            "start_time": now,
            "last_active": now,
            "guesses": 0,
        }
        return True, "【数字炸弹】装好了\n范围 1~100，谁猜中谁挨炸。谁先来？"

    # 结束游戏
    if t in _BOMB_STOP:
        if game and game.get("game") == "bomb":
            target = game["target"]
            active_games.pop(group_id, None)
            return True, f"炸弹拆了，答案是【{target}】"
        return False, None

    # 有活跃炸弹时，纯数字就是一次猜测
    if game and game.get("game") == "bomb":
        val = _parse_guess(t)
        if val is None:
            return False, None

        low = game["low"]
        high = game["high"]
        target = game["target"]

        # 区间是开区间：low/target/high 三个值里，low 和 high 是已经排除掉的
        if val <= low or val >= high:
            # 区间是开区间，low/high 自己已经被排除。旧提示写「在 low ~ high 之间」，
            # 猜 low 的人看到「要猜 1~100」会接着猜 1，来回死循环还觉得自己没错
            return True, f"超出范围啦，能猜的是 {low + 1} ~ {high - 1} 之间的整数"

        game["guesses"] += 1
        game["last_active"] = now

        if val == target:
            guesses = game["guesses"]
            active_games.pop(group_id, None)
            punishments = [
                "发一张自己的丑照！",
                "在群里大喊三声「大肥鱼最可爱最美」！",
                "给群里任意一个人说一句土味情话！",
                "发个 1 块钱 3 个的群红包！",
            ]
            p = _RNG.choice(punishments)
            return True, (
                f"BOOM，炸了\n"
                f"【{sender_nick}】踩中【{target}】\n"
                f"猜了 {guesses} 轮，惩罚是：\n{p}"
            )
        elif val < target:
            game["low"] = val
            return True, f"安全，范围缩到【{val} ~ {high}】"
        else:
            game["high"] = val
            return True, f"安全，范围缩到【{low} ~ {val}】"

    return False, None
