# -*- coding: utf-8 -*-
"""戳一戳：走模型 + 兜底看门狗（poke.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class PokeMixin:
    @filter.event_message_type(EventMessageType.ALL)
    @_handler_guard
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
        # 别人互戳跟我没关系，只认戳到我自己身上的。
        # 目标 id 得走 target_id()：NapCat 有时候把目标放在旧字段 qq 上，
        # 有时候直接给 id=0，只看 .id 会漏掉一整类戳（私聊戳基本都这样）
        self_id = str(event.get_self_id())
        gid = str(event.get_group_id() or "private")

        if not any(_poke_aimed_at_me(s, self_id, gid) for s in poked):
            logger.info("[fish] 收到戳一戳段但目标不是我：%s", [repr(s) for s in poked])
            return
        if self._is_muted(gid):
            # 静默连戳都不理，但也不回戳——不然「全关」了手还在动
            return
        if gid != "private" and not self._group_allowed(gid):
            event.stop_event()
            return
        uid = str(event.get_sender_id())
        if gid == "private" and not self._private_allowed(uid):
            # 陌生号私聊戳一下也能触发一整轮模型调用，白名单外一律不理
            return

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
        # 群里六个人轮流戳，每戳一次换一句「戳我干嘛」就是刷屏。25 秒内回过一次
        # 就只回戳不说话；真有没被回的留言（pending_text）照常补
        if not pending_text and now - st.last_poke_reply_ts < 25:
            logger.info("[fish] 戳一戳 <- %s(%s) 群 %s，%ds 内刚因戳说过话，只回戳",
                        nick, uid, gid, int(now - st.last_poke_reply_ts))
            favor.on_poke(gid, uid)
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
            event.stop_event()
            return
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

        if is_admin:
            who_hint = "戳你的是你主人"
        else:
            who_hint = f"戳你的是群友「{nick}」"

        # 戳一戳直通加分，不进文本冷却——这是群友唯一不被模型误判的回血通道
        favor.on_poke(gid, uid)
        # 戳一戳等价于叫了一次人：顺手开追问窗口，否则他戳完想接着说还得再 @ 一次。
        # 同 @ 一样，窗口内反复戳只续时间不补次数，不然戳一下就又换 5 句
        st.last_poke_reply_ts = now
        if uid != st.follow_uid or now >= st.follow_until:
            st.follow_left = int(self.cfg.get("follow_max_turns", 2) or 0)
        st.follow_uid = uid
        st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)
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
        logger.info("[fish] 戳一戳 <- %s(%s) 群 %s 第 %d 下，走模型；待回消息：%s",
                    nick, uid, gid, st.poke_count, (pending_text[:30] or "无"))
        # 看门狗必须第一个挂：它在任何一步卡住时都还有机会补一句话。
        # 之前它挂在 log_event / _poke_back 后面，实测 _poke_back 的
        # call_action 一旦等不到对端回包，整个 async generator 就悬在那儿，
        # 看门狗没挂上、request_llm 也没交出去，表现是戳完毫无反应且不兜底。
        self._spawn_poke_watch(event, gid, uid, nick, st, now)
        # 记账不该挡回话：库里一次 IO 卡住，戳就白戳了
        try:
            self.log_event("poke", gid, f"{nick or uid} 戳了一下", who=nick or uid)
        except Exception as e:
            logger.warning("[fish] 戳一戳记账失败：%s %s", type(e).__name__, e)
        # 先回戳再回话：两件事互不等待，戳要秒到，话照旧慢慢说。
        # 回戳是锦上添花，绝不能挡住回话，所以只等 1.5 秒，之后丢后台自己跑完
        if self.cfg.get("poke_back", True):
            try:
                await asyncio.wait_for(
                    _poke_back(getattr(event, "bot", None), gid, uid, self_id), 1.5)
            except asyncio.TimeoutError:
                logger.warning("[fish] 回戳 1.5s 没回包，先让回话走")
            except Exception as e:
                logger.warning("[fish] 回戳失败：%s %s", type(e).__name__, e)
        # 模型是异步出的：request_llm 交出去之后这边就收不到「失败了」的回调，
        # 超时没动静就由上面那只看门狗补一句兜底短句
        yield event.request_llm(prompt=user_prompt, system_prompt=system_prompt,
                                contexts=self._ctx_of(st, ""), tool_set=NO_TOOLS)

    def _spawn_poke_watch(self, event, gid: str, uid: str, nick: str, st, now: float) -> None:
        """戳一戳走模型时的兜底看门狗。

        request_llm 是「把活交出去」，模型超时、报错、被出口闸拦掉时这边一个字都收不到。
        超时窗口内最近发言没变化就补一句本地短句，保证这一戳不是石沉大海。
        """
        timeout = float(self.cfg.get("poke_model_timeout_sec", 12) or 12)
        pool = self.cfg.get("poke_fallback_pool") or list(poke_context.FALLBACK_POOL)
        before_n = len(st.recent_bot)
        before_ts = float(getattr(st, "last_bot_reply_ts", 0) or 0)

        logger.info("[fish] 戳兜底看门狗已挂（%.0fs）", timeout)

        async def _watch():
            await asyncio.sleep(timeout)
            # 模型已经出话：正常，不用管
            if len(st.recent_bot) != before_n or float(getattr(st, "last_bot_reply_ts", 0) or 0) != before_ts:
                return
            said = [t for t in st.recent_bot if t]
            picked = _pick_poke(said, pool)
            try:
                bot = getattr(event, "bot", None)
                payload = ({"user_id": int(uid), "message": picked} if gid == "private"
                           else {"group_id": int(gid), "message": picked})
                action = "send_private_msg" if gid == "private" else "send_group_msg"
                await bot.api.call_action(action, **payload)
            except Exception as e:
                logger.warning("[fish] 戳一戳兜底发送失败 %s(%s)：%s %s", nick, uid, type(e).__name__, e)
                return
            st.last_poke_reply_ts = time.time()
            st.recent_bot.append(picked)
            logger.info("[fish] 戳一戳模型 %.0fs 没出话，兜底短句：%s", timeout, picked)
            self.log_event("poke_fallback", gid, picked, who=nick or uid)
            self._record_spoke(gid, uid, picked, reason="戳一戳·兜底")

        try:
            asyncio.get_running_loop().create_task(_watch())
        except Exception as e:
            logger.debug("[fish] 戳兜底看门狗没挂上：%s", e)
