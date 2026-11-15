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

PLUGIN_DIR = Path(__file__).resolve().parent
FISH_CORE = PLUGIN_DIR / "fish_core"
DATA_DIR = PLUGIN_DIR / "data"

# 复用原项目的纯逻辑模块：用 path 直接加载，避免和别的插件的 affect/typo 同名串味
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
anti_template = _load("anti_template", "anti_template.py")
typo = _load("typo", "typo.py")
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
    "typo_prob": 0.06,
    "typo_correct_prob": 0.30,
    "meme_enabled": True,
    "timeline_size": 12,
    "history_keep": 24,
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
    # --- 别的机器人 ---
    "peer_bots_path": "",
    "peer_bots_name_hints": [],
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
    """原子写：心境/记忆状态都在进程里攒着，写一半崩了会留个空文件把状态吃没。"""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)
    os.replace(tmp, path)


# AstrBot 自带指令的白名单。插件必须对它们彻底让路：
# 否则「/help」会被当成一句闲聊回掉，框架的指令处理器永远轮不到

# 回戳的时间戳：同一个人 3 秒内只回戳一次，避免两边互戳成死循环
_POKE_BACK_TS: dict[str, float] = {}
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

        self.memory = fish_memory.FishMemoryEngine(str(DATA_DIR / "fish_memory.db"))
        # 心境状态落盘在同目录；原项目 attach 时故意不调 save，这里由 tick 触发
        affect.attach(writer=_write_json, path=str(DATA_DIR / "affect_state.json"))
        # style_learner 的池子路径写死在原项目的 data/ 下，指到插件自己的目录，别污染上游
        style_learner._POOL_PATH = str(DATA_DIR / "style_fewshot.json")
        # meme_catalog 用 _SCRIPT_DIR 拼相对路径，改指 fish_core，图库走 meme-pack 软链
        meme_catalog._SCRIPT_DIR = str(FISH_CORE)
        meme_catalog.CATALOG_FILE = str(MEME_PATH)

        self._groups: dict[str, GroupState] = {}

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
        logger.info("[fish] 拆条 %s（%d 字起拆，最多 %d 条）；两步生成 %s；看图 %s（%s）",
                    "开" if self.cfg.get("split_enabled", True) else "关",
                    int(self.cfg.get("split_max_chars", 60) or 60),
                    int(self.cfg.get("split_max_parts", 3) or 3),
                    "开" if self.cfg.get("social_read_enabled", True) else "关",
                    "开" if self.cfg.get("vision_enabled", True) else "关",
                    self.cfg.get("vision_model", ""))

    # ---------------- 基础工具 ----------------
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
                return comp.text or ""
            if isinstance(comp, (At, AtAll)):
                continue
        return ""

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
            if name in ("image", "face", "record"):
                has_image = True
            elif name in ("json", "card", "file", "forward"):
                has_media = True
        return has_image, has_media

    def _group_allowed(self, gid: str) -> bool:
        """生效群白名单。

        默认空列表 = 谁都不生效：这套插件会主动插话，装完就闷声往真群里发消息
        是最坏的结果。要上线必须把群号写进 enabled_groups，或者填 ["*"] 全开。
        """
        groups = self.cfg.get("enabled_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        groups = [str(g).strip() for g in groups]
        if "*" in groups:
            return True
        return str(gid) in groups

    # ---------------- 主流程：群消息 ----------------
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return

        gid = self._gid_of(event)
        if not self._group_allowed(gid):
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
        wake_prefix = str(self.cfg.get("wake_prefix") or "")
        if wake_prefix and raw_first.startswith(wake_prefix):
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

        # 时间线与在线学习都要「所有」群消息，包括没人叫它的那些
        if text or has_image:
            st.msgs.append({"uid": uid, "nick": nick, "text": text or "[图片]", "ts": now})
            if gid:
                affect.on_group_message(gid, text, private=False)
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
            # 只有 @ 没有正文：[images] 之类也不该拖住事件
            if not is_at:
                event.stop_event()
                return

        # 框架层的 ab! 唤醒词已关掉（不然那条路走原生流程、没有人设）。
        # 但用户的手感还在：手打前缀就是明着叫它，按「被叫」处理，前缀本身不喂给模型
        forced = False
        prefix = str(self.cfg.get("wake_prefix") or "ab!")
        if text and text.lstrip().lower().startswith(prefix.lower()):
            text = text.lstrip()[len(prefix):].lstrip()
            forced = True

        # ---------------- 该不该开口 ----------------
        if is_at or forced:
            reason = "被@" if is_at else "手打前缀"
            if not self.cfg.get("reply_when_at", True):
                await self._remember(gid, uid, text)
                event.stop_event()
                return
        else:
            ok, reason = self._should_interject(gid, st, text, now, has_image, has_media)
            if not ok:
                # 不接话也要做自动感知（原项目对每条群消息都记，不只记回过的）
                logger.info("[fish] 群 %s 不接（%s）：%s", gid, reason, text[:30])
                await self._remember(gid, uid, text)
                event.stop_event()
                return

        # ---------------- 第一步：这帮人在聊什么、我该不该接；同时看图 ----------------
        # 两件事互不依赖，并发跑。串行的话识图那十几秒会整个叠在 judge 后面
        social_hint = ""
        imgs = [c for c in event.message_obj.message if isinstance(c, Image)] \
            if (has_image and self.cfg.get("vision_enabled", True)) else []
        verdict, vision_desc = await asyncio.gather(
            self._social_read(gid, st, text, nick, is_at or forced),
            vision.from_components(imgs, self.cfg, logger=logger) if imgs else _noop(),
        )
        if vision_desc:
            logger.info("[fish] 群 %s 图片描述：%s", gid, vision_desc[:80])
        if verdict and not verdict.get("join"):
            if not is_at:
                # 判「不接」就到此为止，主模型一次都不调（这是这一步省下的主要延迟）
                logger.info("[fish] 群 %s social_read 判不接（话题=%r），不调主模型：%s",
                            gid, verdict.get("topic", ""), text[:30])
                await self._remember(gid, uid, text)
                event.stop_event()
                return
            # 被直接点名的没法装死不回，记一笔继续走
            logger.info("[fish] 群 %s social_read 想判不接，但这句是 @ 你，仍然回", gid)
        elif verdict:
            social_hint = social_read.hint_line(verdict)

        # 顺序要紧：先召回记忆再落这一条。反过来的话当前这句会被自己的召回捡回来，
        # prompt 里就出现「你还记得我明天要干嘛吗」这种自己引用自己的鬼话
        cross_hint = self.cross.hint(gid, int(self.cfg.get("cross_group_recall", 2) or 2)) \
            if self.cfg.get("cross_group_enabled", True) else ""
        system_prompt, user_prompt = await self._compose(
            gid, uid, nick, text, is_at, st, self_id=self_id,
            vision_desc=vision_desc, social_hint=social_hint, cross_hint=cross_hint,
        )
        await self._remember(gid, uid, text)
        # 立刻占住冷却位：模型要跑十几秒，这期间后面几条群消息还在陆续进来，
        # 等回复真正发出去才记账的话，同一波刷屏会被判断成「还没说过话」连着接好几次
        st.last_bot_reply_ts = now
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick, "text": text, "at": is_at,
        })
        logger.info("[fish] 群 %s %s(%s) 决定回（%s）：%s", gid, nick, uid, reason, text[:40])
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
            await asyncio.to_thread(self.memory.record_turn, gid, uid, text, "")
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
        threshold = interject_score.interject_threshold(float(self.cfg.get("interject_desire", 0.30)))
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
        if gid != "private" and not self._group_allowed(gid):
            event.stop_event()
            return

        uid = str(event.get_sender_id())
        nick = event.get_sender_name() or uid
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        if is_admin:
            role_hint = "是你主人戳的，可以黏人、撒娇、要零食、假装被戳痛"
        else:
            role_hint = f"是群友「{nick}」戳的，随口回一句，可以嫌他手欠"

        await self._remember(gid, uid, f"（{nick} 戳了戳你）")
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"),
            bot_qq=self_id, group_id=gid, is_private=(gid == "private"),
        )
        user_prompt = (
            f"「{nick}」戳了戳你。{role_hint}。\n"
            "随口回一句，两到十个字，像手被人戳了一下那样反应：嫌手欠、问干嘛、要钱、装疼都行。\n"
            "别说「有什么需要帮忙」，别写动作括号，别用客套话。只说这一句："
        )
        logger.info("[fish] 戳一戳 <- %s(%s) 群 %s", nick, uid, gid)
        # 先回戳再回话：两件事互不等待，戳要秒到，话照旧慢慢说
        if self.cfg.get("poke_back", True):
            await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
        yield event.request_llm(prompt=user_prompt, system_prompt=system_prompt,
                                contexts=[], tool_set=NO_TOOLS)

    # ---------------- prompt 拼装 ----------------
    async def _compose(self, gid, uid, nick, text, is_at, st: GroupState, is_private: bool = False,
                       self_id: str = "", vision_desc: str = "", social_hint: str = "",
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
        care = affect.care_hint(gid)
        if care:
            affect_parts.append(care)
        if not is_private:
            variant = affect.style_variant()
            if variant:
                affect_parts.append(f"【这次换个说法】：{variant}")

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
            style_hint=composer.style_samples_hint(gid) if gid != "private" else "",
            anti_repeat_hint=composer.anti_repeat_hint(list(st.recent_bot)),
            games_kb=prompts.GAMES_KB if prompts.needs_games_kb(text) else "",
            is_at=is_at,
            is_private=is_private,
            vision_desc=vision_desc,
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

        # 3. 偶尔手滑（概率很低，只改长句，不碰链接/@/数字）
        final = text
        typo_result = None
        if self.cfg.get("typo_prob", 0):
            typo_result = typo.maybe_typo(text, prob=float(self.cfg["typo_prob"]))
            if typo_result:
                final = typo_result.text

        # 4. 拆条：太长就分成 2~3 条，剩下的放到 on_sent 里隔一会儿再发（像人连发）
        now = time.time()
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
        st.msgs_since_bot = 0
        affect.on_bot_spoke(gid)
        try:
            if gid != "private":
                self.memory.record_turn(gid, meta["uid"], meta["text"], final)
        except Exception as e:
            logger.warning("[fish] 记忆写入失败: %s", e)
        if typo_result and typo.should_correct(float(self.cfg.get("typo_correct_prob", 0.3))):
            event.set_extra("fish_correction", typo.correction_line(typo_result))

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        # 先把 extra 全取走再动手：event.send 会再走一遍 after_message_sent，
        # 不先清空的话补发的那几条会被当成新回复重新处理，变成无限自我复制
        tail = event.get_extra("fish_tail")
        correction = event.get_extra("fish_correction")
        event.set_extra("fish_tail", None)
        event.set_extra("fish_correction", None)
        if not tail and not correction:
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

        if correction:
            await asyncio.sleep(0.6)
            try:
                await event.send(MessageChain().message(correction))
                logger.info("[fish] 补了一句错字纠正：%s", correction)
            except Exception as e:
                logger.warning("[fish] 纠正消息发送失败: %s", e)
