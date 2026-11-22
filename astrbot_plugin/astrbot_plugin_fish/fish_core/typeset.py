# -*- coding: utf-8 -*-
"""发送前的排版归一化：把「一眼就是机器」的空格和换行修掉。

为什么放在代码里而不是提示词里：模型对「不要加空格」这类格式要求遵守率很低，
同一个提示词跑十次能有一半带空格。排版是可确定性判断的事，交给正则，别赌。

规则（都是硬规则，没有审美判断）：
1. 汉字与汉字之间的空格全删；汉字和字母/数字/表情之间的空格保留（"看 BLG 的比赛"）
2. 连续换行压成一个，每行首尾去空格，整条不以空格/换行开头结尾
3. 每条消息最多一个换行；要点多就拆成 2~3 条短消息
4. 10 字以内结尾不加句号；感叹号每条至多一个；表情每条至多一个
"""

from __future__ import annotations

import re

# 汉字、日文假名、韩文：这些字符彼此之间的空格是纯噪音
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
# 前面是汉字、后面也是汉字 → 中间的空格删掉；汉字和英文/数字之间不动
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])[ \t\u00a0\u3000]+(?=[{_CJK}])")
# 汉字与中文标点之间、标点与汉字之间的空格也是噪音（模型常写成「好的 。」）
_SPACE_AROUND_CJK_PUNCT = re.compile(rf"(?<=[{_CJK}])[ \t\u00a0\u3000]+(?=[，。！？、；：」』）])"
                                     rf"|(?<=[，。！？、；：「『（])[ \t\u00a0\u3000]+(?=[{_CJK}])")
# 行首行尾空白
_LINE_TRIM = re.compile(r"[ \t\u00a0\u3000]+$", re.MULTILINE)
_LINE_LEAD = re.compile(r"^[ \t\u00a0\u3000]+", re.MULTILINE)
_MULTI_NL = re.compile(r"\n{2,}")

# 表情：基本平面符号 + 补充平面 emoji（含 ZWJ 组合、肤色修饰）
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
    "\uFE0F?(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]\uFE0F?)*"
    "|[\U0001F3FB-\U0001F3FF]"
)

_END_PUNCT = "。！？!?…~～，,；;、"


def _one_emoji(text: str) -> str:
    """每条只留第一个表情，多的删掉（表情堆一行同样是机器味）"""
    hits = list(_EMOJI.finditer(text))
    if len(hits) <= 1:
        return text
    out, last = [], 0
    for m in hits[1:]:
        out.append(text[last:m.start()])
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _one_exclaim(text: str) -> str:
    """感叹号至多一个：多的直接去掉，别换成句号（换句号会把两句粘成一句）"""
    seen = False
    out = []
    for ch in text:
        if ch in "！!":
            if seen:
                continue
            seen = True
        out.append(ch)
    return "".join(out)


def _drop_period_if_short(text: str, limit: int = 10) -> str:
    """10 字以内结尾的句号是书面语痕迹，真人发短消息不打句号"""
    body = text.rstrip("。")
    if body != text and 0 < len(body) <= limit:
        return body
    return text


# 代码块整段不动：行首缩进就是语义，去掉就把缩进打死、代码也跑不了
_FENCE = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)


def _normalize_plain(t: str) -> str:
    t = _SPACE_BETWEEN_CJK.sub("", t)
    t = _SPACE_AROUND_CJK_PUNCT.sub("", t)
    t = _LINE_LEAD.sub("", t)
    t = _LINE_TRIM.sub("", t)
    t = _MULTI_NL.sub("\n", t)
    # 行间可能因为去空格变出空行，再压一次
    return _MULTI_NL.sub("\n", t)


def normalize(text: str) -> str:
    """规则 1、2：空格与换行。不拆条，不碰标点；代码块原样保留。"""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    # 奇偶切分：偶数段是正文，奇数段是代码（前后可能各贴一段正文）
    segs = _FENCE.split(t)
    for i in range(0, len(segs), 2):
        segs[i] = _normalize_plain(segs[i])
    return "".join(segs).strip(" \t\n\u00a0\u3000")


def finish_one(text: str) -> str:
    """单条消息的最后整形：规则 4。"""
    t = normalize(text)
    t = _one_exclaim(t)
    t = _one_emoji(t)
    t = _drop_period_if_short(t)
    return t.strip(" \t\n\u00a0\u3000")


def to_messages(text: str, max_parts: int = 3) -> list[str]:
    """规则 3：把一段话按空行/换行分组，每组最多并两小段，超过 max_parts 收进最后一条。

    返回的每一条里最多一个换行，且每一条都过一遍 finish_one。
    """
    t = normalize(text)
    if not t:
        return []
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        return [finish_one(lines[0])]

    # 两行并成一条（一个换行），再多就分条；超上限的并进最后一条，用句号断开
    groups = [lines[i:i + 2] for i in range(0, len(lines), 2)]
    cap = max(1, int(max_parts or 1))
    if len(groups) > cap:
        head = groups[:cap - 1]
        rest = ["".join(g) for g in groups[cap - 1:]]
        head.append(rest)
        groups = head
    out = []
    for g in groups:
        msg = "\n".join(g)
        msg = finish_one(msg)
        if msg:
            out.append(msg)
    return out


def finish_parts(parts: list[str], max_parts: int = 3) -> list[str]:
    """拆条之后逐条整形，并把「一条里超过一个换行」的并成多条。

    splitter 是按字数和句末切的，切完每块里可能还留着 2 个以上换行；
    这里统一收口，保证发出去的每一条最多一个换行。
    """
    out: list[str] = []
    for p in parts or []:
        msgs = to_messages(p, max_parts=max(1, int(max_parts or 1)))
        out.extend(msgs or ([finish_one(p)] if normalize(p) else []))
    # 收口后可能超过上限：多出来的并入最后一条，绝不再拆
    cap = max(1, int(max_parts or 1))
    if len(out) > cap:
        out = out[:cap - 1] + [out[cap - 1]]
    return [x for x in out if x]
