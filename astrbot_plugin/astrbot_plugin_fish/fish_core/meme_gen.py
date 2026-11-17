"""表情包生成：把一句「喜报：今天没人迟到」真的画成一张带字的图。

底层用 meme_generator（Rust 引擎的 Python 绑定），300 个模板里只有 70 个是纯文字的，
其余要传图。鱼能自己画的只有纯文字那批——所以「摸头」这种它做不出来，会照常说话。

调用方式不是命令：模型在回复里写 [画图:喜报:今天没人迟到]，主流程把这句剥掉、
画图、跟正文一起发出去。用户什么都不用记。
"""

import asyncio
import re

# 模板里文字格数可能不止一个，用 | 分隔
TAG_RE = re.compile(r"[\[【]画图[:：]([^\]】]{1,80})[\]】]")

_INDEX = None

# 口语 → 模板 key。关键词表是引擎自带的，但群友（和模型）更爱说这些词
_ALIAS = {
    "举牌": "*holdsign",
    "喜报": "good_news",
    "悲报": "bad_news",
    "奖状": "certificate",
    "证书": "certificate",
    "抖音": "douyin",
    "定妆": "douyin",
}


def _index() -> dict:
    """key -> (meme, keywords, params)。只收纯文字模板：需要图的鱼手上未必有图。"""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx = {}
    try:
        from meme_generator import get_memes
    except Exception:
        _INDEX = {}
        return _INDEX
    for m in get_memes():
        try:
            info = m.info
            prm = info.params
            if prm.min_images > 0 or prm.max_texts <= 0:
                continue
            idx[m.key] = (m, [str(k) for k in info.keywords if k], prm)
        except Exception:
            continue
    _INDEX = idx
    return _INDEX


def extract_tag(text: str):
    """返回 (去掉标签的正文, 画图需求 or "")。"""
    text = str(text or "")
    m = TAG_RE.search(text)
    if not m:
        return text, ""
    req = m.group(1).strip()
    return TAG_RE.sub("", text).strip(), req


def match(req: str):
    """按关键词长度打分挑模板：匹配到的词越长越具体（「布洛妮娅举牌」胜过「举牌」）。"""
    idx = _index()
    if not idx:
        return None
    req = str(req or "")
    best, best_score = None, 0
    for key, (_m, kws, _p) in idx.items():
        for kw in kws:
            if kw and kw in req and len(kw) > best_score:
                best, best_score = key, len(kw)
    if best:
        return best
    for word, key in _ALIAS.items():
        if word in req:
            if key.startswith("*"):
                for k in idx:
                    if k.endswith(key[1:]):
                        return k
            elif key in idx:
                return key
    return None


def _plan_texts(req: str, prm, rest: list) -> list:
    """凑够模板要求的文字格数：不够就用需求原文顶上，多了截掉。"""
    texts = [t for t in rest if t]
    if not texts:
        texts = [req]
    default = list(getattr(prm, "default_texts", []) or [])
    while len(texts) < prm.min_texts:
        texts.append(default[len(texts)] if len(texts) < len(default) else texts[-1])
    return texts[: max(1, prm.max_texts)]


def render(req: str, timeout: float = 25.0):
    """同步画图，返回 png bytes；任何失败返回 None。"""
    key = match(req)
    if not key:
        return None
    meme, _kws, prm = _index()[key]
    # 需求写成「模板:文字A|文字B」，冒号后面才是要印上去的话
    parts = re.split(r"[:：|]", str(req))
    if len(parts) > 1 and match(parts[0]) == key:
        texts = _plan_texts(req, prm, parts[1:])
    else:
        texts = _plan_texts(req, prm, [])
    try:
        out = meme.generate([], texts, {})
    except Exception:
        return None
    return out if isinstance(out, (bytes, bytearray)) else None


async def make(req: str, timeout: float = 25.0):
    """异步版：Rust 引擎是阻塞调用，扔线程里跑，别挡住群消息循环。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(render, req), timeout)
    except Exception:
        return None


def names(limit: int = 40) -> list:
    """给提示词用的模板口头名（取每个模板的第一个关键词）。"""
    return [kws[0] for _k, (_m, kws, _p) in list(_index().items())[:limit] if kws]
