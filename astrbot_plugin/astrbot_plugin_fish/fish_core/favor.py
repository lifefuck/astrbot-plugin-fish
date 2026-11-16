# -*- coding: utf-8 -*-
"""好感度：每个群里的每个人一个 0~100 的分，决定她对这个人的态度底色。

和心境（affect.py）的区别要想清楚，不然会做重复的事：

    心境    整个群的当下情绪，几十分钟就衰减，影响这条回复的语气长短；
    好感度  对某个人的长期印象，以周为单位变化，影响「愿不愿意搭理这人」。

打分全部走本地规则，不调模型——旧版把打分交给 LLM，结果群友一句普通玩笑
被判定成挑衅，好感掉了还要被继续怼，越怼分越低，死锁。（详见
qq-group-bot-favor-and-anger-management 技能里的「成见死锁」。）

三条硬规则：
1. 戳一戳这类系统事件直通加分，不进冷却、不进文本打分管道；
2. 分数进 prompt 只说「态度」不说数字，模型看见数字会念出来；
3. 低分只说「带着戒备」，绝不写「对方在阴阳你」——那句话会让她自己脑补敌意。
"""

import json
import logging
import os
import re
import time

import backup

_logger = logging.getLogger(__name__)

DEFAULT_SCORE = 50.0
MIN_SCORE, MAX_SCORE = 0.0, 100.0

# 文本打分冷却：同一个人两分钟内连发十句只算一次，防止刷屏刷分
TEXT_COOLDOWN = 120.0
# 一周往中立回一点：不联系的人不该被永久定在某个分数上
DECAY_PERIOD = 7 * 86400

# 打分规则。顺序有意义：前面的先匹配到就停，避免「谢谢傻逼」两边都加
RULES = (
    (re.compile(r"(投喂|请你吃|请你喝|给你买|给你点|塞你嘴里)"), 5.0, "投喂"),
    (re.compile(r"(谢谢|多谢|辛苦了|厉害|太强|好听|可爱|喜欢你)"), 2.0, "顺毛"),
    (re.compile(r"(对不起|抱歉|我错|别生气|原谅我)"), 2.0, "认错"),
    (re.compile(r"(傻逼|傻b|脑瘫|弱智|废物|死全家|去死|滚蛋|闭嘴)"), -8.0, "辱骂"),
    (re.compile(r"(你不行|太菜|人机|笨死|蠢死|真菜|就这)"), -2.0, "挑刺"),
)

TIERS = ((80.0, "亲近"), (60.0, "熟人"), (40.0, "中立"), (0.0, "嫌弃"))

_state = {}      # {group_id: {person_id: {"s": 分数, "t": 最后变动时间, "d": 上次衰减时间, "n": 最近原因}}}
_path = ""
_writer = None
_last_save = 0.0
_dirty = False
_readonly = False   # 状态文件损坏时置真：这次运行只读，绝不覆盖磁盘上还能人工抢救的那份


def attach(writer=None, path: str = ""):
    global _writer, _path
    _writer = writer
    _path = path
    load()


def load():
    global _state, _dirty, _readonly
    _readonly = False
    if not _path or not os.path.exists(_path):
        return
    # 主文件坏了要回退 .bak：直接置空再保存，等于把所有人攒了几周的好感一次性抹掉
    data = backup.load_json_recover(_path)
    if not isinstance(data, dict):
        _state = {}
        _readonly = True
        _logger.warning("好感度状态读不出来（含备份），本次运行只读不落盘：%s", _path)
        return
    clean = {}
    for g, people in data.items():
        if not isinstance(people, dict):
            continue
        row = {}
        for u, v in people.items():
            if not isinstance(v, dict):
                continue
            try:
                score = float(v.get("s", DEFAULT_SCORE))
            except (TypeError, ValueError):
                continue  # 单条脏数据只丢这一条，不连累整个群
            item = dict(v)
            item["s"] = max(MIN_SCORE, min(MAX_SCORE, score))
            for k in ("t", "d"):
                try:
                    item[k] = float(item.get(k) or 0)
                except (TypeError, ValueError):
                    item[k] = 0.0
            item["n"] = str(item.get("n") or "")
            row[str(u)] = item
        clean[str(g)] = row
    _state = clean
    _dirty = False


def save(force: bool = False):
    global _last_save, _dirty
    if not _path or not _writer or _readonly:
        return
    now = time.time()
    if not force and (not _dirty or now - _last_save < 30):
        return
    try:
        _writer(_path, _state, pretty=False)
        _last_save = now
        _dirty = False
    except Exception as e:
        _logger.warning("好感度状态落盘失败（%s）：%s", _path, e)


def _clamp(v: float) -> float:
    return max(MIN_SCORE, min(MAX_SCORE, v))


def _person(group_id, person_id) -> dict:
    g = _state.setdefault(str(group_id), {})
    rec = g.get(str(person_id))
    if not isinstance(rec, dict):
        rec = {"s": DEFAULT_SCORE, "t": 0.0, "d": 0.0, "n": ""}
        g[str(person_id)] = rec
    return rec


def _decay(rec: dict, now: float):
    """按周往 50 分回落。只动没联系的人，常聊的人本来就会被来回改分。"""
    last = float(rec.get("d") or 0.0)
    if last <= 0:
        rec["d"] = now
        return
    periods = int((now - last) // DECAY_PERIOD)
    if periods <= 0:
        return
    cur = float(rec.get("s", DEFAULT_SCORE))
    for _ in range(min(periods, 8)):   # 离开很久也只回落到接近中立，不无限挨鞭子
        cur += -2.0 if cur > DEFAULT_SCORE else (1.0 if cur < DEFAULT_SCORE else 0.0)
    rec["s"] = _clamp(cur)
    # 低分抬高也要慢慢来：一次只补一个周期，剩下的留到下次
    rec["d"] = last + DECAY_PERIOD * max(1, periods)


def score(group_id, person_id, now: float | None = None) -> float:
    now = now if now is not None else time.time()
    rec = _person(group_id, person_id)
    _decay(rec, now)
    return float(rec.get("s", DEFAULT_SCORE))


def tier(value: float) -> str:
    for floor, name in TIERS:
        if value >= floor:
            return name
    return "嫌弃"


def bump(group_id, person_id, delta: float, reason: str = "", now: float | None = None) -> float:
    """直接改分。系统事件（戳一戳/管理员指令）走这里，绕开所有冷却。"""
    global _dirty
    now = now if now is not None else time.time()
    rec = _person(group_id, person_id)
    _decay(rec, now)
    rec["s"] = _clamp(float(rec.get("s", DEFAULT_SCORE)) + float(delta))
    rec["t"] = now
    if reason:
        rec["n"] = str(reason)[:24]
    _dirty = True
    save()
    return float(rec["s"])


def on_poke(group_id, person_id, now: float | None = None) -> float:
    """被戳：固定 +1，永远直通。

    旧版把戳一戳塞进 120 秒冷却之后再判断，冷却期内的戳直接被丢掉，
    群里表现为「戳了十几次分数一动不动」，最后还容易被模型判成骚扰。
    """
    return bump(group_id, person_id, 1.0, "戳了一下", now)


def on_message(group_id, person_id, text: str, now: float | None = None) -> float:
    """一句话带来的分变化。命中规则才动分，且同一人两分钟内只算一次。"""
    global _dirty
    now = now if now is not None else time.time()
    text = (text or "").strip()
    if not text or len(text) > 140:
        return score(group_id, person_id, now)

    fresh = str(person_id) not in _state.get(str(group_id), {})
    rec = _person(group_id, person_id)
    if fresh:
        # 第一次开口就落一条 50 分的记录：面板上得看得见人，不能只有被骂过的才出现
        # 只把「这人存在」落盘，别动 t——t 是冷却计时器，设成现在等于把这句也冷掉
        global _dirty
        _dirty = True
        save()
    if now - float(rec.get("t") or 0.0) < TEXT_COOLDOWN:
        return float(rec.get("s", DEFAULT_SCORE))

    for pattern, delta, reason in RULES:
        if pattern.search(text):
            return bump(group_id, person_id, delta, reason, now)
    return score(group_id, person_id, now)


def prompt_memo(group_id, person_id, now: float | None = None) -> str:
    """把分数说成一句态度说明。只有两端才注入，中立区不占 prompt。"""
    cur = score(group_id, person_id, now)
    if cur < 40:
        return ("【对这个人】：早前被冒犯过，现在带着点戒备，回他话短、不主动找话题；"
                "但他要是正常说话，就当没事，别翻旧账，更别把好意当成阴阳。")
    if cur >= 80:
        return "【对这个人】：挺熟的，愿意多搭两句，可以主动接他的话，也可以互损。"
    if cur >= 60:
        return "【对这个人】：算熟人，语气松一点就行，不用客气。"
    return ""


def people(group_id=None, now: float | None = None) -> list:
    """给面板用：按分数从低到高，低分的最该看一眼。"""
    now = now if now is not None else time.time()
    out = []
    for gid, persons in _state.items():
        if group_id and str(gid) != str(group_id):
            continue
        for uid, rec in persons.items():
            cur = score(gid, uid, now)
            out.append({"group_id": str(gid), "person_id": str(uid), "score": round(cur, 1),
                        "tier": tier(cur), "reason": str(rec.get("n") or "")})
    out.sort(key=lambda x: x["score"])
    return out


def reset(group_id=None, person_id=None):
    if group_id is None:
        _state.clear()
    elif person_id is None:
        _state.pop(str(group_id), None)
    else:
        _state.get(str(group_id), {}).pop(str(person_id), None)
    save(force=True)
