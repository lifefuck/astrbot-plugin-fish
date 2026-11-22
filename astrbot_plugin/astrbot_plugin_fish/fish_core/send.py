# -*- coding: utf-8 -*-
"""发送：拆句、转发、补发（send.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class SendMixin:
    async def _send_long(self, event: AstrMessageEvent, gid: str, text: str) -> None:
        """答案统一走合并聊天记录，和 /memory 一个形态。

        为什么不按字数分流：检索答案几乎次次上千字，短的那几次反而是残缺的；
        统一成折叠卡片，群里不占屏，点开才展开，观感跟真人转发聊天记录一致。
        节点里的键必须是 user_id/nickname——用 uin/name 实测 QQ 端不认。
        """
        bot = getattr(event, "bot", None)
        bot_qq = str(event.get_self_id() or "")
        # 留空就跟群昵称一致：以前硬编码「小鲸鱼」，改了 bot_nickname 卡片还是老名字
        nick = str(self.cfg.get("self_nickname")
                   or self.cfg.get("bot_nickname") or "小鲸鱼")
        chunks = self._split_for_forward(text)
        nodes = [{"type": "node", "data": {
            "user_id": bot_qq, "nickname": nick,
            "content": [{"type": "text", "data": {"text": c}}]}} for c in chunks]
        is_group = bool(gid)
        action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
        target = {"group_id": int(gid)} if is_group else {"user_id": int(event.get_sender_id())}
        started = time.time()
        if bot is not None:
            try:
                # 必须显式带 self_id：本机同时接着 qqbot/微信/飞书几个适配器，
                # aiocqhttp 有多个连接时靠它选通道，不带就抛 ApiNotAvailable
                extra = {"self_id": int(bot_qq)} if bot_qq.isdigit() else {}
                res = await asyncio.wait_for(
                    bot.call_action(action, messages=nodes, **target, **extra), timeout=20.0)
                if isinstance(res, dict) and res.get("status") == "failed":
                    logger.warning("[fish] 合并转发被拒：%s", str(res)[:150])
                else:
                    logger.info("[fish] 合并转发 %d 节点（%.1fs）", len(nodes), time.time() - started)
                    return
            except asyncio.TimeoutError:
                # 协议端常常消息已经发出去、结果迟迟不回；超时先回头看历史，
                # 免得又补一坨长文本变成双份
                if is_group and await self._forward_landed(bot, gid, started):
                    logger.info("[fish] 合并转发回得慢但已落地，不再补发")
                    return
                logger.warning("[fish] 合并转发超时且查不到历史")
            except Exception as e:
                logger.warning("[fish] 合并转发异常：%s: %s", type(e).__name__, e)
        for c in chunks:      # 兜底：逐条发，别把答案丢了
            await event.send(MessageChain(chain=[Plain(c)]))
            await asyncio.sleep(0.8)

    async def _forward_landed(self, bot, gid: str, since: float) -> bool:
        """超时兜底：查最近几条群消息里有没有自己刚发的转发卡片。"""
        try:
            res = await asyncio.wait_for(
                bot.call_action("get_group_msg_history", group_id=int(gid), count=6), timeout=6.0)
        except Exception:
            return False
        for msg in reversed((res or {}).get("messages") or []):
            if float(msg.get("time") or 0) < since - 10:
                continue
            for seg in msg.get("message") or []:
                if isinstance(seg, dict) and seg.get("type") in ("json", "forward", "node"):
                    return True
        return False

    @staticmethod
    def _split_for_forward(text: str, limit: int = 420) -> list[str]:
        """按段落切，段落太长再按句子切，保证每条不超过 limit。"""
        parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        out, buf = [], ""
        for p in parts:
            while len(p) > limit:
                cut = p.rfind("。", 0, limit)
                cut = cut + 1 if cut > limit // 3 else limit
                if buf:
                    out.append(buf); buf = ""
                out.append(p[:cut]); p = p[cut:]
            if len(buf) + len(p) + 1 <= limit:
                buf = f"{buf}\n{p}" if buf else p
            else:
                out.append(buf); buf = p
        if buf:
            out.append(buf)
        return out[:12]     # 再多就不是回答了，是抄书

    async def _flush_tail(self, event: AstrMessageEvent, tail: dict) -> None:
        if tail:
            parts = tail.get("parts") or []
            gaps = splitter.plan_gaps(
                len(parts),
                float(self.cfg.get("split_gap_min", 0.6) or 0.6),
                float(self.cfg.get("split_gap_max", 1.5) or 1.5),
            )
            sent_all = True
            for i, part in enumerate(parts):
                gap = gaps[i] if i < len(gaps) else 0.8
                await asyncio.sleep(gap)
                sent = False
                for attempt in (1, 2):
                    try:
                        await event.send(MessageChain(chain=[Plain(part)]))
                        logger.info("[fish] 拆条第 %d/%d 条补发（间隔 %.1fs）：%s",
                                    i + 2, len(parts) + 1, gap, part[:24])
                        sent = True
                        break
                    except (asyncio.TimeoutError, TimeoutError) as e:
                        # 超时只说明没等到回包，消息很可能已经进群了。这时重发就是「同一条说两遍」，
                        # 宁可少一条，也不刷屏——所以超时一律不重试
                        logger.warning("[fish] 补发第 %d/%d 条等回包超时，按已发出处理：%s",
                                       i + 2, len(parts) + 1, e)
                        sent = True
                        break
                    except (ConnectionError, OSError) as e:
                        # 连接层的错误是「确定没送出去」，可以放心重试一次
                        if attempt == 1:
                            await asyncio.sleep(0.4)
                            continue
                        logger.warning("[fish] 补发第 %d/%d 条连接失败，剩余 %d 条丢弃: %s",
                                       i + 2, len(parts) + 1, len(parts) - i, e)
                    except Exception as e:
                        # 其它异常（超长/被平台拒/内容审核）重试也不会变好，别白等
                        logger.warning("[fish] 补发第 %d/%d 条失败，剩余 %d 条丢弃: %s",
                                       i + 2, len(parts) + 1, len(parts) - i, e)
                        break
                if not sent:
                    sent_all = False
                    break
            # 半句话断掉之后再单独冒一张表情包是最难看的组合，所以只在全部发出后才补图
            meme = tail.get("meme") if sent_all else None
            if meme:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(meme)]))
                    logger.info("[fish] 拆条后补发表情包")
                except Exception as e:
                    logger.warning("[fish] 表情包补发失败: %s", e)
            drawn = tail.get("drawn") if sent_all else None
            if drawn:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(drawn)]))
                    logger.info("[fish] 拆条后补发画好的图")
                except Exception as e:
                    logger.warning("[fish] 画图补发失败: %s", e)
            pic = tail.get("pixiv") if sent_all else None
            if pic:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(pic)]))
                    logger.info("[fish] 拆条后补发 pixiv 图")
                except Exception as e:
                    logger.warning("[fish] pixiv 补发失败: %s", e)
