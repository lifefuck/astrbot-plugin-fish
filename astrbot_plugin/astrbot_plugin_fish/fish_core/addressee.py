# -*- coding: utf-8 -*-
"""收件人判定表：这条消息到底是说给谁的。

为什么要有它：
判定链只回答了「多该说」（被@/追问/主动），没回答「这句是不是冲我说的」。
群里两个人聊得正热，鱼按话题相邻一头扎进去抢答；有人 @ 了别人顺口提一句「鱼」，
它又当成在叫自己。抢答比漏接更伤——漏接只是安静，抢答是打断别人。

打分口径（五级 + 反向扣分，0–100，越高越是说给鱼）：

    L1 显式     @我 / 引用我 / 手打唤醒前缀          100
    L2 点名     正文出现学到过的别名（At 昵称/群名片） 70
    L2 代词     正文有「你」且鱼刚说过话              60
    L3 接续     追问窗口内                            60
    L4 相邻     鱼 60 秒内刚说过话                    30
    L5 无信号   ——                                    0

反向扣分（叠加）：
    @ 了别人 −60 / 引用了别人 −50 / A↔B 活跃线程 −40 / 该线程已交替 3 轮以上 −30

判定：≥ hard（默认 70）认定说给鱼；40–70 灰区**默认不是**，只有「追问窗口内且
判官质量分过线」才放行；< 40 直接不接。宁可漏接不可抢答。

两张附表：
- 活跃线程表：A、B 交替发言 ⇒ 视为两人对话，鱼是旁观者，除非被 @ / 引用拉入。
  只存内存，180 秒无新消息自动过期（不落盘，重启丢掉也无所谓）。
- 别名表：只从真实 At 昵称 + 群名片学，绝不从聊天内容猜外号；按群隔离，
  一个群学到的小名不许串到另一个群。落盘 data/aliases.json。

这个模块是纯逻辑：不 import 框架、不读配置对象，所有口径从 cfg 字典读，
所有时间从参数传进来——测试可以直接喂构造好的事实。
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

# —— 打分常量（口径，不进配置：没人会去调一个「@别人扣 60 分」的滑块）——
S_EXPLICIT = 100      # @我 / 引用我 / 唤醒前缀
S_ALIAS = 70          # 正文点到鱼的名字
S_PRONOUN = 60        # 「你」+ 鱼刚说过话
S_FOLLOW = 60         # 追问窗口内
S_NEAR = 30           # 鱼 60 秒内刚说过话

P_AT_OTHERS = 60      # @ 了别人
P_REPLY_OTHERS = 50   # 引用了别人的话
P_THREAD = 40         # 处在别人与别人的活跃线程里
P_THREAD_DEEP = 30    # 该线程已经来回 3 轮以上

# 只有「你/你们」这种明确第二人称才算代词指向；「您」在群里基本是阴阳怪气，不算
_PRONOUNS = ("你", "你们", "妳")
# 鱼自己可能被叫的名字，兜底用；真正的别名靠 At 昵称学
_HARD_NAMES = ("小鲸鱼", "鲸鱼", "大肥鱼", "鲸宝", "傻鱼")

_ALIAS_PATH: Optional[str] = None
_ALIASES: Dict[str, Dict[str, str]] = {}   # gid -> {名字: uid}
_ALIAS_LOADED = False
_READONLY = False        # 测试置 True：只算不落盘

_TABLE: Dict[str, deque] = {}              # gid -> [MsgRef] 最近 200 条
_THREADS: Dict[str, Dict[Tuple[str, str], dict]] = {}   # gid -> (a,b) -> {last_ts, turns}


@dataclass
class MsgRef:
    """收件人表里的一行。"""

    uid: str = ""
    nick: str = ""
    text: str = ""
    ts: float = 0.0
    at_uids: tuple = ()
    reply_uid: str = ""
    topic: str = ""
    to_uid: str = ""        # 判定这轮说给谁：self_id / 别人 uid / 空 = 不确定
    score: int = 0
    reason: str = ""

    def line(self) -> str:
        # 不带 [fish][rcpt] 前缀：logger 那层已经带了，重复一次日志里会成双
        return ("%s→%s score=%d(%s) at=%s quote=%s topic=%s" % (
            self.nick or self.uid, self.to_uid or "-", self.score, self.reason or "-",
            ",".join(self.at_uids) or "-", self.reply_uid or "-", self.topic or "-"))


# ---------------------------------------------------------------- 别名表
def attach(path: Optional[str]) -> None:
    """指定别名落盘位置（插件启动时调一次）。"""
    global _ALIAS_PATH, _ALIAS_LOADED
    _ALIAS_PATH = str(path) if path else None
    _ALIAS_LOADED = False
    _ALIASES.clear()


def _load_aliases() -> None:
    global _ALIAS_LOADED
    if _ALIAS_LOADED:
        return
    _ALIAS_LOADED = True
    if not _ALIAS_PATH:
        return
    try:
        raw = json.loads(Path(_ALIAS_PATH).read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    for gid, mp in raw.items():
        if isinstance(mp, dict):
            _ALIASES[str(gid)] = {str(k): str(v) for k, v in mp.items() if k and v}


def _save_aliases() -> None:
    if _READONLY or not _ALIAS_PATH:
        return
    try:
        Path(_ALIAS_PATH).write_text(
            json.dumps(_ALIASES, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def learn_alias(gid: str, name: str, uid: str) -> bool:
    """学一条别名。只认真实来源（At 昵称、群名片），空名/自己和 all 跳过。"""
    name = (name or "").strip()
    uid = str(uid or "").strip()
    if not gid or not name or not uid or uid in ("all", "0"):
        return False
    _load_aliases()
    mp = _ALIASES.setdefault(str(gid), {})
    if mp.get(name) == uid:
        return False
    mp[name] = uid
    _save_aliases()
    return True


def names_of(gid: str, uid: str) -> tuple:
    """这个 uid 在群里被叫过的名字（学到的），不含兜底名单。"""
    _load_aliases()
    mp = _ALIASES.get(str(gid)) or {}
    return tuple(n for n, u in mp.items() if u == str(uid))


def aliases_of(gid: str) -> Dict[str, str]:
    _load_aliases()
    return dict(_ALIASES.get(str(gid)) or {})


def reset(gid: Optional[str] = None) -> None:
    """清空内存表（测试用）。"""
    global _ALIAS_LOADED
    if gid is None:
        _TABLE.clear()
        _THREADS.clear()
        _ALIASES.clear()
        _ALIAS_LOADED = False
    else:
        _TABLE.pop(str(gid), None)
        _THREADS.pop(str(gid), None)
        _ALIASES.pop(str(gid), None)


# ---------------------------------------------------------------- 活跃线程表
def _thread_update(gid: str, uid: str, now: float, window: float) -> None:
    """按「上一条是谁 → 这一条是谁」维护交替计数。"""
    table = _TABLE.get(str(gid))
    if not table or not uid:
        return
    prev = table[-1].uid if table else ""
    if not prev or prev == uid:
        return
    pair = (prev, uid) if prev < uid else (uid, prev)
    th = _THREADS.setdefault(str(gid), {})
    rec = th.get(pair)
    if rec and now - float(rec.get("last_ts") or 0) > window:
        rec = None          # 冷太久了，重新起一轮
    if rec is None:
        th[pair] = {"last_ts": now, "turns": 1}
    else:
        rec["turns"] = int(rec.get("turns") or 0) + 1
        rec["last_ts"] = now
    # 顺手清掉过期线程，别让表无限涨
    for k in [k for k, v in th.items() if now - float(v.get("last_ts") or 0) > window * 2]:
        th.pop(k, None)


def thread_of(gid: str, uid: str, now: float, window: float,
              self_id: str = "") -> Tuple[str, int]:
    """这个人在跟谁聊得正热。返回 (对手 uid, 已交替轮数)；不在线程里给 ("", 0)。

    跟机器人自己构成的线程不算——鱼在场上就是参与方，不需要被压制。
    """
    th = _THREADS.get(str(gid)) or {}
    best: Tuple[str, int] = ("", 0)
    best_ts = 0.0
    for pair, rec in th.items():
        if uid not in pair:
            continue
        if self_id and self_id in pair:
            continue
        ts = float(rec.get("last_ts") or 0)
        if now - ts > window:
            continue
        other = pair[1] if pair[0] == uid else pair[0]
        turns = int(rec.get("turns") or 0)
        # 轮数多的优先；轮数相同看谁更新——否则结果由字典顺序决定
        if turns > best[1] or (turns == best[1] and ts > best_ts):
            best = (other, turns)
            best_ts = ts
    return best


# ---------------------------------------------------------------- 打分
def _cfg_num(cfg: Optional[Dict[str, Any]], key: str, default: float) -> float:
    try:
        v = (cfg or {}).get(key)
        return float(default if v is None else v)
    except Exception:
        return float(default)


def last_bot_facts(dialogue: Iterable[dict]) -> Tuple[float, bool]:
    """从对话流里取「鱼上一条发言」的时间与是否是问句。"""
    for m in reversed(list(dialogue or [])):
        if m.get("me"):
            t = (m.get("text") or "").strip()
            return float(m.get("ts") or 0.0), t.endswith(("？", "?"))
    return 0.0, False


def judge(*, text: str = "", at_uids: Iterable[str] = (), reply_uid: str = "",
          is_at: bool = False, is_quote_bot: bool = False, is_follow: bool = False,
          is_wake: bool = False, self_id: str = "", now: float = 0.0,
          last_bot_ts: float = 0.0, last_bot_question: bool = False,
          bot_names: Iterable[str] = (), at_others: bool = False,
          thread: Tuple[str, int] = ("", 0),
          cfg: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
    """纯打分：给定事实，返回 (分数, 理由串)。不读表、不落盘、不学东西。"""
    text = text or ""
    at_uids = tuple(str(u) for u in (at_uids or ()))
    reply_uid = str(reply_uid or "")
    self_id = str(self_id or "")
    near_gap = _cfg_num(cfg, "addressee_topic_gap_sec", 60)

    score = 0
    why: list = []

    # —— L1 显式指向 ——
    if is_at:
        score, why = S_EXPLICIT, ["被@"]
    elif is_quote_bot:
        score, why = S_EXPLICIT - 5, ["引用我"]
    elif is_wake:
        score, why = S_EXPLICIT, ["唤醒前缀"]
    else:
        # —— L2 点名：只认学到过的别名 + 兜底名字 ——
        names = set(n for n in (bot_names or ()) if n) | set(_HARD_NAMES)
        hit = next((n for n in names if n and n in text), "")
        if hit:
            score, why = S_ALIAS, ["点名:%s" % hit]
        elif any(p in text for p in _PRONOUNS) and last_bot_ts and \
                (not now or now - last_bot_ts <= 300):
            # —— L2 代词：有「你」且鱼刚说过话 ——
            score, why = S_PRONOUN, ["代词·鱼刚说过"]
        elif is_follow:
            # —— L3 问答接续 ——
            score, why = S_FOLLOW, ["追问窗口"]
        elif last_bot_ts and (not now or now - last_bot_ts <= near_gap):
            # —— L4 话题相邻 ——
            score, why = S_NEAR, ["鱼刚说过(%.0fs)" % (now - last_bot_ts)]

    # —— 反向扣分 ——
    others = [u for u in at_uids if u and u != self_id]
    if others:
        score -= P_AT_OTHERS
        why.append("@别人")
    if reply_uid and reply_uid != self_id:
        score -= P_REPLY_OTHERS
        why.append("引用别人")
    # 线程压制：来回两次以上才算「两人在聊」。一次交替（A说完B应一声）不算，
    # 那还是群聊常态；被 @ / 引用 / 唤醒拉进来时一律不压——人点名了必须应声
    partner, turns = thread
    # 追问窗口同样豁免：鱼刚答完这人接着问，正是最该接的时候，
    # 不能因为旁边还有两个人在聊就把分扣光
    if partner and turns >= 2 and not (is_at or is_quote_bot or is_wake or is_follow):
        score -= P_THREAD
        why.append("A↔B在聊")
        if turns >= 3:
            score -= P_THREAD_DEEP
            why.append("已来回%d轮" % turns)

    if score < 0:
        score = 0
    if score > S_EXPLICIT:
        score = S_EXPLICIT
    return int(score), "·".join(why) or "无信号"


def record(*, gid: str, uid: str, nick: str = "", text: str = "", now: float = 0.0,
           self_id: str = "", at_uids: Iterable[str] = (),
           at_named: Iterable[Tuple[str, str]] = (), reply_uid: str = "",
           is_at: bool = False, is_quote_bot: bool = False, is_follow: bool = False,
           is_wake: bool = False, dialogue: Optional[Iterable[dict]] = None,
           topic: str = "", sender_alias: str = "",
           cfg: Optional[Dict[str, Any]] = None, maxlen: int = 200) -> MsgRef:
    """落一行到收件人表并算分。返回这一行的判定结果。"""
    gid = str(gid)
    now = float(now or time.time())
    win = _cfg_num(cfg, "addressee_thread_window_sec", 180)

    # 学别名：只从真实 At 昵称 + 群名片
    if cfg is None or cfg.get("addressee_alias_enabled", True):
        for qq, name in (at_named or ()):
            learn_alias(gid, name, qq)
        if sender_alias:
            learn_alias(gid, sender_alias, uid)

    thread = thread_of(gid, str(uid), now, win, self_id=str(self_id))
    _thread_update(gid, str(uid), now, win)

    last_bot_ts, last_bot_q = last_bot_facts(dialogue or [])

    score, reason = judge(
        text=text, at_uids=at_uids, reply_uid=reply_uid, is_at=is_at,
        is_quote_bot=is_quote_bot, is_follow=is_follow, is_wake=is_wake,
        self_id=self_id, now=now, last_bot_ts=last_bot_ts,
        last_bot_question=last_bot_q,
        bot_names=tuple(names_of(gid, self_id)) if self_id else (),
        thread=thread, cfg=cfg)

    hard = _cfg_num(cfg, "addressee_hard_threshold", 70)
    if is_at or is_quote_bot or is_wake:
        to_uid = str(self_id)
    elif at_uids:
        to_uid = str(tuple(at_uids)[0])
    elif reply_uid:
        to_uid = str(reply_uid)
    elif score >= hard:
        to_uid = str(self_id)
    else:
        to_uid = ""

    ref = MsgRef(uid=str(uid), nick=nick or "", text=text or "", ts=now,
                 at_uids=tuple(str(u) for u in (at_uids or ())),
                 reply_uid=str(reply_uid or ""), topic=topic or "",
                 to_uid=to_uid, score=int(score), reason=reason)

    table = _TABLE.setdefault(gid, deque(maxlen=maxlen))
    table.append(ref)
    return ref


def table(gid: str) -> list:
    return list(_TABLE.get(str(gid)) or ())
