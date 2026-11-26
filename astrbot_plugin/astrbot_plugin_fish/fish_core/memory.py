# -*- coding: utf-8 -*-
"""上下文与记忆：记忆写入、上下文推送、素材蒸馏（memory.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class MemoryMixin:
    def _load_recent_context(self) -> None:
        """启动时把各群最近的对话捞回来。

        这些状态全在内存里，重启一次就失忆：群里刚聊透「神话保底 2000」，
        重启后它又问一遍「神话是啥」，看着就像没在听人说话。
        落一小份盘很便宜，能省掉这种蠢问。
        """
        try:
            raw = json.loads(self._ctx_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("[fish] 最近对话读取失败（忽略）：%s", e)
            return
        cutoff = time.time() - 3600   # 一小时前的话接不上，别让模型当成刚发生的
        for gid, rows in (raw or {}).items():
            st = self._state(str(gid))
            for r in rows or []:
                ts = float(r.get("ts") or 0)
                if ts < cutoff:
                    continue
                me = bool(r.get("me"))
                st.dialogue.append({"who": r.get("who") or "", "text": r.get("text") or "",
                                    "ts": ts, "me": me, "uid": r.get("uid") or ""})
                if not me:
                    st.msgs.append({"uid": r.get("uid") or "", "nick": r.get("who") or "",
                                    "text": r.get("text") or "", "ts": ts})
            # 我上次说话的时间也得跟着捞回来。只捞消息不捞这个，重启后
            # last_bot_reply_ts 归零，戳一戳就把「已经回过的旧话」当成没回，
            # 硬补一句自我介绍——线上真发生过
            spoke = [float(r.get("ts") or 0) for r in (rows or []) if r.get("me")]
            if spoke:
                st.last_bot_reply_ts = max(st.last_bot_reply_ts, max(spoke))
        if self._groups:
            logger.info("[fish] 已恢复 %d 个群/会话的最近对话", len(self._groups))

    def _save_recent_context(self) -> None:
        """把每个会话最近 40 句落盘，重启后能接上话头。失败不影响主流程。"""
        try:
            out = {}
            for gid, st in self._groups.items():
                rows = [{"who": m.get("who") or "", "text": str(m.get("text") or "")[:200],
                         "ts": m.get("ts"), "me": bool(m.get("me")), "uid": m.get("uid") or ""}
                        for m in list(st.dialogue)[-40:]]
                if rows:
                    out[str(gid)] = rows
            tmp = self._ctx_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._ctx_path)
        except Exception as e:
            logger.debug("[fish] 最近对话落盘失败：%s", e)

    @staticmethod
    def _fmt_mem_when(ts):
        """把记忆时间戳压成「今天/昨天/3 天前/9 月 12 日」。
        模型看绝对时间戳没感觉，看「上个月」才知道这是旧事、不能当现状说。"""
        try:
            t = float(ts or 0)
        except (TypeError, ValueError):
            return ""
        if t <= 0:
            return ""
        import time as _t
        now = _t.time()
        d = int((now - t) // 86400)
        if now - t < 3600:
            return "刚才"
        if now - t < 86400 and _t.localtime(t).tm_mday == _t.localtime(now).tm_mday:
            return "今天"
        if d <= 1:
            return "昨天"
        if d < 30:
            return f"{d} 天前"
        lt = _t.localtime(t)
        return f"{lt.tm_mon} 月 {lt.tm_mday} 日"

    async def _distill_atom(self, text: str) -> str:
        """让模型判一句：这是不是这个人的长期信息，是就压成一条印象。

        规则抽取只能看「句子里有没有『我喜欢』」，分不出玩笑、转述和半句，
        档案里于是躺着「我叫XX过去帮你弄干净吧」。一次小调用换干净。
        判不出来就当没有——宁可少记一条，也不往档案里塞垃圾。
        """
        try:
            raw = await self._llm_once(memory_atoms.distill_prompt(text),
                                       memory_atoms.DISTILL_SYSTEM,
                                       float(self.cfg.get("atoms_distill_timeout_sec", 6) or 6))
        except Exception as e:
            logger.debug("[fish] 印象提炼失败（%s），这条不记", e)
            return ""
        return memory_atoms.clean_distilled(raw)

    def _msg_key(self, gid: str, event) -> str:
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        return f"{gid}:{mid}" if mid else ""

    async def _remember(self, gid: str, uid: str, text: str) -> None:
        """自动感知：把群消息喂给记忆引擎（规则抽取事实/偏好/计划/事件）。"""
        if not text:
            return
        try:
            _prov = getattr(self, "_mem_turn", {}).get(str(gid)) or {}
            # log=False：抽取记忆但先不写流水，等回复出来再补，避免一条消息两行流水
            await asyncio.to_thread(
                self.memory.record_turn, gid, uid, text, "", False,
                anchor_mid=str(_prov.get("mid") or ""),
                addressee_id=str(_prov.get("addressee_id") or ""),
                addressee_nick=str(_prov.get("addressee_nick") or ""),
            )
            # 命中的是偏好/事实，才值得单独压成一条印象原子；
            # 事件和计划塞进原子层只会把「同一个人」的档案搅浑
            kinds = set(fish_memory.FishMemoryEngine.detect_types(text))
            if kinds & {"preference", "fact"} and gid:
                # 同一人 60 秒内只提炼一次：连着刷五条消息不该烧五次小调用
                if time.time() - self._distill_ts.get(uid, 0.0) >= 60:
                    self._distill_ts[uid] = time.time()
                    atom = await self._distill_atom(text)
                    if atom:
                        keep = self._atoms_of(gid, uid)
                        self._atoms_all[f"{gid}:{uid}"] = memory_atoms.add_note(keep, atom)
                        self._save_atoms()
                        logger.info("[fish] 群 %s 攒印象 %r ← %r", gid, atom, text[:30])
        except Exception as e:
            logger.warning("[fish] 记忆自动感知失败: %s", e)

    def _push_ctx(self, st: "GroupState", role: str, content: str) -> None:
        """把一句话追加进「上文」。只追加，绝不重排。

        满了就一次砍掉四分之一：每轮都位移等于每轮都 miss，
        宁可隔十几轮 miss 一次，也不要每轮都 miss。
        """
        txt = " ".join(str(content or "").split())
        if not txt:
            return
        st.ctx.append({"role": role, "content": txt[:300]})
        limit = int(self.cfg.get("context_turns_max", 48) or 48)
        if len(st.ctx) > limit:
            del st.ctx[: max(1, limit // 4)]

    def _record_spoke(self, gid: str, uid: str, final: str, reason: str = "",
                      nick: str = "", follow: bool = False) -> None:
        """动作类回复（只发图/点歌/斗图/骂人）的记账口。

        这些路径都是自己 await event.send() 再 return，不走出口那段记账代码，
        结果下一轮模型不知道「我刚发过图/刚唱过歌」，冷却位和追问窗口也不动。
        发完直接调这里补上，跟正常出口记的是同一套账。
        """
        self._account_sent({"gid": gid, "sk": gid, "now": time.time(), "final": final,
                            "reason": reason, "nick": nick, "follow": follow,
                            "meme": 0, "parts": 1, "uid": str(uid), "text": final})

    def _last_human_line(self, st: "GroupState", exclude: str = "") -> str:
        """上文里最近一条真人发言（跳过自己说的），给「空壳指令」找检索词用。"""
        for m in reversed(list(st.dialogue)):
            if m.get("me"):
                continue
            t = str(m.get("text") or "").strip()
            if t and t != exclude and len(t) >= 4:
                return t
        return ""

    def _ctx_of(self, st: "GroupState", current_line: str = "") -> list:
        """取这一轮要带上去的上文。当前这句已经在 ingest 时压进去了，别再重复一遍。"""
        if not self.cfg.get("multi_turn_contexts", True):
            return []
        ctx = list(st.ctx)
        cur = " ".join(str(current_line or "").split())[-60:]
        if cur and ctx and ctx[-1].get("role") == "user" and cur in str(ctx[-1].get("content") or ""):
            ctx.pop()
        # 只留最近 N 轮，且必须从 user 那条切开（搬 dsh-ctxclean 的 _keep_recent_turns）：
        # 从 assistant 切开等于留一句「它自己说的、但对的是被砍掉的那句」，
        # 模型会拿这话当上下文接着编，答非所问的体感一半来自这里
        keep = int(self.cfg.get("ctx_keep_turns", 30) or 30)
        if keep > 0:
            n_user = sum(1 for m in ctx if m.get("role") == "user")
            if n_user > keep:
                seen = 0
                for i in range(len(ctx) - 1, -1, -1):
                    if ctx[i].get("role") == "user":
                        seen += 1
                        if seen == keep:
                            ctx = ctx[i:]
                            break
        return ctx

    def _thin_context(self, st: "GroupState", text: str) -> str:
        """时间线为空、或只有一条且跟当前这句接不上时返回 'empty'/'thin'，否则 ''。

        只判「有没有可用的上文」，说不说仍由 decide() 和调用方定。
        """
        window_sec = float(self.cfg.get("context_window_sec", 1800) or 1800)
        now_ref = time.time()
        raw = [x for x in list(st.dialogue)[:-1]
               if now_ref - float(x.get("ts") or 0) <= window_sec]
        lines = [f"{x.get('who')}：{x.get('text')}" for x in raw]
        return composer.timeline_is_thin(lines, text)
