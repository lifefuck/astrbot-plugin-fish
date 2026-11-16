# -*- coding: utf-8 -*-
"""群内互动关系网：谁最近老跟谁说话。

只回答一个问题——「这个群里眼下谁和谁在互相搭话」，用来给 prompt 补一句上下文，
防止把 A 的话当成 B 的、或者对着刚被怼过的人继续开玩笑。

内存里是一个 dict，可选落盘。落盘路径由调用方传，模块自己不猜目录。
"""

from __future__ import annotations

import collections
import json
import os
import time
from typing import Deque, Dict, List, Optional

__all__ = [
    "record_interaction",
    "get_group_relations_summary",
    "load_store",
    "save_store",
    "prune",
]

MAX_RELATION_RECORDS = 20      # 每个群最近 20 次定向互动
MAX_GROUPS = 200               # 群数量的硬上限，多了按最后活跃时间踢最旧的
RECENT_WINDOW = 900.0          # 15 分钟外的记录不再参与统计
MAX_AGE_SEC = 6 * 3600         # 落盘时更老的记录直接丢，反正永远用不上

# group_id(str) -> deque[{from_id, from_nick, to_id, to_nick, ts}]
group_interactions: Dict[str, Deque[dict]] = {}
# 群的最后活跃时间，用来做 LRU 淘汰
_group_last_seen: Dict[str, float] = {}

_SAVE_MIN_INTERVAL = 3.0       # 每次 @ 都写盘太浪费，3 秒内合并成一次
_last_save_at = 0.0


def _gkey(group_id) -> str:
    """群号统一成字符串：JSON 的 key 只能是字符串，int/str 混用会让读回来的数据对不上。"""
    return str(group_id)


def _bucket(group_id) -> Deque[dict]:
    key = _gkey(group_id)
    bucket = group_interactions.get(key)
    if bucket is None:
        bucket = collections.deque(maxlen=MAX_RELATION_RECORDS)
        group_interactions[key] = bucket
    return bucket


def _evict_groups() -> None:
    """群数量封顶。没有这一步，机器人待的群越多内存越涨，且永远不会回落。"""
    if len(group_interactions) <= MAX_GROUPS:
        return
    order = sorted(group_interactions, key=lambda g: _group_last_seen.get(g, 0.0))
    for key in order[: len(group_interactions) - MAX_GROUPS]:
        group_interactions.pop(key, None)
        _group_last_seen.pop(key, None)


def record_interaction(group_id: int, sender_id: int, sender_nick: str, target_id: int,
                       target_nick: str, path: Optional[str] = None) -> None:
    """记录一次定向互动（@ 或引用回复）。自己 @ 自己不算互动。"""
    global _last_save_at
    if not group_id or not sender_id or not target_id or sender_id == target_id:
        return
    now = time.time()
    _bucket(group_id).append({
        "from_id": sender_id,
        "from_nick": sender_nick or str(sender_id),
        "to_id": target_id,
        "to_nick": target_nick or str(target_id),
        "ts": now,
    })
    _group_last_seen[_gkey(group_id)] = now
    _evict_groups()
    if path:
        if now - _last_save_at >= _SAVE_MIN_INTERVAL:
            _last_save_at = now
            save_store(path)


def get_group_relations_summary(group_id: int, current_user_id: Optional[int] = None,
                                max_pairs: int = 3) -> str:
    """最近 15 分钟最活跃的几对互动，拼成一段 prompt 辅助文本。没料就返回空串。"""
    records = group_interactions.get(_gkey(group_id))
    if not records:
        return ""

    now = time.time()
    recent = [r for r in records if now - r.get("ts", 0) < RECENT_WINDOW]
    if not recent:
        return ""

    pair_counts: Dict[tuple, int] = collections.defaultdict(int)
    nick_map: Dict[int, str] = {}
    for r in recent:
        pair_key = tuple(sorted([r["from_id"], r["to_id"]]))
        pair_counts[pair_key] += 1
        # 昵称会改，取最近一次观测到的
        nick_map[r["from_id"]] = r["from_nick"]
        nick_map[r["to_id"]] = r["to_nick"]

    sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:max_pairs]
    if not sorted_pairs:
        return ""

    snippets = []
    for (u1, u2), cnt in sorted_pairs:
        n1 = nick_map.get(u1, str(u1))
        n2 = nick_map.get(u2, str(u2))
        snippets.append(f"「{n1}」与「{n2}」最近互动较多({cnt}次)")

    return "【群内当前焦点关系】：\n- " + "；".join(snippets) + "\n"


# ---------------------------------------------------------------------------
# 落盘 / 读盘
# ---------------------------------------------------------------------------

def prune(now: Optional[float] = None) -> None:
    """丢掉过期记录。内存里每群有 maxlen 兜底，但置顶群长期不说话会一直占位。"""
    now = time.time() if now is None else now
    for key in list(group_interactions):
        bucket = group_interactions[key]
        fresh = [r for r in bucket if now - r.get("ts", 0) <= MAX_AGE_SEC]
        if not fresh:
            group_interactions.pop(key, None)
            _group_last_seen.pop(key, None)
            continue
        bucket.clear()
        bucket.extend(fresh)


def save_store(path: str) -> bool:
    """写盘。只写最近 MAX_AGE_SEC 内的记录，文件大小因此有上界。"""
    try:
        prune()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        payload = {key: list(bucket) for key, bucket in group_interactions.items() if bucket}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        # 关系网只是锦上添花，写不进去也不能拖垮回复链路
        return False


def load_store(path: str) -> bool:
    """读盘。字段坏掉的行丢掉，不因为一条脏数据让整个模块起不来。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return False
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    now = time.time()
    for key, rows in data.items():
        if not isinstance(rows, list):
            continue
        bucket = collections.deque(maxlen=MAX_RELATION_RECORDS)
        for row in rows[-MAX_RELATION_RECORDS:]:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if now - ts > MAX_AGE_SEC:
                continue
            bucket.append({
                "from_id": row.get("from_id"),
                "from_nick": str(row.get("from_nick") or ""),
                "to_id": row.get("to_id"),
                "to_nick": str(row.get("to_nick") or ""),
                "ts": ts,
            })
        if bucket:
            group_interactions[str(key)] = bucket
            _group_last_seen[str(key)] = max(r["ts"] for r in bucket)
    _evict_groups()
    return True
