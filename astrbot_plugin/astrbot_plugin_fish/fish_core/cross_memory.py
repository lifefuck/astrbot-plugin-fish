# -*- coding: utf-8 -*-
"""跨群记忆：把每个群最近在聊什么存下来，拼 prompt 时允许「提一句别的群」。

只存短摘要，不存整段对话：私事、QQ 号、链接一律在上游削掉，避免把一个群的八卦
搬到另一个群。开关默认开，但每次最多引用 2 条、且只引 3 小时内的。
"""

from __future__ import annotations

import json
import os
import re
import time

MAX_AGE_SEC = 3 * 3600
_STRIP = (
    (re.compile(r"https?://\S+"), "[链接]"),
    (re.compile(r"@\S+"), ""),
    (re.compile(r"\b\d{5,}\b"), ""),          # QQ 号/手机号一类长数字
    (re.compile(r"\s+"), " "),
)


def clean(text: str, limit: int = 40) -> str:
    t = (text or "").strip()
    for pat, rep in _STRIP:
        t = pat.sub(rep, t)
    return t.strip(" ，,。.~～")[:limit]


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard。中文没空格，按词切不现实，二元组够用。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


class CrossGroupMemory:
    def __init__(self, path: str, max_topics: int = 60, logger=None):
        self.path = path
        self.max_topics = max(10, int(max_topics or 60))
        self.logger = logger
        self.topics: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.topics = [t for t in data if isinstance(t, dict) and t.get("text")]
        except FileNotFoundError:
            self.topics = []
        except Exception as e:
            if self.logger:
                self.logger.warning("[fish] 跨群话题读取失败 %s: %s", self.path, e)
            self.topics = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.topics[-self.max_topics:], f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            if self.logger:
                self.logger.warning("[fish] 跨群话题落盘失败 %s: %s", self.path, e)

    def record_topic(self, group_id, sender_display: str, content: str) -> None:
        """记一条群话题。同一群高度重复的说法直接覆盖时间戳，不留一堆近似重复。"""
        text = clean(content, 60)
        if not text or len(text) < 4:
            return
        gid = str(group_id)
        for t in reversed(self.topics[-20:]):
            if t.get("gid") == gid and similarity(t.get("text", ""), text) > 0.6:
                t["text"] = text
                t["ts"] = time.time()
                t["who"] = clean(sender_display, 12)
                self._save()
                return
        self.topics.append({"gid": gid, "who": clean(sender_display, 12), "text": text, "ts": time.time()})
        self.topics = self.topics[-self.max_topics:]
        self._save()

    def other_topics(self, group_id, limit: int = 2, now: float | None = None) -> list[str]:
        """别的群最近在聊什么（不含本群）。每条形如「另一群人那边在聊：xxx」。"""
        now = time.time() if now is None else now
        gid = str(group_id)
        pool = [t for t in self.topics
                if t.get("gid") and t.get("gid") != gid and now - float(t.get("ts") or 0) <= MAX_AGE_SEC]
        if not pool:
            return []
        pool.sort(key=lambda t: -float(t.get("ts") or 0))
        picked: list[dict] = []
        for t in pool:
            # 去重：两个群在聊同一件事就只说一次
            if any(similarity(t["text"], p["text"]) > 0.5 for p in picked):
                continue
            picked.append(t)
            if len(picked) >= max(1, int(limit or 2)):
                break
        return [f"另一个群在聊「{t['text']}」" for t in picked]

    def hint(self, group_id, limit: int = 2) -> str:
        lines = self.other_topics(group_id, limit)
        if not lines:
            return ""
        return ("\n【别的群此刻在聊】（不是这个群的事，只在你确实接得上时顺口带一句，别硬提）：\n"
                + "\n".join("- " + x for x in lines))
