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
    """按 delimiters 切，但括号/引号没闭合的地方一律不切。

    链接里的 `?` 是查询串不是句末：实测「看这个 https://…/BV1xx?p=2 挺好看的」
    被从这个问号切开，第一条成了点不开的死链。
    """
    out, buf, depth = [], "", 0
    in_url = False
    for i, ch in enumerate(text):
        buf += ch
        if not in_url and text.startswith("http", i) and (i == 0 or not text[i - 1].isalnum()):
            in_url = True
        if in_url and ch.isspace():
            in_url = False
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        if depth == 0 and ch in delimiters and not in_url:
            # 软标点（，、；）保留在块尾：这里切出来只是「为了能装进 max_chars」，
            # 后面还要回拼，吃掉逗号等于回拼后整句没标点，读着像两件事粘一起
            # （实测 70 字 4 个逗号，拆完 0 个）。
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


def _pack_sentences(text: str, max_parts: int, max_chars: int) -> list[str]:
    """名额不够时按句号重新打包，而不是按字数硬切。

    按字数切会把句号吃掉、把两句粘成一个空格（实测：「这个很简单。 你看懂了吗？」），
    群里看着就像打字漏标点。这里只在句末标点之间分组。
    """
    sents = [s for s in re.split(r"(?<=[。！？!?…~～])", text) if s.strip()]
    if not sents:
        return [text]
    flat: list[str] = []
    for s in sents:
        flat.extend(_hard_cut(s, max_chars) if len(s) > max_chars * 2 else [s])
    # 按字数装箱，而不是按 n/max_parts 均分：均分会让每条都超过 max_chars
    # （实测 167 字 / 3 条 → 84+83，等于字数上限形同虚设）
    out: list[str] = []
    cur = ""
    for s in flat:
        if cur and len(cur) + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        out.append(cur)
    # max_parts=1 是「只发一条」的硬约束，宁可超字数也不能多发；其余情况
    # 字数优先——多甩一条短的比发一坨 84 字的墙好看
    if len(out) > max_parts and max_parts <= 1:
        return ["".join(out)]
    return out


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
        # 名额不够了：按句末标点重新打包，保留标点，别在句子中间硬切
        whole = "".join(parts)
        return _pack_sentences(whole, max_parts, max_chars) or [_hard_cut(whole, max_chars)[:max_parts]][0]
    return head + extra


def _flatten(chunk: str) -> str:
    """条内不留换行：QQ 里一条消息中间换行就是个缝，像粘贴的。"""
    return re.sub(r"\s*\n+\s*", " ", chunk).strip()


_STUB_CHARS = 6   # 不到这个字数的独立一行，多半是被换行切碎的半句

# 围栏代码块：里面有缩进、空格、成对的 ```，一旦被 max_chars 切开就废了
_FENCE_RE = re.compile(r"```[^\n]*\n.*?```|```.*?```", re.S)


def _split_with_code(text: str, max_chars: int, max_parts: int, min_tail: int) -> list[str]:
    """带 ``` 的回复：代码块整块保留，绝不切、不压成一行。

    实测把「```bash\\npip install -i https://pypi.tuna…\\n```\\n装完再跑」拆成
    两条之后，空格被吃、缩进丢失、围栏只剩一半，粘到终端里直接报错。
    """
    parts: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        before = text[pos:m.start()]
        if before.strip():
            parts.extend(split_reply(before, max_chars=max_chars,
                                     max_parts=max_parts, min_tail=min_tail))
        parts.append(m.group(0).strip("\n"))
        pos = m.end()
    tail = text[pos:]
    if tail.strip():
        parts.extend(split_reply(tail, max_chars=max_chars,
                                 max_parts=max_parts, min_tail=min_tail))
    if len(parts) <= max_parts:
        return parts
    code = [p for p in parts if p.startswith("```")]
    other = " ".join(p for p in parts if not p.startswith("```")).strip()
    if not other:
        return parts[:max_parts]
    if len(code) + 1 <= max_parts:
        # 先说人话，再把代码整块跟在后面
        return [other] + code
    return parts[:max_parts]


def _merge_stubs(parts: list[str]) -> list[str]:
    """把「不是」「啊这」这种碎句并进下一行。

    模型偶尔写成「不是\nlinux 内核是 GPLv2」，按换行切开就变成先发两个字的
    「不是」，再发一整句——群里看着像卡了一下，还重复了一遍开头。
    """
    out = list(parts)
    i = 0
    while i < len(out) - 1:
        if len(out[i]) < _STUB_CHARS:
            head, nxt = out[i], out[i + 1]
            sep = "" if head.endswith(("，", "。", "！", "？", "…", ",", ".", "!", "?")) else "，"
            out[i + 1] = head + sep + nxt
            del out[i]
            continue
        i += 1
    return out


def _clamp(parts: list[str], max_parts: int, max_chars: int) -> list[str]:
    """收尾：先把每条压到 max_chars 内，再在不超 max_chars 的前提下合并到 max_parts。

    以前这里是「按 max_parts 均分」，200 字 / max_chars=45 / max_parts=3 会分出
    三条 68 字的长墙——字数上限是读起来舒服的底线，条数只是「别刷屏」的软约束，
    两者打架时永远让字数赢：内容实在装不下就多发一条，也不发一堵墙。
    """
    if max_chars > 0:
        fixed: list[str] = []
        for p in parts:
            fixed.extend(_hard_cut(p, max_chars) if len(p) > max_chars else [p])
        parts = fixed
    if max_parts and len(parts) > max_parts:
        merged: list[str] = []
        for p in parts:
            if merged and max_chars > 0 and len(merged[-1]) + len(p) <= max_chars:
                merged[-1] = merged[-1] + p
            else:
                merged.append(p)
        parts = merged
    return parts


def split_reply(text: str, max_chars: int = 45, max_parts: int = 3, min_tail: int = 3) -> list[str]:
    """返回 1~max_parts 条待发文本。短于阈值时原样返回单条。

    换行是硬边界：模型自己按「一条一个意思」换的行，不允许再被合并回一条。
    之前按字数合并，23 个字的两行又被拼成一句，看起来就是一长条不喘气。
    """
    text = (text or "").replace("\r", "").strip()
    # 代码块先摘出来单独处理：它不能被换行切分，也不能被 _flatten 压成一行
    if "```" in text:
        return _split_with_code(text, max_chars, max_parts, min_tail)
    if "\n" in text:
        out: list[str] = []
        for line in re.split(r"\n+", text):
            line = line.strip()
            if not line:
                continue
            out.extend(split_reply(line, max_chars=max_chars, max_parts=max_parts, min_tail=min_tail))
        return _clamp(_fold_tail(_merge_stubs(out), max_parts, max_chars), max_parts, max_chars)
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
        # 回吞会把前一条顶过 max_chars（45+2=47）——短尾巴和字数上限里，
        # 守上限：孤零零两个字发一条，比顶破长度更像人
        if len(parts[-2]) + len(parts[-1]) <= max_chars:
            parts[-2] += parts[-1]
            parts.pop()

    parts = _fold_tail(parts, max_parts, max_chars)

    parts = _clamp(parts, max_parts, max_chars)

    return [q for q in (_flatten(p) for p in parts) if q]


def plan_gaps(n_parts: int, gap_min: float = 0.6, gap_max: float = 1.5, rng=None) -> list[float]:
    """n_parts 条之间有 n_parts-1 个间隔，随机落在 [gap_min, gap_max]。"""
    import random as _random

    r = rng or _random
    lo, hi = max(0.0, float(gap_min)), max(0.0, float(gap_max))
    if hi < lo:
        lo, hi = hi, lo
    return [r.uniform(lo, hi) for _ in range(max(0, n_parts - 1))]
