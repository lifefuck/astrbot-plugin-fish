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
    # QQ 号/手机号/身份证：不能用 \b。汉字也算 \w，所以「加我123456789聊」里
    # 数字两边根本没有词边界，旧写法原样放行 —— 实测会把 QQ 号搬去别的群。
    (re.compile(r"(?<!\d)\d{3,}(?!\d)"), ""),
    (re.compile(r"\s+"), " "),
)

# 一句话里出现这些，整条不记（不截断、不打码，直接不存）。
# 截断逻辑永远追不上「密码是我生日」这种自然语言，只能按语义整条放弃。
_SENSITIVE = re.compile(
    r"密码|口令|验证码|身份证|手机号|电话是|住址|家庭住址|银行卡|卡号|护照|"
    r"私聊我|加我|开房|病历|诊断|吃什么药"
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
                # 存量清洗：老版本把「加我123456789」「密码是我生日」这类也记下来了，
                # 载入时按新规则重刷一遍，不指望人工去改那 60 条 json
                cleaned = []
                for t in self.topics:
                    if _SENSITIVE.search(str(t.get("text") or "")):
                        continue
                    nt = clean(str(t.get("text") or ""), 60)
                    if len(nt) < 4:
                        continue
                    t["text"] = nt
                    cleaned.append(t)
                self.topics = cleaned
                self._trim()
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
        raw = str(content or "")
        # 私事一律不记：这类内容一旦进了别的群的 prompt，就再也收不回来了
        if _SENSITIVE.search(raw):
            return
        text = clean(raw, 60)
        if not text or len(text) < 4:
            return
        gid = str(group_id)
        for t in reversed(self.topics[-20:]):
            if t.get("gid") == gid and similarity(t.get("text", ""), text) > 0.6:
                t["text"] = text
                t["ts"] = time.time()
                t["who"] = clean(sender_display, 12)
                self._trim()
                self._save()
                return
        self.topics.append({"gid": gid, "who": clean(sender_display, 12), "text": text, "ts": time.time()})
        self._trim()
        self._save()

    def _trim(self) -> None:
        """按群分摊配额，别让最吵的群把全部槽位吃掉。

        原来只做 `topics[-60:]`：实测 21 个群跑下来只剩 15 个群有话题，
        另外 6 个群被彻底挤空，等于那几个群永远「没听见别的群在聊」。
        """
        gids = {t.get("gid") for t in self.topics if t.get("gid")}
        per = max(2, self.max_topics // max(1, len(gids)))
        keep: list[dict] = []
        cnt: dict[str, int] = {}
        for t in reversed(self.topics):
            g = str(t.get("gid") or "")
            if cnt.get(g, 0) >= per:
                continue
            cnt[g] = cnt.get(g, 0) + 1
            keep.append(t)
        keep.reverse()
        self.topics = keep[-self.max_topics:]

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
