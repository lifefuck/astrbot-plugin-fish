# -*- coding: utf-8 -*-
"""长回复拆条：把一段话切成 2~3 条，像人连着敲出来的。

坑：
- 括号里的内容不能切开，「（笑」「死）」分两条发出去就成了乱码。
- 表情标签 [MEME:xx] 在调用本模块前已经剥掉；万一漏进来也不能切断。
- 结尾只剩「嗯」「。」这种一两字的碎片，单独成条比不发还怪，并回上一条。
"""

from __future__ import annotations

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


def _hard_cut(chunk: str, limit: int) -> list[str]:
    """没有标点的长句按字数硬切，同样避开括号内部。"""
    pieces, buf, depth = [], "", 0
    for ch in chunk:
        buf += ch
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


def split_reply(text: str, max_chars: int = 60, max_parts: int = 3, min_tail: int = 6) -> list[str]:
    """返回 1~max_parts 条待发文本。短于阈值时原样返回单条。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

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

    if len(parts) > max_parts:
        # 条数超了就把多出来的全塞进最后一条，宁可长一点也别刷屏
        parts = parts[: max_parts - 1] + ["".join(parts[max_parts - 1:])]

    return [p.strip() for p in parts if p.strip()]


def plan_gaps(n_parts: int, gap_min: float = 0.6, gap_max: float = 1.5, rng=None) -> list[float]:
    """n_parts 条之间有 n_parts-1 个间隔，随机落在 [gap_min, gap_max]。"""
    import random as _random

    r = rng or _random
    lo, hi = max(0.0, float(gap_min)), max(0.0, float(gap_max))
    if hi < lo:
        lo, hi = hi, lo
    return [r.uniform(lo, hi) for _ in range(max(0, n_parts - 1))]
