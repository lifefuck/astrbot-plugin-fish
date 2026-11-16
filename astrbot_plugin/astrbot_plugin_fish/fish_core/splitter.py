# -*- coding: utf-8 -*-
"""长回复拆条：把一段话切成 2~3 条，像人连着敲出来的。

坑：
- 括号里的内容不能切开，「（笑」「死）」分两条发出去就成了乱码。
- 表情标签 [MEME:xx] 在调用本模块前已经剥掉；万一漏进来也不能切断。
- 结尾只剩「嗯」「。」这种一两字的碎片，单独成条比不发还怪，并回上一条。
"""

from __future__ import annotations

import re

# 句末停顿：只有这些位置才允许切开
_SENT_END = "。！？!?…~～\n"
# 次级停顿：一句流水账太长时按这些切
_SOFT = "，,；;、"
_OPEN = "（(【[「『《“‘"
_CLOSE = "）)】]」』》”’"


def _guarded(text: str, delimiters: str) -> list[str]:
    """按 delimiters 切，但括号/引号没闭合的地方一律不切。"""
    out, buf, depth = [], "", 0
    for ch in text:
        buf += ch
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and ch in delimiters:
            out.append(buf)
            buf = ""
    if buf.strip():
        # 尾巴一两个字的碎片并回上一块，别让它单独成条
        if out and len(buf.strip()) <= 2:
            out[-1] += buf
        else:
            out.append(buf)
    return out


# 不可拆的原子单位：链接、ASCII 单词、emoji 组合（ZWJ 家庭、国旗、肤色修饰）。
# 按码位硬切会把链接切成「https://www.bilibili.」「com/video/…」两条点不开的，
# 也会把 👨‍👩‍👧‍👦 切断成一个悬空的 \u200d
_ATOM_RE = re.compile(
    r"https?://[^\s，。！？、；）】：]+"
    r"|[A-Za-z0-9_\-./:?=&%#@+]{2,}"
    r"|(?:[\U0001F1E6-\U0001F1FF]{2})"
    r"|(?:[\U0001F000-\U0001FAFF\u2600-\u27BF]\uFE0F?(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF]\uFE0F?)*)"
    r"|[\U0001F3FB-\U0001F3FF]"
)


def _atoms(chunk: str) -> list[str]:
    out, last = [], 0
    for m in _ATOM_RE.finditer(chunk):
        if m.start() > last:
            out.extend(list(chunk[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    if last < len(chunk):
        out.extend(list(chunk[last:]))
    return out


def _hard_cut(chunk: str, limit: int) -> list[str]:
    """没有标点的长句按字数硬切，避开括号内部，也不切断链接/英文词/emoji。"""
    pieces, buf, depth = [], "", 0
    for unit in _atoms(chunk):
        buf += unit
        for ch in unit:
            if ch in _OPEN:
                depth += 1
            elif ch in _CLOSE:
                depth = max(0, depth - 1)
        if len(buf) >= limit and depth == 0:
            pieces.append(buf)
            buf = ""
    if buf.strip():
        pieces.append(buf)
    return pieces


def _fold_tail(parts: list[str], max_parts: int, max_chars: int) -> list[str]:
    """条数超了：多出来的部分再按字数切开，而不是拼成一堵长墙。

    原来直接 "".join(剩下的)，26 字阈值下产出一条 188 字的消息，拆条等于白做。
    """
    if len(parts) <= max_parts:
        return parts
    head = parts[: max_parts - 1]
    tail = "".join(parts[max_parts - 1:])
    extra = _hard_cut(tail, max_chars) if len(tail) > max_chars else [tail]
    # 条数预算是剩下的名额，绝不能靠「多切几条」解决——max_parts=1 时那是直接违约
    budget = max(1, max_parts - len(head))
    if len(extra) > budget:
        # 名额不够了：把全文按名额重新均分，而不是把剩下的塞成一条长墙。
        # 两害相权——「3 条各 80 字」比「26+26+188」像人说话，也比刷 8 条强
        whole = "".join(parts)
        target = max(max_chars, (len(whole) + max_parts - 1) // max_parts)
        return _hard_cut(whole, target)[:max_parts] or [whole]
    return head + extra


def _flatten(chunk: str) -> str:
    """条内不留换行：QQ 里一条消息中间换行就是个缝，像粘贴的。"""
    return re.sub(r"\s*\n+\s*", " ", chunk).strip()


def split_reply(text: str, max_chars: int = 26, max_parts: int = 3, min_tail: int = 3) -> list[str]:
    """返回 1~max_parts 条待发文本。短于阈值时原样返回单条。

    换行是硬边界：模型自己按「一条一个意思」换的行，不允许再被合并回一条。
    之前按字数合并，23 个字的两行又被拼成一句，看起来就是一长条不喘气。
    """
    text = (text or "").replace("\r", "").strip()
    if "\n" in text:
        out: list[str] = []
        for line in re.split(r"\n+", text):
            line = line.strip()
            if not line:
                continue
            out.extend(split_reply(line, max_chars=max_chars, max_parts=max_parts, min_tail=min_tail))
        return _fold_tail(out, max_parts, max_chars)
    if not text:
        return []
    # 模型爱用空行隔开两句话，群里显示成一大段空白。
    # 折成一个换行，让它变成正常的分条切点
    text = re.sub(r"\n{2,}", "\n", text)
    if max_chars <= 0:
        return [_flatten(text)]
    # 带换行就不能早退：短句里塞了换行，也必须切开
    if "\n" not in text and len(text) <= max_chars:
        return [_flatten(text)]

    chunks: list[str] = []
    for sent in _guarded(text, _SENT_END):
        if len(sent) <= max_chars:
            chunks.append(sent)
        else:
            chunks.extend(_guarded(sent, _SOFT))
    # 还是没有标点（连续刷字/贴了一串代码），硬切兜底
    expanded: list[str] = []
    for c in chunks:
        expanded.extend(_hard_cut(c, max_chars) if len(c) > max_chars else [c])

    parts: list[str] = []
    cur = ""
    for c in expanded:
        if cur and len(cur) + len(c) > max_chars:
            parts.append(cur)
            cur = c
        else:
            cur += c
    if cur:
        parts.append(cur)

    if len(parts) >= 2 and len(parts[-1].strip()) < min_tail:
        parts[-2] += parts[-1]
        parts.pop()

    parts = _fold_tail(parts, max_parts, max_chars)

    return [q for q in (_flatten(p) for p in parts) if q]


def plan_gaps(n_parts: int, gap_min: float = 0.6, gap_max: float = 1.5, rng=None) -> list[float]:
    """n_parts 条之间有 n_parts-1 个间隔，随机落在 [gap_min, gap_max]。"""
    import random as _random

    r = rng or _random
    lo, hi = max(0.0, float(gap_min)), max(0.0, float(gap_max))
    if hi < lo:
        lo, hi = hi, lo
    return [r.uniform(lo, hi) for _ in range(max(0, n_parts - 1))]
