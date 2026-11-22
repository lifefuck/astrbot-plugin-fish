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
# 环境变量优先：单测和迁移脚本要指到临时库/线上库，不能写死
DEFAULT_DB_PATH = os.environ.get("FISH_MEMORY_DB") or os.path.join(HERE, "..", "data", "fish_memory.db")

# 这个模块以前没定义 logger，except 里的 logger.debug 一执行就是 NameError，
# 记账失败时会直接抛到调用方
logger = logging.getLogger("astrbot_plugin_fish.memory")

# 「上一轮召回给我看了哪几条」的缓存：纠正检测要在下一句话里对上是哪条。
# 必须按 (群, 人) 分开存——只按群存的话，B 的一句「不是吧」会改到 A 的档案上
RECALLED_TTL_SEC = 300.0


def put_recalled(store: Dict, gid: str, uid: str, items, now: float = 0.0) -> None:
    """记下这一轮召回的结果（带时间戳，取的时候要限时）。"""
    store[(str(gid), str(uid))] = (float(now or time.time()), list(items or []))


def take_recalled(store: Dict, gid: str, uid: str,
                  ttl: float = RECALLED_TTL_SEC, now: float = 0.0) -> List[Dict]:
    """取回「这个人上一轮看到的记忆」。超过 ttl 就当没有。

    限时是必须的：半小时前召回的那条被一句「不是」命中，改的是老账，不是这句在说的事
    """
    key = (str(gid), str(uid))
    rec = store.get(key)
    if not rec:
        return []
    ts, items = rec
    if float(now or time.time()) - float(ts) > float(ttl):
        store.pop(key, None)
        return []
    return list(items or [])


def drop_recalled(store: Dict, gid: str, uid: str) -> None:
    """纠错用完就清，免得同一句否定连着命中两轮。"""
    store.pop((str(gid), str(uid)), None)


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

# ---------------- 记忆出处（第 5 步） ----------------
# 四类来源，语义只有一句区别：这话是本人认下的，还是别人嘴里听来的
#   said       本人亲口（可断言）
#   reported   转述/听说（引用时要带「听 X 说」）
#   heard      提到第三人的事（不是关于他自己，容易张冠李戴）
#   inferred   鱼自己总结的印象（永远不许主动提）
#   said_legacy 迁移过来的旧记忆，没有原文锚点，只能当素材
SOURCE_CONF = {"said": 0.9, "reported": 0.5, "heard": 0.6, "inferred": 0.4,
               "said_legacy": 0.45}

# prompt 里怎么说来源。宁可啰嗦一句「听他说」，也别让鱼把别人的事安在这人头上
SOURCE_WORD = {"said": "他自己说的", "reported": "他转述的", "heard": "你听来的",
               "inferred": "你自己的印象", "said_legacy": "旧记录，没对上原话"}

# 记忆按类型给一个「下一步能干什么」，光给事实模型只会背书，不会接着聊
ACTION_HINT = {
    "fact": "可以顺着接一句，别当新信息",
    "preference": "可以顺着提，别替他说喜欢什么",
    "plan": "可以问一句办没办，别催",
    "promise": "可以轻轻提一下，别讨债",
    "event": "当旧事带过，别说成刚发生的",
    "relationship": "提的时候别加评论",
    "note": "他说让你记的，可以复述",
    "plain": "",
}

_REPORTED_RE = re.compile(r"听说|据说|听.{1,4}说|转述|别人说|人家说|网上说")
_HEARD_RE = re.compile(r"(他|她|他们|她们|朋友|同事|同学|室友|对象|老婆|老公)[^。！？]{0,6}(说|讲|觉得|在)")
_SELF_RE = re.compile(r"我(叫|是|在|有|养|住|今年|最近|打算|准备|喜欢|讨厌|不|没|刚|昨天|今天)")

_STOP_CHARS = set("我你他她它们的了是有不在这那就都也还和个天今气什么上下里到说想会能要")


def topics_of(text: str, limit: int = 3) -> str:
    """抽 3 个话题词，不调模型。

    只用汉字二元组里出现过的实词：话题是给检索和「这件事我是不是说过」用的，
    抽虚词进去会让任何两句话都算同一个话题。
    """
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    words = []
    for w in re.findall(r"[\u4e00-\u9fff]{2,6}", t):
        for i in range(len(w) - 1):
            g = w[i:i + 2]
            if g[0] in _STOP_CHARS or g[1] in _STOP_CHARS:
                continue
            if g not in words:
                words.append(g)
            if len(words) >= limit:
                return ",".join(words)
    return ",".join(words[:limit])


def classify_source(text: str) -> str:
    """按措辞判来源，纯规则。

    顺序有讲究：先看「听说」，再看「他说」，最后看「我」。
    「我听说 X」是转述不是自述，反了就把它记成这人自己的事
    """
    t = str(text or "")
    if _REPORTED_RE.search(t):
        return "reported"
    if _SELF_RE.search(t):
        return "said"
    if _HEARD_RE.search(t):
        return "heard"
    return "said"


def topic_overlap(item: Dict, query: str) -> bool:
    """问句里的词跟这条记忆的话题/正文有没有实打实的重合。"""
    q = str(query or "")
    if len(q) < 2:
        return False
    for w in re.split(r"[,，\s]+", str(item.get("topic") or "")):
        if len(w) >= 2 and w in q:
            return True
    shared = {c for c in str(item.get("content") or "") if CJK.match(c)} & \
             {c for c in q if CJK.match(c)}
    return len(shared) >= 2


def is_quotable(item: Dict, query: str = "", mode: str = "active",
                min_conf: float = 0.5) -> bool:
    """这条记忆能不能出现在 prompt 里 —— 全项目只有这一处规则。

    active（鱼主动提起）：必须本人亲口/转述、置信度够、且有原文锚点。
    passive（用户这轮问到相关话题）：旧记录和鱼自己的印象放开，但仅在话题重合时。
    """
    src = str(item.get("source_type") or "said")
    try:
        conf = float(item.get("confidence") if item.get("confidence") is not None else 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    if mode == "active":
        # 阈值走配置 memory_min_confidence：以前这里写死 0.5，配置项在 schema 和 DEFAULTS
        # 里躺着却没有任何读取点，改它等于没改（面板上调了也不会生效）
        return (src in ("said", "reported") and conf >= float(min_conf or 0.0)
                and bool(str(item.get("anchor_text") or "").strip()))
    if src in ("said", "reported"):
        return True
    if src in ("said_legacy", "inferred"):
        return topic_overlap(item, query)
    return False


def restate(content: str, limit: int = 60) -> str:
    """把原话改述成第三人称一句：只做代词翻转，不改事实。

    让模型把「我养了两只猫」整句念出来，它就会复读原话；翻成「你」之后
    它得自己组织句子，来源和口气也就自然带出来了。
    """
    s = " ".join(str(content or "").split())
    if len(s) > limit:
        cut = max(s.rfind("，", 0, limit), s.rfind("；", 0, limit), s.rfind("。", 0, limit))
        s = s[:cut + 1] if cut >= 20 else s[:limit] + "…"
    s = s.replace("我们", "你们").replace("我的", "你的").replace("我", "你")
    return s


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
    is_deleted         INTEGER NOT NULL DEFAULT 0,
    -- 以下 8 列是「记忆补全」加的：没有出处的记忆不许主动提，
    -- 所以出处跟内容一样是必需字段，不能等要用的时候再猜
    addressee_id       TEXT NOT NULL DEFAULT '',
    addressee_nick     TEXT NOT NULL DEFAULT '',
    topic              TEXT NOT NULL DEFAULT '',
    anchor_mid         TEXT NOT NULL DEFAULT '',
    anchor_text        TEXT NOT NULL DEFAULT '',
    source_type        TEXT NOT NULL DEFAULT 'said',
    confidence         REAL NOT NULL DEFAULT 0.5,
    corrected_by       TEXT NOT NULL DEFAULT ''
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

-- 纠正记录：谁把哪条记忆改成了什么，永不自动删。
-- 记忆本身允许被改，但「改过」这件事得留底，否则同一件事会被来回翻烧饼
CREATE TABLE IF NOT EXISTS fish_memory_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   TEXT NOT NULL DEFAULT '',
    group_id    TEXT NOT NULL DEFAULT '',
    person_id   TEXT NOT NULL DEFAULT '',
    old_content TEXT NOT NULL DEFAULT '',
    new_content TEXT NOT NULL DEFAULT '',
    by_uid      TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL DEFAULT 0,
    anchor_mid  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_corr_memory ON fish_memory_corrections (memory_id);

-- 目的地记忆：鱼对谁说过什么。同一件事第三次要开口时先自查，
-- 别第二次第三次还是从头讲一遍
CREATE TABLE IF NOT EXISTS fish_said_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT NOT NULL DEFAULT '',
    to_id        TEXT NOT NULL DEFAULT '',
    to_nick      TEXT NOT NULL DEFAULT '',
    topic        TEXT NOT NULL DEFAULT '',
    gist         TEXT NOT NULL DEFAULT '',
    anchor_mid   TEXT NOT NULL DEFAULT '',
    anchor_text  TEXT NOT NULL DEFAULT '',
    ts           REAL NOT NULL DEFAULT 0,
    repeat_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_said_lookup ON fish_said_records (group_id, to_id, topic, ts);
"""


# 旧库补列清单：(列名, 定义)。只加不删，存量行走 DEFAULT
_MIGRATE_COLS = [
    ("addressee_id", "TEXT NOT NULL DEFAULT ''"),
    ("addressee_nick", "TEXT NOT NULL DEFAULT ''"),
    ("topic", "TEXT NOT NULL DEFAULT ''"),
    ("anchor_mid", "TEXT NOT NULL DEFAULT ''"),
    ("anchor_text", "TEXT NOT NULL DEFAULT ''"),
    ("source_type", "TEXT NOT NULL DEFAULT 'said'"),
    ("confidence", "REAL NOT NULL DEFAULT 0.5"),
    ("corrected_by", "TEXT NOT NULL DEFAULT ''"),
]


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
                # 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
                # 存量库（线上就是）必须逐列 ALTER，重复执行报 duplicate column 属于正常
                for col, decl in _MIGRATE_COLS:
                    try:
                        conn.execute("ALTER TABLE fish_memory_records ADD COLUMN %s %s" % (col, decl))
                    except sqlite3.OperationalError:
                        pass
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
        ts: Optional[float] = None,
        addressee_id: str = "",
        addressee_nick: str = "",
        topic: str = "",
        anchor_mid: str = "",
        anchor_text: str = "",
        source_type: str = "",
        confidence: Optional[float] = None,
    ) -> Dict:
        """写入单条记忆，精确指纹或语义近重（Jaccard > 0.75）则合并。

        「记忆补全」加的六个参数不是可选项：没有出处的记忆一律不许主动召回，
        所以默认值按「本人亲口说的」保守给，但锚点留空 → 只会被当成素材。
        """
        content = (content or "").strip()
        if not content:
            return {"merged": False, "skipped": True}

        title = (title or content[:20]).strip()
        now_ts = float(ts if ts is not None else time.time())
        imp = importance if importance is not None else TYPE_IMPORTANCE.get(memory_type, 0.5)
        src = source_type or classify_source(content)
        conf = confidence if confidence is not None else SOURCE_CONF.get(src, 0.5)
        topic = (topic or topics_of(content)).strip()
        anchor_text = (anchor_text or content)[:80]

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
                # 老行可能是迁移过来的、没有锚点：这次带了锚点就补上，
                # 才有资格从「只做素材」升成「可以主动提」
                conn.execute(
                    "UPDATE fish_memory_records SET activation_count=activation_count+1, "
                    "last_activated_at=?, updated_at=?, is_deleted=0, "
                    "anchor_mid=CASE WHEN anchor_mid='' THEN ? ELSE anchor_mid END, "
                    "anchor_text=CASE WHEN anchor_text='' THEN ? ELSE anchor_text END, "
                    "topic=CASE WHEN topic='' THEN ? ELSE topic END, "
                    "confidence=MAX(confidence, ?) WHERE memory_id=?",
                    (now_ts, now_ts, anchor_mid, anchor_text, topic, conf, row["memory_id"])
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
                    "memory_type, importance, created_at, updated_at, last_activated_at, activation_count, is_deleted, "
                    "addressee_id, addressee_nick, topic, anchor_mid, anchor_text, source_type, confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (fp, group_id, person_id, title, content, memory_type, imp, now_ts, now_ts, now_ts,
                     addressee_id, addressee_nick, topic, anchor_mid, anchor_text, src, conf)
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
        half_life_days: float = 14.0,
        mode: str = "active",
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
                "last_activated_at, activation_count, addressee_nick, topic, anchor_mid, "
                "anchor_text, source_type, confidence, person_id FROM fish_memory_records "
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
            # 出处闸：没锚点的存量、鱼自己的印象，主动聊时一律不出现。
            # 过滤放在打分之后、入列之前，保证「能不能提」只由这一个函数决定
            if not is_quotable(d, query, mode):
                continue
            results.append({
                "id": d["memory_id"],
                "content": d["content"],
                "type": d["memory_type"],
                "score": score,
                # 带上归属人：纠错要断言「这条到底是谁的」，不然会改到别人档案上
                "person_id": d.get("person_id") or "",
                # 谁在什么时候说的：拼进 prompt 时要用，不然模型会把旧事当现状
                "created_at": d.get("created_at"),
                "source_type": d.get("source_type") or "said",
                "confidence": d.get("confidence"),
                "topic": d.get("topic") or "",
                "anchor_mid": d.get("anchor_mid") or "",
                "anchor_text": d.get("anchor_text") or "",
                "addressee_nick": d.get("addressee_nick") or "",
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        top = results[:top_k]
        # 命中即算「真被用到」：刷新激活时间与计数。
        # 以前只有 add_memory 的 merge 分支会写这两列，衰减任务看到的
        # last_activated_at 永远是入库那天，于是天天被提起的记忆照样被当成
        # 「没人再印证」一路降到软删（重启几次就把记忆清空的那种事故）
        if top:
            try:
                with self._get_conn() as conn:
                    conn.executemany(
                        "UPDATE fish_memory_records SET last_activated_at=?, "
                        "activation_count=activation_count+1 WHERE memory_id=?",
                        [(now_ts, str(it["id"])) for it in top if it.get("id")],
                    )
                    conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.debug("[fish][mem] 刷新激活时间失败：%s", e)
        return top

    def record_turn(self, group_id: str, person_id: str, user_text: str, reply_text: str = "",
                    log: bool = True, anchor_mid: str = "", addressee_id: str = "",
                    addressee_nick: str = ""):
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
                    ts=now_ts,
                    anchor_mid=anchor_mid,
                    anchor_text=clean_user[:80],
                    addressee_id=addressee_id,
                    addressee_nick=addressee_nick,
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

    # ---------------- 目的地记忆：鱼对谁说过什么 ----------------
    def record_said(self, group_id: str, to_id: str, text: str, to_nick: str = "",
                    anchor_mid: str = "", ts: Optional[float] = None,
                    window_days: float = 7.0) -> Dict:
        """记下鱼刚才对谁说了什么。

        同一件事 7 天内不新增行、只把 repeat_count 加一：三次是阈值，
        一行一条会把「说过三次」摊成三行，查的时候还得再去数
        """
        body = " ".join(str(text or "").split())
        if not body or body.startswith("[只发图"):
            return {"skipped": True}
        gist = re.split(r"[。！？!?\n]", body)[0][:40]
        topic = topics_of(body)
        now_ts = float(ts if ts is not None else time.time())
        since = now_ts - window_days * 86400
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, repeat_count FROM fish_said_records "
                "WHERE group_id=? AND to_id=? AND topic=? AND ts>=? "
                "ORDER BY ts DESC LIMIT 1",
                (group_id, str(to_id), topic, since)
            ).fetchone()
            if row and topic:
                conn.execute(
                    "UPDATE fish_said_records SET repeat_count=repeat_count+1, ts=?, "
                    "gist=?, anchor_mid=?, anchor_text=?, to_nick=? WHERE id=?",
                    (now_ts, gist, anchor_mid, body[:120], to_nick, row["id"])
                )
                conn.commit()
                return {"id": row["id"], "repeat": int(row["repeat_count"]) + 1}
            cur = conn.execute(
                "INSERT INTO fish_said_records (group_id, to_id, to_nick, topic, gist, "
                "anchor_mid, anchor_text, ts, repeat_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (group_id, str(to_id), to_nick, topic, gist, anchor_mid, body[:120], now_ts)
            )
            conn.commit()
            return {"id": cur.lastrowid, "repeat": 1}

    def said_repeats(self, group_id: str, to_id: str, topic: str,
                     within_days: float = 7.0) -> Tuple[int, str]:
        """查「这件事跟这个人说过几次」，返回 (次数, 上次原话摘要)。"""
        if not str(to_id):
            return 0, ""
        since = time.time() - within_days * 86400
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT topic, gist, repeat_count FROM fish_said_records "
                "WHERE group_id=? AND to_id=? AND ts>=? ORDER BY ts DESC LIMIT 40",
                (group_id, str(to_id), since)
            ).fetchall()
        best, gist = 0, ""
        for r in rows:
            if not topic:
                continue
            rt = str(r["topic"] or "")
            if not rt:
                continue
            hit = any(w and w in rt for w in topic.split(",")) or \
                any(w and w in topic for w in rt.split(","))
            if hit:
                n = int(r["repeat_count"] or 1)
                if n > best:
                    best, gist = n, str(r["gist"] or "")
        return best, gist

    # ---------------- 纠正与降级 ----------------
    # 虚词：凑数用的，不能算「说话人在改的内容」
    _STOP_CHARS = set("的了着过是在有就也都我还你他她它们这那哪不没很太更最和与或啊吧呢吗呀哦嗯个次点些")
    _CORRECT_RE = re.compile(r"不是(这样|这个|那只|那个)?|我说的是|你记错|记错了|我没说过|"
                             r"我说过(吗|么)|别瞎记|不对[，,]?我")

    def detect_correction(self, text: str, recalled: List[Dict],
                          threshold: float = 0.55) -> Optional[Dict]:
        """用户这句是不是在纠正刚才被召回的那条记忆。

        只认「有纠正话术 + 与某条召回内容重合」的交叉证据：
        单凭一句「不是」就改记忆，等于谁都能删库
        """
        t = str(text or "")
        if not t or not recalled or not self._CORRECT_RE.search(t):
            return None
        best, best_sim = None, 0.0
        for item in recalled:
            sim = self.jaccard(t, str(item.get("content") or ""))
            if sim > best_sim:
                best, best_sim = item, sim
        if best is not None and best_sim >= threshold:
            return best
        # 句子短、改的内容（杭州→苏州、两只→三只）时 Jaccard 会被拉低，退一步看共享实词。
        # 但「共享 2 个字」必须扣掉虚词：以前 text「不是吧」和「他不是很喜欢上班」
        # 光靠「不」「是」两个字就能凑够 2，于是整句被当改口、0.9 写进档案
        for item in recalled:
            shared = {c for c in str(item.get("content") or "") if CJK.match(c)} & \
                     {c for c in t if CJK.match(c)}
            if len(shared - _STOP_CHARS) >= 2:
                return item
        return None

    def apply_correction(self, group_id: str, person_id: str, old: Dict,
                         new_text: str, by_uid: str = "", anchor_mid: str = "") -> Dict:
        """旧条作废 + 写纠正记录 + 按新原话重建一条。"""
        # 归属断言：old 必须是这个人的条目。串档（拿到的是别人那轮的召回）
        # 时改错档案比不改更糟——宁可这轮不纠错，也不能动别人的记忆
        _owner = str((old or {}).get("person_id") or "")
        if _owner and _owner != str(person_id):
            logger.warning("[fish][mem] 纠错归属不符（%s → %s），放弃", _owner[:20], str(person_id)[:20])
            return {}
        now_ts = time.time()
        old_id = str(old.get("id") or "")
        new_text = " ".join(str(new_text or "").split())
        with self._get_conn() as conn:
            if old_id:
                conn.execute(
                    "UPDATE fish_memory_records SET confidence=0.0, is_deleted=1, "
                    "updated_at=?, corrected_by=? WHERE memory_id=?",
                    (now_ts, "correction", old_id)
                )
            conn.execute(
                "INSERT INTO fish_memory_corrections (memory_id, group_id, person_id, "
                "old_content, new_content, by_uid, ts, anchor_mid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (old_id, group_id, str(person_id), str(old.get("content") or "")[:140],
                 new_text[:140], str(by_uid), now_ts, anchor_mid)
            )
            conn.commit()
        rec = self.add_memory(group_id=group_id, person_id=person_id, content=new_text,
                              memory_type="fact", importance=0.85, ts=now_ts,
                              anchor_mid=anchor_mid, anchor_text=new_text[:80],
                              source_type="said", confidence=0.9)
        return {"old": old_id, "new": rec.get("id", ""), "ts": now_ts}

    def decay_low_confidence(self, days: float = 30.0, factor: float = 0.85) -> int:
        """低置信度且长期没人印证的记忆，慢慢降权；降到 0.2 转软删。

        不直接删：这条可能是真的，只是没人再提过；留行才能被人工捞回来
        """
        now_ts = time.time()
        cut = now_ts - days * 86400
        with self._get_conn() as conn:
            n = conn.execute(
                # 判据用 updated_at 而不是 last_activated_at：衰减本身会写 updated_at，
                # 于是「同一条一天只降一次」是天然的；用 last_activated_at 判等于
                # 每次进程启动都给同一批再打一次折（一天重启五次就降五档 → 全被清空）
                "UPDATE fish_memory_records SET confidence=MAX(0.0, confidence*?), updated_at=? "
                "WHERE is_deleted=0 AND confidence<0.5 AND updated_at<?",
                (factor, now_ts, cut)
            ).rowcount
            conn.execute(
                "UPDATE fish_memory_records SET is_deleted=1 WHERE is_deleted=0 AND confidence<0.2"
            )
            conn.commit()
        return n

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
            # 目的地记忆只留两个月：它是「刚才是不是重复了」用的，不是档案
            n4 = conn.execute("DELETE FROM fish_said_records WHERE ts < ?",
                              (now_ts - 60 * 86400,)).rowcount
            conn.commit()
        # 纠正记录（fish_memory_corrections）有意不清理：它是记忆被改过的唯一底稿
        return {"turn_logs": n1, "tags": n2, "memories": n3, "said": n4}


# 单例实例
fish_memory_engine = FishMemoryEngine()


def shape_recalled(recalled, *, nick: str, text: str, fmt_when, limit: int = 2,
                   mode: str = "active", min_conf: float = 0.5) -> list[str]:
    """把召回结果整成 prompt 里的记忆行。

    记忆去重只在这一处：召回、拼装两边各写一份，规则迟早分叉
    （一边按子串、一边按二元组，同一句话就能在 prompt 里出现两遍）。
    """
    lines: list[str] = []
    for item in recalled or []:
        content = " ".join(str(item.get("content") or "").split())
        # 刚说的那句也可能以近重形式被召回，别让模型看到自己刚说的话
        if not content or content in text or text in content:
            continue
        # 记忆是按「一条 QQ 消息」存的，人常一句话拆成七八条发；
        # 碎片原样塞进 prompt 会拼成一坨读不通的长句。
        # 但不能只按长度砍：「我到了」「明天见」「吃了吗」都是完整信息，
        # 砍掉等于把有效事实永久丢掉。真正的碎片是又短又没收句的（「我跟你，」）
        # 3 字以下（「哈哈」「嗯嗯」）才是真碎片；「我到了」「明天见」这类
        # 短句本身就是完整信息，按长度一刀切等于永久丢掉有效事实
        if len(content) < 3 or (len(content) < 12 and content[-1] in "，、,（("):
            continue
        if len(content) > 60:
            cut = max(content.rfind("，", 0, 60), content.rfind("；", 0, 60),
                      content.rfind("。", 0, 60))
            content = content[:cut + 1] if cut >= 20 else content[:60] + "…"
        # 同一段话被拆成好几条发时，库里会同时存「前缀碎片」和「长句」，
        # 两条都塞进去就是叠字：留长的，短的让位
        # 只认「同一条话的前缀/后缀」，不认纯子串：原来的 content in m 会让
        # 「今天」被「我今天去了杭州」判成重复而整条丢掉，短事实就这样静默消失
        def _same_line(a: str, b: str) -> bool:
            if a == b:
                return True
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            if len(long_) < len(short) * 2:
                return False          # 长度差太小，是两句话而不是碎片与全句
            return long_.startswith(short) or long_.endswith(short)

        dup = [m for m in lines if _same_line(content, m)]
        if dup:
            for i, m in enumerate(lines):
                if len(m) < len(content) and _same_line(m, content):
                    lines[i] = content
            continue
        # 出处第二次把关：召回层已经滤过一遍，这里再滤一次是因为
        # 召回结果会被别的调用方拼装（面板、检索命令），单一规则必须都过一遍
        if not is_quotable(item, text, mode, min_conf=min_conf):
            continue
        _who = nick or "他"
        _when = fmt_when(item.get("created_at") or item.get("ts"))
        src = str(item.get("source_type") or "said")
        try:
            conf = float(item.get("confidence") if item.get("confidence") is not None else 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        said = restate(content)
        # 置信度决定敢不敢断言：0.8 以上直接说，0.5~0.8 加「好像」，
        # 0.5 以下必须先声明自己不确定——比说错再道歉便宜得多
        if conf >= 0.8:
            head = f"{_who}{_when}说的（{SOURCE_WORD.get(src, '他说的')}）：" if _when \
                else f"{_who}说的（{SOURCE_WORD.get(src, '他说的')}）："
        elif conf >= 0.5:
            head = f"{_who}好像{_when}说过（{SOURCE_WORD.get(src, '他说的')}）："
        else:
            head = f"我记得好像{_who}{_when}提过（{SOURCE_WORD.get(src, '他说的')}，不确定）："
        hint = ACTION_HINT.get(str(item.get("type") or ""), "")
        lines.append(head + said + (f" —— {hint}" if hint else ""))
        if len(lines) >= limit:    # 给太多反而更乱，两条够了
            break
    return lines
