# -*- coding: utf-8 -*-
"""基础设施：HTTP、状态、去重、备份、记账（runtime.py，从 main.py 逐字搬出，逻辑未改）。"""

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

from .kernel import *  # noqa: F401,F403


class RuntimeMixin:
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
                # 顺路清点歌缓存：7 天没被点到的 mp3 删掉，不然只进不出
                if self._song_svc is not None:
                    try:
                        _n = self._song_svc.prune_cache()
                        if _n:
                            logger.info("[fish] 清掉 %d 个过期点歌缓存", _n)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[fish] 清理点歌缓存失败：%s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[fish] 定时备份失败：%s", e)

    async def terminate(self) -> None:
        """插件卸载/重载时收尾：关连接、把关系网落盘。失败也不能抛，否则框架停不下来。"""
        try:
            if self._song_svc is not None:
                await self._song_svc.close()
        except Exception:
            pass
        # 备份任务必须收掉，否则重载后旧任务还握着上一份 self 在跑
        if getattr(self, "_backup_task", None):
            self._backup_task.cancel()
        try:
            if self._sess is not None and not self._sess.closed:
                await self._sess.close()
        except Exception:
            pass
        try:
            await vision.close()   # 视觉那条常驻连接也得收，否则退出时刷 Unclosed client session
        except Exception:
            pass
        try:
            cross_interaction.save_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网落盘失败：%s", e)

    def _load_atoms(self) -> dict:
        """印象原子按「群+人」各一份。

        读坏了先试 .bak，再试不出才算空——原来直接 return {}，然后任意一次
        保存就把 {}/空dict 覆盖回磁盘，全部印象静默清零、事后无从恢复。
        """
        try:
            return backup.load_json_recover(str(self.atoms_path)) or {}
        except Exception as e:
            logger.warning("[fish] 印象原子读取失败：%s", e)
            return {}

    def _save_atoms(self) -> None:
        # 走统一的原子写 + 快照：这里原来是裸 write_text，写一半断电就是空文件
        try:
            _write_json(str(self.atoms_path), self._atoms_all)
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

    def _seen_mid(self, mid: str) -> bool:
        """同一条消息是否已经处理过。真的发生过：重连补投导致同一条被答两遍。"""
        if not mid:
            return False
        now = time.time()
        # 顺手剪掉 3 分钟前的，量级就几十个 key，不用 OrderedDict
        for k in [k for k, ts in self._mids.items() if now - ts > 180]:
            self._mids.pop(k, None)
        if mid in self._mids:
            return True
        self._mids[mid] = now
        return False

    @staticmethod
    def _claim(st: "GroupState", now: float) -> int:
        """占住冷却位，返回本次占位的 token。真正的确认在 on_decorate 里，失败要 _release_claim 回滚。"""
        st.claim_token += 1
        st.claim_prev = st.last_bot_reply_ts
        st.claiming = True
        st.claim_owner = st.claim_token
        st.last_bot_reply_ts = now
        return st.claim_owner

    @staticmethod
    def _release_claim(st: "GroupState", token=_UNSET) -> None:
        """没发出去就把冷却位还回去——不然一次生成失败要白静默五分钟。

        带 token 时只有持有者能回滚：同群两条消息并发时，先失败的那条
        不能把后一条的占位一起抹掉（实测会连着吐好几条，密度闸白设）。
        """
        if not st.claiming:
            return
        if token is not _UNSET and token != st.claim_owner:
            return
        st.last_bot_reply_ts = st.claim_prev
        st.claiming = False
        st.claim_owner = 0

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
        _t0 = time.time()
        resp = await asyncio.wait_for(
            prov.text_chat(prompt=prompt, system_prompt=system, contexts=[]), timeout
        )
        # 判官也要记账，否则「总用量」会少掉一块别人看不见的开销
        try:
            us = _usage_of(resp)
            self.memory.log_token("judge", us[0], us[1], us[2],
                                  int((time.time() - _t0) * 1000))
        except Exception:
            pass
        return getattr(resp, "completion_text", "") or ""

    async def _social_read(self, gid: str, st: GroupState, text: str, nick: str, is_at: bool,
                           is_follow: bool = False):
        """第一步：判该不该接。任何异常都当成「判不了」，直接放行给主模型，别把回复吞掉。"""
        if not self.cfg.get("social_read_enabled", True):
            return None
        if not text and not is_at:
            return None
        # 判官以前只看最近 5 条：群里刷得快的时候，这条窗口连半分钟都覆盖不到，
        # 「神话」这种 10 分钟前刚解释过的东西它根本没见过，于是让主模型再问一遍。
        # 改成时间窗 + 条数上限，判官至少能看见这轮话题是怎么起来的
        win = float(self.cfg.get("social_context_sec", 1800) or 1800)
        now_t = time.time()
        pool = [m for m in list(st.msgs)[:-1]
                if now_t - float(m.get("ts") or 0) <= win][-24:]
        timeline = [f"{m['nick']}：{m['text']}" for m in pool]
        timeout = float(self.cfg.get("social_read_timeout_sec", 8) or 8)

        async def call(prompt, system):
            return await self._llm_once(prompt, system, timeout)

        t0 = time.time()
        try:
            verdict = await social_read.decide(
                call, timeline=timeline, latest=text, nick=nick,
                bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), is_at=is_at,
                is_follow=is_follow,
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
            min_gap_sec=float(self.cfg.get("meme_min_gap_sec", 180) or 0),
            follow_gap_sec=float(self.cfg.get("meme_follow_gap_sec", 60) or 0),
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
