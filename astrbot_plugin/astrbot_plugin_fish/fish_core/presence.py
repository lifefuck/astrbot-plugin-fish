# -*- coding: utf-8 -*-
"""在场模式：不需要回答、但值得有个反应的消息，给一张表情或一个短词。

和「说话」是两件事，所以配额也分开：
- 说话走主动闸（90 秒、占比、连说），一条条都是真在接话；
- 在场是「嗯」一下，10 分钟最多 2 次、间隔 120 秒，且不许紧跟在自己上一句后面
  （不然就变成「连着说」的变种）。

产物只有两种，都不调主模型：
    [MEME:场景]  从四个场合（调侃/接梗/自嘲/冷场）里挑一张
    短词         草 / 哈哈哈哈 / 确实 / 好家伙 / 就这

算在场的：发图梗图（不是求解释）、冷场收尾、@ 第三人的有意思的话、群友自嘲摆烂炫耀、
表情包接龙（连着两张以上）。
不算的：任何问句、@鱼、引用鱼、喊名字、待解读链接、有人在争执、报错求助、指令、纯转发、正在被骂。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# 短词池：按场合分，避免「确实」刷屏
SHORTS = {
    "接梗": ("草", "哈哈哈哈", "确实", "好家伙", "笑死"),
    "调侃": ("就这", "急了", "有点东西", "牛", "行吧"),
    "自嘲": ("我不配", "菜是原罪", "又来了", "手滑"),
    "冷场": ("嗯", "在", "说", "然后呢"),
}

_LAUGH_RE = ("哈哈", "笑死", "草", "233", "6", "绝了", "乐", "离谱", "破防", "绷不住")
_BOAST_RE = ("我太强", "无敌", "乱杀", "秒了", "赢了", "满血", "五杀", "MVP", "稳")
_SELF_DEPRE_RE = ("我太菜", "我又", "我不行", "废了", "摆烂", "躺平", "要饭", "手残", "完蛋")
_ASK_RE = ("?", "？", "怎么", "如何", "为啥", "为什么", "报错", "求助", "在哪", "哪个",
           "谁有", "能不能", "是不是", "有没有", "请问", "咋")
_LINK_RE = ("http://", "https://", "b23.tv", "t.cn", "www.")

# 每群的在场记账（内存，重启清零——这只是「别连着嗯」，不值得落盘）
_state: Dict[str, Dict[str, Any]] = {}
_PER_10MIN = 2
_GAP_SEC = 120.0


def _row(gid: str) -> dict:
    row = _state.get(gid)
    if row is None:
        row = {"ts": [], "last_text": ""}
        _state[gid] = row
    return row


def has_image(event_or_sig) -> bool:
    for k in ("has_image", "has_pic", "image"):
        v = getattr(event_or_sig, k, None)
        if callable(v):
            try:
                v = v()
            except Exception:
                v = None
        if v:
            return True
    return False


def classify_text(text: str, images: int = 0) -> Tuple[bool, str]:
    """只看文字/图，判「值不值得有个反应」。返回 (算不算在场, 场合)。"""
    t = (text or "").strip()
    low = t.lower()
    # 问句、链接、指令一律不在场：那是要回答的，不是要「嗯」的
    if any(k in t for k in _ASK_RE) or any(k in low for k in _LINK_RE):
        return False, ""
    if t.startswith("/") or t.startswith("。"):
        return False, ""
    if any(k in t for k in _SELF_DEPRE_RE):
        return True, "自嘲"
    if any(k in t for k in _LAUGH_RE):
        return True, "接梗"
    if any(k in t for k in _BOAST_RE):
        return True, "调侃"
    if images >= 2:
        return True, "接梗"
    if images == 1 and len(t) <= 6:
        return True, "冷场"
    return False, ""


def judge(sig, dialogue: Optional[List[dict]] = None, cfg: Optional[dict] = None,
          now: Optional[float] = None) -> Tuple[bool, str, str]:
    """完整判断：先排除硬条件，再看内容。返回 (可否, 场合, 理由)。"""
    now = float(now if now is not None else time.time())
    if not (cfg or {}).get("presence_enabled", False):
        return False, "", "在场模式没开"
    # 有指向（@鱼/引用鱼/喊名字/追问）的时候不是「在场」，是「应答」，走正常链路
    for attr in ("is_at", "is_quote_bot", "is_wake", "is_follow"):
        if getattr(sig, attr, False):
            return False, "", "有指向，走正常应答"
    if str(getattr(sig, "target_uid", "") or ""):
        return False, "", "在跟别人说话"
    if getattr(sig, "is_bot", False) or getattr(sig, "from_bot", False):
        return False, "", "别的机器人"
    if getattr(sig, "muted", False) or getattr(sig, "abused", False):
        return False, "", "被骂/被禁"
    if getattr(sig, "is_forward", False) or getattr(sig, "has_forward", False):
        return False, "", "转发"
    text = str(getattr(sig, "text", "") or "")
    images = 0
    img_flag = getattr(sig, "images", None)
    if isinstance(img_flag, (list, tuple)):
        images = len(img_flag)
    elif getattr(sig, "has_image", False):
        images = 1
    ok, scene = classify_text(text, images)
    if not ok:
        return False, "", "内容不需要反应"
    # 有人在等答案就不凑热闹——但只认「最后一条人话是问句、且鱼还没接」。
    # 早先问过、后来已经答了的，不该一直挡着在场（第一版看最近三条人话，
    # 结果一屏人问过一轮之后，鱼就再也不敢发表情了）
    if dialogue:
        # 找最近一条「问句」人话的位置；它之后鱼没接过话，就说明还挂着
        _ask_at = None
        for i in range(len(dialogue) - 1, max(-1, len(dialogue) - 4), -1):
            d = dialogue[i]
            if d.get("me"):
                continue
            if any(k in str(d.get("text") or "") for k in _ASK_RE):
                _ask_at = i
                break
        if _ask_at is not None and not any(d.get("me") for d in dialogue[_ask_at + 1:]):
            return False, "", "有人在等答案，先让人答"
    return True, scene, "在场·%s" % scene


def quota_ok(gid: str, dialogue: Optional[List[dict]] = None,
             cfg: Optional[dict] = None, now: Optional[float] = None) -> Tuple[bool, str]:
    now = float(now if now is not None else time.time())
    gap = float((cfg or {}).get("presence_gap_sec", _GAP_SEC) or _GAP_SEC)
    limit = int((cfg or {}).get("presence_per_10min", _PER_10MIN) or _PER_10MIN)
    row = _row(gid)
    row["ts"] = [t for t in row.get("ts") or [] if now - float(t) < 600.0]
    if len(row["ts"]) >= limit:
        return False, "在场配额满（10 分钟 %d 次）" % limit
    if row["ts"] and now - max(float(t) for t in row["ts"]) < gap:
        return False, "在场间隔不足 %.0fs" % gap
    # 不许紧跟在自己上一句后面：那已经是在说话了，再补个表情就是连发
    if dialogue:
        for d in reversed(dialogue[-3:]):
            if d.get("me"):
                if now - float(d.get("ts") or 0) < 45.0:
                    return False, "刚说过话，别再补表情"
                break
            if not d.get("me"):
                break       # 中间隔了人说话，就无所谓
    return True, ""


def try_present(gid: str, sig, dialogue: Optional[List[dict]] = None,
                cfg: Optional[dict] = None, now: Optional[float] = None) -> Optional[Tuple[str, str]]:
    """判定 + 配额一起过，返回 (场合, 短词) 或 None。"""
    now = float(now if now is not None else time.time())
    ok, scene, _why = judge(sig, dialogue, cfg, now)
    if not ok:
        return None
    qok, _qwhy = quota_ok(gid, dialogue, cfg, now)
    if not qok:
        return None
    pool = SHORTS.get(scene) or SHORTS["接梗"]
    short = pool[int(now) % len(pool)]
    return scene, short


def mark(gid: str, text: str = "", now: Optional[float] = None):
    now = float(now if now is not None else time.time())
    row = _row(gid)
    row.setdefault("ts", []).append(now)
    del row["ts"][:-12]
    row["last_text"] = (text or "")[:40]


def stats() -> dict:
    return {"groups": len(_state),
            "total": sum(len(v.get("ts") or []) for v in _state.values())}
