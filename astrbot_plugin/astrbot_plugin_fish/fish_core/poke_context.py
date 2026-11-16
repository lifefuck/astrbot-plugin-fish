# -*- coding: utf-8 -*-
"""戳一戳到底在说什么：判断这一戳是「催我没回的那条」还是「纯手欠」。

单独拆出来是为了能离线测——这段判断埋在 handler 里的话，只能靠真群戳，
而真群戳一次就得拉一个人来配合，测不了边界。

规则很简单，但每条都有来由：
- 只看这个人自己说过的话（别人的话不是他在等）；
- 只认最近三十分钟（一小时前的发言早就不算「还在等你回」）；
- 只认我上次说话之后的那部分（我说过话之后再发，才有「没被回」这回事）。
"""

RECENT_WINDOW = 1800.0   # 30 分钟
TIMELINE_SIZE = 8


def decide(msgs, uid, last_bot_reply_ts: float, now: float, bot_nick: str = "") -> dict:
    """返回 {pending: 待回的话, timeline: [...], quiet_min: 沉默分钟数|None}。"""
    uid = str(uid)
    floor = max(float(last_bot_reply_ts or 0.0), now - RECENT_WINDOW)

    said = []
    for m in list(msgs):
        if str(m.get("uid")) != uid:
            continue
        text = (m.get("text") or "").strip()
        if not text or float(m.get("ts") or 0.0) <= floor:
            continue
        said.append(text)

    pending = said[-1] if said else ""
    timeline = [f"{m.get('nick') or m.get('uid')}：{m.get('text')}" for m in list(msgs)[-TIMELINE_SIZE:]]
    quiet_min = (now - last_bot_reply_ts) / 60 if last_bot_reply_ts else None
    return {"pending": pending, "timeline": timeline, "quiet_min": quiet_min}


def _quiet_text(quiet_min):
    if quiet_min is None:
        return ""
    if quiet_min >= 1440:
        return f"你上一次说话是 {quiet_min / 1440:.1f} 天前"
    if quiet_min >= 120:
        return f"你上一次说话是 {quiet_min / 60:.1f} 小时前"
    return f"你上一次说话是 {quiet_min:.0f} 分钟前"


def build_prompt(info: dict, who_hint: str, repeat: int = 1, last_said=None) -> str:
    """按判定结果拼 prompt。两条分支的写法完全不同，所以放在一起看。

    repeat 是「一分钟内被同一个人戳的第几下」，last_said 是刚回过的两句。
    不告诉模型这些，它就每次都当第一次被戳，回出十句一样的「戳上瘾了是吧」。
    """
    extra = ""
    if repeat > 1:
        prev = "、".join(f"「{t[:20]}」" for t in (last_said or []) if t)
        extra = (f"\n注意：这人一分钟内已经戳了你 {repeat} 下，你上一句是{prev or '刚回过他'}。"
                 "这次别重复上次的句式，也别再讲道理，两个到六个字打发掉，语气可以更没耐心一点：\n")
    ctx = ""
    if info["timeline"]:
        ctx = "【最近群聊】\n" + "\n".join(info["timeline"]) + "\n"

    if info["pending"]:
        # 有没被回的发言：这多半是催，不是在撒娇
        return (
            f"{ctx}"
            f"{who_hint}，戳了你一下。注意：他在这之前说过「{info['pending']}」，"
            "你当时没接，所以这一戳大概率是在提醒你那条还没回。\n"
            "先把那句话接住：针对它给个实在的回应，可以顺口嫌一句「催什么」，但别只嫌不答。\n"
            f"{extra}"
            "不要反问他「怎么了」，别写动作括号，别用客套话，别解释你在忙什么。只输出要发出去的那句话："
        )

    quiet = _quiet_text(info["quiet_min"])
    return (
        f"{ctx}"
        f"{who_hint}，戳了你一下。他戳之前没在等你的回复（{quiet or '你还没在这个群说过话'}），"
        "这就是纯手欠。\n"
        "短促回一句，两到十个字：嫌手欠、问干嘛、装疼、要钱都行。"
        "如果上面的群聊正好有能接的一句，可以顺势接，但别硬凑。\n"
        f"{extra}"
        "不要说「有什么需要帮忙」，别写动作括号，别用客套话。只输出这一句："
    )
