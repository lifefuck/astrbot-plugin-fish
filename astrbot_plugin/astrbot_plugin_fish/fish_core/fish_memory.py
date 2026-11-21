# -*- coding: utf-8 -*-
"""fish_memory —— 借鉴自 DSH (zhugeweimu/DSH-a-complete-life-for-every-fish) 的持久化记忆与检索系统。
适配 QQ 群聊/私聊多群多用户隔离架构：
1. SQLite 本地存储：记录（带 group_id / person_id 隔离）、标签、流水账
2. 自动规则感知（autocapture）：7 类事实/偏好/事件免显式唤醒捕获
3. 中文二元分词 + BM25 字面检索 + 时间衰减召回（hybrid）
4. 会话分段沉淀（turnlog -> event）
"""
from __future__ import annotations

import glob
import hashlib
import logging
import math
import os
import re
import shutil
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
# HERE 是 services/，只要上跳一级到项目根；以前多跳一级，库落在了 projects/data/
DEFAULT_DB_PATH = os.path.join(HERE, "..", "data", "fish_memory.db")

# 这个模块以前没定义 logger，except 里的 logger.debug 一执行就是 NameError，
# 记账失败时会直接抛到调用方
logger = logging.getLogger("astrbot_plugin_fish.memory")

CJK = re.compile(r"[\u4e00-\u9fff]")
NEG = re.compile(r"[不没别未无]")

# 7 大记忆类型与捕获规则
RULES = [
    ("fact", r"我(今年|现在)?\s*\d+\s*岁|我(叫|名字是)|我(在|于).{1,12}(工作|上班|上学|读书)|我住在?.{1,12}"),
    ("preference", r"我(很|最|特别|超)?(喜欢|爱|讨厌|不喜欢|受不了|爱吃|不吃|过敏)"),
    # plan 一定要有「我」+ 时间或打算，且后面跟着具体动作。
    # 原来只匹配「打算/要去/明天」这类裸词，「我想到了一个伟大的计划」
    # 「我要去觅食了」全被当成计划存下来，74 条计划里一半是这个。
    ("plan", r"(?:我|我们)[^。！？]{0,10}(?:打算|准备|计划|约好|决定)[^。！？]{1,20}"
             r"|(?:明天|后天|下周|下个月|周末|待会儿|一会儿|晚点|过几天)"
             r"[^。！？]{0,10}(?:我|我们)?[^。！？]{0,6}(?:打算|准备|要|得|先|再|才|就|去|回来|开始)"),
    # promise 要有明确的承诺动作，光「一定」不算（「一定很好吃」被记过承诺）
    ("promise", r"(?:我(?:会|一定|保证|答应|绝不|准)|说好了|一言为定|答应你|跟你保证)"),
    ("relationship", r"(我和|跟他|跟她|和朋友|和同事|和家人).{0,20}(吵架|和好|分手|认识|关系|见面)"),
    ("event", r"(今天|昨天|刚才|刚刚|早上|中午|下午|晚上|周末|前几天).{0,30}(了|过|去|见|吃|买|聊|玩|睡)"),
    ("note", r"(记住|记一下|记着|别忘了|别忘记|帮我记)"),
]

# 单个记忆超过这个长度基本就是整段粘贴，不是「记住的事」
MAX_MEMORY_CHARS = 140

# 见到这些标记直接不记：转发记录、图片占位、协议原文、裸链接
JUNK_MARKERS = (
    "合并转发", "聊天记录 · 共", "[图片]", "[表情]", "[文件]",
    "版权所有", "Licensed under", "TERMS AND CONDITIONS", "http://", "https://",
)

# 看图结果会跟着用户原话一起进记忆，得先剥掉：
# 「[用户发送/附带的图片/表情包内容: 画面中是一位……]」这种是模型描述，不是这人说过的话
VISION_NOISE = re.compile(
    r"\[(?:用户发送/附带的图片/表情包内容|发送了图片/表情包|图片内容)[^\]]*\]"
)

SKIP_EXACT = {
    "你好", "在吗", "在么", "晚安", "早安", "哈哈", "嗯", "哦", "好的", "谢谢", "拜拜",
    "没事", "算了", "好的呢", "行", "可以", "收到", "6", "666", "1", "打卡"
}

TYPE_IMPORTANCE = {
    "fact": 0.85, "preference": 0.75, "plan": 0.7, "promise": 0.8,
    "relationship": 0.8, "event": 0.55, "note": 0.85, "plain": 0.35
}

TYPE_TAG = {
    "fact": "事实", "preference": "偏好", "plan": "计划", "promise": "承诺",
    "relationship": "关系", "event": "事件", "note": "指定记录", "plain": "流水"
}

DDL = """
CREATE TABLE IF NOT EXISTS fish_memory_records (
    memory_id          TEXT PRIMARY KEY,
    group_id           TEXT NOT NULL DEFAULT '',
    person_id          TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    content            TEXT NOT NULL,
    memory_type        TEXT NOT NULL DEFAULT 'event',
    importance         REAL NOT NULL DEFAULT 0.5,
    created_at         REAL NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL DEFAULT 0,
    last_activated_at  REAL NOT NULL DEFAULT 0,
    activation_count   INTEGER NOT NULL DEFAULT 0,
    is_deleted         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fish_mem_group_person ON fish_memory_records (group_id, person_id);
CREATE INDEX IF NOT EXISTS idx_fish_mem_type         ON fish_memory_records (memory_type);
CREATE INDEX IF NOT EXISTS idx_fish_mem_created      ON fish_memory_records (created_at);

CREATE TABLE IF NOT EXISTS fish_memory_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  TEXT NOT NULL,
    tag_value  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fish_tag_memory ON fish_memory_tags (memory_id);

CREATE TABLE IF NOT EXISTS fish_turn_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL DEFAULT '',
    person_id  TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    closed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fish_log_lookup ON fish_turn_logs (group_id, person_id, closed);

-- 每轮真实 token 账本。/tocken 的全部数字都从这张表算，
-- 不读日志、不猜网关，落库时记的是网关返回的原值
CREATE TABLE IF NOT EXISTS fish_token_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'chat',   -- chat 聊天 / search 联网 / judge 判官 / vision 识图
    input   INTEGER NOT NULL DEFAULT 0,
    hit     INTEGER NOT NULL DEFAULT 0,
    output  INTEGER NOT NULL DEFAULT 0,
    ms      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_token_ts ON fish_token_logs (ts);
"""


class FishMemoryEngine:
    """本地 SQLite + BM25 + 时间衰减的记忆引擎（多群/私聊隔离）"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        # timeout 从 10s 压到 3s：这个库是同步写在事件循环上的，卡满 10 秒整只 bot 都不响应
        conn = sqlite3.connect(self.db_path, timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL 让读写不互斥，别家脚本/备份进程读库时不会把写者锁死；
            # busy_timeout 让 sqlite 自己短等，而不是立刻抛 database is locked
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return conn

    def _init_db(self):
        """建库/建表；库文件坏了就地隔离并回滚，绝不让整个插件加载失败。

        踩过的坑：库一旦 malformed，`conn.executescript` 直接抛 DatabaseError，
        而这段是在模块顶层 import 期执行的 —— 整个 astrbot_plugin_fish 起不来，
        群里 @ 它 79 秒毫无反应，只能人工 mv 坏库。备份任务也在加载之后才跑，
        所以「有备份」救不了这种情况。
        """
        try:
            with self._get_conn() as conn:
                conn.executescript(DDL)
                conn.commit()
            return
        except sqlite3.DatabaseError as e:
            logger.error("[fish] 记忆库不可用（%s）：%s，开始隔离并尝试回滚", self.db_path, e)
        self._recover_db()

    def _backup_candidates(self) -> List[str]:
        """按新到旧列出可用的库备份（日备目录 + 同目录 .bak）。"""
        cands: List[str] = []
        dirs = [os.path.expanduser("~/.astrbot/backups"),
                os.path.join(os.path.dirname(os.path.abspath(self.db_path)), "backups")]
        for d in dirs:
            cands.extend(glob.glob(os.path.join(d, "fish_memory-*.db")))
        cands.extend(glob.glob(self.db_path + ".bak-*"))
        return sorted(cands, key=lambda p: os.path.getmtime(p), reverse=True)

    def _recover_db(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if not os.path.exists(p):
                continue
            try:
                os.replace(p, f"{p}.corrupt-{stamp}")
            except Exception:
                try:
                    os.remove(p)
                except Exception:
                    pass
        for cand in self._backup_candidates():
            try:
                shutil.copy2(cand, self.db_path)
                with sqlite3.connect(self.db_path) as conn:
                    if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                        raise sqlite3.DatabaseError("回滚副本自身校验失败")
                    conn.executescript(DDL)
                    conn.commit()
                logger.warning("[fish] 记忆库已从备份回滚：%s（%d 字节）",
                               cand, os.path.getsize(self.db_path))
                return
            except Exception as e:
                logger.warning("[fish] 回滚副本不可用 %s：%s", cand, e)
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(self.db_path + suffix)
                    except Exception:
                        pass
        # 没有可用备份：建空库继续跑，宁可丢记忆也不能让整只 bot 起不来
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            with self._get_conn() as conn:
                conn.executescript(DDL)
                conn.commit()
            logger.error("[fish] 没有可用备份，已重建空记忆库：%s", self.db_path)
        except Exception as e:
            logger.error("[fish] 重建记忆库失败：%s", e)

    # ---------------- 分词与特征提取 ----------------
    @staticmethod
    def tokens(text: str) -> List[str]:
        """中文二元分词 + 英文/数字连续串"""
        text = (text or "").lower()
        out = re.findall(r"[a-z0-9_]{2,}", text)
        # 纯数字被 CJK 切分整段吃掉，"出差3天"和"出差9天"就会 jaccard=1.0 被当成同一条
        out.extend(re.findall(r"\d+", text))
        cjk = "".join(ch if CJK.match(ch) else " " for ch in text)
        for seg in cjk.split():
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend([seg[i:i + 2] for i in range(len(seg) - 1)])
        return out

    # 问句里的句子骨架，检索前全部丢掉，留下真正指向内容的词
    _QUERY_NOISE = re.compile(
        r"(你还?记得|记不记得|我跟?你说过|我说过吗?|我之前?说过|"
        r"你知不知道|知不知道|告诉过你|问过你|"
        r"(什么|怎么|怎样|如何|哪|谁|多少|几时)|"
        r"[吗呢么呀啊吧的了吗着过]|"
        r"(?<![\u4e00-\u9fff])(你|我|他|她|它|们)(?![\u4e00-\u9fff]))"
    )

    @classmethod
    def _content_words(cls, query: str) -> str:
        """把一句问话剥成实词：'你还记得我有几只猫吗' → '有 几 只 猫'。"""
        q = query or ""
        # 书名号/引号里的东西是实词，先摘出来，免得被句式词规则误伤
        keep = re.findall(r"[《「\"']([^《》「」\"']{1,10})[》」\"']", q)
        q = cls._QUERY_NOISE.sub(" ", q)
        return (q + " " + " ".join(keep)).strip()

    @staticmethod
    def jaccard(a: str, b: str) -> float:
        set_a = set(FishMemoryEngine.tokens(a))
        set_b = set(FishMemoryEngine.tokens(b))
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / max(1, len(set_a | set_b))

    @staticmethod
    def fingerprint(group_id: str, person_id: str, content: str, memory_type: str = "") -> str:
        raw = f"{group_id}|{person_id}|{content.strip()}|{memory_type}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # ---------------- 自动捕获（Autocapture） ----------------
    @staticmethod
    def detect_types(text: str) -> List[str]:
        hits = []
        s = text or ""
        for kind, pat in RULES:
            m = re.search(pat, s)
            if not m:
                continue
            # 否定词前缀过滤（如「不是我」）
            prefix = s[max(0, m.start() - 4):m.start()]
            if NEG.search(prefix):
                continue
            hits.append(kind)
        return hits

    @staticmethod
    def should_skip(text: str) -> bool:
        """明显不该进记忆库的东西，在入口就拦掉。

        以前只拦了「太短」和「纯问句」，结果合并转发、整段粘贴、命令说明、许可文本
        全被当成记忆存下来，面板上一屏全是这种东西，看着像抽风。
        """
        t = (text or "").strip()
        if not t or t in SKIP_EXACT or len(t) < 4:
            return True
        # 纯问句不记
        if len(t) <= 12 and (t.endswith("吗") or t.endswith("呢") or t.endswith("?") or t.endswith("？")):
            return True
        # 整段粘贴没有记忆价值：转发记录、文件内容、许可协议都在这里
        if len(t) > MAX_MEMORY_CHARS:
            return True
        if any(m in t for m in JUNK_MARKERS):
            return True
        # 命令说明/帮助文本：一句里塞了好几个 /command
        if len(re.findall(r"(?<!\S)/[a-zA-Z][\w-]{1,20}", t)) >= 2:
            return True
        # 表情/符号堆：去掉表情后几乎没字
        if len(CJK.findall(t)) <= 1 and len(set(t)) <= 4:
            return True
        return False

    # ---------------- 写入与去重合并 ----------------
    def add_memory(
        self,
        group_id: str,
        person_id: str,
        content: str,
        title: str = "",
        memory_type: str = "event",
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        ts: Optional[float] = None
    ) -> Dict:
        """写入单条记忆，精确指纹或语义近重（Jaccard > 0.75）则合并"""
        content = (content or "").strip()
        if not content:
            return {"merged": False, "skipped": True}

        title = (title or content[:20]).strip()
        now_ts = float(ts if ts is not None else time.time())
        imp = importance if importance is not None else TYPE_IMPORTANCE.get(memory_type, 0.5)

        with self._get_conn() as conn:
            # 1. 精确指纹排重
            fp = self.fingerprint(group_id, person_id, content, memory_type)
            # 注意别加 is_deleted=0：软删行仍然占着 memory_id 主键，
            # 过滤掉再 INSERT 会直接撞 UNIQUE 约束，这条事实就静默写不进来了。
            # 命中软删行 = 同一个人又说了同一件事，正确做法是复活它。
            row = conn.execute(
                "SELECT memory_id, content, activation_count, is_deleted FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND memory_id=?",
                (group_id, person_id, fp)
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE fish_memory_records SET activation_count=activation_count+1, "
                    "last_activated_at=?, updated_at=?, is_deleted=0 WHERE memory_id=?",
                    (now_ts, now_ts, row["memory_id"])
                )
                if row["is_deleted"]:
                    _logger.info("[记忆] 复活软删记录 %s（同一件事又提到一次）", row["memory_id"][:8])
                conn.commit()
                return {"id": row["memory_id"], "merged": True, "score": 1.0}

            # 2. 局部近重搜索
            candidates = conn.execute(
                "SELECT memory_id, title, content, activation_count FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND memory_type=? AND is_deleted=0 "
                "ORDER BY created_at DESC LIMIT 50",
                (group_id, person_id, memory_type)
            ).fetchall()

            new_numbers = set(re.findall(r"\d+", content))
            for cand in candidates:
                # 数字不一样就不是同一个事实（3天/9天、25岁/28岁），重合度再高也不能合并，
                # 否则新事实被旧内容覆盖，用户再说一遍也只会 merge 回旧句，永远改不回来
                old_numbers = set(re.findall(r"\d+", cand["content"]))
                if new_numbers and old_numbers and new_numbers != old_numbers:
                    continue
                sim = self.jaccard(content, cand["content"])
                if sim >= 0.75:
                    new_content = content if len(content) > len(cand["content"]) else cand["content"]
                    # title 跟着正文走：只改 content 不改 title，/memory 里显示的还是旧内容前 20 字
                    new_title = title if new_content == content else cand["title"]
                    conn.execute(
                        "UPDATE fish_memory_records SET content=?, title=?, activation_count=activation_count+1, "
                        "last_activated_at=?, updated_at=? WHERE memory_id=?",
                        (new_content, new_title, now_ts, now_ts, cand["memory_id"])
                    )
                    conn.commit()
                    return {"id": cand["memory_id"], "merged": True, "score": round(sim, 2)}

            # 3. 插入新记录
            try:
                conn.execute(
                    "INSERT INTO fish_memory_records (memory_id, group_id, person_id, title, content, "
                    "memory_type, importance, created_at, updated_at, last_activated_at, activation_count, is_deleted) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                    (fp, group_id, person_id, title, content, memory_type, imp, now_ts, now_ts, now_ts)
                )
                for tag in (tags or [TYPE_TAG.get(memory_type, "记忆")]):
                    conn.execute(
                        "INSERT INTO fish_memory_tags (memory_id, tag_value) VALUES (?, ?)",
                        (fp, tag)
                    )
                conn.commit()
            except sqlite3.IntegrityError:
                # 两条消息并发写同一件事：另一边已经插进去了，按「合并」返回，
                # 不让调用方以为写失败而反复重试
                conn.rollback()
                conn.execute(
                    "UPDATE fish_memory_records SET activation_count=activation_count+1, "
                    "last_activated_at=?, updated_at=?, is_deleted=0 WHERE memory_id=?",
                    (now_ts, now_ts, fp)
                )
                conn.commit()
                return {"id": fp, "merged": True, "score": 1.0}
            return {"id": fp, "merged": False, "score": 0.0}

    # ---------------- BM25 检索与时间衰减召回 ----------------
    def recall(
        self,
        group_id: str,
        person_id: str,
        query: str,
        top_k: int = 2,
        half_life_days: float = 14.0
    ) -> List[Dict]:
        """根据当前发言检索该群友最相关的历史记忆条目。
        融合分数 = BM25字面分 * 重要度 * 时间半衰期衰减
        """
        # 问「你还记得我有几只猫吗」拿整句检索必然 0 命中：原话里根本没有这一整句。
        # 先把问句剥成实词再检索，剥完空了才退回原句。
        q_words = self._content_words(query)
        # 「猫」这种单字在正文里是「养猫」的一部分，二元分词永远对不上，
        # 单独记下来按字面包含加一点分，不然问「几只猫」永远 0 命中
        # 高频虚字不能当检索线索，否则「今天天气怎么样」能匹配上「天天熬夜」
        q_chars = [c for c in dict.fromkeys(q_words)
                   if CJK.match(c) and c not in "我你他她它们的了是有不在这那就都也还和个天今气什么上下里到说想会能要"]
        # 这几个二元词在聊天里几乎每句都有，留着只会把不相干的记忆勾出来
        _GENERIC = {"天天", "今天", "现在", "时候", "东西", "事情", "问题",
                    "一下", "有点", "真的", "就是", "可以", "知道", "觉得"}
        q_tokens = [t for t in self.tokens(q_words) if t not in _GENERIC]
        if not q_tokens:
            q_tokens = self.tokens(query)
        if not q_tokens:
            return []
        # 数量型提问的答案往往不带实体词（「4只」），单独扫一遍数字+量词
        wants_number = bool(re.search(r"(几|多少)\s*(只|个|次|条|张|天|岁|块|分|位|门|套)", query or ""))

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT memory_id, title, content, memory_type, importance, created_at, "
                "last_activated_at, activation_count FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND is_deleted=0",
                (group_id, person_id)
            ).fetchall()

        if not rows:
            return []

        now_ts = time.time()
        docs = [dict(r) for r in rows]
        n_docs = len(docs)
        avgdl = sum(len(self.tokens(d["content"])) for d in docs) / max(1, n_docs)

        # 统计 IDF
        df = {}
        for d in docs:
            dtoks = set(self.tokens(d["content"]))
            for t in set(q_tokens):
                if t in dtoks:
                    df[t] = df.get(t, 0) + 1

        k1, b = 1.2, 0.75
        results = []

        for d in docs:
            dtoks = self.tokens(d["content"])
            doc_len = len(dtoks)
            bm25 = 0.0
            for t in q_tokens:
                if t not in df:
                    continue
                idf = math.log((n_docs - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
                tf = dtoks.count(t)
                denom = tf + k1 * (1.0 - b + b * (doc_len / max(1, avgdl)))
                bm25 += idf * (tf * (k1 + 1.0)) / max(0.001, denom)

            if q_chars:
                bm25 += 0.8 * sum(1 for c in q_chars if c in (d["content"] or ""))

            # 数量型加分只在「本来就是同一件事」时生效：
            # 否则问「你几岁了」会把「我养了4只猫」当成答案（纯数字+量词就加分）
            if wants_number and bm25 > 0.1 and re.search(r"\d+\s*(只|个|次|条|张|天|岁|块|分|位|门|套)", d["content"] or ""):
                # 扫到「4只猫」这种，直接抬分，不然这条永远排不进前二
                bm25 += 1.5

            if bm25 <= 0.1:
                continue

            # 半衰期衰减因子: 越久远没激活的分数越低（但 fact/preference 衰减很慢）
            age_days = (now_ts - d.get("last_activated_at", d["created_at"])) / 86400.0
            effective_half_life = half_life_days * (2.0 if d["memory_type"] in ("fact", "preference") else 1.0)
            decay = math.exp(-0.693 * max(0.0, age_days) / max(1.0, effective_half_life))
            # 衰减设地板：一年前的旧事也会衰减到 0.0002，等于永久失效，
            # 但「我养猫」这种事不该因为没人提就忘光。地板 0.15 让它沉底但活着
            decay = max(0.15, decay)

            score = bm25 * d["importance"] * decay
            if score <= 0.05:
                # 0 分条目别占 top_k 名额
                continue
            results.append({
                "id": d["memory_id"],
                "content": d["content"],
                "type": d["memory_type"],
                "score": score
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def record_turn(self, group_id: str, person_id: str, user_text: str, reply_text: str = "",
                    log: bool = True):
        """记录回合流水账，并自动探测是否包含值得捕获的 7 类记忆。

        log=False 只做记忆抽取、不写流水：收到消息时先抽一次，等回复出来再用
        attach_reply() 把回复补进同一行。以前两处各插一行，一条消息留两条流水。
        """
        clean_user = VISION_NOISE.sub(" ", (user_text or "")).strip()
        clean_user = re.sub(r"\s{2,}", " ", clean_user)
        if not clean_user:
            return

        now_ts = time.time()
        if log:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO fish_turn_logs (group_id, person_id, ts, user_text, reply_text, closed) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (group_id, person_id, now_ts, clean_user, reply_text or "")
                )
                conn.commit()

        # 规则自动捕获：一句话只落一条记忆。
        # 以前一条消息命中几类就存几条，同一句话在面板上重复四五遍，
        # 类型还各不相同（plan/promise/event 全是同一段文本）。
        if not self.should_skip(clean_user):
            detected = self.detect_types(clean_user)
            if detected:
                m_type = max(detected, key=lambda k: TYPE_IMPORTANCE.get(k, 0.5))
                self.add_memory(
                    group_id=group_id,
                    person_id=person_id,
                    content=clean_user,
                    memory_type=m_type,
                    importance=TYPE_IMPORTANCE.get(m_type, 0.7),
                    ts=now_ts
                )


    def attach_reply(self, group_id: str, person_id: str, user_text: str, reply_text: str,
                     within: float = 600.0) -> bool:
        """把回复补到刚记的那行流水上；找不到就新插一行，别把话丢了。"""
        if not reply_text:
            return False
        clean = re.sub(r"\s{2,}", " ", VISION_NOISE.sub(" ", (user_text or "")).strip())
        now_ts = time.time()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM fish_turn_logs WHERE group_id=? AND person_id=? AND user_text=? "
                "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                (group_id, person_id, clean, now_ts - within)
            ).fetchone()
            if row:
                conn.execute("UPDATE fish_turn_logs SET reply_text=?, closed=1 WHERE id=?",
                             (reply_text, row["id"]))
                conn.commit()
                return True
            conn.execute(
                "INSERT INTO fish_turn_logs (group_id, person_id, ts, user_text, reply_text, closed) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (group_id, person_id, now_ts, clean, reply_text)
            )
            conn.commit()
        return False

    # ---------- token 账本 ----------
    def log_token(self, kind: str, input_tokens: int, hit_tokens: int,
                  output_tokens: int, ms: int, ts: Optional[float] = None) -> None:
        """记一轮真实用量。落库的是网关原值，不做任何估算补全。"""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO fish_token_logs (ts, kind, input, hit, output, ms)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (float(ts if ts is not None else time.time()), str(kind or "chat"),
                     int(input_tokens or 0), int(hit_tokens or 0),
                     int(output_tokens or 0), int(ms or 0)),
                )
        except Exception as e:  # 记账失败绝不能影响回话
            logger.debug("[fish] token 记账失败：%s", e)

    def token_stats(self) -> Dict[str, Dict[str, float]]:
        """/tocken 的全部数据源：最近 10 条 + 7 日/昨日/今日 三窗口。"""
        now = time.time()
        lt = time.localtime(now)
        today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        yday0 = today0 - 86400
        windows = {"7日": (now - 7 * 86400, None), "昨日": (yday0, today0),
                   "今日": (today0, None)}

        def _agg(since: float, until: Optional[float], kind: Optional[str] = None):
            sql = ("SELECT COUNT(*) n, COALESCE(SUM(input),0) inp, COALESCE(SUM(hit),0) hit,"
                   " COALESCE(SUM(output),0) outp, COALESCE(SUM(ms),0) ms FROM fish_token_logs"
                   " WHERE ts >= ?")
            args: list = [since]
            if until is not None:
                sql += " AND ts < ?"
                args.append(until)
            if kind:
                sql += " AND kind = ?"
                args.append(kind)
            try:
                with self._get_conn() as conn:
                    row = conn.execute(sql, args).fetchone()
            except Exception as e:
                logger.debug("[fish] token 统计失败：%s", e)
                return {"n": 0, "inp": 0, "hit": 0, "outp": 0, "ms": 0}
            return dict(row) if row else {"n": 0, "inp": 0, "hit": 0, "outp": 0, "ms": 0}

        out: Dict[str, Dict[str, float]] = {}
        for name, (since, until) in windows.items():
            allw = _agg(since, until)
            chat = _agg(since, until, "chat")
            search = _agg(since, until, "search")
            out[name] = {
                "n": allw["n"], "inp": allw["inp"], "hit": allw["hit"],
                "outp": allw["outp"], "ms": allw["ms"],
                "n_chat": chat["n"], "chat_inp": chat["inp"], "chat_outp": chat["outp"],
                "chat_ms": chat["ms"],
                "n_search": search["n"], "search_inp": search["inp"],
                "search_outp": search["outp"], "search_ms": search["ms"],
            }
        # 最近 10 条聊天：只算 chat，联网那条路的输入天然大一个量级，混进来就没意义
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT input, hit, output FROM fish_token_logs"
                    " WHERE kind='chat' ORDER BY ts DESC LIMIT 10").fetchall()
        except Exception:
            rows = []
        # 当前上下文大小 = 最近一次聊天调用真实喂进去的输入 token。
        # 这个数比「猜历史条数」准，它含人设、规则、记忆、ctx 全部内容
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT input, ts FROM fish_token_logs WHERE kind='chat'"
                    " ORDER BY ts DESC LIMIT 1").fetchone()
        except Exception:
            row = None
        out["当前"] = {"inp": row["input"] if row else 0,
                       "ts": row["ts"] if row else 0}
        out["最近10"] = {
            "n": len(rows),
            "inp": sum(r["input"] for r in rows),
            "hit": sum(r["hit"] for r in rows),
            "outp": sum(r["output"] for r in rows),
        }
        return out

    def prune(self, log_days: float = 45.0, grave_days: float = 30.0) -> dict:
        """定期清理：流水账留 45 天，软删的记忆留 30 天。

        流水账是每句话一行，不清会一直涨（搬过来的旧库已经有 8800 多行）。
        """
        now_ts = time.time()
        with self._get_conn() as conn:
            n1 = conn.execute("DELETE FROM fish_turn_logs WHERE ts < ?",
                              (now_ts - log_days * 86400,)).rowcount
            n2 = conn.execute("DELETE FROM fish_memory_tags WHERE memory_id IN "
                              "(SELECT memory_id FROM fish_memory_records "
                              " WHERE is_deleted=1 AND updated_at < ?)",
                              (now_ts - grave_days * 86400,)).rowcount
            n3 = conn.execute("DELETE FROM fish_memory_records WHERE is_deleted=1 AND updated_at < ?",
                              (now_ts - grave_days * 86400,)).rowcount
            conn.commit()
        return {"turn_logs": n1, "tags": n2, "memories": n3}


# 单例实例
fish_memory_engine = FishMemoryEngine()
