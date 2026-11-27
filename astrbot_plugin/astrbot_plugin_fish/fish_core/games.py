# -*- coding: utf-8 -*-
"""群文字游戏：掷骰子 / 抛硬币 / 抽签。

纯本地判定，零模型调用，也不碰 astrbot——这样 selfcheck 能直接跑，出问题也
不用先怀疑是模型抽风。全是单次触发、无跨消息状态：不存 active_games，
也就没有超时回收、没有「进程重启这局就没了」这类问题。

数字炸弹已删除（2026-11-27）：它要维护每群一份 target/low/high 状态，
和插话调度、记忆注入抢同一条消息通路，收益远小于复杂度，用户决定砍掉。
旧的 process_number_bomb / active_games / sweep_expired 一并移除。
"""

from __future__ import annotations

import random
import re
from typing import Optional

# 猜拳/掷骰/抽签这类无状态玩法共用的随机源。测试里直接替换成固定种子即可复现。
_RNG = random.Random()


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
