# -*- coding: utf-8 -*-
"""本群自己的梗：从真实聊天记录里挖出来的词和出圈话。

数据由 scripts/mine_group_memes.py 统计生成（只收群里真实出现过的，
一个字不编），这里只负责读和拼提示词。

为什么不让人设直接教它「讲点好笑的」：通用黄段子和网络烂梗哪个群都能说，
一开口就是外地人。本群自己的词（大肥鱼、搬史、堵桥这种）才是「自己人」的证明，
用对一句顶十句。数据没生成时退回 group_slang.json 的老词表，什么都没有就不注入。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from astrbot.api import logger

# 这里不能 `from .kernel import DATA_DIR`：本模块是被 kernel 自己在初始化途中
# _load() 进来的，那时 kernel 还没跑完，反向 import 直接 ImportError（已踩）
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_FILE = Path(DATA_DIR) / "group_memes.json"
_LEGACY = Path(DATA_DIR) / "group_slang.json"

_cache: dict = {"mtime": 0.0, "data": {}}


def _load() -> dict:
    """按文件 mtime 缓存；挖掘脚本改完文件不用重启就能生效。"""
    for path in (_FILE, _LEGACY):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if _cache["mtime"] == mtime and _cache["data"]:
            return _cache["data"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            logger.warning("[fish] 读群梗库失败 %s：%s", path.name, e)
            continue
        groups = raw.get("groups") or {}
        # 老格式只有 words（键 w/n/u），新格式带 phrases
        data = {}
        for gid, val in groups.items():
            if isinstance(val, list):
                data[str(gid)] = {"words": val, "phrases": []}
            else:
                data[str(gid)] = {"words": val.get("words") or [],
                                  "phrases": val.get("phrases") or []}
        _cache.update({"mtime": mtime, "data": data})
        return data
    return {}


def has_group(gid) -> bool:
    return str(gid) in _load()


def words(gid) -> list[dict]:
    return list((_load().get(str(gid)) or {}).get("words") or [])


def phrases(gid) -> list[dict]:
    return list((_load().get(str(gid)) or {}).get("phrases") or [])


def _hit(text: str, line_list: list[str]) -> list[str]:
    flat = str(text or "")
    return [w for w in line_list if len(w) >= 2 and w in flat]


def cold_hint(gid, *, silent_min: float = 0.0, topic: str = "") -> str:
    """冷场时怎么把气氛接回来：先垫表情包，再拿本群的梗抛个话头。

    只给「往哪个方向说」，不给具体台词——写成台词它就会照念，群里一眼假。
    """
    lines = []
    mins = int(silent_min // 60) if silent_min else 0
    when = f"群里安静了 {mins} 分钟" if mins >= 1 else "这会儿群里没人吭声"
    lines.append(
        f"【{when}，气氛冷着】：先甩一张表情包垫场（句末写 [MEME:冷场]），"
        "再顺一句短话把话头递出去——接龙、问他们最近在忙的那个事、或者拿群里的老梗起个头。"
    )
    if topic:
        lines.append(f"【刚聊完的是】：{topic}（从这里接着往下说，别另起一个无关话题）")
    ph = phrases(gid)
    if ph:
        sample = [p["t"] for p in random.sample(ph, min(3, len(ph)))]
        lines.append("【你们群反复出现过的话（当话头素材用，别照抄整句）】：" + " / ".join(sample))
    w = [i["w"] for i in words(gid)[:10] if len(i["w"]) >= 2]
    if w:
        lines.append("【本群自己的说法】：" + "、".join(w))
    lines.append("荤的、骂人的、编出来的段子都不要——气氛靠梗和表情包，不靠下三路。")
    return "\n".join(lines)


def match_hint(gid, text: str) -> str:
    """这句里用到了本群的词/老话时点一下，比给一坨词表准得多。"""
    w = _hit(text, [i["w"] for i in words(gid)])
    p = _hit(text, [i["t"] for i in phrases(gid)])
    bits = []
    if w:
        bits.append("「" + "」「".join(w[:3]) + "」是你们群自己的说法")
    if p:
        bits.append("「" + "」「".join(p[:2]) + "」是群里常挂嘴边的那句")
    if not bits:
        return ""
    return "【" + "，".join(bits) + "，听懂就顺着接，别解释】"


def stats() -> dict:
    data = _load()
    return {
        "groups": len(data),
        "words": sum(len(v.get("words") or []) for v in data.values()),
        "phrases": sum(len(v.get("phrases") or []) for v in data.values()),
        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(_cache["mtime"] or 0)),
    }
