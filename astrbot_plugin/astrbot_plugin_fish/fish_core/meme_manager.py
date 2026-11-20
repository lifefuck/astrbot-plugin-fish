#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热梗 / 热词轮换管理器（fish_core 版）

移植自旧仓库 meme_manager.py，改动点：
1. 数据目录可配置：默认「模块同级 ../data/」，即插件的 data/ 目录。
   不再用 SCRIPT_DIR/data 拼死，也绝不碰旧仓库路径。
2. 池子上限 + 冷却：轮换池条数有硬上限（prompt 吃不下更多），
   刚被选中的词要隔若干轮才允许再进池，避免同一批词连着几轮刷屏。
3. 新增 push()：运行时（面板改词库 / 群友带梗）增量补词，带去重、上限与兜底过滤。
4. 只依赖标准库，不 import astrbot，可以用 importlib 按文件路径加载。

用法（main.py）：
    meme_manager = _load("meme_manager", "meme_manager.py")
    meme_manager.meme_manager.attach(data_dir=str(DATA_DIR))
    meme_manager.meme_manager.load_pool()

作者: life (lifefuck)
"""

import collections
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("fish.meme_manager")

# 默认落在插件 data/：模块在 <plugin>/fish_core/ 下，上一级就是 <plugin>
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_DATA_DIR = os.path.join(_PLUGIN_ROOT, "data")

# 词库文件名：同步脚本默认写 hot_words.json；memes_pool.json 是旧仓库的名字，
# 保留为只读兼容，方便直接从旧部署拷文件过来而不必改名
MEMES_FILENAME = "hot_words.json"
LEGACY_MEMES_FILENAME = "memes_pool.json"
CORE_FILENAME = "memes_core.json"

# 轮换池硬上限：再大的话注入文本本身就把注意力吃光了，等于没注入
POOL_HARD_CAP = 200
# 全库上限：防止 push 无限堆积（旧库 1900 条左右，留足余量）
TOTAL_HARD_CAP = 5000

# 模块级路径（旧代码习惯直接读 meme_manager.MEMES_FILE，这里同步维护）
DATA_DIR = _PLUGIN_DATA_DIR
MEMES_FILE = os.path.join(DATA_DIR, MEMES_FILENAME)
CORE_FILE = os.path.join(DATA_DIR, CORE_FILENAME)
# 本群黑话：由 scripts/mine_group_slang.py 从真实群聊里挖出来，按群分开
SLANG_FILENAME = "group_slang.json"
SLANG_MIN_N = 4        # 群黑话至少出现这么多次才算词，低于这个多是分词碎片
# 这些 source 给的是新闻标题，不是梗
_HOT_NEWS_SOURCES = ("news", "bilibili-hot-search", "tieba-hot", "weibo-hot")

# 兜底过滤：这是最后一道闸。正式过滤在 scripts/sync_hot_words.py，
# 但 push() 的词可能来自实时消息，不能假设上游已经清洗过
_BLOCKED = (
    "政治", "国家领导", "领导人", "反华", "涉政", "台独", "港独", "自杀", "献忠",
    "暴恐", "毒品", "枪支", "遇难", "身亡", "刑拘", "性侵", "猥亵", "拐卖",
)

# 圈子关键词：原来一长串 any(...)，抽出来便于按需调整
_CATEGORY_HINTS: List[Tuple[str, Tuple[str, ...]]] = [
    ("game", ("瓦", "三角洲", "原神", "王者", "lol", "英雄联盟", "cs", "游戏",
              "开黑", "死斗", "老六", "堵桥", "巡飞弹", "奶妈", "红温")),
    ("acg", ("b站", "二创", "番剧", "动漫", "初音", "cos", "阿宅", "二次元")),
    ("abstract", ("贴吧", "抽象", "鼠鼠", "牢", "急了", "典", "乐", "蚌埠", "发疯")),
]

# 圈子配比（按 pool_size 比例切，默认 30 条 → 12/6/6/6）
_CATEGORY_RATIO = (("game", 0.40), ("acg", 0.20), ("abstract", 0.20), ("general", 0.20))


def _resolve_data_dir(data_dir: Optional[str]) -> str:
    if not data_dir:
        return _PLUGIN_DATA_DIR
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(data_dir))))


def _safe_meme(name: str, desc: str = "") -> bool:
    """push 时的兜底校验：太短/太长/纯符号/带风控词的一律不进池"""
    text = f"{name} {desc}"
    if any(k in text for k in _BLOCKED):
        return False
    if not (2 <= len(name) <= 20):
        return False
    if re.fullmatch(r"[\d\W_]+", name):
        return False
    return True


class MemeManager:
    """热词库 + 动态轮换池。

    线程安全说明：只被插件主循环调用，没加锁；push 与 rotate 都在同一事件循环里跑。
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        pool_size: int = 30,
        rotation_interval: int = 1800,
        cooldown_rounds: int = 3,
        auto_load: bool = True,
    ):
        self.pool_size = max(1, min(int(pool_size or 30), POOL_HARD_CAP))
        self.rotation_interval = int(rotation_interval or 1800)  # 秒
        self.cooldown_rounds = max(0, int(cooldown_rounds))
        self.last_rotation = 0.0
        self.last_file_mtime = 0.0

        self.core_memes: List[Dict[str, Any]] = []        # 常驻核心梗：每次都注入，不轮换
        self._snippet_calls = 0                           # 轮换词按需注入的计数器
        self.all_memes: List[Dict[str, Any]] = []
        self.structured_memes: List[Dict[str, Any]] = []  # 带解释的高质量梗
        self.word_only_memes: List[str] = []              # 纯热词
        self.current_pool: List[Dict[str, Any]] = []      # 当前轮换池
        self.group_slang: Dict[str, List[Dict[str, Any]]] = {}   # 每群自己的黑话
        self._slang_mtime = 0.0

        # 分类索引池（用于按圈子配比抽词）
        self.category_pools: Dict[str, List[Dict[str, Any]]] = {
            "game": [], "acg": [], "abstract": [], "general": [],
        }

        # 冷却：记录每个词最后一次进池是第几轮。deque 只用来给日志/调试看最近几轮
        self._round = 0
        self._last_round: Dict[str, int] = {}
        self._recent_rounds: collections.deque = collections.deque(maxlen=10)

        self.data_dir = _resolve_data_dir(data_dir)
        self._refresh_paths()
        if auto_load:
            self.load_pool()

    # ---------------- 路径 / 初始化 ----------------
    def _refresh_paths(self) -> None:
        global DATA_DIR, MEMES_FILE, CORE_FILE
        self.memes_file = os.path.join(self.data_dir, MEMES_FILENAME)
        self.core_file = os.path.join(self.data_dir, CORE_FILENAME)
        self.slang_file = os.path.join(self.data_dir, SLANG_FILENAME)
        self._memes_candidates = [
            os.path.join(self.data_dir, MEMES_FILENAME),
            os.path.join(self.data_dir, LEGACY_MEMES_FILENAME),
        ]
        DATA_DIR, MEMES_FILE, CORE_FILE = self.data_dir, self.memes_file, self.core_file

    def attach(
        self,
        data_dir: Optional[str] = None,
        pool_size: Optional[int] = None,
        rotation_interval: Optional[int] = None,
        cooldown_rounds: Optional[int] = None,
        load: bool = False,
    ) -> "MemeManager":
        """由调用方（main.py）在拿到插件 data/ 绝对路径后调用。

        只 attach 不 load：路径换掉以后旧池子已经没意义了，由调用方决定何时
        调 load_pool()，避免插件 import 阶段就去读不存在的文件刷一堆 warning。
        """
        if data_dir:
            self.data_dir = _resolve_data_dir(data_dir)
        if pool_size is not None:
            self.pool_size = max(1, min(int(pool_size), POOL_HARD_CAP))
        if rotation_interval is not None:
            self.rotation_interval = int(rotation_interval)
        if cooldown_rounds is not None:
            self.cooldown_rounds = max(0, int(cooldown_rounds))
        self.last_file_mtime = 0.0
        self._refresh_paths()
        if load:
            self.load_pool(force=True)
        return self

    def _existing_memes_file(self) -> Optional[str]:
        for p in self._memes_candidates:
            if os.path.exists(p):
                return p
        return None

    # ---------------- 载入 ----------------
    def load_group_slang(self, force: bool = False) -> None:
        """读本群黑话表。文件不存在就当没有——这层是加分项，缺了不能影响主流程。"""
        path = getattr(self, "slang_file", "")
        if not path or not os.path.exists(path):
            self.group_slang = {}
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if not force and mtime == getattr(self, "_slang_mtime", 0.0):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            groups = payload.get("groups") if isinstance(payload, dict) else payload
            clean = {}
            for gid, items in (groups or {}).items():
                words = []
                for it in items or []:
                    if isinstance(it, dict) and str(it.get("w") or "").strip():
                        words.append({"w": str(it["w"]).strip(),
                                      "n": int(it.get("n") or 0),
                                      "u": int(it.get("u") or 0)})
                if words:
                    clean[str(gid)] = words[:60]
            self.group_slang = clean
            self._slang_mtime = mtime
            logger.info("已载入本群黑话：%d 个群、%d 个词",
                        len(clean), sum(len(v) for v in clean.values()))
        except Exception as e:
            logger.warning("本群黑话加载失败: %s", e)
            self.group_slang = {}

    def group_slang_words(self, group_id) -> List[Dict[str, Any]]:
        if not getattr(self, "group_slang", None):
            self.load_group_slang()
        words = list(self.group_slang.get(str(group_id or ""), []))
        # 挖词是按字面切的，切出来一半是「问个」「穿个」「本群」这种语法碎片。
        # 它们照样被写进【你们群常挂嘴边的词】，白占预算还教模型说怪话。
        # 真梗至少被人反复说过：留出现 4 次以上、且不止一个人说的
        return [w for w in words
                if int(w.get("n") or 0) >= SLANG_MIN_N and int(w.get("u") or 0) >= 2]

    def load_pool(self, force: bool = False) -> None:
        """从 JSON 加载词库并初始化轮换池（mtime 没变就跳过）"""
        path = self._existing_memes_file()
        if not path:
            logger.warning("词库文件不存在: %s（等 scripts/sync_hot_words.py 生成）", self.memes_file)
            return
        self.memes_file = path

        try:
            mtime = os.path.getmtime(path)
            if not force and mtime == self.last_file_mtime and self.all_memes:
                return

            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw = payload.get("memes") if isinstance(payload, dict) else payload
            raw = raw if isinstance(raw, list) else []

            # 没有 name 的词条直接丢弃：旧版会在建索引时 KeyError，而 mtime
            # 已经写进去了，下次 load 直接 return，整库永久半死
            self.all_memes = [
                m for m in raw
                if isinstance(m, dict) and str(m.get("name") or "").strip()
                # news 是热搜里的新闻标题，不是梗，进池子只会挤掉真梗。
                # bilibili-hot-search / 贴吧 那批同理：67 条词库里 44 条是
                # 「特朗普抵达深圳」这种标题，模型被喂这个只会更接不上话
                and str(m.get("source") or "") not in _HOT_NEWS_SOURCES
                and str(m.get("type") or "") != "社区热搜"
            ][:TOTAL_HARD_CAP]
            self.last_file_mtime = mtime

            # 核心梗单独一个文件：同步脚本会整份重写词库，手写内容放里面会被冲掉
            try:
                with open(self.core_file, "r", encoding="utf-8") as cf:
                    core = json.load(cf)
                core = core.get("core", []) if isinstance(core, dict) else (core or [])
                self.core_memes = [
                    m for m in core if isinstance(m, dict) and str(m.get("name") or "").strip()
                ]
            except FileNotFoundError:
                self.core_memes = []
            except Exception as ce:
                logger.warning("核心梗加载失败: %s", ce)
                self.core_memes = []

            self._rebuild_index()
            # 词库整体换了，冷却表一起清掉，否则新词会被旧轮次卡住
            self._last_round.clear()
            self.rotate_pool(force=True)
            logger.info(
                "已载入热梗词库 %s：总词数 %d（带释义 %d / 纯热词 %d），核心梗 %d，当前轮换池 %d",
                os.path.basename(path), len(self.all_memes), len(self.structured_memes),
                len(self.word_only_memes), len(self.core_memes), len(self.current_pool),
            )
        except Exception as e:
            logger.error("加载词库文件异常: %s", e)

    def _classify(self, m: Dict[str, Any]) -> str:
        cat = (m.get("category") or "").strip()
        if cat in self.category_pools:
            return cat
        name = str(m.get("name") or "").lower()
        desc = str(m.get("desc") or "")
        for c, hints in _CATEGORY_HINTS:
            if any(h in name or h in desc for h in hints):
                return c
        return "general"

    def _rebuild_index(self) -> None:
        """从 all_memes 重建：带释义/纯热词分流 + 别名分类（同步脚本会改文件，必须可重入）"""
        for k in self.category_pools:
            self.category_pools[k] = []
        self.structured_memes = []
        self.word_only_memes = []
        for m in self.all_memes:
            desc = str(m.get("desc") or "").strip()
            if desc:
                m["desc"] = desc
                m["category"] = cat = self._classify(m)
                self.structured_memes.append(m)
                self.category_pools[cat].append(m)
            else:
                self.word_only_memes.append(str(m["name"]))

    # ---------------- 轮换 ----------------
    def _eligible(self, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """冷却期内的词不进候选，避免相邻几轮重复"""
        floor = self._round - self.cooldown_rounds
        return [m for m in items if self._last_round.get(m["name"], -10 ** 9) <= floor]

    @staticmethod
    def _sample(items: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        if n <= 0 or not items:
            return []
        return random.sample(items, min(n, len(items)))

    def rotate_pool(self, force: bool = False) -> List[Dict[str, Any]]:
        """按圈子配比洗牌出一批词；冷却期内的词优先排除，凑不够才降级回全量"""
        now = time.time()
        if not force and (now - self.last_rotation) < self.rotation_interval and self.current_pool:
            return self.current_pool

        target_total = min(self.pool_size, POOL_HARD_CAP)
        # 按比例切，最后一项吃掉余数，保证凑满 target_total
        counts: List[Tuple[str, int]] = []
        left = target_total
        for cat, ratio in _CATEGORY_RATIO[:-1]:
            n = min(left, int(round(target_total * ratio)))
            counts.append((cat, n))
            left -= n
        counts.append((_CATEGORY_RATIO[-1][0], max(0, left)))

        selected: List[Dict[str, Any]] = []
        seen: set = set()
        for cat, count in counts:
            pool = [m for m in self.category_pools.get(cat, []) if m["name"] not in seen]
            picked = self._sample(self._eligible(pool), count)
            if len(picked) < count:
                # 冷库不够就放宽冷却，但绝不重复已选中的
                rest = [m for m in pool if m not in picked]
                picked.extend(self._sample(rest, count - len(picked)))
            for item in picked:
                seen.add(item["name"])
                selected.append(item)

        # 不足用其余带释义的词补齐，再不够拿纯热词填（旧版这里会永远短一截）
        if len(selected) < target_total:
            need = target_total - len(selected)
            rest = [m for m in self.structured_memes if m["name"] not in seen]
            for item in (self._sample(self._eligible(rest), need) or self._sample(rest, need)):
                seen.add(item["name"])
                selected.append(item)
        if len(selected) < target_total and self.word_only_memes:
            need = target_total - len(selected)
            words = [w for w in self.word_only_memes if w not in seen]
            for w in random.sample(words, min(need, len(words))):
                seen.add(w)
                selected.append({"name": w, "desc": ""})

        combined = []
        for item in selected[:target_total]:
            desc = str(item.get("desc") or "").strip()
            if len(desc) > 20:  # 压缩解释，整条不超过 20 字
                desc = desc[:17] + "..."
            combined.append({"name": item["name"], "desc": desc,
                             "category": item.get("category", "general")})

        self.current_pool = combined
        self.last_rotation = now
        for item in combined:
            self._last_round[item["name"]] = self._round
        self._recent_rounds.append(self._round)
        self._round += 1
        logger.debug("轮换池第 %d 轮已刷新：%d 条", self._round, len(self.current_pool))
        return self.current_pool

    # ---------------- 匹配 ----------------
    def match_user_message(self, text: str, limit: int = 2, group_id=None) -> List[Dict[str, Any]]:
        """认出这条消息在玩什么梗，按相关度排序返回。

        排序而不是「谁名字长谁先」：正名命中 > 别名命中 > 容错召回，
        同层里名字越长越具体。默认只给两条——梗是调味，三条以上就开始稀释注意力。
        """
        clean = re.sub(r"\[[^\]]*\]", " ", text or "").lower()
        scored: List[Tuple[float, Dict[str, Any]]] = []
        seen: set = set()

        def take(item: Dict[str, Any], score: float) -> None:
            name = str(item.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                scored.append((score, item))

        # 1. 名字/别名子串；核心梗略微加权，因为它是手写、带接法示范的
        core_bonus = {str(m.get("name")): 1.5 for m in self.core_memes if m.get("name")}
        for item in self.core_memes + self.structured_memes:
            name = str(item.get("name") or "").lower()
            if len(name) >= 2 and name in clean:
                take(item, 3.0 + len(name) * 0.3 + core_bonus.get(item.get("name"), 0))
                continue
            for alias in item.get("aliases") or []:
                a = str(alias).lower()
                if len(a) >= 2 and a in clean:
                    take(item, 2.0 + len(a) * 0.2 + core_bonus.get(item.get("name"), 0))
                    break

        # 2. 本群黑话：只在库里的梗一个都没命中时才补位。
        #    它没有释义，接法只能靠模型自己从上下文里猜，所以优先级压低，
        #    也绝不让它挤掉真正的梗库条目
        if not scored and group_id:
            for it in self.group_slang_words(group_id):
                w = str(it.get("w") or "")
                if len(w) >= 2 and w in clean:
                    take({"name": w, "desc": "你们群里自己的说法", "source": "group_slang"},
                         1.2 + len(w) * 0.2)
                    if len(scored) >= limit:
                        break

        # 3. 一个都没认出来才容错：只对核心梗，重合度 0.75 起，
        #    否则「牛头人」会被认成「牢明」这种纯字形事故
        if not scored:
            msg_chars = set(clean)
            for item in self.core_memes:
                name = str(item.get("name") or "")
                if len(name) < 3:
                    continue
                chars = set(name)
                shared = len(chars & msg_chars)
                if shared >= 2 and shared / len(chars) >= 0.75:
                    take(item, 1.0 + shared / len(chars))

        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:max(0, int(limit))]]

    # ---------------- 增量补词 ----------------
    def push(self, items, source: str = "") -> int:
        """运行时往库里补词，返回真正新增的条数。

        - 按 name 去重（同名只更新 desc，不重复占位）
        - 违反兜底过滤 / 结构不对的直接跳过
        - 新词立刻进当前轮换池头部（不然要等下一轮，热梗当天就凉了）
        - 全库超过 TOTAL_HARD_CAP 时裁掉尾部非核心旧词
        """
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, (list, tuple)):
            return 0

        added = 0
        core_names = {str(m.get("name")) for m in self.core_memes if m.get("name")}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            desc = str(raw.get("desc") or "").strip()
            if not _safe_meme(name, desc):
                continue
            # 核心梗每轮都整段注入，再进轮换池就是同一句话占两份预算
            if name in core_names:
                continue
            exist = next((m for m in self.all_memes if m["name"] == name), None)
            if exist is not None:
                if desc and not exist.get("desc"):
                    exist["desc"] = desc
                    self._rebuild_index()
                continue
            if len(self.all_memes) >= TOTAL_HARD_CAP:
                logger.warning("词库已达上限 %d，忽略 push: %s", TOTAL_HARD_CAP, name)
                break
            entry = {"name": name, "desc": desc, "source": source or raw.get("source") or "push"}
            for key in ("aliases", "type", "category"):
                if raw.get(key):
                    entry[key] = raw[key]
            self.all_memes.append(entry)
            added += 1

        if added:
            self._rebuild_index()
            # 新词优先注入本轮，排在前面的位置天然先被看到
            fresh = [{"name": m["name"], "desc": (m.get("desc") or "")[:20],
                      "category": m.get("category", "general")}
                     for m in self.all_memes[-added:]]
            merged, seen = [], set()
            for it in fresh + self.current_pool:
                if it["name"] not in seen:
                    seen.add(it["name"])
                    merged.append(it)
            self.current_pool = merged[:self.pool_size]
            for it in self.current_pool:
                self._last_round.setdefault(it["name"], self._round)
            logger.info("push 补入热词 %d 条（来源 %s）", added, source or "unknown")
        return added

    # ---------------- Prompt ----------------
    @staticmethod
    def _short_desc(item: Dict[str, Any], limit: int = 26) -> str:
        desc = str(item.get("desc") or "").strip()
        if len(desc) <= limit:
            return desc
        cuts = [i for i in (desc.find("。"), desc.find("；"), desc.find("，")) if 8 <= i <= limit]
        return desc[:min(cuts)] if cuts else desc[:limit]

    def get_prompt_snippet(self, user_message: str = "", group_id=None) -> str:
        """三层注入：常驻核心梗 + 这句沾边的梗 + 一小撮流行词。

        旧版是「命中的梗 + 100 条随机词表」，1910 条库随机抽 100 条命中率极低，
        还占掉一大截注意力。现在常驻的少而准，剩下的靠检索，检索不到就别硬接。
        """
        self.load_pool()
        self.rotate_pool()
        if not self.core_memes and not self.current_pool and not self.structured_memes:
            return ""

        lines: List[str] = []
        rendered_core: set = set()
        if self.core_memes:
            parts, budget = [], 520  # 旧版硬切 [:14]，第 15 条起既不展示也匹配不到
            for m in self.core_memes:
                name = str(m.get("name") or "")
                if not name:
                    continue
                cue = m.get("cue") or self._short_desc(m)
                piece = f"{name}→{cue}" if cue else name
                if parts and len("｜".join(parts)) + len(piece) > budget:
                    break
                parts.append(piece)
                rendered_core.add(name)
            if parts:
                lines.append("【群里常聊的梗（别背解释，贴切才接，最多用一个）】：" + "｜".join(parts))

        matched = (self.match_user_message(user_message, group_id=group_id)
                   if (user_message or "").strip() else [])
        hits = [m for m in matched if str(m.get("name") or "") not in rendered_core][:3]
        if hits:
            lines.append("【这句沾边的梗】：" + "；".join(
                f"{m.get('name', '')}（{self._short_desc(m)}）" for m in hits))

        # 本群专属黑话：命中就点出来，没命中就给一小撮让模型知道这圈子在说什么。
        # 这是「接梗」最有效的一层：热搜榜永远是别人的梗，这里的才是他们自己的
        slang = self.group_slang_words(group_id) if group_id else []
        if slang:
            flat = re.sub(r"\[[^\]]*\]", " ", user_message or "")
            used = [it["w"] for it in slang if len(it["w"]) >= 2 and it["w"] in flat][:3]
            if used:
                lines.append("【他这句话里的「" + "」「".join(used) +
                             "」是你们群自己的说法，听懂就顺着用，别解释】")
            else:
                words = [it["w"] for it in slang[:12] if len(it["w"]) >= 2]
                if words:
                    lines.append("【你们群常挂嘴边的词（用得上才用，不懂别硬接）】：" + "、".join(words))

        self._snippet_calls += 1
        if self.current_pool and (hits or self._snippet_calls % 5 == 1):
            words = [
                str(i.get("name") or "") for i in self.current_pool
                if str(i.get("name") or "") not in rendered_core
                # 带斜杠/括号的词（CPU/KTV、牛头人(NTR)）在自然句里接不上，只放纯词
                and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{2,8}", str(i.get("name") or ""))
            ][:30]
            if words:
                lines.append("【最近常刷到的词】：" + "、".join(words))
        return "\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        """给面板/自检用的一眼概览"""
        return {
            "data_dir": self.data_dir,
            "memes_file": self.memes_file,
            "total": len(self.all_memes),
            "structured": len(self.structured_memes),
            "word_only": len(self.word_only_memes),
            "core": len(self.core_memes),
            "pool_size": len(self.current_pool),
            "pool_cap": self.pool_size,
            "round": self._round,
            "categories": {k: len(v) for k, v in self.category_pools.items()},
        }


# 模块级单例：旧代码是 `from meme_manager import meme_manager`，保持同名可用。
# 这里 auto_load=False —— import 阶段不碰磁盘，等 main.py attach() 后再 load_pool()
meme_manager = MemeManager(auto_load=False)


if __name__ == "__main__":
    # 手动跑一下看看当前 data/ 里有什么，不做任何写入
    import pprint

    mgr = MemeManager(auto_load=False)
    mgr.load_pool()
    pprint.pprint(mgr.stats())
    demo = "今天星期四，感觉好想吃炸鸡，天天加班整个人班味好重啊"
    print("--- 命中 ---")
    print(mgr.get_prompt_snippet(demo))
