# -*- coding: utf-8 -*-
"""生成器侧的「人味」：打字延迟、草稿、过时重判。

拆到哪里：调度器只说「要开口」，主模型产出文本以后，真正发出去之前还有一段
「看起来像人在打字」的时间。这段时间群里可能又进来新消息——原来那套是模型
一吐完就发，晚一步也不管，于是经常出现「人家已经换了话题，它还在回答上一个」。

这里做三件事：
1. 按字数算延迟，别让长段回复秒回（秒回最像机器人）。
2. 延迟期间收到的消息要重新评估：草稿还是不是这轮该说的话。
3. 草稿只保留一份/群，后到的冲掉先到的（不做并行生成，两句话打架最难看）。

落点：`on_decorate` 是唯一出口，模型文本到这一步才拿得到，所以延迟与重判都在
那里执行；本模块只提供纯逻辑，方便单测。

三种结局：
    send        原样发
    superseded  有人在叫鱼 → 旧草稿作废，交给新消息自己走一轮
    abandoned   群里聊开了 / 超时 → 丢掉，一声不出
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

SEND = "send"
SUPERSEDED = "superseded"
ABANDONED = "abandoned"

_TTL_DEFAULT = 25.0
_PUNCT_RE = re.compile(r"[，。！？、,.!?；;：:~…]+")


@dataclass
class Draft:
    """一份还没发出去的草稿。"""

    text: str = ""
    ts: float = 0.0
    ver: int = 0
    intent_id: int = 0
    mode: str = ""
    priority: int = 0
    uid: str = ""
    gid: str = ""
    delay: float = 0.0
    revalidate: str = ""
    reason: str = ""

    def log_line(self) -> str:
        return ("intent=%s ver=%s delay=%.1fs len=%d uid=%s reval=%s(%s)" % (
            self.intent_id, self.ver, self.delay, len(self.text or ""),
            self.uid or "-", self.revalidate or "-", self.reason or "-"))


def _num(cfg: Optional[Dict[str, Any]], key: str, default: float) -> float:
    try:
        v = (cfg or {}).get(key)
        return float(default if v is None else v)
    except Exception:
        return float(default)


def typing_delay(text: str, cfg: Optional[Dict[str, Any]] = None) -> float:
    """按字数算打字时间：0.6s 起手 + 每秒 12 字 + 标点各 0.03s。

    真实的人敲键盘看的只有字数，不看模型耗时；短回复反而要留一点停顿，
    否则一排「嗯」「好耶」秒回看着很假。上限 typing_max_sec（默认 3s）。

    原来是每秒 6.5 字、上限 6s：模型本身已经要四五秒，再加三四秒打字延迟，
    一句二十字的回复让人等快十秒，体感就是「这 bot 好慢」。拟人靠的是停顿
    的位置和长短差，不是把总时长拉长，所以速度翻倍、上限砍半。
    """
    if not (cfg or {}).get("typing_delay_enabled", True):
        return 0.0
    body = re.sub(r"\s+", "", text or "")
    if not body:
        return 0.0
    punct = len(_PUNCT_RE.findall(text or ""))
    sec = 0.6 + len(body) / 12.0 + punct * 0.03
    top = _num(cfg, "typing_max_sec", 3.0)
    return round(min(max(sec, 0.8), top), 2)


def make_draft(*, text: str, now: float, ver: int, intent_id: int, mode: str = "",
               priority: int = 0, uid: str = "", gid: str = "",
               cfg: Optional[Dict[str, Any]] = None) -> Draft:
    return Draft(text=text or "", ts=float(now or time.time()), ver=int(ver or 0),
                 intent_id=int(intent_id or 0), mode=mode, priority=int(priority or 0),
                 uid=str(uid or ""), gid=str(gid or ""),
                 delay=typing_delay(text, cfg))


def store(st: Any, draft: Draft, logger=None) -> None:
    """把草稿挂到群状态上。后到的直接覆盖——不做并行，两句话打架比慢一句难看得多。"""
    old = getattr(st, "draft", None) or {}
    if old and int(old.get("intent_id") or 0) != draft.intent_id and logger:
        logger.info("[fish][draft] 覆盖旧草稿 intent=%s → %s（旧的那份不发了）",
                    old.get("intent_id"), draft.intent_id)
    st.draft = {"text": draft.text, "ts": draft.ts, "ver": draft.ver,
                "intent_id": draft.intent_id, "mode": draft.mode,
                "priority": draft.priority, "uid": draft.uid, "gid": draft.gid}


def take(st: Any) -> Optional[Dict[str, Any]]:
    d = getattr(st, "draft", None) or {}
    if d:
        st.draft = {}
        return d
    return None


def _ts(e: Dict[str, Any]) -> float:
    try:
        return float(e.get("ts") or 0.0)
    except Exception:
        return 0.0


def new_since(dialogue: Iterable[Dict[str, Any]], ts: float) -> list:
    """草稿生成之后，群里新进来的活人发言。"""
    return [e for e in (dialogue or ()) if not e.get("me") and _ts(e) > ts]


def classify(new: list, uid: str, names: Iterable[str] = ()) -> Tuple[str, str]:
    """给延迟期间的新消息分类：叫鱼 / 聊开了 / 同一个人接着说。"""
    if not new:
        return "quiet", ""
    names = tuple(n for n in names if n)
    for e in new:
        t = str(e.get("text") or "")
        if any(n in t for n in names):
            return "called", "有人在叫鱼"
    uids = {str(e.get("uid") or e.get("who") or "") for e in new}
    if len(uids) >= 2 or len(new) >= 3:
        return "chatted", "群里聊开了（%d 条 / %d 人）" % (len(new), len(uids))
    if uid and uids == {uid}:
        return "continue", "同一个人接着说"
    return "other", "别人插了一句（草稿属主=%s 新消息来自=%s）" % (uid or "-", sorted(uids))


def revalidate(draft: Draft, *, now: float, ver: int, dialogue: Iterable[Dict[str, Any]] = (),
               names: Iterable[str] = (), cfg: Optional[Dict[str, Any]] = None
               ) -> Tuple[str, str]:
    """延迟结束后的重判。返回 (处置, 理由)。"""
    ttl = _num(cfg, "draft_ttl_sec", _TTL_DEFAULT)
    if now - float(draft.ts or 0) > ttl:
        return ABANDONED, "草稿超时 %.0fs" % (now - draft.ts)
    if int(ver or 0) == int(draft.ver or 0):
        return SEND, "期间没人说话"
    kind, why = classify(new_since(dialogue, draft.ts), draft.uid, names)
    if kind == "called":
        # 有人在叫鱼：旧草稿作废，那条新消息会自己走一轮判定，内容才是最新的
        return SUPERSEDED, why
    if kind == "chatted":
        return ABANDONED, why
    if kind == "continue":
        return SEND, "同一人接着说"
    return SEND, "别人插一句·不冲突"
