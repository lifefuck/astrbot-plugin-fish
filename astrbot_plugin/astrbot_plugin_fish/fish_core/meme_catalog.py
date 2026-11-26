#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表情包索引（meme_catalog）

memes_catalog.json 是给 138 张图逐张打过标的成品：图上的字、画面描述、情绪、关键词。
这里把它变成选图用的索引，不再扫目录、不再调视觉模型。

作者: life (lifefuck)
"""

import base64
import collections
import json
import logging
import os
import random
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("DeepSeekBot")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(_SCRIPT_DIR, "meme-pack", "memes_catalog.json")

# 模型只需要认这 8 个情绪，太细的标签它记不住，反而乱用
BUCKETS: Dict[str, Tuple[str, ...]] = {
    "卖萌": ("卖萌", "心动"),
    "得意": ("得意", "庆祝", "赞同"),
    "嘲讽": ("嘲讽", "嫌弃", "吃瘪"),
    "震惊": ("震惊", "困惑"),
    "愤怒": ("愤怒",),
    "委屈": ("委屈", "哭泣", "要饭"),
    "无奈": ("无奈", "摆烂", "睡觉"),
    "干饭": ("干饭",),
}

# 按「用到什么场合」再分一层：幽默标签是一回事，鱼该在什么气氛里甩哪张是另一回事。
# 冷场时甩一张嘲讽图等于往冷场里再浇一盆冷水，所以冷场只给卖萌/睡觉/干饭这类没攻击性的。
SCENES: Dict[str, Tuple[str, ...]] = {
    "调侃": ("嘲讽", "嫌弃", "得意", "吃瘪", "傲娇"),
    "接梗": ("震惊", "困惑", "心动", "赞同", "庆祝"),
    "自嘲": ("委屈", "哭泣", "要饭", "摆烂", "无奈"),
    "冷场": ("卖萌", "睡觉", "干饭"),
}
# 场景桶里挑不到（或图坏了）时的退路：冷场退到卖萌，别的退到本场景第一桶
SCENE_FALLBACK = {"冷场": ("卖萌", "无奈"), "调侃": ("嘲讽",), "接梗": ("震惊",), "自嘲": ("委屈",)}

# 全角方括号（［］）也要认：模型偶尔用全角，老正则漏掉就把标签原样发进群
_TAG_RE = re.compile(r"[\[【［]\s*MEME\s*[:：]\s*([^\]】］]{1,12})\s*[\]】］]", re.IGNORECASE)

# 模型手滑的第二种写法：只写情绪不写 MEME 前缀（实测出现过 [得意]），
# 这种标签要认成配图意图并剥掉，否则群里会看到一句「这话说得 [得意]」
_EMOTIONS = ("卖萌", "得意", "嘲讽", "震惊", "愤怒", "委屈", "无奈", "干饭")
_BARE_RE = re.compile(r"[\[【［]\s*(%s)\s*[\]】］]" % "|".join(_EMOTIONS))

_by_emotion: Dict[str, List[dict]] = collections.defaultdict(list)
_by_keyword: Dict[str, List[dict]] = collections.defaultdict(list)
_recent_paths: collections.deque = collections.deque(maxlen=12)

# stealer 插件（群里偷表情包那个）把图存在自己的 plugin_data 下，按英文情绪分目录。
# 它的目录和本索引完全不搭界，等于偷了 91 张一张也用不上；这里按情绪映射并进来，
# 让偷来的图和原目录的 138 张一起参与选图。
STEALER_DIR = os.path.normpath(os.path.join(
    _SCRIPT_DIR, "..", "..", "..", "plugin_data", "astrbot_plugin_stealer", "categories"))
_STEALER_EMO: Dict[str, str] = {
    "happy": "卖萌", "love": "心动", "shy": "卖萌", "embarrassed": "卖萌",
    "excitement": "得意", "thank": "得意", "troll": "嘲讽", "disgust": "嫌弃",
    "dumb": "无奈", "sigh": "摆烂", "tired": "睡觉", "sad": "委屈",
    "cry": "哭泣", "angry": "愤怒", "confused": "困惑",
    "surprised": "震惊", "fear": "震惊",
}


def _load_stealer() -> None:
    if not os.path.isdir(STEALER_DIR):
        return
    added = 0
    for cat in sorted(os.listdir(STEALER_DIR)):
        cdir = os.path.join(STEALER_DIR, cat)
        if not os.path.isdir(cdir):
            continue
        emo = _STEALER_EMO.get(cat.lower(), "")
        if not emo:
            continue
        for fn in sorted(os.listdir(cdir)):
            if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                continue
            it = {
                "file_path": os.path.join(cdir, fn),
                "emotion": emo,
                "keywords": [cat, emo],
                "desc": "群里偷来的表情包",
            }
            _by_emotion[emo].append(it)
            for kw in it["keywords"]:
                _by_keyword[kw].append(it)
            added += 1
    if added:
        logger.info("stealer 偷来的表情包并入索引: %d 张", added)


def _load() -> None:
    if _by_emotion:
        return
    if not os.path.exists(CATALOG_FILE):
        logger.warning("表情包索引不存在: %s", CATALOG_FILE)
        _load_stealer()
        return
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8-sig") as f:
            items = json.load(f)
    except Exception as e:
        logger.warning("读取表情包索引失败: %s", e)
        return

    for it in items:
        fpath = it.get("file_path") or ""
        if fpath and not os.path.isabs(fpath):
            fpath = os.path.join(_SCRIPT_DIR, fpath)
            it["file_path"] = fpath
        if not fpath or not os.path.exists(fpath):
            continue
        emo = (it.get("emotion") or "").strip()
        if emo:
            _by_emotion[emo].append(it)
        for kw in it.get("keywords") or []:
            kw = str(kw).strip()
            # 单字关键词命中率太高，选出来的图往往驴唇不对马嘴
            if len(kw) >= 2:
                _by_keyword[kw].append(it)
    logger.info("表情包索引载入完成: %d 条，%d 个情绪标签", len(items), len(_by_emotion))
    _load_stealer()


_STRAY_TAG_RE = re.compile(r"\[\s*[A-Za-z]{2,10}\s*[:：][^\]\n]{0,12}\]")


def extract_tag(text: str) -> Tuple[str, str]:
    """把回复里的 [MEME:嘲讽] 剥出来，返回 (干净文本, 情绪标签)"""
    if not text:
        return text, ""
    m = _TAG_RE.search(text)
    if not m:
        # 只写了情绪、忘了 MEME 前缀：照样当成配图意图
        bare = _BARE_RE.search(text)
        if bare:
            return _BARE_RE.sub("", text).strip(), bare.group(1)
        # 模型手滑写成 [MEME:]/[MEME：情绪+解释] 时也要清掉，别把残渣发给群友
        # 结尾括号也可能被 max_tokens 截断（实测出现过 [MEME:嘲讽 没有右括号），闭合括号设为可选
        leftover = re.sub(r"[\[【［]\s*MEME\s*[:：][^\]】］\n]*[\]】］]?", "", text, flags=re.I)
        # 手滑写错前缀（[CAME:无奈]、[EMOJI:得意]）以前会原样发给群友，
        # 群里看着就是一段乱码方括号。任何「英文前缀:内容」的方括号标签都抹掉
        leftover = _STRAY_TAG_RE.sub("", leftover)
        return leftover.strip(), ""
    tag = m.group(1).strip()
    return _TAG_RE.sub("", text).strip(), tag


def _candidates_for_tag(tag: str) -> List[dict]:
    _load()
    tag = tag.strip()
    if not tag:
        return []
    for bucket, raws in BUCKETS.items():
        if tag == bucket or tag in raws:
            pool = []
            for raw in raws:
                pool.extend(_by_emotion.get(raw, []))
            if pool:
                return pool
    # 模型自己发明了个不在桶里的标签，退回精确匹配
    return _by_emotion.get(tag, [])


def _candidates_for_text(text: str) -> List[dict]:
    """没有标签时，用回复本身去撞关键词，撞不到再用高频情绪兜底"""
    _load()
    if not text:
        return []
    pool: List[dict] = []
    seen = set()
    for kw, items in _by_keyword.items():
        if kw in text:
            for it in items:
                if it["file_path"] not in seen:
                    seen.add(it["file_path"])
                    pool.append(it)
    if pool:
        return pool
    fallback = []
    for raw in ("卖萌", "得意", "无奈"):
        fallback.extend(_by_emotion.get(raw, []))
    return fallback


MAX_BYTES = 1200 * 1024  # 再大就会撑爆 WebSocket

def _pick_file(items: List[dict]) -> Optional[str]:
    if not items:
        return None
    # 只校验文件还在。体积不在这里筛：一个桶可能全是大图，筛干净就等于
    # 「这个情绪永远发不出图」（实测「赞同」「干饭」两桶因此常年发错别桶的图），
    # 改成读的时候压一压
    usable = []
    for it in items:
        try:
            if os.path.getsize(it["file_path"]) > 0:
                usable.append(it)
        except OSError:
            continue
    if not usable:
        return None
    fresh = [it for it in usable if it["file_path"] not in _recent_paths] or usable
    chosen = random.choice(fresh)["file_path"]
    _recent_paths.append(chosen)
    return chosen


# 桶里图太少或者全被体积过滤掉时的备选，别让「干饭」这种只有一个桶直接发不出图
FALLBACK = {
    "干饭": ("卖萌", "得意"),
    "心动": ("卖萌",),
    "委屈": ("卖萌", "无奈"),
    "愤怒": ("嘲讽",),
    "震惊": ("困惑", "卖萌"),
}

def _read_capped(path: str) -> Optional[str]:
    """读成 base64。超限就压：静图降质量再降尺寸，动图取首帧——

    原来超限直接当没有，但「干饭」桶只有一张 5.4MB 的 gif，等于这个情绪
    永远发不出图，只能掉到 FALLBACK 去发「卖萌」的图，看着就是发错意思。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        logger.warning("读取表情包失败 %s: %s", path, e)
        return None
    if len(raw) <= MAX_BYTES:
        return base64.b64encode(raw).decode()
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        is_gif = path.lower().endswith(".gif")
        im.seek(0)  # 动图只取首帧
        if im.mode in ("RGBA", "LA", "P"):
            # 透明底压 JPEG 会变黑边，先垫白
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        # 先降质量，不行再缩尺寸，一共最多五轮；表情包这个体积怎么都够
        for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
            frame = im
            if scale < 1.0:
                w, h = im.size
                frame = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            for q in (86, 74, 62):
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=q, optimize=True)
                if buf.tell() <= MAX_BYTES:
                    if is_gif:
                        logger.info("表情包 %s 动图超限，改发首帧", os.path.basename(path))
                    return base64.b64encode(buf.getvalue()).decode()
        return None
    except Exception as e:
        logger.warning("压缩表情包失败 %s: %s", path, e)
        return None


def _try_pick(items: List[dict], tries: int = 4) -> Optional[str]:
    """挑一张并读出来。挑中的万一读/压失败（坏文件），换一张再试，别一次就放弃。"""
    if not items:
        return None
    for _ in range(tries):
        path = _pick_file(items)
        if not path:
            return None
        b64 = _read_capped(path)
        if b64:
            return b64
    return None


def pick_b64(tag: str = "", text: str = "") -> Optional[str]:
    """按情绪标签优先、文本关键词兜底挑一张图，返回 base64；挑不到返回 None"""
    if tag:
        for candidate in (tag,) + FALLBACK.get(tag, ()):
            b64 = _try_pick(_candidates_for_tag(candidate))
            if b64:
                return b64
        # 指定了情绪就别再拿关键词乱配：以前 [表情:生气] 库里有图但文件名没对上，
        # 就顺着文本兜底发出一张大笑图，群里就是「骂完人自己笑」。宁可不发
        return None
    return _try_pick(_candidates_for_text(text))


def pick_scene_b64(scene: str, text: str = "") -> Optional[str]:
    """按「场合」挑图：冷场/调侃/接梗/自嘲。

    和 pick_b64 的区别是它吃的是情绪标签，这里吃的是场合 —— 冷场时不能甩嘲讽图。
    """
    scene = (scene or "").strip()
    if scene not in SCENES:
        return pick_b64("", text)
    _load()
    # 以前把场景下所有情绪的图堆成一个池子随机抽，等于按数量加权：
    # 冷场桶 78 张里卖萌占 72，于是每次冷场都是同一类卖萌图。
    # 改成先均匀抽一个情绪、再从那个情绪里抽图，卖萌的份额降到 1/3
    _emos = [e for e in SCENES[scene] if _by_emotion.get(e)]
    random.shuffle(_emos)
    for emo in _emos:
        b64 = _try_pick(_by_emotion.get(emo, []))
        if b64:
            return b64
    b64 = None
    for emo in SCENE_FALLBACK.get(scene, ()):
        b64 = _try_pick(_by_emotion.get(emo, []))
        if b64:
            return b64
    return None


def stats() -> Dict[str, int]:
    _load()
    return {k: len(v) for k, v in sorted(_by_emotion.items(), key=lambda x: -len(x[1]))}
