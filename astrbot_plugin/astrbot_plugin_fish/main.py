# -*- coding: utf-8 -*-
"""小鲸鱼 · AstrBot 群聊拟真插件

把自研 QQ 机器人 qq-deepseek-bot 的对话能力整体搬到 AstrBot 上：
人设、心境、长期记忆、群表达学习、反模板、手滑错字、表情包、接话时机。

和原项目最大的结构差异：原项目是「一条消息进来 → 自己决定要不要回 → 自己调模型」，
AstrBot 的默认 LLM 只在被 @ 或唤醒前缀时才会触发。所以这里让 handler 的 filter
永远通过（拿到群里每一条消息），然后：
  - 该回的时候自己 yield event.request_llm(...)；
  - 不该回的时候 event.stop_event()。
ab! 前缀的消息不碰，原样留给 AstrBot 自己的 LLM 链路（见 README「为什么不 stop_event」）。
"""

from __future__ import annotations

import asyncio
import importlib.util

import aiohttp
import json
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, AtAll, Image, Plain, Poke, Reply

try:  # 空工具集 = 这次请求不给模型任何工具
    from astrbot.core.agent.tool import ToolSet

    NO_TOOLS = ToolSet()
except Exception:  # 版本差异时降级：至少不会因此起不来
    NO_TOOLS = None

# 只在消息里真有链接/视频号时才去解析；不用 findall，命不中就算了
_URL_RE = re.compile(r"https?://|www\.|b23\.tv|bili2233\.cn|\bBV[a-zA-Z0-9]{10}\b|\bav\d{3,}")

# 触发联网的实时性关键词。只在「被叫到」时才看，一次检索 0.3 秒，成本可接受
_REALTIME_RE = re.compile(
    r"联网|网上|搜索|查一下|查查|查下|帮我查|搜一下|搜搜|搜个|百度|谷歌|必应|bing|google|"
    r"最新|新闻|比分|夺冠|结果|进展|开播|发布会|上市|多少钱|价格|天气|几点|什么时候|"
    r"真的假的|是不是真的|有没有发生")
# 技术讨论里也常出现「搜索」两个字（搜索算法/正则/接口），命中这些词就别外联
_NO_SEARCH_RE = re.compile(r"算法|复杂度|正则|代码|排序|二叉树|sql|prompt|注入|接口|函数|编译")


def _search_query(text: str) -> str:
    """把「帮我联网搜索一下BLG夺冠了吗」洗成「BLG夺冠」。

    只去口语外壳，不动实体词——检索质量全看实体有没有留下。
    """
    q = re.sub(r"\[[^\]]*\]|@\S+", " ", text or "")
    q = re.sub(r"帮我|麻烦|请|联网|网上|搜索一下|搜索|查一下|查查|查下|搜一下|搜搜|搜个|"
               r"百度|谷歌|必应|bing|google", " ", q, flags=re.I)
    q = re.sub(r"^(那|就|你|你们|能不能|可以|能不能帮我)+", " ", q)
    # 「现在/今天/刚刚」这类时间副词对检索没用，反而把实体词冲散
    q = re.sub(r"(现在|今天|刚刚|目前|这会儿)", " ", q)
    # 口语问法换成检索词：搜「多少钱」出来的全是比价站和百科，搜「价格」才准
    q = re.sub(r"多少钱|什么价|卖多少|啥价格|贵不贵", " 价格 ", q)
    # 语气词可能连着一串（「了吗」「呢啊」），一次只能削一个，得循环
    q = re.sub(r"(?:吗|么|呢|吧|啊|呀|了|没有)+$", "", q.strip())
    q = re.sub(r"\s+", " ", q).strip(" ，,。？！?！")
    return q.strip()

PLUGIN_DIR = Path(__file__).resolve().parent
FISH_CORE = PLUGIN_DIR / "fish_core"
# 同目录模块之间要能互相 import（media_resolver / connectors 这类成对的模块），
# 模块本身是按文件路径加载的、没有包结构，只能靠把目录塞进 sys.path 让扁平 import 生效
if str(FISH_CORE) not in sys.path:
    sys.path.insert(0, str(FISH_CORE))
DATA_DIR = PLUGIN_DIR / "data"

# 复用原项目的纯逻辑模块：用 path 直接加载，避免和别的插件同名串味
def _load(alias: str, filename: str):
    name = f"astrbot_plugin_fish_{alias}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FISH_CORE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


affect = _load("affect", "affect.py")
favor = _load("favor", "favor.py")
poke_context = _load("poke_context", "poke_context.py")
anti_template = _load("anti_template", "anti_template.py")
interject_planner = _load("interject_planner", "interject_planner.py")
style_learner = _load("style_learner", "style_learner.py")
meme_catalog = _load("meme_catalog", "meme_catalog.py")
meme_gate = _load("meme_gate", "meme_gate.py")
prompts = _load("prompts", "prompts.py")
composer = _load("composer", "composer.py")
interject_score = _load("interject_score", "interject_score.py")
fish_memory = _load("fish_memory", "fish_memory.py")
splitter = _load("splitter", "splitter.py")
vision = _load("vision", "vision.py")
social_read = _load("social_read", "social_read.py")
peer_bots = _load("peer_bots", "peer_bots.py")
cross_memory = _load("cross_memory", "cross_memory.py")
panel_api = _load("panel", "panel.py")
admin_actions = _load("admin_actions", "admin_actions.py")
yunzai_link = _load("yunzai", "yunzai.py")
backup = _load("backup", "backup.py")
# 从旧仓库搬过来的能力：游戏/斗图/链接解析/联网检索/护栏/关系网/印象原子/热梗池
games = _load("games", "games.py")
fun_features = _load("fun_features", "fun_features.py")
media_resolver = _load("media_resolver", "media_resolver.py")
connectors = _load("connectors", "connectors.py")
safety_guard = _load("safety_guard", "safety_guard.py")
cross_interaction = _load("cross_interaction", "cross_interaction.py")
memory_atoms = _load("memory_atoms", "memory_atoms.py")
meme_manager = _load("meme_manager", "meme_manager.py")

DEFAULTS = {
    "enabled": True,
    "enabled_groups": [],
    "bot_nickname": "小鲸鱼",
    "wake_prefix": "ab!",
    "interject_desire": 0.30,
    "interject_hourly_cap": 8,
    "interject_cooldown_sec": 300,
    "reply_when_at": True,
    "reply_private": True,
    "meme_enabled": True,
    "timeline_size": 12,
    "memory_recall_topk": 2,
    "style_learning": True,
    "debug_dump_prompt": False,
    # 云崽等面板指令前缀：这些消息归别的插件，绝不插嘴
    "command_prefixes": ["#", "*", "%", "~"],
    # --- 长回复拆条 ---
    "split_enabled": True,
    "split_max_chars": 60,
    "split_max_parts": 3,
    "split_gap_min": 0.6,
    "split_gap_max": 1.5,
    # --- Gemini 看图 ---
    "vision_enabled": True,
    "vision_model": "gemini-3.5-flash-lite",
    "vision_fallback_models": ["gemini-3.6-flash", "gemini-3.8-flash"],
    "vision_max_images": 2,
    "vision_timeout_sec": 6,
    "vision_total_budget_sec": 12,
    "vision_proxy": "http://127.0.0.1:7890",
    "vision_api_key": "",
    "vision_key_env_file": os.path.expanduser("~/.config/qq-deepseek-bot.env"),
    # --- 两步生成 ---
    "social_read_enabled": True,
    "social_read_provider": "",
    "social_read_timeout_sec": 8,
    # --- 斗图闸 ---
    "meme_gate_enabled": True,
    "meme_min_gap_sec": 300,
    "meme_follow_gap_sec": 90,
    # 被 @ 或被戳后，同一人在这个时间里继续说话可以不再 @（免@追问窗口）
    "follow_window_sec": 300,
    "follow_max_turns": 5,
    # --- 文字游戏 / 斗图 / 联网扩展 ---
    "enable_games": True,          # 数字炸弹、掷骰子、抽签、点名
    "meme_battle_enabled": True,   # @ 带图时斗图反杀
    "media_parse_enabled": True,   # 解析消息里的 B 站/GitHub/音乐/短视频链接
    "connectors_enabled": True,    # 被叫到且明确问「出处/搜一下/新番」时才外联
    "meme_pool_size": 30,          # 热梗池每轮注入多少条
    "meme_rotation_sec": 1800,     # 多久换一轮
    # --- 别的机器人 ---
    "peer_bots_path": "",
    "peer_bots_name_hints": [],
    # --- 刹车（老仓库的 /off、禁言短路、刷屏闸）---
    "owner_qq": "",                # 主人 QQ：/off /on /memes 这类指令的主人也放行
    "bot_ban_guard": True,         # 机器人在禁言期不跑模型，省 token
    "density_guard": True,         # 两分钟里主动说过两条就不再主动开口
    "density_window_sec": 120,
    "density_limit": 2,
    # --- 自然语言管理（老仓库 handlers/actions.py 的处置段）---
    "admin_actions_enabled": True,
    # --- 云崽白名单联动 ---
    "yunzai_sync": True,
    "yunzai_other_yaml": "",       # 留空用默认 ~/projects/trss-yunzai/config/config/other.yaml
    # --- 数据备份 ---
    "backup_enabled": True,
    "backup_dir": "",              # 留空落 ~/.astrbot/backups（放插件目录外，重装不丢）
    "backup_keep": 7,
    "backup_interval_hours": 24,
    # --- 跨群记忆 ---
    "cross_group_enabled": True,
    "cross_group_recall": 2,
    "cross_group_max_topics": 60,
}

MEME_PATH = FISH_CORE / "meme-pack" / "memes_catalog.json"


async def _noop() -> str:
    """并发 gather 里的占位：没图时也要有个 awaitable，省得写分支。"""
    return ""


def _write_json(path: str, data, pretty: bool = False) -> None:
    """原子写：心境/记忆状态都在进程里攒着，写一半崩了会留个空文件把状态吃没。

    覆盖前先留一份 .bak-时间戳 快照（老仓库同一套），状态文件写坏一次就是
    好感度/心境整段归零，事后没有任何地方能捞回来。
    """
    backup.snapshot_json(str(path))
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)
    os.replace(tmp, path)


# AstrBot 自带指令的白名单。插件必须对它们彻底让路：
# 否则「/help」会被当成一句闲聊回掉，框架的指令处理器永远轮不到

# 回戳的时间戳：同一个人 3 秒内只回戳一次，避免两边互戳成死循环
_POKE_BACK_TS: dict[str, float] = {}
# 被同一个人一分钟内戳第二下时的固定短句池。2~4 字，避开上一次说过的：
# 这里不走模型——连戳时模型每次都写长句，拆条之后就是刷屏，
# 而这种情况本来也不需要"懂上下文"，只需要一句没耐心的打发
_POKE_SNARK = ("手欠", "别戳", "再戳收费", "干嘛啊", "戳啥", "轻点", "欠揍")

_BUILTIN_COMMANDS = frozenset({
    "help", "reset", "new", "stop", "sid", "stats",
    "provider", "set", "unset", "name", "ping", "dashboard_update",
    # 记忆查询由 astrbot_plugin_memory_cmd 接管，聊天插件必须让路
    "memary", "memory",
})


def _is_builtin_cmd(text: str) -> bool:
    # 注意：AstrBot 在唤醒阶段就把 wake_prefix（现在是「/」）剥掉了，
    # 到插件手里 message_str 已经是「memary」。所以斜杠可有可无，别只认带斜杠的
    t = (text or "").strip()
    if t.startswith("/"):
        t = t[1:]
    if not t:
        return False
    return t.split(" ", 1)[0].strip().lower() in _BUILTIN_COMMANDS


async def _poke_back(bot, gid: str, uid: str, self_id: str = "") -> None:
    """被戳就立刻戳回去。

    必须是模块级函数，不能写成类的 async 方法：AstrBot 会把类里第一个参数
    叫 event 的协程方法统统注册成消息处理器，`_poke_back` 会被当成 handler
    调用，报 missing positional arguments，而且每个事件都炸一次。
    """
    if bot is None:
        return
    key = f"{gid}:{uid}"
    now = time.time()
    if now - _POKE_BACK_TS.get(key, 0.0) < 3.0:
        return
    _POKE_BACK_TS[key] = now
    # 先带 self_id 精确路由，找不到 API 通道再退回「唯一连接」那条路。
    # 注入测试时 self_id 是假的，带 self_id 会查不到通道直接 ApiNotAvailable
    def _params():
        base = {"user_id": int(uid)}
        if gid != "private":
            base["group_id"] = int(gid)
        return base

    action = "friend_poke" if gid == "private" else "group_poke"
    try:
        try:
            await bot.call_action(action, **({"self_id": int(self_id)} if self_id else {}), **_params())
        except Exception as e:
            if type(e).__name__ != "ApiNotAvailable":
                raise
            await bot.call_action(action, **_params())
        logger.info("[fish] 已回戳 %s（群 %s）", uid, gid)
    except Exception as e:
        logger.warning("[fish] 回戳失败 %s(%s)：%s %s", uid, gid, type(e).__name__, e)


@dataclass
class GroupState:
    msgs: deque = field(default_factory=lambda: deque(maxlen=64))
    # style_learner.observe 要的是 [{sender, sender_id, content}] 形态的历史池
    pool: list = field(default_factory=list)
    recent_bot: deque = field(default_factory=lambda: deque(maxlen=8))
    last_msg_ts: float = 0.0
    last_bot_reply_ts: float = 0.0
    msgs_since_bot: int = 0
    interject_times: deque = field(default_factory=lambda: deque(maxlen=40))
    # 免@追问窗口：被 @ 或被戳之后，这个人在几分钟内继续说话不用再 @。
    # 三个字段必须一起看：uid 决定「谁」还有特权，until 决定「到什么时候」，
    # left 防止一句 @ 换来无限连击。
    follow_uid: str = ""
    follow_until: float = 0.0
    follow_left: int = 0

    # 戳一戳连击：同一个人一分钟内反复戳，得知道这是第几下，
    # 否则每戳一次都当「第一次被戳」来写，十下就是十句同款抱怨
    poke_uid: str = ""
    poke_count: int = 0
    poke_first_ts: float = 0.0

    # 刷屏闸：机器人自己主动说了几次、各自什么时候说的
    reply_times: deque = field(default_factory=lambda: deque(maxlen=20))

    # 斗图闸：上一张表情包什么时候发的、什么标签、对方最近有没有发图
    last_meme_ts: float = 0.0
    recent_meme_tags: deque = field(default_factory=lambda: deque(maxlen=4))
    incoming_image_ts: float = 0.0


@register(
    "astrbot_plugin_fish",
    "life",
    "把「小鲸鱼」自研 QQ 机器人的对话能力搬进 AstrBot：人设/心境/记忆/群表达/反模板/表情包/接话时机",
    "1.0.0",
    "https://github.com/lifefuck/qq-deepseek-realistic-bot",
)
class FishPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = {**DEFAULTS, **(config or {})}
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 刹车状态落盘：/off 的静默和机器人自身禁言期都得记住。
        # 只放内存的话，重启一次「我刚说全关了」就全忘，群里照旧开麦
        self._mute_path = str(DATA_DIR / "group_muted.json")
        self._mute_state: dict = {}
        self._load_mute_state()
        self._ban_expire: dict = {}
        self._ban_checked: dict = {}

        self.memory = fish_memory.FishMemoryEngine(str(DATA_DIR / "fish_memory.db"))
        # 启动时清一次：流水账留 45 天，软删记忆留 30 天，不然库只涨不落
        try:
            cleaned = self.memory.prune()
            if any(cleaned.values()):
                logger.info("[fish] 启动清理：流水 %d 行、软删记忆 %d 条、标签 %d 条",
                            cleaned["turn_logs"], cleaned["memories"], cleaned["tags"])
        except Exception as e:
            logger.warning("[fish] 启动清理失败：%s", e)
        # 心境状态落盘在同目录；原项目 attach 时故意不调 save，这里由 tick 触发
        affect.attach(writer=_write_json, path=str(DATA_DIR / "affect_state.json"))
        # 好感度：每群每人一个长期分，和心境分开存，别互相污染
        favor.attach(writer=_write_json, path=str(DATA_DIR / "favor_state.json"))
        # style_learner 的池子路径写死在原项目的 data/ 下，指到插件自己的目录，别污染上游
        style_learner._POOL_PATH = str(DATA_DIR / "style_fewshot.json")
        # meme_catalog 用 _SCRIPT_DIR 拼相对路径，改指 fish_core，图库走 meme-pack 软链
        meme_catalog._SCRIPT_DIR = str(FISH_CORE)
        meme_catalog.CATALOG_FILE = str(MEME_PATH)

        # 热梗池：数据目录这时才确定，所以只 attach，落盘载入显式调一次
        self.memes = meme_manager.meme_manager
        try:
            self.memes.attach(data_dir=str(DATA_DIR),
                              pool_size=int(self.cfg.get("meme_pool_size", 30) or 30),
                              rotation_interval=int(self.cfg.get("meme_rotation_sec", 1800) or 1800))
            self.memes.load_pool()
            self.memes.load_group_slang()
        except Exception as e:
            logger.warning("[fish] 热梗池载入失败：%s", e)
        # 群友 @ 关系网：只记近期互动，落盘前剪掉 6 小时前的记录，不会无限涨
        self.relations_path = str(DATA_DIR / "relations.json")
        try:
            cross_interaction.load_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网载入失败：%s", e)
        # 印象原子：和 fish_memory 的句子分开存，自己的去重/衰减逻辑
        self.atoms_path = DATA_DIR / "atoms.json"
        self._atoms_all = self._load_atoms()
        # 联网类能力共用一个 session，别每句话新建连接
        self._sess = None

        self._groups: dict[str, GroupState] = {}

        # 控制台用的最近事件环形缓冲：老面板那栏「实时交互日志」搬到面板里，
        # 不再自己维护一份文件，进程重启就重来，够看就行
        self._events: deque = deque(maxlen=int(self.cfg.get("panel_event_buffer", 200) or 200))
        # 群级 override：面板改的开关落在这个文件，跟 AstrBot 全局配置分开，
        # 面板改错也只是这一项，不会把整份插件配置写坏
        self.overrides_path = DATA_DIR / "group_overrides.json"
        self._group_overrides: dict = self._load_overrides()

        # 跨群话题库：只存短摘要，落盘在插件自己的 data/ 下
        self.cross = cross_memory.CrossGroupMemory(
            str(DATA_DIR / "cross_group_topics.json"),
            max_topics=int(self.cfg.get("cross_group_max_topics", 60) or 60),
            logger=logger,
        )
        # 别的机器人名单：文件不在就建空数组（日志里会打路径）
        self.peer_path = str(self.cfg.get("peer_bots_path") or (DATA_DIR / "peer_bots.json"))
        self.peers = peer_bots.load(self.peer_path, logger=logger)
        self._peer_mtime = self._mtime(self.peer_path)

        self._started = time.time()
        logger.info(
            "[fish] 插件已加载：记忆库 %s，心境 %s，表情包 %d 张，已知机器人 %d 个，跨群话题 %d 条",
            DATA_DIR / "fish_memory.db",
            DATA_DIR / "affect_state.json",
            sum(meme_catalog.stats().values()),
            len(self.peers),
            len(self.cross.topics),
        )
        try:
            panel_api.PanelApi(self).register()
        except Exception as e:
            # 面板挂了不该拖死聊天，这里只报警
            logger.warning("[fish] 控制台接口注册失败：%s: %s", type(e).__name__, e)

        logger.info("[fish] 拆条 %s（%d 字起拆，最多 %d 条）；两步生成 %s；看图 %s（%s）",
                    "开" if self.cfg.get("split_enabled", True) else "关",
                    int(self.cfg.get("split_max_chars", 60) or 60),
                    int(self.cfg.get("split_max_parts", 3) or 3),
                    "开" if self.cfg.get("social_read_enabled", True) else "关",
                    "开" if self.cfg.get("vision_enabled", True) else "关",
                    self.cfg.get("vision_model", ""))

        # 记忆库是这次迁移唯一「丢了就没了」的资产：启动先备一次，
        # 之后每小时醒来比一次心跳，真备份与否由 min_interval 决定
        # 备份默认落在插件目录之外：插件重装/删目录时备份不能被一起带走
        self._backup_dir = str(self.cfg.get("backup_dir") or (Path.home() / ".astrbot" / "backups"))
        self._backup_task = None
        if self.cfg.get("backup_enabled", True):
            try:
                backup.backup_db(str(DATA_DIR / "fish_memory.db"), self._backup_dir,
                                 int(self.cfg.get("backup_keep", 7) or 7),
                                 min_interval=self._backup_interval())
            except Exception as e:
                logger.warning("[fish] 启动备份失败：%s", e)
            try:
                self._backup_task = asyncio.create_task(self._backup_loop())
            except Exception as e:
                logger.warning("[fish] 后台备份任务起不来：%s", e)

    def _backup_interval(self) -> float:
        return float(self.cfg.get("backup_interval_hours", 24) or 24) * 3600

    async def _backup_loop(self):
        """每小时醒一次，够不够间隔交给 backup_db 自己判断。"""
        while True:
            await asyncio.sleep(3600)
            try:
                backup.backup_db(str(DATA_DIR / "fish_memory.db"), self._backup_dir,
                                 int(self.cfg.get("backup_keep", 7) or 7),
                                 min_interval=self._backup_interval())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[fish] 定时备份失败：%s", e)

    async def terminate(self) -> None:
        """插件卸载/重载时收尾：关连接、把关系网落盘。失败也不能抛，否则框架停不下来。"""
        # 备份任务必须收掉，否则重载后旧任务还握着上一份 self 在跑
        if getattr(self, "_backup_task", None):
            self._backup_task.cancel()
        try:
            if self._sess is not None and not self._sess.closed:
                await self._sess.close()
        except Exception:
            pass
        try:
            cross_interaction.save_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网落盘失败：%s", e)

    # ---------------- 基础工具 ----------------
    def _load_atoms(self) -> dict:
        """印象原子按「群+人」各一份，读不出来就当空。"""
        try:
            return json.loads(self.atoms_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_atoms(self) -> None:
        try:
            self.atoms_path.write_text(json.dumps(self._atoms_all, ensure_ascii=False),
                                       encoding="utf-8")
        except Exception as e:
            logger.debug("[fish] 印象原子落盘失败：%s", e)

    def _atoms_of(self, gid: str, uid: str) -> list:
        return list((self._atoms_all.get(f"{gid}:{uid}") or []))

    async def _http(self) -> aiohttp.ClientSession:
        if self._sess is None or self._sess.closed:
            self._sess = aiohttp.ClientSession()
        return self._sess

    def _gid_of(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    def _state(self, gid: str) -> GroupState:
        st = self._groups.get(gid)
        if st is None:
            st = self._groups[gid] = GroupState()
        return st

    # ---------------- 控制台支撑 ----------------
    def _load_overrides(self) -> dict:
        """读群级 override。文件坏了就当空，别让插件起不来。"""
        try:
            raw = json.loads(self.overrides_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("[fish] 群 override 读取失败（按空处理）：%s", e)
            return {}

    def save_group_overrides(self, groups: dict) -> None:
        """先写临时文件再改名：面板保存时正赶上进程被杀，也不会留下半截 JSON。"""
        tmp = self.overrides_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.overrides_path)
        self._group_overrides = groups

    def meme_stats(self) -> dict:
        """表情包按标签计数，面板要的是汇总数。"""
        try:
            return dict(meme_catalog.stats() or {})
        except Exception as e:
            logger.debug("[fish] 表情包统计失败：%s", e)
            return {}

    def log_event(self, kind: str, gid: str, text: str, **extra) -> None:
        """往环形缓冲里记一条，控制台「最近交互」读的就是它。

        只存长度受限的单行文本：这里的内容会被面板渲染，太长或带换行都会把界面撑乱。
        """
        try:
            self._events.append({
                "ts": time.time(),
                "kind": kind,
                "group_id": str(gid or ""),
                **{k: (str(v)[:80] if k != "score" else v) for k, v in extra.items()},
                "text": str(text or "").replace("\n", " ")[:120],
            })
        except Exception:
            pass

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _peer_set(self) -> set:
        """名单手改后不用重启：mtime 变了就重读。"""
        m = self._mtime(self.peer_path)
        if m != self._peer_mtime:
            self._peer_mtime = m
            self.peers = peer_bots.load(self.peer_path, logger=logger)
            logger.info("[fish] 机器人名单已重载：%d 个", len(self.peers))
        return self.peers

    async def _llm_once(self, prompt: str, system: str, timeout: float) -> str:
        """给 social_read 用的一次性小调用：不落会话历史、不挂工具。"""
        prov = None
        pid = str(self.cfg.get("social_read_provider") or "").strip()
        if pid:
            try:
                prov = await self.context.provider_manager.get_provider_by_id(pid)
            except Exception as e:
                logger.warning("[fish] social_read 指定的 provider %s 不可用：%s", pid, e)
        if prov is None:
            prov = await self.context.get_using_provider_async()
        if prov is None:
            raise RuntimeError("没有可用的对话模型")
        resp = await asyncio.wait_for(
            prov.text_chat(prompt=prompt, system_prompt=system, contexts=[]), timeout
        )
        return getattr(resp, "completion_text", "") or ""

    async def _social_read(self, gid: str, st: GroupState, text: str, nick: str, is_at: bool):
        """第一步：判该不该接。任何异常都当成「判不了」，直接放行给主模型，别把回复吞掉。"""
        if not self.cfg.get("social_read_enabled", True):
            return None
        if not text and not is_at:
            return None
        timeline = [f"{m['nick']}：{m['text']}" for m in list(st.msgs)[-6:-1]]
        timeout = float(self.cfg.get("social_read_timeout_sec", 8) or 8)

        async def call(prompt, system):
            return await self._llm_once(prompt, system, timeout)

        t0 = time.time()
        try:
            verdict = await social_read.decide(
                call, timeline=timeline, latest=text, nick=nick,
                bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), is_at=is_at,
            )
        except Exception as e:
            logger.warning("[fish] 群 %s social_read 失败（%s: %s），按能接处理",
                           gid, type(e).__name__, str(e)[:80])
            return None
        logger.info(
            "[fish] 群 %s social_read（%.1fs）判=%s 话题=%r 怎么接=%r | 原句：%s",
            gid, time.time() - t0, "接" if verdict.get("join") else "不接",
            verdict.get("topic", ""), verdict.get("how", ""), text[:30],
        )
        return verdict

    def _meme_gate(self, gid: str, st: GroupState, tag: str, now: float | None = None) -> tuple[bool, str]:
        """斗图闸：同一话题一回合只发一张，被跟图/刚发过就不发。"""
        now = time.time() if now is None else now
        # 判定逻辑在 fish_core/meme_gate.py（纯函数），这边只负责把状态喂进去
        return meme_gate.decide(
            tag, now,
            last_meme_ts=st.last_meme_ts,
            recent_tags=list(st.recent_meme_tags),
            incoming_image_ts=st.incoming_image_ts,
            enabled=bool(self.cfg.get("meme_gate_enabled", True)),
            min_gap_sec=float(self.cfg.get("meme_min_gap_sec", 300) or 0),
            follow_gap_sec=float(self.cfg.get("meme_follow_gap_sec", 90) or 0),
        )

    @staticmethod
    def _first_plain_text(event: AstrMessageEvent) -> str:
        """原始第一个文本段。

        waking_check 已经把 wake_prefix 从 event.message_str 里剥掉了，
        想判断「这条是不是 ab! 唤醒的」只能看原始消息段。
        """
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                # 带 @ 的消息里文本段常以空格开头（"@机器人 你好"），
                # 不去掉的话 startswith("ab!") 永远判不到
                return (comp.text or "").lstrip()
            if isinstance(comp, (At, AtAll)):
                continue
        return ""

    def _wake_prefixes(self) -> tuple[str, ...]:
        """真正生效的唤醒前缀集合。

        插件自己配的那一条优先；没配就回落到 AstrBot 全局配置——
        因为 AstrBot 的唤醒词写在 provider_settings.wake_prefix（对话唤醒，
        如 ab!），顶层 wake_prefix 是指令前缀列表（/）。两个都得让路，否则
        同一句话框架 LLM 回一遍、插件再回一遍，群里像两个人在抢话。
        """
        items = []
        own = str(self.cfg.get("wake_prefix") or "").strip()
        if own:
            items.append(own)
        try:
            conf = self.context.get_config() or {}
            ps = conf.get("provider_settings") or {}
            wp = ps.get("wake_prefix") if hasattr(ps, "get") else None
            if isinstance(wp, str) and wp.strip():
                items.append(wp.strip())
            top = conf.get("wake_prefix")
            if isinstance(top, (list, tuple)):
                items.extend(str(x).strip() for x in top if str(x).strip())
            elif isinstance(top, str) and top.strip():
                items.append(top.strip())
        except Exception as e:
            logger.debug("[fish] 读取 AstrBot 唤醒前缀失败：%s", e)
        # dict.fromkeys 去重又保序
        return tuple(dict.fromkeys(items))

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        """@ 我、或者引用回复我。@全体 不算点名（群里一天能 @ 全体几十回）。"""
        self_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
            if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                return True
        return False

    def _has_media(self, event: AstrMessageEvent) -> tuple[bool, bool]:
        has_image = has_media = False
        for comp in event.message_obj.message:
            name = type(comp).__name__.lower()
            if name == "image":
                has_image = True
            elif name in ("face", "record", "json", "card", "file", "forward"):
                # 表情和语音不是图片：算进 has_image 的话发个表情就被当成「发了图」，
                # 表情包闸白压 90 秒，@ + 表情还会误触发斗图
                has_media = True
        return has_image, has_media

    def _group_allowed(self, gid: str) -> bool:
        """生效群白名单。

        默认空列表 = 谁都不生效：这套插件会主动插话，装完就闷声往真群里发消息
        是最坏的结果。要上线必须把群号写进 enabled_groups，或者填 ["*"] 全开。
        """
        ov = (self._group_overrides.get(str(gid)) or {}).get("enabled")
        # 面板上单独关掉的群优先：群里出事了第一反应是让它闭嘴，
        # 不该逼人回去翻整份配置找 group 白名单
        if ov is False:
            return False
        if ov is True:
            return True

        groups = self.cfg.get("enabled_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        groups = [str(g).strip() for g in groups]
        if "*" in groups:
            return True
        return str(gid) in groups

    # ---------------- 刹车：静默 / 禁言 / 刷屏 ----------------
    def _load_mute_state(self) -> None:
        try:
            if os.path.exists(self._mute_path):
                with open(self._mute_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._mute_state = data if isinstance(data, dict) else {}
        except Exception as e:
            # 状态文件坏了就当没静默过：宁可多嘴，也不能因为一个坏文件永久装死
            logger.warning("[fish] 静默状态读取失败，按未静默处理：%s", e)
            self._mute_state = {}

    def _is_muted(self, gid: str) -> bool:
        return bool(self._mute_state.get(str(gid)))

    def _set_muted(self, gid: str, muted: bool, restore_yunzai: bool | None = None) -> None:
        """记静默状态；restore_yunzai 记「关之前这个群在不在云崽白名单」。

        不记这一位的话，/on 会无条件 sync(True)，把本来就不在白名单的群
        塞进别人的 other.yaml —— 关一次开一次就永久多出一个群号。
        """
        if muted:
            self._mute_state[str(gid)] = (
                {"restore": bool(restore_yunzai)} if restore_yunzai is not None else True
            )
        else:
            self._mute_state.pop(str(gid), None)
        _write_json(self._mute_path, self._mute_state, pretty=True)

    def _mute_restore(self, gid: str) -> bool:
        """静默前它在云崽白名单里吗。老格式（直接 True）当作「不在」。"""
        v = self._mute_state.get(str(gid))
        if isinstance(v, dict):
            return bool(v.get("restore"))
        return False

    async def _bot_banned(self, event: AstrMessageEvent, gid: str) -> bool:
        """机器人在这个群是不是还在禁言期。

        被禁言照样收得到群消息，不拦住就是白烧 token，解禁后还要把一堆
        过期话接一遍。离解禁还远（>60 秒）就不复核，免得每条消息打一次 API。
        """
        if not self.cfg.get("bot_ban_guard", True) or not gid.isdigit():
            return False
        now = time.time()
        expire = self._ban_expire.get(gid, 0.0)
        if now < expire:
            if expire - now > 60:
                return True
            if now - self._ban_checked.get(gid, 0.0) < 30:
                return True
        elif now - self._ban_checked.get(gid, 0.0) < 600:
            # 没有禁言记录时十分钟顺手核一次：抓禁言那一刻可能一条消息都没走完
            return False
        self._ban_checked[gid] = now
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return False
            info = await asyncio.wait_for(
                bot.call_action("get_group_member_info", group_id=int(gid),
                                user_id=int(event.get_self_id()), no_cache=True),
                timeout=2.0)
        except Exception as e:
            logger.debug("[fish] 群 %s 禁言状态核实失败（按未禁言跑）：%s", gid, e)
            return False
        shut = int((info or {}).get("shut_up_timestamp") or 0)
        if shut > now:
            self._ban_expire[gid] = float(shut)
            logger.info("[fish] 群 %s 机器人还在禁言到 %s，这段时间不回复",
                        gid, time.strftime("%H:%M:%S", time.localtime(shut)))
            return True
        self._ban_expire.pop(gid, None)
        return False

    def _density_blocked(self, st: "GroupState", now: float) -> bool:
        """两分钟里已经主动说了两条就闭嘴。连刷换禁言，比少接一句贵多了。"""
        if not self.cfg.get("density_guard", True):
            return False
        window = float(self.cfg.get("density_window_sec", 120) or 120)
        limit = int(self.cfg.get("density_limit", 2) or 2)
        q = st.reply_times
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    def _sender_role(self, event: AstrMessageEvent) -> str:
        """尽最大努力取群角色（owner / admin / member）。

        官方 is_admin() 只认 event.role，而 aiocqhttp 适配器根本不给它赋值，
        于是 is_admin() 在 QQ 群永远 False —— 群管发 /off 会被回"这条得群管发"，
        管理动作的 is_admin 分支也形同虚设。唯一可靠来源是原始事件里的
        sender.role，这里把两条路都试一遍。
        """
        role = str(getattr(event, "role", "") or "").lower()
        # 注意：aiocqhttp 下 event.role 常常恒为 "member"（适配器没填），
        # 所以只有明确是 admin/owner 才能直接采信，member 必须继续往下查原始事件
        if role in ("owner", "admin"):
            return role
        # aiocqhttp 把原始事件整个挂在 raw_message 上（不是 dict，是 Event 对象，
        # 但同样支持 .get / 属性访问），所以两种取法都得试
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is not None:
            sender = None
            try:
                sender = raw.get("sender") if hasattr(raw, "get") else None
            except Exception:
                sender = None
            if sender is None:
                sender = getattr(raw, "sender", None)
            if sender is not None:
                try:
                    r = sender.get("role") if hasattr(sender, "get") else getattr(sender, "role", "")
                except Exception:
                    r = ""
                if r:
                    return str(r).lower()
                r2 = getattr(sender, "role", "")
                if r2:
                    return str(r2).lower()
        return role

    def _is_boss(self, event: AstrMessageEvent) -> bool:
        """群管或主人。这两个指令会真的把群关掉，不能谁发都算。"""
        try:
            owner = str(self.cfg.get("owner_qq") or "").strip()
            if owner and str(event.get_sender_id()) == owner:
                return True
            if self._sender_role(event) in ("owner", "admin"):
                return True
            return bool(event.is_admin())
        except Exception:
            return False

    @filter.command("off", alias={"静默"})
    async def cmd_off(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            yield event.plain_result("静默只能在群里发")
            return
        if not self._is_boss(event):
            yield event.plain_result("这条得群管发")
            return
        yaml_path = self.cfg.get("yunzai_other_yaml") or ""
        was_listed = yunzai_link.in_whitelist(yaml_path, int(gid))
        self._set_muted(gid, True, restore_yunzai=was_listed)
        if self.cfg.get("yunzai_sync", True):
            # 只关我自己没用：云崽那边的插件还在群里动，「全关」就是句空话
            yunzai_link.sync_whitelist(yaml_path, int(gid), False)
        logger.info("[fish] 群 %s 静默（%s 触发）", gid, event.get_sender_id())
        self.log_event("mute", gid, "静默", who=event.get_sender_name())
        event.stop_event()
        yield event.plain_result("全关了，从现在起群里我就当没听见，发 /on 才回来")

    @filter.command("on", alias={"恢复"})
    async def cmd_on(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            yield event.plain_result("这条只能在群里发")
            return
        if not self._is_boss(event):
            yield event.plain_result("这条得群管发")
            return
        restore_yunzai = self._mute_restore(gid)
        self._set_muted(gid, False)
        if self.cfg.get("yunzai_sync", True) and restore_yunzai:
            # 只有当 /off 之前它本来在白名单里才加回去，否则就是凭空塞群号
            yunzai_link.sync_whitelist(self.cfg.get("yunzai_other_yaml") or "", int(gid), True)
        # 禁言缓存也清掉：刚解禁的那一刻不该还拿旧记录装死
        self._ban_expire.pop(gid, None)
        self._ban_checked.pop(gid, None)
        logger.info("[fish] 群 %s 恢复（%s 触发）", gid, event.get_sender_id())
        # 不 stop 的话框架默认 LLM 会拿「/on」再回一句闲聊
        event.stop_event()
        yield event.plain_result("回来了，该说说该闹闹")

    @filter.command("memes", alias={"reload_memes", "梗库"})
    async def cmd_memes(self, event: AstrMessageEvent):
        """手动重载热梗池和本群黑话，改完 json 不用重启 AstrBot。"""
        if not self._is_boss(event):
            yield event.plain_result("这条得群管发")
            return
        try:
            self.memes.load_pool(force=True)
            self.memes.load_group_slang()
        except Exception as e:
            logger.warning("[fish] 重载热梗失败：%s", e)
            yield event.plain_result(f"重载失败：{e}")
            return
        stats = {}
        try:
            stats = self.memes.stats() or {}
        except Exception:
            pass
        pool = stats.get("pool_size") or len(getattr(self.memes, "current_pool", []) or [])
        total = stats.get("total") or 0
        # 本群黑话单独存，按群号计数才看得出重载的是哪几个群
        slang = getattr(self.memes, "group_slang", None) or {}
        slang_desc = f"，本群黑话 {len(slang)} 个群" if slang else ""
        logger.info("[fish] 手动重载梗库：池 %s/%s 条，黑话 %s 群", pool, total, len(slang))
        event.stop_event()
        yield event.plain_result(f"热梗池 {pool}/{total} 条已重载{slang_desc}")

    # ---------------- 主流程：群消息 ----------------
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return

        gid = self._gid_of(event)
        if not self._group_allowed(gid):
            return
        if self._is_muted(gid):
            # /off 之后群里说什么都当没听见：连时间线都不记，静默要真的静。
            # 这里必须 stop_event——不拦的话 AstrBot 自己的默认 LLM 还会接 @
            event.stop_event()
            return
        # 戳一戳交给 on_poke。这里必须先让路：群消息 handler 排在前面，
        # 它一旦对空文本调 stop_event，后面注册的 on_poke 就再也不会被执行
        if any(isinstance(c, Poke) for c in (event.message_obj.message or [])):
            return
        uid = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        if uid == self_id:
            # 自己发的消息（含别的插件代发）：不入时间线，免得学自己说话
            return

        raw_first = self._first_plain_text(event)
        # 插件自己的唤醒词（ab!）由下面 forced 分支接管，不在此让路；
        # 只有框架自己的指令前缀（/ 之类）才需要让给 AstrBot
        own_prefix = str(self.cfg.get("wake_prefix") or "").strip()
        wake_prefixes = tuple(p for p in self._wake_prefixes() if p and p != own_prefix)
        if wake_prefixes and raw_first.startswith(wake_prefixes):
            # 带唤醒前缀 = 明确在跟 AstrBot 的主 LLM 说话，这里彻底让路。
            # 注意不能 stop_event：stop_event 会带上 STOP 结果，AstrBot 的默认
            # LLM 分支（process_stage）就整段跳过了，等于把用户的问题吃掉。
            logger.debug("[fish] 群 %s 收到唤醒前缀消息，交给 AstrBot 自身处理", gid)
            return

        text = (event.message_str or "").strip()
        if _is_builtin_cmd(text):
            logger.debug("[fish] 群 %s 让路给自带指令：%s", gid, text[:20])
            return
        prefixes = tuple(self.cfg.get("command_prefixes") or ())
        if text.startswith(prefixes):
            # 云崽/面板指令归别的插件，原项目铁律是静默。这里只「不接话」，
            # 不能 stop_event——那会把下游别的插件的同名指令一起掐死
            logger.debug("[fish] 群 %s 面板指令前缀，跳过：%s", gid, text[:20])
            return

        nick = event.get_sender_name() or uid
        # 别的机器人：不接、不进时间线、不占插话机会。
        # 它的播报被当成人话去接、或者跟它对吵刷屏，都是事故；名单见 data/peer_bots.json
        if peer_bots.is_peer_bot(uid, nick, self._peer_set(),
                                 self.cfg.get("peer_bots_name_hints") or ()):
            logger.info("[fish] 群 %s 忽略机器人消息：%s(%s) %s", gid, nick, uid, text[:20])
            return

        st = self._state(gid)
        now = time.time()
        has_image, has_media = self._has_media(event)
        is_at = self._is_at_bot(event)

        # 注入防护放在最前面：真要套人设的，不必等到后面调模型再说。
        # 只认「明确在改指令/要系统提示词」的说法，正常技术提问不会被拦
        if text:
            injected, snark = safety_guard.check_prompt_injection(text)
            if injected:
                logger.info("[fish] 群 %s 判定注入（%s）：%s", gid, nick, text[:40])
                self.log_event("inject", gid, text[:60], who=nick)
                yield event.plain_result(snark or "这句我不接。")
                event.stop_event()
                return

        # @ 关系：谁爱跟谁搭话，进 prompt 时给一句体感
        if gid and gid.isdigit() and text:
            self_id_now = str(event.get_self_id())
            for comp in event.message_obj.message:
                if isinstance(comp, At) and str(getattr(comp, "qq", "")) not in (self_id_now, "all", ""):
                    try:
                        # 第 5 个位置参数是 target_nick，不是时间戳；
                        # 传错的话文件里的名字会变成一串浮点数
                        cross_interaction.record_interaction(
                            int(gid), int(uid) if uid.isdigit() else 0, nick,
                            int(str(comp.qq)), getattr(comp, "name", "") or "",
                            self.relations_path)
                    except Exception as e:
                        logger.debug("[fish] 关系网记录失败：%s", e)

        # 时间线与在线学习都要「所有」群消息，包括没人叫它的那些
        if text or has_image:
            st.msgs.append({"uid": uid, "nick": nick, "text": text or "[图片]", "ts": now})
            if gid:
                affect.on_group_message(gid, text, private=False)
                # 好感度走本地规则，不吃 token；同一人两分钟内只算一次
                favor.on_message(gid, uid, text)
            if self.cfg.get("style_learning", True) and text:
                st.pool.append({"sender": nick, "sender_id": uid, "content": text})
                if len(st.pool) > 200:
                    del st.pool[:-200]
                style_learner.observe(gid, st.pool)
            if has_image:
                st.incoming_image_ts = now
            # 跨群话题：只留削过隐私的短摘要，别的群能不能被引用看开关
            if self.cfg.get("cross_group_enabled", True) and text:
                try:
                    self.cross.record_topic(gid, nick, text)
                except Exception as e:
                    logger.debug("[fish] 跨群话题记录失败：%s", e)
        st.last_msg_ts = now
        st.msgs_since_bot += 1

        if not text and not has_image:
            # 纯语音/表情/卡片：不接，但别 stop——语音转文字之类的插件还要处理这条
            if not is_at:
                return

        # 框架层的 ab! 唤醒词已关掉（不然那条路走原生流程、没有人设）。
        # 但用户的手感还在：手打前缀就是明着叫它，按「被叫」处理，前缀本身不喂给模型
        forced = False
        prefix = str(self.cfg.get("wake_prefix") or "ab!")
        if text and text.lstrip().lower().startswith(prefix.lower()):
            text = text.lstrip()[len(prefix):].lstrip()
            forced = True

        # 文字游戏：纯本地状态机，跟 @ 和插话概率都无关，谁发都能玩
        if self.cfg.get("enable_games", True) and text and gid.isdigit():
            quick = games.handle_quick_game_commands(text)
            if quick:
                yield event.plain_result(quick)
                event.stop_event()
                return
            handled, bomb_reply = games.process_number_bomb(int(gid), text, uid, nick)
            if handled:
                if bomb_reply:
                    yield event.plain_result(bomb_reply)
                event.stop_event()
                return

        # 斗图：有人 @ 它还甩图，就当是来斗图的。发图比说话慢半拍，两条分开发
        if is_at and has_image and self.cfg.get("meme_battle_enabled", True):
            img_url = ""
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    img_url = getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                    break
            action = fun_features.meme_battle(
                text=text, image_url=img_url or None, meme_catalog=meme_catalog,
                last_battle_ts=st.last_meme_ts, group_id=gid, user_id=uid, now=now)
            if action:
                st.last_meme_ts = float(action.get("battle_ts") or now)
                try:
                    await event.send(MessageChain(chain=[Plain(action["text"])]))
                    await asyncio.sleep(float(action.get("delay_sec") or 0.4))
                    await event.send(MessageChain(chain=[Image.fromBase64(action["payload"])]))
                    logger.info("[fish] 群 %s 斗图：%s", gid, action["text"][:24])
                except Exception as e:
                    logger.warning("[fish] 斗图发送失败：%s", e)
                event.stop_event()
                return

        # ---------------- 免@追问窗口 ----------------
        # 被叫过一次之后，同一个人的后续发言不用再 @。这是群里最自然的手感：
        # 谁也不会每次说话都记得点一下头像。别人说话不受影响，仍然走插话判定
        if is_at or forced:
            st.follow_uid = uid
            st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)
            st.follow_left = int(self.cfg.get("follow_max_turns", 5) or 0)
        if not (is_at or forced) and uid == st.follow_uid and now < st.follow_until:
            if st.follow_left > 0:
                reason = "追问窗口"
                is_follow = True
            else:
                # 配额用完就当窗口关闭，别让他继续白嫖
                st.follow_uid = ""
                is_follow = False
        else:
            is_follow = False

        # ---------------- 禁言期直接收工 ----------------
        # 被禁言也收得到消息，不拦住就是白烧 token，解禁后还得回头接一堆过期话
        if await self._bot_banned(event, gid):
            event.stop_event()
            return

        # ---------------- 被指挥处置人：禁言 / 踢人 ----------------
        # 放闲聊之前：群管说「把某人禁言十分钟」，不该先被模型当玩笑回掉
        if self.cfg.get("admin_actions_enabled", True) and gid and gid.isdigit():
            nick_map = {str(m["uid"]): (m.get("nick") or str(m["uid"]))
                        for m in list(st.msgs)[-40:]}
            try:
                acted = await admin_actions.maybe_run(
                    event, event.bot, gid, str(uid), str(event.get_self_id()),
                    str(self.cfg.get("owner_qq") or ""), nick_map, text,
                    addressed=bool(is_at or forced), is_admin=self._sender_role(event) in ("admin", "owner"))
            except Exception as e:
                logger.warning("[fish] 处置动作执行异常：%s", e)
                acted = None
            if acted:
                logger.info("[fish] 群 %s 处置动作：%s", gid, acted)
                event.stop_event()
                yield event.plain_result(acted)
                return

        # ---------------- 该不该开口 ----------------
        if is_at or forced:
            reason = "被@" if is_at else "手打前缀"
            if not self.cfg.get("reply_when_at", True):
                await self._remember(gid, uid, text)
                event.stop_event()
                return
        elif is_follow:
            reason = "追问窗口"
            logger.info("[fish] 群 %s %s 在追问窗口内（剩 %d 次），直接回", gid, nick, st.follow_left)
        else:
            ok, reason = self._should_interject(gid, st, text, now, has_image, has_media)
            if ok and self._density_blocked(st, now):
                # 被点名和追问窗口不受这个闸限制，主动搭腔才受限制
                ok, reason = False, "两分钟内已经主动说过两条"
            if not ok:
                # 不接话也要做自动感知（原项目对每条群消息都记，不只记回过的）
                logger.info("[fish] 群 %s 不接（%s）：%s", gid, reason, text[:30])
                self.log_event("skip", gid, text[:60], who=nick, why=reason)
                await self._remember(gid, uid, text)
                if is_at:
                    # 只有「被 @ 了却不回」才拦：不拦的话框架 LLM 会换个方式再回一遍。
                    # 主动搭腔这条根本没 @ 它，stop 只会顺手掐死同群别的插件
                    event.stop_event()
                return

        # 到这里这一条已经确定要回：立刻占住冷却位。
        # social_read 和识图还要几秒，这期间同群陆续进来的消息若不占位，
        # 每条都以为自己「还没说过话」，一波刷屏会被连着接好几次
        st.last_bot_reply_ts = time.time()

        # ---------------- 第一步：这帮人在聊什么、我该不该接；同时看图 ----------------
        # 两件事互不依赖，并发跑。串行的话识图那十几秒会整个叠在 judge 后面
        social_hint = ""
        imgs = [c for c in event.message_obj.message if isinstance(c, Image)] \
            if (has_image and self.cfg.get("vision_enabled", True)) else []
        verdict, vision_desc = await asyncio.gather(
            self._social_read(gid, st, text, nick, is_at or forced or is_follow),
            vision.from_components(imgs, self.cfg, logger=logger) if imgs else _noop(),
        )
        if vision_desc:
            logger.info("[fish] 群 %s 图片描述：%s", gid, vision_desc[:80])
        if verdict and not verdict.get("join"):
            # 追问窗口里的人是「在跟我说话」，跟被 @ 一个性质：
            # social_read 判「群里这话题我不该插嘴」对这种情况不成立
            if not (is_at or is_follow):
                # 判「不接」就到此为止，主模型一次都不调（这是这一步省下的主要延迟）
                logger.info("[fish] 群 %s social_read 判不接（话题=%r），不调主模型：%s",
                            gid, verdict.get("topic", ""), text[:30])
                self.log_event("skip", gid, text[:60], who=nick,
                               why=f"social_read 判不接（{verdict.get('topic', '')}）")
                await self._remember(gid, uid, text)
                # 能走到这里必然不是 @ 也不是追问窗口，不 stop，让兄弟插件照常处理
                return
            # 被直接点名的没法装死不回，记一笔继续走
            logger.info("[fish] 群 %s social_read 想判不接，但这句是 @ 你，仍然回", gid)
        elif verdict:
            social_hint = social_read.hint_line(verdict)

        # 顺序要紧：先召回记忆再落这一条。反过来的话当前这句会被自己的召回捡回来，
        # prompt 里就出现「你还记得我明天要干嘛吗」这种自己引用自己的鬼话
        # 链接/视频解析：B站/GitHub/音乐/短视频/普通网页，失败静默——
        # 解析不到就当这条消息没链接，绝不能因为它让整条回复卡住
        media_desc = ""
        if self.cfg.get("media_parse_enabled", True) and text and _URL_RE.search(text):
            try:
                card = await media_resolver.parse_media_content(await self._http(), text)
                if card and card.get("summary"):
                    media_desc = "【这条链接的内容】" + str(card["summary"])[:220]
                    logger.info("[fish] 群 %s 解析链接：%s", gid, media_desc[:60])
            except Exception as e:
                logger.debug("[fish] 链接解析失败：%s", e)

        # 联网扩展只在「被叫到 + 明确问了」时才发请求：普通闲聊一句都不外联。
        # 这里是体验和能力的平衡点——宁可少搜，也不让每句话多挂一秒外网延迟
        if self.cfg.get("connectors_enabled", True) and (is_at or forced) and text:
            try:
                img_url = next((getattr(c, "url", "") or getattr(c, "file", "")
                                for c in event.message_obj.message if isinstance(c, Image)), "")
                if has_image and img_url and re.search(r"出处|哪部|什么番|动漫|动画|叫什么", text):
                    anime = await connectors.search_anime_by_image(await self._http(), img_url)
                    if anime and anime.get("title"):
                        media_desc += f"\n【以图搜番】{anime['title']} 相似度{anime.get('similarity', '?')}"
                elif _REALTIME_RE.search(text) and not _NO_SEARCH_RE.search(text):
                    # 「搜索」和「搜一下」是两回事，只写后者会漏掉「帮我联网搜索一下」这种最自然的说法
                    q = _search_query(text)
                    if q:
                        briefs = await connectors.search_web_brief(await self._http(), q, limit=3)
                        if briefs:
                            media_desc += ("\n【刚查到的东西（这就是实时结果，直接拿来回答，"
                                           "别再说自己查不了网）】：" + "；".join(
                                f"{b['title']}：{b['snippet'][:60]}" for b in briefs))
                            logger.info("[fish] 群 %s 联网检索「%s」命中 %d 条", gid, q, len(briefs))
                        else:
                            # 检索挂了也要说话，但别让模型编「我没有联网能力」这种谎
                            media_desc += "\n（这次没查到结果，照实说没搜到，可以说回头再帮着看一眼）"
                            logger.warning("[fish] 群 %s 联网检索「%s」无结果", gid, q)
                elif re.search(r"新番|有什么番|番剧推荐", text):
                    vids = await connectors.fetch_bilibili_hot_videos(await self._http(), limit=3)
                    if vids:
                        media_desc += "\n【B站热门】" + "；".join(v.get("title", "") for v in vids)
            except Exception as e:
                logger.warning("[fish] 联网扩展失败：%s", e)

        cross_hint = self.cross.hint(gid, int(self.cfg.get("cross_group_recall", 2) or 2)) \
            if self.cfg.get("cross_group_enabled", True) else ""
        system_prompt, user_prompt = await self._compose(
            gid, uid, nick, text, is_at, st, self_id=self_id,
            vision_desc=vision_desc,
            media_hint=media_desc,
            social_hint=social_hint, cross_hint=cross_hint,
        )
        await self._remember(gid, uid, text)
        if is_follow:
            st.follow_left -= 1
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick, "text": text, "at": is_at,
            # 决定回的原因一并带走：控制台要显示「这句为什么接」
            "reason": reason,
        })
        logger.info("[fish] 群 %s %s(%s) 决定回（%s）：%s", gid, nick, uid, reason, text[:40])
        # 这里不能 stop_event：它有 STOP 结果，连我们自己刚 yield 的 request_llm 一起拦掉，
        # 实测 ab! 消息一条回复都发不出去。防双回靠「框架只在无 request_llm 时才走默认 LLM」
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt,
            system_prompt=system_prompt,
            contexts=[],
            image_urls=[],
        )

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True) or not self.cfg.get("reply_private", True):
            return
        text = (event.message_str or "").strip()
        if _is_builtin_cmd(text):
            return
        uid = str(event.get_sender_id())
        if uid == str(event.get_self_id()):
            return
        nick = event.get_sender_name() or uid
        affect.on_group_message("private", text, private=True)
        system_prompt, user_prompt = await self._compose("private", uid, nick, text, True, self._state("private"),
                                                        is_private=True, self_id=str(event.get_self_id()))
        event.set_extra("fish_pending", {"gid": "private", "uid": uid, "nick": nick, "text": text, "at": True})
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt, system_prompt=system_prompt, contexts=[], image_urls=[]
        )

    async def _remember(self, gid: str, uid: str, text: str) -> None:
        """自动感知：把群消息喂给记忆引擎（规则抽取事实/偏好/计划/事件）。"""
        if not text:
            return
        try:
            # log=False：抽取记忆但先不写流水，等回复出来再补，避免一条消息两行流水
            await asyncio.to_thread(self.memory.record_turn, gid, uid, text, "", False)
            # 命中的是偏好/事实，才值得单独压成一条印象原子；
            # 事件和计划塞进原子层只会把「同一个人」的档案搅浑
            kinds = set(fish_memory.FishMemoryEngine.detect_types(text))
            if kinds & {"preference", "fact"} and gid:
                keep = self._atoms_of(gid, uid)
                self._atoms_all[f"{gid}:{uid}"] = memory_atoms.add_note(keep, text[:60])
                self._save_atoms()
        except Exception as e:
            logger.warning("[fish] 记忆自动感知失败: %s", e)

    def _should_interject(self, gid, st, text, now, has_image, has_media) -> tuple[bool, str]:
        """旁观态接话闸门：质量分 × 时机修正 vs 意愿门槛。"""
        # adjust() 的「刚说过就 ×0.55」读的是模块里的 COOLDOWN_SEC，不是参数，
        # 不把配置同步进去，会出现「硬冷却放行了、软权重还在按 300 秒打折」的矛盾
        interject_planner.COOLDOWN_SEC = float(self.cfg.get("interject_cooldown_sec", 300))
        if interject_planner.too_soon(now, st.last_bot_reply_ts,
                                      float(self.cfg.get("interject_cooldown_sec", 300))):
            return False, "刚说过话，冷却中"

        cap = int(self.cfg.get("interject_hourly_cap", 8))
        recent = [t for t in st.interject_times if now - t < 3600]
        if len(recent) >= cap:
            return False, f"近一小时已插话 {len(recent)} 次"

        # 复读机不接：同一个人连着发同一句，接一次就够，接三次就是跟人一起刷屏
        tail = list(st.msgs)
        if len(tail) >= 2 and text:
            uid_now = tail[-1]["uid"]
            prev = [m["text"] for m in tail[-3:-1] if m["uid"] == uid_now]
            if prev and all(p == text for p in prev):
                return False, "同一句复读"

        score, why = interject_score.score_message_quality(
            text,
            has_image=has_image,
            has_media=has_media,
            recent_msgs=st.msgs_since_bot,
            now=now,
            last_msg_ts=st.last_msg_ts if st.msgs_since_bot > 1 else 0.0,
            last_bot_reply_ts=st.last_bot_reply_ts,
        )
        score, timing = interject_planner.adjust(
            score,
            now=now,
            last_bot_reply=st.last_bot_reply_ts,
            msgs_since_bot=st.msgs_since_bot,
            last_reply_answered=affect.is_last_reply_answered(gid),
        )
        # 群级 override 优先于全局意愿：一个群吵、一个群安静，只能分别调
        ov_desire = self._group_overrides.get(str(gid), {}).get("interject_desire")
        desire = float(self.cfg.get("interject_desire", 0.30) if ov_desire is None else ov_desire)
        threshold = interject_score.interject_threshold(desire)
        passed = score >= threshold
        if passed:
            st.interject_times.append(now)
        logger.info(
            "[fish] 群 %s 接话质量 %.2f vs 门槛 %.2f（%s | 时机 %s）-> %s",
            gid, score, threshold, why, timing or "无修正", "接" if passed else "略过",
        )
        return passed, f"主动接话 {score:.2f}/{threshold:.2f}"

    # ---------------- 戳一戳 ----------------
    @filter.event_message_type(EventMessageType.ALL)

    async def on_poke(self, event: AstrMessageEvent):
        """戳一戳必须自己接，不能交给原生流程。

        原生路径会把「被戳」当成一条普通消息丢给模型，模型不知道自己在被戳，
        回出来就是「有什么需要帮忙的」这种客服腔。这里自己拼 prompt，直接按
        「手被人戳了一下」的反应回一句。
        """
        if not self.cfg.get("enabled", True):
            return
        segs = list(getattr(event.message_obj, "message", []) or [])
        poked = [s for s in segs if isinstance(s, Poke)]
        if not poked:
            return
        # 别人互戳跟我没关系，只认戳到我自己身上的
        self_id = str(event.get_self_id())
        if not any(str(getattr(s, "id", "")) == self_id for s in poked):
            return

        gid = str(event.get_group_id() or "private")
        if self._is_muted(gid):
            # 静默连戳都不理，但也不回戳——不然「全关」了手还在动
            return
        if gid != "private" and not self._group_allowed(gid):
            event.stop_event()
            return

        uid = str(event.get_sender_id())
        nick = event.get_sender_name() or uid
        is_admin = self._sender_role(event) in ("admin", "owner")
        # 戳一戳在群里最常见的含义不是撒娇，是「你刚才没理我」。
        # 所以先看这人在我上次说话之后有没有发过还没被回的话，再决定这次是
        # 「补上那条没回的消息」还是「真的只是手欠」。
        now = time.time()
        st = self._state(gid)
        # 一分钟内的连戳算一串：第一下正常回，第二下简短打发，第三下起只回戳不说话。
        # 真人被连戳十几次也就是这个反应，而且再回下去只会越说越冲、越说越像坏掉的复读机
        if st.poke_uid != uid or now - st.poke_first_ts > 60:
            st.poke_uid, st.poke_count, st.poke_first_ts = uid, 1, now
        else:
            st.poke_count += 1
        poke_info = poke_context.decide(st.msgs, uid, st.last_bot_reply_ts, now)
        pending_text = poke_info["pending"]
        if st.poke_count >= 3 and not pending_text:
            # 连着戳到第三下还没正事：戳回去就完事，别再刷一句「戳上瘾了是吧」
            logger.info("[fish] 戳一戳连击 %d 次 <- %s(%s) 群 %s，只回戳不说话",
                        st.poke_count, nick, uid, gid)
            self.log_event("poke", gid, f"{nick or uid} 连戳 {st.poke_count} 次", who=nick or uid)
            favor.on_poke(gid, uid)
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
            event.stop_event()
            return

        if st.poke_count == 2 and not pending_text:
            # 第二下：本地短句，别再调模型。实测模型这里会写 9 个字还带上
            # 半句解释，被拆条后连戳四下就变成三条消息，正是"刷屏疲劳"
            said = [t for t in list(st.recent_bot)[-2:] if t]
            picked = next((w for w in _POKE_SNARK if all(w not in t for t in said)), _POKE_SNARK[0])
            logger.info("[fish] 戳一戳第二下 <- %s(%s) 群 %s，本地短句：%s", nick, uid, gid, picked)
            self.log_event("poke", gid, f"{nick or uid} 连戳 2 次", who=nick or uid)
            favor.on_poke(gid, uid)
            st.recent_bot.append(picked)
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
            event.stop_event()
            yield event.plain_result(picked)
            return

        if is_admin:
            who_hint = "戳你的是你主人"
        else:
            who_hint = f"戳你的是群友「{nick}」"

        # 戳一戳直通加分，不进文本冷却——这是群友唯一不被模型误判的回血通道
        favor.on_poke(gid, uid)
        # 戳一戳等价于叫了一次人：顺手开追问窗口，否则他戳完想接着说还得再 @ 一次
        st.follow_uid = uid
        st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)
        st.follow_left = int(self.cfg.get("follow_max_turns", 5) or 0)
        await self._remember(gid, uid, f"（{nick} 戳了戳你）")
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"),
            bot_qq=self_id, group_id=gid, is_private=(gid == "private"),
        )
        # 拼 prompt 的逻辑放在 poke_context 里，两条分支才测得动
        user_prompt = poke_context.build_prompt(
            poke_info, who_hint,
            repeat=st.poke_count, last_said=list(st.recent_bot)[-2:],
        )
        # 记账同样要落到这次对话上：补回的那条用他的原话，纯手欠用戳的动作
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick,
            "text": pending_text or f"（{nick} 戳了戳你）",
            "at": False, "reason": "戳一戳·补回未回" if pending_text else "戳一戳·手欠",
        })
        logger.info("[fish] 戳一戳 <- %s(%s) 群 %s，待回消息：%s", nick, uid, gid,
                    (pending_text[:30] or "无"))
        # 先回戳再回话：两件事互不等待，戳要秒到，话照旧慢慢说
        self.log_event("poke", gid, f"{nick or uid} 戳了一下", who=nick or uid)
        if self.cfg.get("poke_back", True):
            await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
        yield event.request_llm(prompt=user_prompt, system_prompt=system_prompt,
                                contexts=[], tool_set=NO_TOOLS)

    # ---------------- prompt 拼装 ----------------
    async def _compose(self, gid, uid, nick, text, is_at, st: GroupState, is_private: bool = False,
                       self_id: str = "", vision_desc: str = "", media_hint: str = "",
                       social_hint: str = "",
                       cross_hint: str = ""):
        affect.tick(gid)
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), bot_qq=self_id,
            group_id=gid or "-", is_private=is_private,
        )

        # 记忆召回：按当前这句检索这个人在这个群的历史记忆
        memory_lines = []
        if gid and gid != "private":
            try:
                recalled = await asyncio.to_thread(
                    self.memory.recall, gid, uid, text,
                    int(self.cfg.get("memory_recall_topk", 2)))
                for item in recalled:
                    content = str(item.get("content") or "").strip()
                    # 刚说的那句也可能以近重形式被召回，别让模型看到自己刚说的话
                    if content and content not in text and text not in content:
                        memory_lines.append(content[:80])
            except Exception as e:
                logger.warning("[fish] 记忆召回失败: %s", e)

        # 心境：只给体感，不给数字（数字会被模型念出来）
        affect_parts = []
        memo = affect.prompt_memo(gid)
        if memo:
            affect_parts.append(memo)
        favor_memo = favor.prompt_memo(gid, uid) if not is_private else ""
        if favor_memo:
            affect_parts.append(favor_memo)
        care = affect.care_hint(gid)
        if care:
            affect_parts.append(care)
        if not is_private:
            variant = affect.style_variant()
            if variant:
                affect_parts.append(f"【这次换个说法】：{variant}")

        # 印象原子：去过重、衰减之后留下的几句话，比整句记忆耐看，也不会重复提同一件事
        try:
            atoms_raw = self._atoms_of(gid, uid)
            if atoms_raw:
                atoms = memory_atoms.apply_decay(atoms_raw)
                atom_text, _used = memory_atoms.render(atoms)
                if atom_text:
                    memory_lines.append(atom_text[:160])
                if atoms != atoms_raw:
                    self._atoms_all[f"{gid}:{uid}"] = atoms
                    self._save_atoms()
        except Exception as e:
            logger.debug("[fish] 印象原子渲染失败：%s", e)

        # 关系网：谁最近爱跟谁搭话，只给一句体感，不给统计表
        try:
            if gid and gid.isdigit():
                rel = cross_interaction.get_group_relations_summary(int(gid))
                if rel:
                    memory_lines.append(rel[:120])
        except Exception as e:
            logger.debug("[fish] 关系网摘要失败：%s", e)

        # 热梗：常驻核心梗 + 这句沾边的梗，调味用，命中不了就空着
        meme_hint = ""
        try:
            # group_id 必传：本群黑话是逐群挖的，传错群等于把别人家的梗塞给它
            meme_hint = self.memes.get_prompt_snippet(text or "", group_id=gid)
        except Exception as e:
            logger.debug("[fish] 热梗取词失败：%s", e)

        timeline = []
        size = int(self.cfg.get("timeline_size", 12))
        for m in list(st.msgs)[-(size + 1):-1]:
            timeline.append(f"{m['nick']}：{m['text']}")

        user_prompt = composer.build_user_prompt(
            timeline=timeline,
            latest_line=text,
            sender_nick=nick,
            sender_uid=uid,
            memory_lines=memory_lines,
            affect_parts=affect_parts,
            style_hint=((composer.style_samples_hint(gid) if gid != "private" else "")
                        + ("\n" + meme_hint if meme_hint else "")),
            anti_repeat_hint=composer.anti_repeat_hint(list(st.recent_bot)),
            games_kb=prompts.GAMES_KB if prompts.needs_games_kb(text) else "",
            is_at=is_at,
            is_private=is_private,
            vision_desc=vision_desc,
            media_hint=media_hint,
            social_hint=social_hint,
            cross_hint=cross_hint,
        )
        if self.cfg.get("debug_dump_prompt"):
            try:
                body = "=== system ===\n" + system_prompt + "\n\n=== user ===\n" + user_prompt
                (DATA_DIR / "last_prompt.txt").write_text(body, encoding="utf-8")
                # 每次留一份带时间戳的档：last_prompt 只留最后一次，回头看「心境/记忆到底进没进」
                # 就没证据了，尤其是要贴给别人的时候
                arch = DATA_DIR / "prompt_archive"
                arch.mkdir(exist_ok=True)
                (arch / f"{time.strftime('%Y%m%d-%H%M%S')}_{uid}.txt").write_text(body, encoding="utf-8")
                for old in sorted(arch.iterdir())[:-50]:
                    old.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("[fish] prompt 落盘失败: %s", e)
        logger.debug("[fish] system_prompt=%r\nuser_prompt=%r", system_prompt[:80], user_prompt[:200])
        return system_prompt, user_prompt

    # ---------------- 出口加工 ----------------
    @filter.on_decorating_result()
    async def on_decorate(self, event: AstrMessageEvent):
        meta = event.get_extra("fish_pending")
        if not meta:
            return  # 不是本插件产生的回复（例如 ab! 唤醒的 AstrBot 原生回复），一律不碰
        event.set_extra("fish_pending", None)

        result = event.get_result()
        if not result or not result.chain:
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if not text.strip():
            return

        gid = meta["gid"]
        st = self._state(gid)

        # 1. 剥表情标签：模型经常写 [MEME:嘲讽]，不剥就原样发进群
        clean, tag = meme_catalog.extract_tag(text)
        text = composer.sanitize_reply(clean or text)
        # 出口清洗：内部独白、动作旁白、Markdown 痕迹在这剥掉。
        # URL 和代码块会先变占位符再还原，联网搜到的内容不会被误删
        try:
            text2 = safety_guard.strip_internal_monologue(text)
            _changed, text2 = safety_guard.sanitize_outgoing_text(text2)
            if text2 and text2.strip():
                text = text2
        except Exception as e:
            logger.debug("[fish] 出口清洗失败：%s", e)
        if not text:  # 整句都在解释格式，被削空了：宁可回个嗯，也别发空串
            text = (clean or "").strip() or "嗯"

        # 2. 反模板：先削撞车的起手/收尾，再摘重复语气词、去复述式开场
        recent = [r for r in st.recent_bot if r]
        reason = anti_template.match(text, recent)
        if reason != "none":
            reshaped = anti_template.reshape(text, reason)
            if reshaped and reshaped != text:
                logger.info("[fish] 反模板 %s，削形: %r -> %r", reason, text[:24], reshaped[:24])
                text = reshaped
        text = anti_template.trim_particle(text, recent)
        text = anti_template.strip_echo_opener(text, meta["text"])
        text = text.strip() or text

        # 3. 拆条：太长就分成 2~3 条，剩下的放到 on_sent 里隔一会儿再发（像人连发）
        now = time.time()
        final = text
        parts = [final]
        if self.cfg.get("split_enabled", True):
            parts = splitter.split_reply(
                final,
                max_chars=int(self.cfg.get("split_max_chars", 60) or 60),
                max_parts=int(self.cfg.get("split_max_parts", 3) or 3),
            ) or [final]
        chain = [Plain(parts[0])]
        tail = parts[1:]

        # 5. 配图：过斗图闸，一回合只发一张；有尾巴时挪到最后一条后面，别插在中间打断话
        meme_b64 = None
        if tag and self.cfg.get("meme_enabled", True):
            ok_meme, why_not = self._meme_gate(gid, st, tag, now)
            if not ok_meme:
                logger.info("[fish] 表情包闸拦住（%s），这轮纯文字", why_not)
            else:
                meme_b64 = meme_catalog.pick_b64(tag, clean or text)
                if meme_b64:
                    st.last_meme_ts = now
                    st.recent_meme_tags.append(tag)
                    logger.info("[fish] 按标签 %s 配了一张表情包%s", tag,
                                "（排在拆条之后）" if tail else "")

        if meme_b64 and not tail:
            chain.append(Image.fromBase64(meme_b64))
        if tail:
            event.set_extra("fish_tail", {"gid": gid, "parts": tail, "meme": meme_b64})
            logger.info("[fish] 长回复拆成 %d 条（%s 字）：%s", len(parts), len(final),
                        " / ".join(p[:18] for p in parts))

        event.set_result(MessageEventResult(chain=chain))

        # 6. 记账：心境、记忆、反重复、接话节流
        st.recent_bot.append(final)
        st.last_bot_reply_ts = now
        st.reply_times.append(now)
        st.msgs_since_bot = 0
        affect.on_bot_spoke(gid)
        self.log_event("reply", gid, final, who=meta.get("nick", ""),
                       why=meta.get("reason", ""), meme=1 if meme_b64 else 0,
                       parts=len(parts))
        try:
            if gid != "private":
                self.memory.attach_reply(gid, meta["uid"], meta["text"], final)
        except Exception as e:
            logger.warning("[fish] 记忆写入失败: %s", e)

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        # 先把 extra 全取走再动手：event.send 会再走一遍 after_message_sent，
        # 不先清空的话补发的那几条会被当成新回复重新处理，变成无限自我复制
        tail = event.get_extra("fish_tail")
        event.set_extra("fish_tail", None)
        if not tail:
            return

        if tail:
            parts = tail.get("parts") or []
            gaps = splitter.plan_gaps(
                len(parts),
                float(self.cfg.get("split_gap_min", 0.6) or 0.6),
                float(self.cfg.get("split_gap_max", 1.5) or 1.5),
            )
            for i, part in enumerate(parts):
                gap = gaps[i] if i < len(gaps) else 0.8
                await asyncio.sleep(gap)
                try:
                    await event.send(MessageChain(chain=[Plain(part)]))
                    logger.info("[fish] 拆条第 %d/%d 条补发（间隔 %.1fs）：%s",
                                i + 2, len(parts) + 1, gap, part[:24])
                except Exception as e:
                    logger.warning("[fish] 补发失败: %s", e)
                    break
            meme = tail.get("meme")
            if meme:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(meme)]))
                    logger.info("[fish] 拆条后补发表情包")
                except Exception as e:
                    logger.warning("[fish] 表情包补发失败: %s", e)

