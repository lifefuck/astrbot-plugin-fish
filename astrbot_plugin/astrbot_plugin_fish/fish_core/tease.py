# -*- coding: utf-8 -*-
"""调侃许可：谁能被挤对、谁能听鱼自嘲，都按人按群算，不靠模型自觉。

为什么要有这一层：好感分只回答「他最近对鱼怎么样」，回答不了「能不能拿他开玩笑」。
新人刚进群好感 50，跟熟人聊了三周的 50 分完全是两码事；被拒绝过一次的人更是
必须记住——模型每轮都会忘，文件不会。

三级（默认最低）：
    L0 无许可  中性。不调侃对方，也不自嘲（对生人自嘲显得轻浮）
    L1 低许可  可以自嘲（犯蠢、要饭、摆烂），不许拿对方说事
    L2 高许可  可以挤对对方，只限行为与话语（迟到、连跪、嘴硬、说话绕）

禁区任何级别都不许碰：外貌、性别、年龄、家庭、工作能力与收入、健康、地域民族、宗教。
这条不交给模型判断——出口还有一道 banned_attribute() 兜底。

降级永不过期：被拒绝一次就记 30 天，道歉和投喂都不缩短；累计三次锁死 L0，
只能人工 force() 解。升级 30 天不互动回落到 L1（不直接掉回 L0，忽冷忽热更像假的）。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import backup

LEVEL_NONE = 0
LEVEL_LOW = 1
LEVEL_HIGH = 2

# 升级门槛（不开放配置：没人会去调「攒几点算熟」）
_POINTS_LOW = 6
_POINTS_HIGH = 12
_UPGRADE_TTL_SEC = 30 * 86400.0   # 升级多久不用就回落
_SELF_TEASE_WINDOW = 3600.0
_SELF_TOPIC_TTL = 3 * 86400.0
_SELF_RECENT_KEEP = 6
_FP_SIM = 0.6                     # 自嘲指纹相似度到这个数就算重了

# 明确拒绝：命中即降级。宁可多认几句（「别闹」「正经点」），也别漏掉真生气
_REFUSE_RE = re.compile(
    r"别开这种玩笑|别拿我(说事|开玩笑|打趣|寻开心)|不喜欢(这样|被说|你这么说)|不舒服|"
    r"过了啊|别闹了|别逗我|生气了|我真生气了|闭嘴|别这样|少来这套|正经点|说正经的|"
    r"别损我|别嘴我|别调侃我|不熟|咱俩不熟|别套近乎|别跟我套近乎")

# 正向互动：接住玩笑、投喂、戳、主动找话
_GOOD_RE = re.compile(r"哈哈|嘿嘿|笑死|草|6{2,}|确实|服了|行吧|好吧|戳|喂|给你|记得|辛苦|谢谢")

# 行为层调侃：只认这些词，别的都算越界
_BEHAVIOR_RE = re.compile(
    r"迟到|早退|睡过头|熬夜|通宵|连跪|掉分|手滑|手抖|又忘|忘性|嘴硬|犟|杠精|"
    r"摸鱼|摆烂|划水|鸽了|放鸽子|拖拉|磨叽|话痨|话多|绕|啰嗦|记性差")

# 禁区词：出口兜底用。宁可误拦一句，也不要让「你胖了」这种话发出去
# 只收「明确指人」的说法：光有「脸」「土」「病」这种单字会误伤正常话，
# 实测「你脸上写着不想干」这种正话会被拦掉
_BANNED_RE = re.compile(
    r"你(好胖|胖了|太瘦|瘦了|好丑|真丑|太矮|好矮|秃了|秃头)|颜值低|长得丑|难看死了|"
    r"你(的脸|皮肤|身材|胸|腿)|腿粗|胸小|平胸|"
    r"娘炮|男人婆|女汉子|剩女|光棍|单身狗|嫁不出去|娶不到|"
    r"你妈|你爸|你家人|你儿子|你闺女|你老婆|你老公|离婚|"
    r"废物|啥也干不成|能力差|水平不行|混得差|你工资|你收入|穷鬼|穷酸|"
    r"你有病|生病|残疾|抑郁|吃药|"
    r"外地人|农村人|乡下人|没文化|土包子|"
    r"信教|基督徒|佛教|伊斯兰")

_state: Dict[str, Any] = {"people": {}, "self": {}}
_writer = None
_path = ""
_dirty = False
_readonly = False


def attach(writer=None, path: str = ""):
    global _writer, _path
    _writer = writer
    _path = path
    load()


def load():
    global _state, _dirty, _readonly
    _readonly = False
    _state = {"people": {}, "self": {}}
    if not _path or not os.path.exists(_path):
        return
    data = backup.load_json_recover(_path)
    if not isinstance(data, dict):
        # 读不出来就只读运行：宁可这一轮少一句调侃，也不能把攒下的 ban 洗掉
        _readonly = True
        return
    people = {}
    for g, row in (data.get("people") or {}).items():
        if not isinstance(row, dict):
            continue
        clean = {}
        for u, v in row.items():
            if isinstance(v, dict):
                clean[str(u)] = v
        people[str(g)] = clean
    _state = {"people": people, "self": dict(data.get("self") or {})}
    _dirty = False


def save(force: bool = False):
    global _dirty
    if _readonly or not _path or not _writer:
        return
    if not (_dirty or force):
        return
    try:
        _writer(_path, _state)
        _dirty = False
    except Exception:
        pass


def _row(gid: str, uid: str, create: bool = False) -> Optional[dict]:
    g = str(gid or "")
    u = str(uid or "")
    if not g or not u:
        return None
    people = _state.setdefault("people", {}).setdefault(g, {})
    row = people.get(u)
    if row is None and create:
        row = {"level": LEVEL_NONE, "points": 0, "since": time.time(),
               "last": 0.0, "strikes": 0, "ban_until": 0.0,
               "locked": False, "why": "", "evidence": []}
        people[u] = row
        _dirty = True
    return row


def _base_level(favor_score: Optional[float], cfg: Optional[dict] = None) -> int:
    hi = float((cfg or {}).get("tease_high_favor", 80) or 80)
    lo = float((cfg or {}).get("tease_low_favor", 60) or 60)
    try:
        s = float(favor_score if favor_score is not None else 50)
    except (TypeError, ValueError):
        s = 50.0
    if s >= hi:
        return LEVEL_HIGH
    if s >= lo:
        return LEVEL_LOW
    return LEVEL_NONE


def level(gid: str, uid: str, favor_score: Optional[float] = None,
          cfg: Optional[dict] = None, now: Optional[float] = None) -> int:
    """当前生效级别。ban / locked 永远压到 0，好感分只给上限。"""
    now = float(now if now is not None else time.time())
    row = _row(gid, uid)
    cap = _base_level(favor_score, cfg)
    if not row:
        return cap
    if row.get("locked") or float(row.get("ban_until") or 0) > now:
        return LEVEL_NONE
    earned = int(row.get("level") or 0)
    since = float(row.get("since") or 0) or now
    # 升级过期回落：只掉一档，直接掉回 L0 会让人觉得「昨天还熟，今天装不认识」
    if earned and now - since > _UPGRADE_TTL_SEC:
        earned -= 1
    return max(LEVEL_NONE, min(earned, cap))


def _rise(row: dict, target: int, now: float):
    need = _POINTS_LOW if target <= LEVEL_LOW else _POINTS_HIGH
    row["points"] = int(row.get("points") or 0) + 1
    if int(row.get("points") or 0) >= need and int(row.get("level") or 0) < target:
        row["level"] = int(row.get("level") or 0) + 1
        row["points"] = 0
        row["since"] = now
        _dirty = True


def note_interaction(gid: str, uid: str, user_text: str, bot_text: str = "",
                     favor_score: Optional[float] = None, cfg: Optional[dict] = None,
                     now: Optional[float] = None) -> int:
    """一次互动后的许可记账。返回记账后的级别。

    升级只看「他没翻脸」：接了话、笑了一下、或者好感分本来就够。
    被拒绝走 refuse()，这里只处理温和的升与降。
    """
    now = float(now if now is not None else time.time())
    row = _row(gid, uid, create=True)
    if not row:
        return LEVEL_NONE
    row["last"] = now
    if refuse_matched(user_text):
        return _downgrade(gid, uid, "他说了拒绝的话", now, cfg)
    cap = _base_level(favor_score, cfg)
    if cap > int(row.get("level") or 0):
        # 好感够了直接给，不用攒点数：熟不熟本来就该由分数说了算
        row["level"] = cap
        row["since"] = now
        _dirty = True
    elif _GOOD_RE.search(user_text or "") and int(row.get("level") or 0) < cap:
        _rise(row, cap, now)
    save()
    return level(gid, uid, favor_score, cfg, now)


def refuse_matched(text: str) -> bool:
    return bool(_REFUSE_RE.search(text or ""))


def _downgrade(gid: str, uid: str, why: str, now: float,
               cfg: Optional[dict] = None) -> int:
    row = _row(gid, uid, create=True)
    if not row:
        return LEVEL_NONE
    days = float((cfg or {}).get("tease_ban_days", 30) or 30)
    row["level"] = LEVEL_NONE
    row["points"] = 0
    row["strikes"] = int(row.get("strikes") or 0) + 1
    row["ban_until"] = now + days * 86400.0
    row["why"] = why
    row["since"] = now
    if int(row["strikes"]) >= 3:
        row["locked"] = True     # 三次就别再试了，只能人工解
        row["why"] = why + "（累计三次，锁死）"
    _dirty = True
    save()
    return LEVEL_NONE


def refuse(gid: str, uid: str, text: str = "", now: Optional[float] = None,
           cfg: Optional[dict] = None) -> bool:
    """被明确拒绝：当场降级并长期保留。返回是否命中。"""
    if not refuse_matched(text):
        return False
    now = float(now if now is not None else time.time())
    row = _row(gid, uid, create=True)
    if row:
        ev = row.setdefault("evidence", [])
        ev.append({"ts": now, "text": (text or "")[:60]})
        del ev[:-5]
    _downgrade(gid, uid, "被明确拒绝：%s" % (text or "")[:30], now, cfg)
    return True


def force(gid: str, uid: str, lv: int = LEVEL_NONE, why: str = "", unlock: bool = True):
    """人工改许可（主人用）。唯一能解 locked 的口子。"""
    row = _row(gid, uid, create=True)
    if not row:
        return
    row["level"] = max(LEVEL_NONE, min(LEVEL_HIGH, int(lv)))
    row["since"] = time.time()
    row["why"] = why or "人工设定"
    if unlock:
        row["locked"] = False
        row["ban_until"] = 0.0
        row["strikes"] = 0
    _dirty = True
    save(force=True)


def memo(gid: str, uid: str, favor_score: Optional[float] = None,
         cfg: Optional[dict] = None, now: Optional[float] = None) -> str:
    """注入 prompt 的一句分寸说明。三级各一句，绝不堆。"""
    lv = level(gid, uid, favor_score, cfg, now)
    if lv >= LEVEL_HIGH:
        return ("【玩笑的分寸】：跟这人算熟，可以笑着挤对他一下——只说行为（迟到、连跪、"
                "嘴硬、说话绕），外貌、性别、家里、工作能力一个字都不碰；他没接就收。")
    if lv == LEVEL_LOW:
        return ("【玩笑的分寸】：可以拿自己开涮（犯蠢、要饭、摆烂那种），别拿他本人说事，"
                "也别问他私事。")
    return ("【玩笑的分寸】：跟这人只有正事和普通闲聊，不开他玩笑，也别自嘲——"
            "刚认识就自嘲显得轻浮。")


def banned_attribute(text: str) -> str:
    """出口兜底：命中禁区属性就把这个词报出来，调用方整句丢掉。"""
    m = _BANNED_RE.search(text or "")
    return m.group(0) if m else ""


# ---------------- 自嘲节流与防模板化 ----------------

def _self_row(gid: str) -> dict:
    g = str(gid or "")
    self_map = _state.setdefault("self", {})
    row = self_map.get(g)
    if not isinstance(row, dict):
        row = {"ts": [], "topics": {}, "recent": [], "turn": 0}
        self_map[g] = row
    return row


def allow_self_tease(gid: str, cfg: Optional[dict] = None,
                     now: Optional[float] = None) -> bool:
    now = float(now if now is not None else time.time())
    per = int((cfg or {}).get("self_tease_per_hour", 1) or 1)
    row = _self_row(gid)
    ts = [t for t in (row.get("ts") or []) if now - float(t) < _SELF_TEASE_WINDOW]
    row["ts"] = ts
    return len(ts) < per


def next_topic(gid: str, now: Optional[float] = None) -> str:
    """轮换自嘲主题：同一主题 3 天内不用第二次，免得「我怎么这么笨」天天见。"""
    now = float(now if now is not None else time.time())
    topics = ("犯蠢", "要饭", "摆烂", "手残", "穷")
    row = _self_row(gid)
    used = row.setdefault("topics", {})
    for t in topics:
        if now - float(used.get(t) or 0) > _SELF_TOPIC_TTL:
            return t
    oldest = min(topics, key=lambda t: float(used.get(t) or 0))
    return oldest


def _fp(text: str) -> set:
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text or "")
    return {s[i:i + 2] for i in range(max(0, len(s) - 1))}


def _sim(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def note_self_tease(gid: str, text: str, topic: str = "",
                    now: Optional[float] = None):
    now = float(now if now is not None else time.time())
    row = _self_row(gid)
    row.setdefault("ts", []).append(now)
    del row["ts"][:-12]
    if topic:
        row.setdefault("topics", {})[topic] = now
    recent = row.setdefault("recent", [])
    recent.append({"ts": now, "fp": sorted(_fp(text))})
    del recent[:-_SELF_RECENT_KEEP]
    _dirty = True
    save()


def looks_like_self_tease(text: str) -> bool:
    """粗判这句是不是自嘲：主语是自己 + 一个贬义词。"""
    return bool(re.search(r"我(怎么|真是|又|这|也太|就是|好|真)", text or "")) and bool(
        re.search(r"蠢|笨|菜|穷|废|手残|摆烂|要饭|记不住|脑子|完了|没救了|不行", text or ""))


def strip_repeat_self_tease(gid: str, text: str) -> Tuple[str, str]:
    """出口指纹去重：跟最近几句自嘲撞得太像就删掉那句，不重新生成。

    返回 (可用文本, 被删掉的那句)。全删空了就返回空串，调用方按「没正文」静默。
    """
    if not text or not looks_like_self_tease(text):
        return text, ""
    row = _self_row(gid)
    olds = [set(r.get("fp") or []) for r in (row.get("recent") or [])]
    if not olds:
        return text, ""
    parts = re.split(r"(?<=[。！？!?~～])|\n", text)
    kept, dropped = [], ""
    for p in parts:
        p = (p or "").strip()
        if not p:
            continue
        if looks_like_self_tease(p):
            fp = _fp(p)
            if any(_sim(fp, o) >= _FP_SIM for o in olds):
                dropped = p
                continue        # 撞车的那句丢，其余照发
        kept.append(p)
    return ("".join(kept) if kept else ""), dropped


def stats() -> dict:
    people = _state.get("people") or {}
    n = sum(len(v) for v in people.values() if isinstance(v, dict))
    locked = sum(1 for v in people.values() if isinstance(v, dict)
                 for r in v.values() if isinstance(r, dict) and r.get("locked"))
    return {"people": n, "locked": locked, "groups": len(people)}
