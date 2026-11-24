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

from __future__ import annotations

import asyncio
import base64
import functools
from difflib import SequenceMatcher
import importlib.util

import aiohttp
import json
import os
import random
import re
import socket
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

from .fish_core.kernel import *  # noqa: F401,F403
from .fish_core.runtime import RuntimeMixin
from .fish_core.admin import AdminMixin
from .fish_core.media import MediaMixin
from .fish_core.decision import DecisionMixin
from .fish_core.memory import MemoryMixin
from .fish_core.send import SendMixin
from .fish_core.prompt import PromptMixin
from .fish_core.respond import RespondMixin
from .fish_core.poke import PokeMixin
from .fish_core.commands import CommandsMixin


@register("astrbot_plugin_fish", "life", "把「小鲸鱼」自研 QQ 机器人的对话能力搬进 AstrBot：人设/心境/记忆/群表达/反模板/表情包/接话时机", "1.0.0", "https://github.com/lifefuck/qq-deepseek-realistic-bot")
class FishPlugin(RuntimeMixin, AdminMixin, MediaMixin, DecisionMixin, MemoryMixin, SendMixin, PromptMixin, RespondMixin, PokeMixin, CommandsMixin, Star):
    """事件分发层。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 面板上 78 个调优项收进了 advanced_* 五个对象节，这里摊平一次，
        # 上层照旧读扁平键；老配置里的扁平键还在，升级不用迁移
        self.cfg = {**DEFAULTS, **flatten_conf(config)}
        # 同一个标签 10 分钟内只搜一次：pixiv 有频率限制，被 429 之后整段时间都发不出图
        self._pixiv_cd = pixiv.Cooldown(600)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 刹车状态落盘：/off 的静默和机器人自身禁言期都得记住。
        # 只放内存的话，重启一次「我刚说全关了」就全忘，群里照旧开麦
        self._mute_path = str(DATA_DIR / "group_muted.json")
        self._mute_state: dict = {}
        self._load_mute_state()
        self._ban_expire: dict = {}
        self._ban_checked: dict = {}
        # 消息去重：NapCat 断线重连会补投历史，同一条 message_id 走两遍就是两条回答
        self._mids: dict = {}
        # 群秩序巡检的限流：每个群上次上报的时间
        self._order_last: dict = {}
        # 点歌服务懒加载，第一次有人点歌才建 aiohttp 会话
        self._song_svc = None

        self.memory = fish_memory.FishMemoryEngine(str(DATA_DIR / "fish_memory.db"))
        # 启动时清一次：流水账留 45 天，软删记忆留 30 天，不然库只涨不落
        try:
            cleaned = self.memory.prune()
            # 低置信度记忆按「多久没人再印证」慢慢降权：留在库里但不再冒头，
            # 降到 0.2 以下才软删（留行可人工捞回）
            _decayed = self.memory.decay_low_confidence(
                days=float(self.cfg.get("memory_conf_decay_days", 30) or 30))
            if any(cleaned.values()) or _decayed:
                logger.info("[fish] 启动清理：流水 %d 行、软删记忆 %d 条、标签 %d 条、"
                            "目的地 %d 行、降权 %d 条",
                            cleaned["turn_logs"], cleaned["memories"], cleaned["tags"],
                            cleaned.get("said", 0), _decayed)
        except Exception as e:
            logger.warning("[fish] 启动清理失败：%s", e)
        # 心境状态落盘在同目录；原项目 attach 时故意不调 save，这里由 tick 触发
        affect.attach(writer=_write_json, path=str(DATA_DIR / "affect_state.json"))
        # 好感度：每群每人一个长期分，和心境分开存，别互相污染
        favor.attach(writer=_write_json, path=str(DATA_DIR / "favor_state.json"))
        # 调侃许可：按人按群记「能不能被挤对」「还能不能自嘲」，ban 落盘不清零
        try:
            tease.attach(writer=_write_json, path=str(DATA_DIR / "tease_license.json"))
        except Exception as e:
            logger.warning("[fish] 调侃许可加载失败：%s", e)
        # 主人在所有群锁满分：他骂模型、骂程序、骂鱼都是日常，好感分跟着掉
        # 只会让最熟的人被冷处理。配置 owner_favor_lock 可以改这个数
        _lock_owner = str(self.cfg.get("owner_qq") or "").strip()
        if _lock_owner:
            favor.set_locked(_lock_owner, float(self.cfg.get("owner_favor_lock", 100) or 100))
            favor.save(force=True)
        # style_learner 的池子路径写死在原项目的 data/ 下，指到插件自己的目录，别污染上游
        style_learner._POOL_PATH = str(DATA_DIR / "style_fewshot.json")
        # meme_catalog 用 _SCRIPT_DIR 拼相对路径，改指 fish_core，图库走 meme-pack 软链
        meme_catalog._SCRIPT_DIR = str(FISH_CORE)
        # 日志再落一份文件：journald 只留得住几分钟，出问题回头查就没证据了
        try:
            self._attach_file_log(DATA_DIR / "astrbot.log")
        except Exception:
            logger.warning("[fish] 文件日志挂载失败，只能看 journal", exc_info=True)
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
        # 收件人判定表的别名表：只存「真实 At 昵称 / 群名片 → uid」，按群隔离。
        # 不从聊天里猜外号——猜错一次就是喊着别人的名字回话
        try:
            addressee.attach(str(DATA_DIR / "aliases.json"))
        except Exception as e:
            logger.warning("[fish] 别名表载入失败：%s", e)
        # 群友 @ 关系网：只记近期互动，落盘前剪掉 6 小时前的记录，不会无限涨
        self.relations_path = str(DATA_DIR / "relations.json")
        try:
            cross_interaction.load_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网载入失败：%s", e)
        # 印象原子：和 fish_memory 的句子分开存，自己的去重/衰减逻辑
        self.atoms_path = DATA_DIR / "atoms.json"
        # 最近一次给每个群发的 B 站视频，用于「播不了就换 H.264 重发」
        # 每群上次下载视频的时间，防止连贴链接时并发下好几份
        self._atoms_all = self._load_atoms()
        self._distill_ts: dict[str, float] = {}   # 印象提炼节流：uid -> 上次调用时间
        # 本轮消息的出处（原文 id / 说给谁），按群存一份。_remember 有十来个调用点，
        # 逐个加参数迟早漏一个，统一从这里取
        self._mem_turn: dict[str, dict] = {}
        # 上一轮喂给模型的记忆，纠正检测要拿它比对（按群+人存，见 fish_memory.put_recalled）
        # 键是 (群号, QQ号)，值是 (时间戳, 召回条目)：纠正要认准谁的记忆，还要限时
        self._last_recalled: dict[tuple, tuple] = {}
        # 同一张图只识一次：时间线回填和「要不要回这条」会各要一次描述
        self._vision_jobs: dict[str, asyncio.Task] = {}
        # 联网类能力共用一个 session，别每句话新建连接
        self._sess = None

        self._groups: dict[str, GroupState] = {}
        # 最近对话落地一份：重启不该等于失忆
        self._ctx_path = DATA_DIR / "recent_context.json"
        self._ctx_saved_at = 0.0
        self._load_recent_context()
        # 预热视觉链路：第一次请求要付 DNS+TLS+代理握手，实测 5~6 秒，
        # 正好压着 6 秒超时线，群里第一张图必挂。开机先空跑一张，把连接焐热
        try:
            asyncio.create_task(self._warm_vision())
        except RuntimeError:
            logger.debug("[fish] 没有运行中的事件循环，跳过视觉预热")

        # 控制台用的最近事件环形缓冲：老面板那栏「实时交互日志」搬到面板里，
        # 不再自己维护一份文件，进程重启就重来，够看就行
        self._events: deque = deque(maxlen=cfg_int(self.cfg, "panel_event_buffer", 200))
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

        # 看图走框架的视觉 provider（data/cmd_config.json 里的 gemini-vision），
        # 这样插件的识图逻辑只剩「取字节 + 兜底」，模型换代/换家都在面板上配，不用改代码
        try:
            vision.set_framework_caller(self._vision_via_framework)
            # 听觉复用视觉那套 key/连接池/降级链：语音和图片本来就是同一个模型在处理
            hearing.configure(vision)
        except Exception as e:
            logger.warning("[fish] 框架视觉通道挂不上（仍走直连）：%s: %s", type(e).__name__, e)

        logger.info("[fish] 拆条 %s（%d 字起拆，最多 %d 条）；两步生成 %s；看图 %s（%s）",
                    "开" if self.cfg.get("split_enabled", True) else "关",
                    cfg_int(self.cfg, "split_max_chars", 45),
                    cfg_int(self.cfg, "split_max_parts", 3),
                    "开" if self.cfg.get("social_read_enabled", True) else "关",
                    "开" if self.cfg.get("vision_enabled", True) else "关",
                    # 识图实际先走 WB 池，日志里打真实顺序，别让人以为还在用 Gemini
                    (",".join(self.cfg.get("vision_wb_models") or [])
                     if self.cfg.get("vision_wb_enable", True) and self.cfg.get("vision_wb_base")
                     else self.cfg.get("vision_model", "")))

        # 记忆库是这次迁移唯一「丢了就没了」的资产：启动先备一次，
        # 之后每小时醒来比一次心跳，真备份与否由 min_interval 决定
        # 备份默认落在插件目录之外：插件重装/删目录时备份不能被一起带走
        self._backup_dir = str(self.cfg.get("backup_dir") or (Path.home() / ".astrbot" / "backups"))
        self._backup_task = None
        # 后台任务（拆条补发、上报、预热）统一存引用，否则可能被 GC 掉，
        # 异常也只会打「Task exception was never retrieved」，不进日志
        self._bg_tasks = set()
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
    @filter.event_message_type(EventMessageType.ALL)
    @_handler_guard
    async def on_at_only(self, event: AstrMessageEvent):
        """纯 @ 无文字的兜底入口。

        实测：群里发一条只有 @、没有文字的群消息，on_group 根本不触发
        （journal 里没有任何 [fish] 行，回话来自 AstrBot 原生流程）。
        后果不只是绕过开口判定：不记时间线、不记 turn log、不受密度闸和冷却约束。

        这里用 ALL 类型的独立入口接住它，再交给 on_group 走同一套流程：
        收消息 → 记时间线 → decide() 判定 → 该说才调模型。
        只要文本非空就不插手（避免和 on_group 抢同一条消息）。
        """
        if not self.cfg.get("enabled", True):
            return
        try:
            segs = list(getattr(event.message_obj, "message", []) or [])
            if not segs:
                return
            if any(type(c).__name__ in ("Poke", "Video", "File") for c in segs):
                return
            self_id = str(event.get_self_id())
            has_at_me = any(
                type(c).__name__ in ("At", "AtAll")
                and str(getattr(c, "qq", "")) in (self_id, "all")
                for c in segs
            )
            if not has_at_me:
                return
            if (event.message_str or "").strip():
                return
            logger.info("[fish] 纯@无文字消息进入兜底入口：%s <- %s",
                        event.get_group_id() or "private", event.get_sender_id())
            async for r in self.on_group(event):
                yield r
        except Exception as e:
            logger.error("[fish] on_at_only 异常：%s: %s", type(e).__name__, e)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @_handler_guard
    async def on_group(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return

        gid = self._gid_of(event)
        # 心跳：先确认「消息到底进没进来」。群里没反应时，这一行是分界线——
        # 有它 = 进到插件了，问题在后面；没它 = NapCat/适配器/白名单，跟对话逻辑无关
        logger.info("[fish] 收到群消息 %s <- %s：%s", gid, event.get_sender_id(),
                    (event.message_str or "")[:30] or "<非文本>")
        if not self._group_allowed(gid):
            # 白名单外的群一个字都不说。但被 @ 时必须拦下来：
            # 只 return 不 stop，AstrBot 自己的默认 LLM 会顶替鱼回话，
            # 等于白名单形同虚设（而且口径完全不是鱼）
            if getattr(event, "is_at_or_wake_command", False):
                event.stop_event()
            return
        if self._is_muted(gid):
            # /off 之后群里说什么都当没听见：连时间线都不记，静默要真的静。
            # 这里必须 stop_event——不拦的话 AstrBot 自己的默认 LLM 还会接 @
            # 但解除静默要留一条缝：cmd_on 是后注册的独立 handler，这里一 stop，
            # star_request 循环就 break，cmd_on 永远轮不到 ⇒ 静默不可逆。
            async for r in self.muted_branch(event, gid):
                yield r
            event.stop_event()
            return
        # 戳一戳自己接。不能只 return：on_group 排在 on_poke 前面，
        # return 之后如果 on_poke 也因为「目标 id 对不上」提前返回，
        # 这条空消息就没人拦，原生 LLM 会当成普通消息自由发挥
        if any(isinstance(c, Poke) for c in (event.message_obj.message or [])):
            async for r in self.on_poke(event):
                yield r
            event.stop_event()
            return
        uid = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        if uid == self_id:
            # 自己发的消息（含别的插件代发）：不入时间线，免得学自己说话
            return

        # 去重必须在动时间线之前：NapCat 断线重连会把最近的历史重投一遍，
        # 同一条走两遍就是两次模型调用、两条不同的回答（群里「答两遍」的来源）
        # 键里必须带群号：NapCat 重连后序号会重排，两个群撞同一个 id 时
        # 会把后到的真消息当重复静默丢掉（日志还写「重复投递」，排查会跑偏）
        _mid = getattr(event.message_obj, "message_id", "") or ""
        # 没有 message_id 时不能拼成「群号:」当作 key：那样 180 秒内该群所有消息
        # 都会被判成重复、直接 stop_event 丢掉。退化成内容指纹：群 + 发送人 + 正文，
        # 重连补投同一条能挡住，同一人正常连发不同内容不受影响
        if not _mid:
            try:
                _sender = str(event.get_sender_id() or "")
            except Exception:
                _sender = ""
            _mid = "h|%s|%s|%s" % (gid, _sender, (getattr(event, "message_str", "") or "")[:80])
        if self._seen_mid("%s:%s" % (gid, _mid)):
            logger.info("[fish] 群 %s 收到重复投递的消息，忽略", gid)
            event.stop_event()
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

        # 视频：看图链路只吃 Image，视频到这儿就是「这行字是空的」，
        # 然后照样走接话评分（0.54）被判「接」，于是对着一个看不见的视频瞎点评。
        # 没画面就别装看见：只有视频 → 不进时间线、不接；视频配字 → 只聊字
        _unseen = ""
        try:
            for _c in (event.message_obj.message or []):
                _cn = type(_c).__name__
                # 只认视频和文件；语音有转文字通路，不能当「看不见」拦下来
                if _cn in ("Video", "File"):
                    _unseen = "视频" if _cn == "Video" else "文件"
                    break
        except Exception:
            _unseen = ""
        if _unseen and not text:
            logger.info("[fish] 群 %s 收到%s但没配字，看不见就不接", gid, _unseen)
            await self._remember(gid, uid, "[发了个%s]" % _unseen)
            event.stop_event()
            return

        if _is_builtin_cmd(text):
            logger.debug("[fish] 群 %s 让路给自带指令：%s", gid, text[:20])
            return
        # 写成字符串时不能直接 tuple()：tuple("ab!") 是 ('a','b','!')，
        # 于是任何以 a/b/! 开头的消息都被当面板指令跳过
        _pfx = self.cfg.get("command_prefixes") or ()
        if isinstance(_pfx, str):
            _pfx = [p.strip() for p in re.split(r"[,，\s]+", _pfx) if p.strip()]
        prefixes = tuple(str(p) for p in _pfx)
        if text.startswith(prefixes):
            # 云崽/面板指令归别的插件，原项目铁律是静默。这里只「不接话」，
            # 不能 stop_event——那会把下游别的插件的同名指令一起掐死
            logger.debug("[fish] 群 %s 面板指令前缀，跳过：%s", gid, text[:20])
            return

        # 引用/合并转发的内容得单独捞：框架只给了 id 和（引用场景下的）原文，
        # 不捞就等于把「这条你听听」的上下文整段丢掉
        quote_ctx = ""
        if any(type(c).__name__.lower() in ("reply", "forward", "nodes", "node")
               for c in (event.message_obj.message or [])):
            try:
                quote_ctx = await self._quote_context(event)
            except Exception as e:
                logger.debug("[fish] 取引用内容失败：%s", e)
        # 合并转发喂进来的是 OCR 式转述：鱼读得懂一半、读不懂另一大半，
        # 读不懂就只能评一句「这记录我看了两遍还是没看出在讲啥」。没人叫就别接
        _has_forward = any(type(c).__name__.lower() in ("forward", "nodes", "node")
                           for c in (event.message_obj.message or []))

        nick = event.get_sender_name() or uid
        # 别的机器人：不接、不进时间线、不占插话机会。
        # 它的播报被当成人话去接、或者跟它对吵刷屏，都是事故；名单见 data/peer_bots.json
        if peer_bots.is_peer_bot(uid, nick, self._peer_set(),
                                 self.cfg.get("peer_bots_name_hints") or ()):
            logger.info("[fish] 群 %s 忽略机器人消息：%s(%s) %s", gid, nick, uid, text[:20])
            # 只 return 不 stop 的话，对方机器人 @ 鱼时框架会拿默认人格再答一遍，
            # 群里就冒出一条不是它说的回复，紧接着两边对吵刷屏。这里明确掐掉。
            event.stop_event()
            return

        st = self._state(gid)
        # 版本号：每进一条群消息 +1。打字延迟醒来只比对这一个数，
        # 就知道「等这几秒里群里有没有新动静」，不用去 diff 整个对话流
        st.msg_ver = int(getattr(st, "msg_ver", 0) or 0) + 1
        now = time.time()
        has_image, has_media = self._has_media(event)
        is_at = self._is_at_bot(event)
        # 指向信号：@ 了谁、引用的是谁、引用的是不是我。
        # 以前只有一个 bool 概括「@了别人」，判定里塞的是假 uid，引用场景整个看不见
        at_targets = self._at_targets(event)
        reply_to_uid = self._reply_to_uid(event)
        is_quote_bot = self._is_quote_bot(event)
        # 别名表的原料：At 段带出来的真实昵称 + 群名片。只认这两样，别的一概不学
        at_named = tuple((str(getattr(c, "qq", "") or ""), getattr(c, "name", "") or "")
                         for c in event.message_obj.message
                         if type(c).__name__.lower() == "at")
        _sender = getattr(event.message_obj, "sender", None)
        sender_alias = str(getattr(_sender, "card", "") or getattr(_sender, "nickname", "") or "")
        # 语音单独拎出来：有它就能「听见」，纯语音消息也就不再是空消息
        recs = [c for c in event.message_obj.message
                if type(c).__name__.lower() == "record"] \
            if self.cfg.get("hearing_enabled", True) else []

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
            # 引用也是一次定向互动：只记 @ 的话，关系网里看不到「这句是顺着谁说的」
            try:
                _rc = next((c for c in event.message_obj.message
                            if type(c).__name__.lower() == "reply"), None)
                _rc_uid = str(getattr(_rc, "sender_id", "") or "") if _rc is not None else ""
                if _rc_uid and _rc_uid != "0":
                    cross_interaction.record_interaction(
                        int(gid), int(uid) if uid.isdigit() else 0, nick,
                        int(_rc_uid), getattr(_rc, "sender_nickname", "") or "",
                        self.relations_path)
            except Exception as e:
                logger.debug("[fish] 关系网记录（引用）失败：%s", e)

        # 时间线与在线学习都要「所有」群消息，包括没人叫它的那些
        # 纯语音也得进时间线：只认 text/图片的话，模型上下文里那条转写就成了
        # 没有前文的「凭空出现」，判官还会按「这人什么都没说」判
        if text or has_image or recs:
            # 文字配图是最常见的发法（「这啥」「看这个」+ 图）：只留文字等于把图吞了
            if text and has_image:
                _shown = f"{text} [图片]"
            else:
                _shown = text or ("[图片]" if has_image else "[语音]")
            _msg_entry = {"uid": uid, "nick": nick, "text": _shown, "ts": now}
            _dlg_entry = {"who": nick, "text": _shown, "ts": now,
                          "me": False, "uid": uid}
            st.msgs.append(_msg_entry)
            st.dialogue.append(_dlg_entry)
            # 同一句也压进「只增不改」的上文：下一轮它就是缓存前缀的一部分
            self._push_ctx(st, "user", f"{nick}：{_shown}")
            # 记下这条上文的确切长相：_ctx_of 靠它把「本轮这句」从上下文里弹掉。
            # 以前传的是出口那段被改写过的 text（加了视觉描述、媒体提示），
            # 跟压进去的 _shown 对不上就弹不掉，图和语音那一轮这句会被喂两遍
            event.set_extra("fish_ctxline", f"{nick}：{_shown}")
            # 图不能只留个 [图片] 占位：模型看得见「有人发了图」却拿不到内容，
            # 后面聊到就只能瞎编（实测「一堆截图我这也看不清」就是这么来的）。
            # 这里后台识图，识别完把描述写回时间线，代价是一条消息一次视觉调用
            if has_image and self.cfg.get("vision_enabled", True):
                asyncio.create_task(
                    self._backfill_image_desc(gid, event, _msg_entry, _dlg_entry,
                                              ctx_line=f"{nick}：{_shown}"))
            if now - getattr(self, "_ctx_saved_at", 0.0) > 20:
                self._ctx_saved_at = now
                self._save_recent_context()
            if gid:
                affect.on_group_message(gid, text, private=False, uid=uid, nick=nick)
                # 好感度走本地规则，不吃 token；同一人两分钟内只算一次
                try:
                    before = favor.score(gid, uid)
                    after = favor.on_message(gid, uid, text)
                    # 好感涨跌顺手喂心境：被夸语气会软、被骂会掉脸。
                    # affect.on_favor_delta 搬过来之后一直没人调，是条死链路
                    if abs(after - before) >= 1:
                        affect.on_favor_delta(gid, after - before, text)
                except Exception as e:
                    logger.debug("[fish] 好感/心境联动失败：%s", e)
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

        # 想听歌就直说，不用敲 /搜索歌曲：交给点歌插件发语音，自己不出声
        if self.cfg.get("natural_song", True) and text:
            song_res = await self._try_song_request(event, gid, text)
            if song_res in ("ok", "notfound", "fail"):
                # 三种结果分开说：以前搜不到和下载失败共用一句
                # 「搜到了但弄不下来」，搜不到时这句是假话
                if song_res == "ok":
                    # 歌已经发出去了，回一句就够，别学客服播报
                    line = random.choice(("喏", "放这了", "来了", "听着"))
                    self._record_spoke(gid, uid, line, reason="点歌", nick=nick)
                elif song_res == "notfound":
                    line = random.choice(("没搜到这首，歌名再说准点",
                                          "站上没有这首，换个名字试试"))
                    self._record_spoke(gid, uid, line, reason="点歌没搜到", nick=nick)
                else:
                    line = random.choice(("找到了但弄不下来，换一首吧",
                                          "这首拉不下来，换一个"))
                    self._record_spoke(gid, uid, line, reason="点歌失败", nick=nick)
                yield event.plain_result(line)
                event.stop_event()
                return

        # 说要告状就直接私发给接收人，自己不接话。
        # 只认关键词太蠢：群里聊「它会自动告状」也被当成请求转过去（实测就是这样）。
        # 现在三层：关键词初筛 → 必须是对着它说 → 模型确认这是「让我去告状」而不是在讨论功能
        if (self.cfg.get("natural_tattle", True) and text and _is_tattle_request(text)
                and (is_at
                     # 这里不能引用下面才赋值的 is_follow（会 UnboundLocalError，
                     # 非 @ 的告状请求全崩在入口），就地算一遍追问窗口
                     or (uid == st.follow_uid and now < st.follow_until and st.follow_left > 0)
                     or str(uid) == str(self.cfg.get("owner_qq") or ""))):
            asked = await self._tattle_intent(text, nick)
            if asked is None:
                # 判官挂了就按原样转，宁可多转一次，也别让人喊了没人管
                asked = True
            if asked and await self._try_tattle(event, text):
                event.stop_event()
                return
            elif not asked:
                logger.info("[fish] 群 %s 提到了告状，但看着是在聊功能不是求我办事，不转", gid)

        if not text and not has_image and not recs and not quote_ctx:
            if is_at:
                # 只是 @ 一下没说话。以前这里直接本地应一声就 return，
                # 判定对象永远看不到这条路——「任何路径都要过开口判定」的第一个漏洞就是它。
                # 现在先判：说不说归 decide()，说什么降级成本地应声（为一句「？」烧模型不值，
                # 模型还会顺着空白编出客服腔）。
                from .fish_core import speak_decision as _sd
                _replied = (now - float((getattr(st, "bare_at_by", None) or {}).get(str(uid), 0.0) or 0.0)) <= 45
                _bare_sig = self._sd_signals(
                    gid, st, now, is_at=True, empty=True, uid=str(uid), nick=nick,
                    text="<裸@>", replied_recently=_replied,
                )
                _bare_dec = _sd.decide(_bare_sig, self.cfg)
                logger.info("[fish][sd] 群 %s 裸@ %s", gid, _bare_dec.log_line())
                if not _bare_dec.speak:
                    logger.info("[fish] 群 %s 裸@不接（%s）", gid, _bare_dec.reason)
                    self.log_event("skip", gid, "<裸@>", who=nick, why=_bare_dec.reason)
                    await self._remember(gid, uid, "")
                    event.stop_event()
                    return
                st.last_bare_at_ts = now
                st.bare_at_by[str(uid)] = now
                yield event.plain_result(random.choice(["？", "咋了", "在呢", "你说"]))
                event.stop_event()
                return
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
                    self._record_spoke(gid, uid, f"{action['text']} [斗图]",
                                       reason="斗图", nick=nick)
                except Exception as e:
                    logger.warning("[fish] 斗图发送失败：%s", e)
                event.stop_event()
                return

        # ---------------- 免@追问窗口 ----------------
        # 窗口的开启推迟到「这条真的要回」之后（见下面的 _claim 处）。
        # 早开的话 reply_when_at=false 也会开出窗口，后续不 @ 的发言照样被接
        # ---------------- 问好感度：本地报真实分 ----------------
        # 线上实测把这条交给模型，它会编成「系统提示：当前好感度 233，
        # 已解锁「炖鱼」隐藏剧情，你想刷到多少」——数字对不上，人设也塌了
        if is_at and "好感" in text and any(k in text for k in ("多少", "几", "查", "看", "显示", "值", "分")):
            _v = favor.score(gid, uid)
            line = random.choice([
                f"你现在 {_v:.0f} 分，{favor.tier(_v)}",
                f"查这个干嘛，{_v:.0f} 分，{favor.tier(_v)}",
                f"{_v:.0f} 分，{favor.tier(_v)}——问这个是想刷还是想掉",
            ])
            logger.info("[fish] 好感度查询 <- %s(%s) 群 %s：%.0f", nick, uid, gid, _v)
            self.log_event("reply", gid, line, who=nick, why="好感度查询", meme=0, parts=1)
            st.recent_bot.append(line)
            st.last_bot_reply_ts = now
            st.reply_times.append(now)
            event.stop_event()
            yield event.plain_result(line)
            return

        # @ 了别人就不算「接着跟我说」：追问窗口是按「我刚说过话」开的，
        # 不是按「他冲我说」开的，中间他一转头跟别人聊，窗口就该让路
        _talk_to_other = self._at_others(event)
        if _talk_to_other:
            # 记一笔：这人这会儿在跟别人说话。紧跟着的那句没 @，也还是冲对方说的
            # （实测：@我是神人「感觉哈基米没有GPT有人味」→ 下一句「感觉Muse是最
            #  有人味的」没带 @，鱼当是在夸自己，回了「主人自己做的当然偏心它」）
            st.other_dir[uid] = now
        elif not is_at:
            _other_ts = st.other_dir.get(uid, 0.0)
            # 90 秒内且这中间我没插过话 = 还在跟对方聊，不该轮到我
            if _other_ts and now - _other_ts < 90 and st.last_bot_reply_ts < _other_ts:
                _talk_to_other = True

        # 结算上一次「主动插话有没有人搭理」：有人 @ 它、叫它名字，就是接上了；
        # 90 秒过完还是没人理，计一次冷场，连着两次就撤退一段时间
        if bool(is_at) or self._addressed_to_me(text):
            st.pending_interject_ts = 0.0
            st.unanswered_interjects = 0
            st.silent_until = 0.0
        elif st.pending_interject_ts and now - st.pending_interject_ts > 90:
            st.pending_interject_ts = 0.0
            st.unanswered_interjects += 1
            if st.unanswered_interjects >= int(self.cfg.get("cold_shoulder_strikes", 2) or 2):
                st.silent_until = now + float(self.cfg.get("cold_shoulder_sec", 600) or 600)
                logger.info("[fish] 群 %s 连续 %d 次插话没人接，安静 %d 秒",
                            gid, st.unanswered_interjects, int(st.silent_until - now))
        if (not (is_at or forced) and not _talk_to_other and self.cfg.get("reply_when_at", True)
                and uid == st.follow_uid and now < st.follow_until):
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

        # ---------------- 解析外包给 media_parser 时的闭嘴闸 ----------------
        # 光贴链接是在「分享」，不是找它说话；以前它会接一句「我看不了视频」，
        # 等于主动自曝短板。带评价词（「你觉得呢」）的照旧交给模型。
        # 但「被点名 + 贴链接」是在跟它说话（@它看个视频、引用它的话补个链接），
        # 以前一律早退，整条消息被吞，一个字都不回
        if (text and gid and _URL_RE.search(text) and not _ASK_OPINION_RE.search(text)
                and not (is_at or forced or is_quote_bot or is_follow)):
            logger.info("[fish] 群 %s 链接消息，交给解析插件，不接话", gid)
            event.stop_event()
            return

        # ---------------- 秩序巡检 ----------------
        # 必须在接话判定之前：判「不接」的那些消息（@的是别人 / 质量分不够 / 冷却中）
        # 原来一次模型都不调，等于把对着骂和刷广告的全放过去了
        if text.strip():
            self._maybe_patrol(event, gid, text, nick, uid,
                               [m["text"] for m in list(st.msgs)[-6:]])

        # ---------------- 有人让我去骂人（动作层，搬自旧 bot 的 actions.py） ----------------
        # 「群友借它的嘴」和「它被骂了回嘴」是两件事，通道就这么分开：
        # 这条自己生成几句、真 @ 到人头上，不走接话概率、也不进人设
        if text and gid and self.cfg.get("insult_actions_enabled", True):
            _act = insult_actions.parse(text)
            if _act:
                if await self._do_insult(event, gid, text, uid, st, _act):
                    await self._remember(gid, uid, text)
                    event.stop_event()
                    return

        # ---------------- 挨骂了：要不要还嘴、凶到什么程度 ----------------
        # 判定全在代码里：别人互骂、吐槽第三方一律不动，只有点名骂它、
        # 或者骂到 life 头上才加火。提示词里没有常驻的「被骂就喷」——
        # 常驻一条会让它把调侃也当挑衅，好不容易追上的闲聊立刻退回去
        quarrel_hint, quarrel_force = "", False
        if text and self.cfg.get("quarrel_enabled", True):
            _same = [m for m in list(st.msgs)[-3:] if str(m.get("uid")) == str(uid)]
            _owner = str(self.cfg.get("owner_qq") or "").strip()
            _owner_hit = bool(_owner) and str(uid) == _owner
            # 25 秒内它刚说过话，就当这条是在对它说：群里骂人一大半不带 @，
            # 少这条判断，「你他妈傻逼」这种明摆着冲它来的会被当空气
            _to_me = ((now - getattr(self, "_last_bot_ts", {}).get(gid, 0.0)) < 25
                      and (is_at or self._addressed_to_me(text)
                           or bool(re.search(r"你(?:他妈|妈|丫|个|们|这|那|是|说|在)", text))))
            # 注意：这里要的是「这句在骂主人」，不是「这句话是主人说的」。
            # 以前传 _owner_hit，结果主人自己吐槽一句「这 sb 又崩了」，
            # 鱼收到的提示是「life 刚指名骂了你主人 life」，直接进三档开喷
            _owner_cursed = (not _owner_hit) and bool(_owner) and any(
                u in text for u in (f"@{_owner}", str(_owner)))
            _lvl = quarrel.classify(text, at_me=is_at, mentions_owner=_owner_cursed,
                                    consecutive=len(_same) >= 2, reply_to_me=_to_me)
            _hot = self._anger_unlimited(gid)
            if _lvl >= 3 and not _hot:
                # 同一个人五分钟两次封顶，超过自动降成二档：不然对方阴阳十句它喷十句
                _lvl = quarrel.gate(f"{gid}:{uid}", _lvl)
            if _lvl >= 2:
                quarrel_hint = quarrel.hint(_lvl, nick, owner=_owner_hit, anger=_hot)
                # 被骂了必须当场回：等冷却、等判官点头，等它想好骂战早结束了。
                # forced 一起置位，后面「该不该开口」和判官那两处都按被叫处理
                quarrel_force = forced = True
                logger.info("[fish] 群 %s %r 骂到头上（%d 档%s），加火：%s", gid, nick, _lvl,
                            "·红温" if _hot else "", text[:30])

        # 同一个人连发十条它回十条，真人不会这样。90 秒内已经被回过两次的人，
        # 第三条起只在被 @ 或带问号时才接——嘴上说「我在听」，手别抢着答
        # 被点名、被骂到头上、在追问窗口里，都不受这条限制：
        # 以前没豁免 forced，于是「有人刚骂了它，但同一人 90 秒内已被回 2 次」
        # 会被静默丢掉——anger 加了、force 置了，群里一个字没有，像装死
        if not is_at and not forced and gid and text:
            _hist = [x for x in getattr(self, "_replied_uids", {}).get(gid, []) if now - x[0] < 90]
            hit = sum(1 for x in _hist if x[1] == uid)
            if hit >= 2 and not re.search(r"[?？]", text):
                logger.info("[fish] 群 %s %s 90 秒内已被回 %d 次，这条先不接：%s",
                            gid, nick, hit, text[:30])
                self.log_event("skip", gid, text[:60], who=nick, why="同一人连发")
                await self._remember(gid, uid, text)
                return

        # ---------------- 开口判定（唯一入口） ----------------
        # 这里以前是一串 if：被 @ 一条、手打前缀一条、追问窗口一条、主动插话一条，
        # 各自判各自的密度和冷却，改一处漏三处。现在只剩下一个 decide()：
        # @ / 唤醒 / 被骂 / 追问 / 主动插话都只是输入信号（优先级），
        # 密度、冷却、配额、判官、冷场退避全部由对象统一算，任何路径都不许绕过。
        from .fish_core import speak_decision as _sd
        from .fish_core import scheduler as _sch_mod

        _link = bool(_URL_RE.search(text or ""))
        # 旁观态打分器只做「质量分 + 按人冷却 + 小时配额 + 复读」这几个信号，
        # 结论塞进 decide() 由它统一裁决（不再由它自己决定说不说）
        _gate = ""
        _ambient = not (is_at or forced or is_follow)
        if _ambient:
            _q_ok, _q_why = self._should_interject(gid, st, text, now, has_image, has_media, uid)
            if not _q_ok:
                _gate = _q_why
        _cap_win = float(self.cfg.get("follow_reply_window_sec", 180) or 180)
        _recent_replies = len([t for t in st.reply_times if now - t <= _cap_win])
        # ---------- 收件人判定表 ----------
        # 落一行：谁说的、@ 了谁、引用的是谁、什么时候、什么话题，并算「这句说给谁」。
        # 判定链只看「多该说」，这一步补上「是不是冲我说的」：两个人聊得正热时
        # 它按话题相邻抢一句，比漏接更伤。任何异常都不许挡住正常回话，所以兜一条 try
        _rcpt = None
        _rcpt_score, _rcpt_reason = 100, ""
        try:
            _rcpt = addressee.record(
                gid=str(gid), uid=str(uid), nick=nick, text=text or "", now=now,
                self_id=str(event.get_self_id()), at_uids=tuple(at_targets),
                at_named=at_named, reply_uid=str(reply_to_uid or ""),
                is_at=bool(is_at), is_quote_bot=bool(is_quote_bot),
                is_follow=bool(is_follow), is_wake=bool(forced),
                dialogue=st.dialogue, sender_alias=sender_alias, cfg=self.cfg)
            _rcpt_score, _rcpt_reason = _rcpt.score, _rcpt.reason
            # 主人开口基本就是在跟鱼说话，但实测一天 13 条里 9 条被「无信号」吞掉。
            # 把他抬进灰区（40~70）交给判官定，不直接放行——他要是 @ 了别人就照样不理
            _own_qq = str(self.cfg.get("owner_qq") or "").strip()
            if (_own_qq and str(uid) == _own_qq and _rcpt_score < 55
                    and "@别人" not in _rcpt_reason and "引用别人" not in _rcpt_reason):
                _rcpt_score = 55
                _rcpt_reason = (_rcpt_reason + "·主人抬灰区") if _rcpt_reason else "主人"
            if _rcpt_score != _rcpt.score:
                logger.info("[fish][rcpt] 群 %s %s → 抬到 %d（%s）", gid, _rcpt.line(), _rcpt_score, _rcpt_reason)
            else:
                logger.info("[fish][rcpt] 群 %s %s", gid, _rcpt.line())
        except Exception as e:
            logger.warning("[fish] 收件人判定表异常（不影响回话）：%s: %s", type(e).__name__, e)

        # 出处三元组：原文 id + 这句说给谁。记忆没有出处就不许主动提，
        # 所以这一步必须在写记忆之前做完
        _at_first = at_named[0] if at_named else ("", "")
        self._mem_turn[str(gid)] = {
            "mid": str(getattr(getattr(event, "message_obj", None), "message_id", "") or ""),
            "addressee_id": str(event.get_self_id()) if is_at
            else str(_at_first[0] or ""),
            "addressee_nick": (str(self.cfg.get("bot_nickname") or "鱼") if is_at
                               else str(_at_first[1] or "")),
        }

        # 纠正检测：这句像在纠正上一轮引用过的记忆，就当场改库。
        # 放到模型之前做——先改库再生成，这一轮的回话才是对的；
        # 等下一轮再改，群里已经多了一条坚持旧答案的回复
        if text and gid and self.cfg.get("memory_source_required", True):
            try:
                _prev = fish_memory.take_recalled(self._last_recalled, str(gid), str(uid))
                _hit = self.memory.detect_correction(text, _prev)
                if _hit:
                    _fix = self.memory.apply_correction(
                        str(gid), str(uid), _hit, text, by_uid=str(uid),
                        anchor_mid=str((self._mem_turn.get(str(gid)) or {}).get("mid") or ""))
                    logger.info("[fish][mem][fix] 群 %s 纠正记忆 %s → 新条 %s",
                                gid, str(_hit.get("id"))[:8], str(_fix.get("new"))[:8])
                    self.log_event("mem_fix", gid, text[:40], who=nick)
                    fish_memory.drop_recalled(self._last_recalled, str(gid), str(uid))
            except Exception as e:
                logger.warning("[fish] 纠正检测异常（不影响回话）：%s: %s", type(e).__name__, e)

        _sig = _sd.Signals(
            group_id=str(gid), uid=str(uid), nick=nick, text=text or "", now=now,
            is_at=bool(is_at), is_wake=bool(forced),
            # 被骂必须有指向证据才算数，无证据会被模块降级成普通消息
            abused=bool(quarrel_force), abuse_evidence="被点名骂" if quarrel_force else "",
            is_follow=bool(is_follow), follow_left=int(getattr(st, "follow_left", 0) or 0),
            pending_interject_ts=float(getattr(st, "pending_interject_ts", 0.0) or 0.0),
            unanswered_interjects=int(getattr(st, "unanswered_interjects", 0) or 0),
            silent_until=float(getattr(st, "silent_until", 0.0) or 0.0),
            last_bot_reply_ts=float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0),
            last_interject_ts=float(getattr(st, "last_interject_ts", 0.0) or 0.0),
            interject_times=tuple(getattr(st, "interject_times", ()) or ()),
            recent_replies=_recent_replies,
            has_image=bool(has_image), has_media=bool(has_media),
            has_forward=bool(_has_forward), is_link=_link,
            empty=not (text or "").strip(),
            # 分数只作记录与门槛参考：门槛由打分器按群意愿算过，这里不再重判一次
            judge_score=(getattr(self, "_last_quality", None) if _ambient else None),
            judge_threshold=0.0,
            ambient_gate_blocked=_gate,
            # 指向：@ 到的人和引用目标都如实带过去。引用我自己的话等于在跟我说话，
            # 这时候哪怕同时 @ 了别人，也不该判成「说的是别人」
            addressee_score=int(_rcpt_score), addressee_reason=str(_rcpt_reason),
            is_quote_bot=bool(is_quote_bot),
            at_targets=tuple(at_targets),
            reply_to_uid=str(reply_to_uid or ""),
            target_uid=("someone_else" if (_talk_to_other and not is_quote_bot) else ""),
            address_evidence=("@别人" if _talk_to_other else ""),
            muted=bool((is_at or forced) and not self.cfg.get("reply_when_at", True)),
            banned=bool(getattr(st, "banned", False)),
        )
        # 调侃许可记账：拒绝词当场降级并记 30 天，正常接话慢慢升级。
        # 纯本地规则，不调模型；主人不参与（跟主人不用讲分寸）
        if self.cfg.get("tease_guard_enabled", True) \
                and str(uid) != str(self.cfg.get("owner_qq") or "").strip():
            try:
                if tease.refuse(gid, uid, text, cfg=self.cfg):
                    logger.info("[fish] 群 %s %s 明确拒绝调侃，许可归零并记 %.0f 天",
                                gid, uid, float(self.cfg.get("tease_ban_days", 30) or 30))
                else:
                    tease.note_interaction(gid, uid, text,
                                           favor_score=favor.score(gid, uid), cfg=self.cfg)
            except Exception as e:
                logger.debug("[fish] 许可记账失败：%s", e)
        _dec = _sd.decide(_sig, self.cfg)
        logger.info("[fish][sd] 群 %s %s", gid, _dec.log_line())
        # 调度器：判定链说「能说」之后，再问一句「配不配说」。
        # 默认不说，只有 @/引用/追问/卡话题/明确求助才进入候选，还要过发言率闸。
        # 它不改优先级链本身——被点名一律 must，不吃发言率
        # 在场：不需要回答但值得有个反应的消息（群里在笑/在摆烂/在抛梗）。
        # 判定和配额都在 presence 里，调度器只负责把它排到「什么都不说」之后
        _present = None
        if self.cfg.get("presence_enabled", True):
            try:
                _present = presence.try_present(gid, _sig, tuple(st.dialogue)[-6:], self.cfg)
            except Exception as e:
                logger.debug("[fish] 在场判定失败：%s", e)
        _intent = _sch_mod.schedule(_sig, self.cfg, decision=_dec,
                                     dialogue=tuple(st.dialogue)[-50:], present=_present)
        logger.info("[fish][sch] 群 %s %s", gid, _intent.log_line())
        _rescued = False
        if _intent.speak and not _dec.speak:
            # 调度器翻案：一群人连着问没人答（卡话题）或明确求助时，允许它主动开口，
            # 即便判定链认为「不是在跟鱼说」。翻案理由写进日志，事后能复查
            logger.info("[fish] 群 %s 调度器翻案（%s）：%s", gid, _intent.reason,
                        (text or "")[:30])
            _dec = _sd.SpeakDecision(True, _intent.reason, mode=_intent.mode,
                                     priority=_intent.priority)
            _rescued = True
        elif _dec.speak and not _intent.speak:
            # 复用下面「不接」那条路：记 skip 事件、写记忆、必要时 stop_event
            _dec = _sd.SpeakDecision(False, _intent.reason, mode=_dec.mode,
                                     priority=_dec.priority)
        if not _dec.speak:
            logger.info("[fish] 群 %s 不接（%s）：%s", gid, _dec.reason, (text or "")[:30])
            self.log_event("skip", gid, (text or "")[:60], who=nick, why=_dec.reason)
            await self._remember(gid, uid, text)
            # /off 之后被 @ 也不该让原生流程接手，这里照旧收口
            if _dec.reason.startswith("已 /off"):
                event.stop_event()
            return
        # 在场：只丢一张表情或一个短词。不调模型、不占主动发言配额，
        # 也不记成「鱼说了一轮」——它不是接话，是在场
        if str(getattr(_intent, "tier", "")) == _sch_mod.TIER_PRESENT:
            if await self._send_presence(event, gid, _intent, nick, uid):
                return
        reason = _dec.reason

        # 到这儿这条已经确定要回：先开追问窗口，再占住冷却位。
        # 窗口在这里开（而不是在收到 @ 时开）——没回出去的 @ 不该换来后续免@ 特权。
        # 同一人在窗口内再 @，把截止时间往后推，次数只补到 2，不补满：
        # 补满就等于「@ 一下换 5 次回」，一个人能把机器人拖着吵一整天
        if is_at or forced:
            # 同一个人在窗口里再 @，至少补到 2 次：他明确点了名，接着说话还接不上太蠢。
            # 但不补满（补满就是「@ 一下换 N 次回」，一个人能把鱼拖着吵一整天）
            _fresh = uid != st.follow_uid or now >= st.follow_until
            _max = int(self.cfg.get("follow_max_turns", 2) or 0)
            st.follow_left = _max if _fresh else max(st.follow_left, min(2, _max))
            st.follow_uid = uid
            st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)

        # 占位而不是落定：social_read 和识图还要几秒，这期间同群陆续进来的消息
        # 若不占位，每条都以为自己「还没说过话」，一波刷屏会被连着接好几次。
        # 真发出去才确认，失败要 _release_claim 回滚（带上 token，只回滚自己这次）
        _claim_tok = self._claim(st, time.time())
        # 把 token 挂到事件上：handler 中途抛异常时，最外层包装器只能拿到 event，
        # 没有它就不知道这个冷却位是不是本轮的，回滚会把别人的占位一起抹掉
        try:
            event.set_extra("fish_claim_tok", _claim_tok)
        except Exception:
            pass

        # 链接的卡片和下载在进「接不接话」判定之前就丢出去了（见 on_group 开头的
        # 媒体链接分支），这里只记一下，免得下面又解析一遍卡片、又下一次视频

        # ---------------- 第一步：这帮人在聊什么、我该不该接；同时看图 ----------------
        # 两件事互不依赖，并发跑。串行的话识图那十几秒会整个叠在 judge 后面
        social_hint = ""
        how_used = ""
        imgs = [c for c in event.message_obj.message if isinstance(c, Image)] \
            if (has_image and self.cfg.get("vision_enabled", True)) else []
        # 判官合并：没人点名时不再单独发一次小请求（那一次要 3~5 秒，而且
        # prompt 每轮都不同、缓存永远命不中），改成把「要不要开口」交给主模型，
        # 不想接就吐 [PASS]（出口已有 PASS 闸）。被 @ / 追问窗口里不许 PASS，照旧必回
        # 救场时同样不准 PASS：这时候群里正等人答，模型自己掂量「值不值得接」
        # 十有八九掂量成不说，救场就白救了
        # 被 @ 的时候判官纯属白花三秒：反正必须回，「该不该接」问了也是白问，
        # 它唯一能给的「这句在说什么」对点名场景价值很低。省下的三秒直接进生成。
        _at_skip = is_at and bool(self.cfg.get("judge_skip_on_at", True))
        _inline_judge = (bool(self.cfg.get("judge_inline", True))
                         and not (forced or is_follow or _rescued or _at_skip))
        if _at_skip:
            social_hint = "【这条点名叫你】直接回答，不要输出 [PASS]。"
        elif _inline_judge:
            social_hint = ("【这条没人叫你】先掂量一下值不值得接：插进去只是废话、或者接不住，"
                           "就只输出 [PASS]，一个字都别多说；要接就一句话接住话头。")
        verdict, vision_desc, voice_text = await asyncio.gather(
            _noop() if _inline_judge else self._social_read(gid, st, text, nick, is_at or forced, is_follow),
            self._image_desc(gid, event) if imgs else _noop(),
            hearing.from_components(recs, self.cfg, logger=logger) if recs else _noop(),
        )
        if voice_text:
            logger.info("[fish] 群 %s 语音转写：%s", gid, voice_text[:60])
        if vision_desc:
            logger.info("[fish] 群 %s 图片描述：%s", gid, vision_desc[:80])
        if verdict and not verdict.get("join"):
            if not (is_at or forced):
                # 判「不接」就到此为止，主模型一次都不调（这是这一步省下的主要延迟）
                logger.info("[fish] 群 %s social_read 判不接（话题=%r），不调主模型：%s",
                            gid, verdict.get("topic", ""), text[:30])
                self.log_event("skip", gid, text[:60], who=nick,
                               why=f"social_read 判不接（{verdict.get('topic', '')}）")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)  # 模型都没调，冷却位不该被这条占掉
                # 能走到这里必然不是 @ 也不是追问窗口，不 stop，让兄弟插件照常处理
                return
            # 被直接点名的没法装死不回，记一笔继续走
            logger.info("[fish] 群 %s social_read 想判不接，但这句是 @ 你，仍然回", gid)
        elif verdict:
            # 把上下文一起交给清洗：判官编的细节只有对上上下文才判得出来
            _ctx = " ".join(str(m.get("text", "")) for m in list(st.msgs)[-24:])
            # 判官只决定「接不接」，不再往主模型塞「怎么接」的角度提示：
            # 表达阶段被塞了提纲，写出来就带汇报腔（照抄闸的样本全是这么来的）。
            # how 仍然取出来，只用于出口照抄闸和日志
            social_hint = ""
            how_used = social_read.how_of(verdict, _ctx)
            # 判官读出来的「这句在说什么」只当一行释义塞给主模型。
            # 梗和多义词（cpdd = 处CP / 组队上分）光靠模型自己猜会挑错意思，
            # 而判官那边早就读对了，卡在它的判断没往表达层传。
            # 只给释义、不给「怎么接」：表达阶段吃到提纲就会写成汇报腔
            _topic = str((verdict or {}).get("topic") or "").strip()
            # 话题是 social_read 才有的，拿到就补回收件人表那一行（表只在内存里）
            if _topic and _rcpt is not None:
                _rcpt.topic = _topic[:24]
            # 开关留在配置里，万一发现它把闲聊带成解说腔，关一行就回到原样
            if (_topic and self.cfg.get("topic_hint_enabled", True)
                    and _topic not in ("无", "none", "None", "—")):
                social_hint = f"【这句在说：{_topic[:24]}】"
                logger.info("[fish] 群 %s 释义注入：%s", gid, social_hint)

        # 顺序要紧：先召回记忆再落这一条。反过来的话当前这句会被自己的召回捡回来，
        # prompt 里就出现「你还记得我明天要干嘛吗」这种自己引用自己的鬼话
        media_desc = ""
        if _unseen:
            # 「看不见但可以说话」：明确告诉模型没有画面，别猜内容
            media_desc = f"【他刚发了个{_unseen}，你没有画面也听不到，看不到里面是什么】"
        # 检索只走模型自己吐的 [搜索:关键词] 指针（见 _decorate_impl 0 段）。
        # 以前这里还有一条「被 @ + detect() 命中就抢先查」的近路，等于绕过模型判断：
        # 「帮我查查我昨天说的话」这种不该外联的也会被拖去搜。已删。

        # 联网扩展只在「被叫到 + 明确问了」时才发请求：普通闲聊一句都不外联。
        # 这里是体验和能力的平衡点——宁可少搜，也不让每句话多挂一秒外网延迟
        _searched = False
        if self.cfg.get("connectors_enabled", True) and (is_at or forced) and text:
            try:
                img_url = next((getattr(c, "url", "") or getattr(c, "file", "")
                                for c in event.message_obj.message if isinstance(c, Image)), "")
                if has_image and img_url and re.search(r"出处|哪部|什么番|动漫|动画|叫什么", text):
                    anime = await connectors.search_anime_by_image(await self._http(), img_url)
                    if anime and anime.get("title"):
                        media_desc += f"\n【以图搜番】{anime['title']} 相似度{anime.get('similarity', '?')}"
                elif _REALTIME_RE.search(text) and (
                        _EXPLICIT_SEARCH_RE.search(text) or not _NO_SEARCH_RE.search(text)):
                    # 「搜索」和「搜一下」是两回事，只写后者会漏掉「帮我联网搜索一下」这种最自然的说法
                    q = _search_query(text)
                    # 「联网搜索回答」这种只剩指令碎片的，改用上一句真人提问来搜
                    if len(q) < 4 or q.strip(" ，。！？") in _VAGUE_Q:
                        _prev = self._last_human_line(st, exclude=text)
                        if _prev:
                            _q2 = _search_query(_prev) or _prev
                            logger.info("[fish] 群 %s 检索词是空壳（%r），改用上一句提问：「%s」",
                                        gid, q, _q2[:30])
                            q = _q2
                        else:
                            q = ""
                    if q and (len(q) >= 4 or q not in _VAGUE_Q):
                        _searched = True
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

        # 判官（social_read）每句本来就在跑，顺手让它吐检索词：碰到不认识的梗、
        # 或者要具体数字才答得上，它比主模型有余力停下来想一句。
        # 只在被@/被追问时才有判官，所以闲聊不会因此多挂一秒外网
        _jq = str((verdict or {}).get("search") or "").strip()
        if (_jq and not _searched and text and self.cfg.get("connectors_enabled", True)
                and not _SEARCH_MARK_RE.search(text)):
            try:
                _searched = True
                briefs = await connectors.search_web_brief(await self._http(), _jq, limit=3)
                if briefs:
                    media_desc += ("\n【刚查到的东西（这就是实时结果，直接拿来回答，"
                                   "别再说自己查不了网）】：" + "；".join(
                        f"{b['title']}：{b['snippet'][:60]}" for b in briefs))
                    logger.info("[fish] 群 %s 判官要查「%s」命中 %d 条", gid, _jq, len(briefs))
                else:
                    logger.info("[fish] 群 %s 判官要查「%s」，没查到", gid, _jq)
            except Exception as e:  # noqa: BLE001
                logger.debug("[fish] 判官检索失败：%s", e)


        cross_hint = self.cross.hint(gid, int(self.cfg.get("cross_group_recall", 2) or 2)) \
            if self.cfg.get("cross_group_enabled", True) else ""
        # 纯图片消息的 text 是空串。把一个「用户什么都没说」的空位喂给模型，
        # 它会自己脑补一句（真实翻车：回了「？光戳不说话」）。用图片描述顶上位
        prompt_text = text
        if text and voice_text:
            # 同一条消息既打字又发语音：两样都给它，别丢掉一半
            prompt_text = f"{text}（同一句还发了语音，说的是：{voice_text[:100]}）"
        if not prompt_text.strip():
            if voice_text:
                prompt_text = f"（发了段语音，说的是：{voice_text[:120]}）"
            else:
                prompt_text = f"（发了张图，图里是：{vision_desc[:80]}）" if vision_desc else ""
            if quote_ctx:
                # 只有转发/引用、没打字的：拿内容顶上位，别当空消息丢掉
                prompt_text = quote_ctx
            if not prompt_text:
                logger.info("[fish] 群 %s 这条媒体读不出内容（图/语音都没转出来），不调主模型", gid)
                self.log_event("skip", gid, "[媒体]", who=nick, why="媒体读不出内容")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)
                if is_at:
                    # 被 @ 却不回，必须拦下来，否则框架默认 LLM 会用另一副口气回一句
                    event.stop_event()
                return
        # 有正文也有引用：两样都给模型，不然「你看看这个」接不上引用的是啥
        if quote_ctx and prompt_text and quote_ctx not in prompt_text:
            prompt_text = f"{prompt_text}\n{quote_ctx}"
        # 空上下文：没上文、或只有一条且跟这句接不上时，模型只能靠编——
        # 常见的是把「你们刚才说的那个」当真事接着圆。被叫就先问一句短的让人补，
        # 没人叫就直接不开口。这一条在 decide() 之后，只决定「调不调模型」。
        # 有引用就别走「你说的啥」：引用原文本身就是这条消息的上下文，
        # 只看时间线会把「你看这个」判成空上下文，回一句「你说的啥」
        if not self._ctx_of(st, event.get_extra("fish_ctxline") or text) and not quote_ctx:
            _thin = self._thin_context(st, prompt_text or text)
            if _thin:
                _ask = bool(self.cfg.get("empty_context_ask_short", True)) and is_at
                logger.info("[fish] 群 %s 上下文太薄（%s），不调主模型%s", gid, _thin,
                            "，先问一句" if _ask else "")
                self.log_event("skip", gid, (text or "")[:40], who=nick,
                               why=f"上下文太薄·{_thin}")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)
                if _ask:
                    yield event.plain_result(random.choice(prompts.THIN_CONTEXT_ASKS))
                elif is_at:
                    event.stop_event()
                return
        try:
            system_prompt, user_prompt, _ctx_llm = await self._compose(
                gid, uid, nick, prompt_text, is_at, st, self_id=self_id,
                ctx_line=event.get_extra("fish_ctxline") or "",
                vision_desc=vision_desc,
                media_hint=media_desc,
                social_hint=social_hint, cross_hint=cross_hint,
                quarrel_hint=quarrel_hint,
            )
        except Exception:
            # 组 prompt 就炸了：这条发不出去，冷却位必须还回去。
            # 必须带 token：并发时另一条已经拿到新的占位，无 token 释放会把人家的抹掉
            self._release_claim(st, _claim_tok)
            raise
        await self._remember(gid, uid, text)
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick, "text": text, "at": is_at,
            # 决定回的原因一并带走：控制台要显示「这句为什么接」
            "reason": reason,
            # 追问窗口的次数和状态键在真正发出去时才结算（见 on_decorate）
            "sk": gid, "follow": is_follow,
            # 判官给的角度：出口要比一比是不是被逐字念出去了
            "how": how_used,
            # 图里的内容不在 msgs 里，离题闸要靠它跟上下文对上（见 _decorate_impl 2.5 段）
            "vision": vision_desc,
            "claim_tok": _claim_tok,
        })
        # 生成器侧：记下这轮的意图与消息版本号，出口（on_decorate）醒来要拿它
        # 比对「等我打完这几秒，群里是不是已经换了话题」
        event.set_extra("fish_turn", {
            "gid": gid, "uid": uid, "ts": now, "ver": st.msg_ver,
            "intent_id": _intent.intent_id, "mode": _intent.mode,
            "prio": _intent.priority, "tier": _intent.tier,
        })
        _ru = getattr(self, "_replied_uids", {}).setdefault(gid, [])
        _ru.append((now, uid))
        del _ru[:-20]
        self._replied_uids = {**getattr(self, "_replied_uids", {}), gid: _ru}
        # 主动接话要记账：90 秒后没人理它就算一次冷场（见下面的退避闸）
        if str(reason).startswith("主动接话"):
            st.pending_interject_ts = now
        logger.info("[fish] 群 %s %s(%s) 决定回（%s）：%s", gid, nick, uid, reason, text[:40])
        # 这里不能 stop_event：它有 STOP 结果，连我们自己刚 yield 的 request_llm 一起拦掉，
        # 实测 ab! 消息一条回复都发不出去。防双回靠「框架只在无 request_llm 时才走默认 LLM」
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt,
            system_prompt=system_prompt,
            contexts=_ctx_llm,
            image_urls=[],
        )
    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    @_handler_guard
    async def on_private(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True) or not self.cfg.get("reply_private", True):
            # 关私聊要真关：只 return 不 stop，AstrBot 自己的 LLM 链路照样会回
            event.stop_event()
            return
        text = (event.message_str or "").strip()
        if _is_builtin_cmd(text):
            return
        uid = str(event.get_sender_id())
        if uid == str(event.get_self_id()):
            return
        if not self._private_allowed(uid):
            # 私聊白名单：陌生人私聊一律不理，也不吭声
            logger.info("[fish] 私聊来自白名单外 %s，忽略", uid)
            event.stop_event()
            return
        # 戳一戳消息通常没有 message_id，走「p:」去重会互相顶掉——
        # 前一次空 id 把这次记成重复，戳就永远不回。这里对 Poke 直接跳过去重
        _is_poke_msg = any(isinstance(c, Poke) for c in (getattr(event.message_obj, "message", []) or []))
        if not _is_poke_msg and self._seen_mid("p:%s" % (getattr(event.message_obj, "message_id", "") or "")):
            logger.info("[fish] 私聊收到重复投递，忽略")
            event.stop_event()
            return
        nick = event.get_sender_name() or uid
        self_id = str(event.get_self_id())
        # 状态必须按人分：所有人共用一份 GroupState 的话，A 的对话会进 B 的 prompt
        # （隐私），B 在 A 说完 3 秒内发消息还会被冷却闸整条吞掉
        sk = f"private:{uid}"
        st = self._state(sk)
        now = time.time()
        # 群聊里有注入防护和机器人识别，私聊原来一条都没有——谁私聊都能套人设
        if text:
            injected, snark = safety_guard.check_prompt_injection(text)
            if injected:
                logger.info("[fish] 私聊判定注入 <- %s：%s", uid, text[:40])
                self.log_event("inject", "private", text[:60], who=uid)
                yield event.plain_result(snark or "这句我不接。")
                event.stop_event()
                return
        if peer_bots.is_peer_bot(uid, nick, self._peer_set(),
                                 self.cfg.get("peer_bots_name_hints") or ()):
            logger.info("[fish] 私聊忽略机器人消息：%s(%s)", nick, uid)
            return
        # 私聊里的戳一戳会被 AstrBot 当成「没有正文的私聊消息」派到这里，
        # on_poke 根本轮不上（实测 4 下戳全落在空消息分支）。所以在这里自己认 Poke：
        # 回戳必须照给，人话 25 秒内只说一句
        # 两个来源都要看：get_messages() 有时把 Poke 段吞了（转成空文本），
        # message_obj.message 还留着原始组件。只看一个会漏判，表现就是「戳了不回」
        _seg_lists = [list(event.get_messages() or []),
                      list(getattr(event.message_obj, "message", []) or [])]
        poked = any(
            isinstance(c, Poke) or "poke" in str(getattr(c, "type", "")).lower()
            for segs in _seg_lists for c in segs
        )
        if poked:
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), "private", uid, self_id)
            if now - st.last_bot_reply_ts < 25:
                logger.info("[fish] 私聊被戳 <- %s，%.0fs 内刚说过话，只回戳",
                            nick, now - st.last_bot_reply_ts)
                event.stop_event()
                return
            st.last_bot_reply_ts = now
            # 私聊的戳同样走模型：本地短句只有 5 句，戳几次就开始复读
            _pi = poke_context.decide(st.msgs, uid, st.last_bot_reply_ts, now)
            _sysp = composer.build_system_prompt(
                prompts.SYSTEM_PROMPT,
                bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"),
                bot_qq=self_id, group_id="private", is_private=True,
            )
            _userp = poke_context.build_prompt(
                _pi, f"戳你的是「{nick}」", repeat=1, last_said=list(st.recent_bot)[-2:])
            logger.info("[fish] 私聊被戳 <- %s，走模型", nick)
            self._spawn_poke_watch(event, "private", uid, nick, st, now)
            yield event.request_llm(prompt=_userp, system_prompt=_sysp,
                                    contexts=self._ctx_of(st, ""), tool_set=NO_TOOLS)
            return

        # 私聊里空消息/纯 @/纯表情：插件不接，框架自己会回「？」「咋了」，一句一句跟着刷。
        # 12 分钟内回过就彻底闭嘴，否则只给一句「说事」，然后把事件停掉
        if not text:
            if now - st.last_bot_reply_ts < 720:
                logger.info("[fish] 私聊 <- %s 空消息，%.0fs 内已应过，静默", nick, now - st.last_bot_reply_ts)
                event.stop_event()
                return
            st.last_bot_reply_ts = now
            event.stop_event()
            yield event.plain_result(random.choice(["说事", "在，说", "嗯？"]))
            return
        # 私聊是一对一，但也不是一句一回：连着发的先等他说完
        if now - st.last_bot_reply_ts < 3:
            logger.info("[fish] 私聊 <- %s %.1fs 前刚回，这轮先不说", nick, now - st.last_bot_reply_ts)
            event.stop_event()
            return
        st.last_bot_reply_ts = now
        st.dialogue.append({"who": nick, "text": text, "ts": now, "me": False, "uid": uid})
        # 私聊同样压进「只增不改」的上文：不然它每轮都要把整段对话重看一遍
        self._push_ctx(st, "user", f"{nick}：{text}")
        affect.on_group_message("private", text, private=True)
        try:
            system_prompt, user_prompt, _ctx_llm = await self._compose(
                "private", uid, nick, text, True, st,
                is_private=True, self_id=self_id,
                ctx_line=event.get_extra("fish_ctxline") or "")
        except Exception:
            # 组 prompt 失败：这条发不出去，别把冷却位留在现在（否则白静默三秒起）
            st.last_bot_reply_ts = 0.0
            raise
        event.set_extra("fish_pending", {"gid": "private", "uid": uid, "nick": nick,
                                         "text": text, "at": True, "sk": sk})
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt, system_prompt=system_prompt, contexts=_ctx_llm, image_urls=[]
        )

    # ============ 钩子转发层（装饰器只能挂在 Star 所在模块）============
    # AstrBot 取 handler 时用 star_map.get(handler_module_path) 过滤，star_map 里只有
    # Star 子类所在的模块（本文件）。拆成 11 个 Mixin 之后，on_decorate / on_sent /
    # on_poke / cmd_* 全部落在 fish_core/*.py 下，被静默过滤掉——[PASS] 原样进群、
    # 排版清洗不生效、戳一戳不回、ab! 命令全哑，根因都是这一条。
    # 所以装饰器统一回归本文件，实体仍留在各自 Mixin，避免以后再加钩子又踩一遍。

    @filter.on_decorating_result()
    async def on_decorate(self, event: AstrMessageEvent):
        await RespondMixin.on_decorate(self, event)

    @filter.on_llm_request()
    async def _mark_llm_t0(self, event: AstrMessageEvent, req):
        await RespondMixin._mark_llm_t0(self, event, req)

    @filter.on_llm_response()
    async def _log_cache(self, event: AstrMessageEvent, resp):
        await RespondMixin._log_cache(self, event, resp)

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        await RespondMixin.on_sent(self, event)

    @filter.event_message_type(EventMessageType.ALL)
    @_handler_guard
    async def on_poke(self, event: AstrMessageEvent):
        async for _r in PokeMixin.on_poke(self, event):
            yield _r

    @filter.command("tocken", alias={"token", "用量", "token用量"})
    async def cmd_tocken(self, event: AstrMessageEvent):
        async for _r in CommandsMixin.cmd_tocken(self, event):
            yield _r

    @filter.command("off", alias={"静默"})
    async def cmd_off(self, event: AstrMessageEvent):
        async for _r in CommandsMixin.cmd_off(self, event):
            yield _r

    @filter.command("on", alias={"恢复"})
    async def cmd_on(self, event: AstrMessageEvent):
        async for _r in CommandsMixin.cmd_on(self, event):
            yield _r

    @filter.command("memes", alias={"reload_memes", "梗库"})
    async def cmd_memes(self, event: AstrMessageEvent):
        async for _r in CommandsMixin.cmd_memes(self, event):
            yield _r
