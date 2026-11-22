# -*- coding: utf-8 -*-
"""媒体与引用：识图、转发、Pixiv、语音转写（media.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class MediaMixin:
    # 框架把图 / 合并转发解析成这些占位字符串，光看 message_str 等于什么都没看见。
    # 拆分 Mixin 时这个类常量留在旧 main 里没跟过来，_quote_context 一取引用就
    # AttributeError，被外层 except 吞成 debug——引用原文整段没进过 prompt
    _QUOTE_PLACEHOLDERS = ("[图片]", "[聊天记录]", "[转发消息]", "[语音]", "[视频]",
                           "[文件]", "[动画表情]", "[表情]")

    async def _vision_via_framework(self, raws: list) -> str:
        """把图片交给 AstrBot 配好的视觉 provider 描述，失败返回空串（外层转直连）。"""
        pid = str(self.cfg.get("vision_provider_id") or "gemini-vision")
        prov = self.context.get_provider_by_id(pid)
        if prov is None:
            logger.warning("[fish] 框架里没有 provider %s，视觉转直连", pid)
            return ""
        urls = []
        for raw in raws:
            if not raw:
                continue
            mime = "image/png" if raw[:8].startswith(b"\x89PNG") else "image/jpeg"
            urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode()}")
        if not urls:
            return ""
        resp = await prov.text_chat(
            prompt="仔细看这些图，用中文说清画面内容：有没有人、在做什么、是什么表情或梗；判不准就说看不出。",
            image_urls=urls,
        )
        return str(getattr(resp, "completion_text", "") or "").strip()

    async def _warm_vision(self) -> None:
        """开机空跑一次视觉请求，把 DNS/TLS/代理连接先建起来。

        实测同一张图冷启动 5.6 秒、预热后 1.2~3.4 秒。不预热的话，重启后
        群里发来的第一张图会连吃三个超时，等于刚上线就不识图。
        """
        await asyncio.sleep(6)   # 错开启动期其他初始化，别抢依赖
        try:
            cfg = dict(self.cfg)
            # 用一张真实的群表情包当样本（缩到 96px）：1x1 的空白 png 会被
            # Gemini 直接判 400，只热身了 TCP 却没验到 key 和出图路径
            sample = None
            try:
                import glob as _glob
                files = sorted(_glob.glob(str(PLUGIN_DIR / "fish_core" / "meme-pack" / "**" / "*.png"), recursive=True))
                if files:
                    with open(files[0], "rb") as f:
                        shrunk, _mime = vision._shrink(f.read(), max_side=96)
                    sample = base64.b64encode(shrunk).decode()
            except Exception:
                sample = None
            t0 = time.time()
            out = await vision.describe([sample or _WARM_PNG], cfg, logger)
            logger.info("[fish] 视觉预热完成（%.1fs，%s）",
                        time.time() - t0, "可用" if out else "本次没出描述，不影响后续")
        except Exception as e:
            logger.debug("[fish] 视觉预热失败（不影响使用）：%s", e)

    async def _grab_pixiv(self, tag: str, gid: str = "") -> str:
        """按标签取一张全年龄原图，返回 base64；任何一步不对都返回空串。

        R18 的判定全在 fish_core/pixiv.py 里（只信 xRestrict/sl 两个官方字段）。
        这里只负责：拿 cookie、控频、把字节转成能塞进消息链的 base64。
        失败一律静默——发不出图只是少一张，不能连累正文
        """
        try:
            cookie = str(self.cfg.get("pixiv_cookie") or "") or _media_parser_pixiv_cookie()
            if not cookie:
                logger.info("[fish] pixiv 没配 cookie，发图跳过")
                return ""
            now = time.time()
            # 冷却按群隔离：只用 tag 当 key 时，A 群问过一句，B 群两秒内同标签直接被拒
            cd_key = f"{gid}:{tag}"
            if not self._pixiv_cd.allow(cd_key, now):
                logger.info("[fish] pixiv 标签「%s」本群刚搜过，%d 秒内不重复搜",
                            tag, int(self._pixiv_cd.window))
                return ""
            proxy = str(self.cfg.get("pixiv_proxy") or "") or None
            # 整条取图给一个硬上限：3 个候选 × 详情 + 下载，最坏也能拖到几十秒，
            # 而这一步是在拼消息链之前同步 await 的，卡住整条回复就发不出去
            async with aiohttp.ClientSession() as sess:
                got = await asyncio.wait_for(
                    pixiv.fetch_one(sess, tag, cookie, proxy),
                    timeout=float(self.cfg.get("pixiv_timeout_sec", 25) or 25))
            if not got:
                logger.info("[fish] pixiv 搜「%s」没拿到可用图（全被分级闸掉或搜不到）", tag)
                return ""
            data, pid, title = got
            # 只有真取到图才记账，失败别白占窗口
            self._pixiv_cd.mark(cd_key, now)
            logger.info("[fish] pixiv 发图：标签「%s」→ %s 《%s》 %.0fKB",
                        tag, pid, title[:20], len(data) / 1024)
            return base64.b64encode(data).decode()
        except asyncio.TimeoutError:
            logger.info("[fish] pixiv 取图超时（%ss），本次不带图", self.cfg.get("pixiv_timeout_sec", 25))
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning("[fish] pixiv 取图异常：%s: %s", type(e).__name__, e)
            return ""

    async def _quote_audio_desc(self, event, seg_data: dict | None = None,
                                comp=None) -> str:
        """引用的那段语音听一下，返回转写文字（可能为空）。

        坑一：语音段里的 file 往往只是文件名，真正能取的是 url；NapCat 有时连 url
        都不给，那就走 get_record 让它按 mp3 导出——但导出的是容器上的本地路径，
        我们在另一台机器上读不到，只认它回的 url。
        坑二：silk/amr ffmpeg 不认 silk，所以优先要 mp3 导出。
        """
        if not self.cfg.get("hearing_enabled", True):
            return ""
        cands: list[str] = []
        for src in ((seg_data or {}).get("url"), (seg_data or {}).get("file"),
                    getattr(comp, "url", None), getattr(comp, "file", None),
                    getattr(comp, "path", None)):
            s = str(src or "").strip()
            if s and s not in cands:
                cands.append(s)
        deadline = time.time() + float(self.cfg.get("hearing_timeout_sec", 30) or 30)
        raw = b""
        for s in cands:
            if s.lower().startswith(("http://", "https://")):
                raw = await vision.download_audio(s, logger=logger)
            elif s.startswith("/") and os.path.isfile(s):
                # 框架自己下到 data/temp 的那种，直接读
                try:
                    with open(s, "rb") as f:
                        raw = f.read(16 * 1024 * 1024)
                except Exception:
                    raw = b""
            if raw:
                break
        if not raw:
            # 兜底：让 NapCat 转成 mp3 再给个能取的地址
            try:
                fid = str((seg_data or {}).get("file") or getattr(comp, "file", "") or "")
                if fid:
                    res = await asyncio.wait_for(
                        event.bot.call_action("get_record", file=fid, out_format="mp3"), 8)
                    got = ((res or {}).get("data") or {})
                    url2 = str(got.get("url") or got.get("file") or "")
                    if url2.lower().startswith(("http://", "https://")):
                        raw = await vision.download_audio(url2, logger=logger)
                    elif url2.startswith("/") and os.path.isfile(url2):
                        with open(url2, "rb") as f:
                            raw = f.read(16 * 1024 * 1024)
            except Exception as e:
                logger.info("[fish] get_record 兜底失败：%s: %s", type(e).__name__, str(e)[:60])
        if not raw:
            logger.info("[fish] 引用的语音取不到内容，当没听见")
            return ""
        # 转写复用 hearing.py：silk 解码 + ffmpeg 转码 + Gemini 直连都在那儿，
        # 别为「引用的语音」再养一条并行的转写链
        try:
            text = await asyncio.wait_for(
                hearing.transcribe(raw, self.cfg, logger=logger),
                max(3.0, deadline - time.time()))
        except Exception as e:
            logger.info("[fish] 引用语音转写失败：%s: %s", type(e).__name__, str(e)[:60])
            return ""
        return (text or "").strip()

    async def _quote_image_desc(self, url: str) -> str:
        """引用的那张图自己看一眼。缩图 + 走 WB 识图链，1~4 秒。"""
        if not url or not self.cfg.get("vision_enabled", True):
            return ""
        try:
            raw = await vision.component_bytes(_ImgRef(url), logger=logger)
            if not raw:
                return ""
            desc = await vision.describe([base64.b64encode(raw).decode()], self.cfg, logger=logger)
            desc = (desc or "").strip()
            if desc:
                logger.info("[fish] 引用图片识图完成（%d 字）：%s", len(desc), desc[:50])
            return desc
        except Exception as e:
            logger.warning("[fish] 引用图片识图失败：%s: %s", type(e).__name__, str(e)[:80])
            return ""

    async def _fetch_forward(self, event, fid: str):
        """拉合并转发内容。

        坑：转发卡片这一层的 id 不是 get_forward_msg 认的 id，直接拿消息 id 去查
        会回「消息已过期或者为内层消息」。得先 get_msg 拿到 json 卡片里的
        meta.detail.resid，再用它查；实测只有 resid 那条路能出节点。
        """
        try:
            return await asyncio.wait_for(
                event.bot.call_action("get_forward_msg", message_id=fid), 6)
        except Exception as e:
            logger.info("[fish] 转发直查失败（%s）：%s，改查 resid", fid, str(e)[:50])
        try:
            got = await asyncio.wait_for(event.bot.call_action("get_msg", message_id=fid), 6)
            for seg in (((got or {}).get("data") or {}).get("message") or []):
                if not isinstance(seg, dict) or seg.get("type") != "json":
                    continue
                raw = (seg.get("data") or {}).get("data") or ""
                card = json.loads(raw) if isinstance(raw, str) else (raw or {})
                resid = (((card.get("meta") or {}).get("detail") or {}).get("resid") or "")
                if resid:
                    return await asyncio.wait_for(
                        event.bot.call_action("get_forward_msg", message_id=resid), 6)
        except Exception as e:
            logger.info("[fish] 转发 resid 路也失败（%s）：%s", fid, str(e)[:60])
        return None

    async def _forward_text(self, event, fid: str) -> str:
        """合并转发 → 「昵称：内容 / …」。纯图转发会带上第一张图的描述。"""
        if not fid:
            return ""
        res = await self._fetch_forward(event, fid)
        txt = self._flatten_forward(res)
        # 纯图转发：只展开成「[图片]」的话模型完全不知道图里是什么
        if res and "[图片]" in txt:
            url = self._forward_first_image(res)
            if url:
                desc = await self._quote_image_desc(url)
                if desc:
                    txt += f"（转发的图：{desc}）"
        return txt

    async def _quote_detail(self, event, comp) -> str:
        """引用内容为空或只是占位符时，回查被引用的那条消息：文本 / 图片 / 语音 / 合并转发。"""
        mid = str(getattr(comp, "id", "") or "")
        texts: list[str] = []
        imgs: list[str] = []
        audios: list[tuple[dict, object]] = []
        for c in (getattr(comp, "chain", None) or []):
            n = type(c).__name__.lower()
            if n in ("plain", "text"):
                texts.append(str(getattr(c, "text", "") or ""))
            elif n == "image":
                u = str(getattr(c, "url", "") or getattr(c, "file", "") or "")
                if u:
                    imgs.append(u)
            elif n in ("record", "voice", "audio"):
                audios.append(({}, c))
            elif n in ("forward", "node", "nodes"):
                t = await self._forward_text(event, str(getattr(c, "id", "") or ""))
                if t:
                    texts.append(t)
        if not texts and not imgs and not audios and mid:
            try:
                res = await asyncio.wait_for(event.bot.call_action("get_msg", message_id=mid), 6)
                segs = ((res or {}).get("data") or {}).get("message") or []
                for seg in segs:
                    if not isinstance(seg, dict):
                        continue
                    st = seg.get("type")
                    d = seg.get("data") or {}
                    if st == "text":
                        texts.append(str(d.get("text") or ""))
                    elif st == "image":
                        u = str(d.get("url") or d.get("file") or "")
                        if u:
                            imgs.append(u)
                    elif st == "record":
                        audios.append((d, None))
                    elif st in ("forward", "node", "nodes"):
                        t = await self._forward_text(event, str(d.get("id") or ""))
                        if t:
                            texts.append(t)
            except Exception as e:
                logger.info("[fish] 引用回查失败（%s）：%s", mid, str(e)[:60])
        out = "".join(texts).strip()
        if imgs:
            desc = await self._quote_image_desc(imgs[0])
            out = (out + " " if out else "") + (
                f"（引用的图：{desc}）" if desc else "（引用了一张图，没看清内容）")
        if audios:
            seg_data, comp_obj = audios[0]
            heard = await self._quote_audio_desc(event, seg_data=seg_data, comp=comp_obj)
            # 「仅供参考」是硬要求：转写错字不少，模型别把它当原话复述
            out = (out + " " if out else "") + (
                f"（引用的语音，转写仅供参考、可能有错字：{heard}）" if heard
                else "（引用了一段语音，没听清）")
        return out

    async def _quote_context(self, event) -> str:
        """把「引用回复」和「合并转发」的内容取出来放进 prompt。

        框架其实已经调过 get_msg，Reply 段上直接带了被引用的原文（message_str），
        鱼以前只拿 sender_id 判「是不是回我」，原文全丢了；合并转发更惨，
        段里只有一个 id，内容从来没人拉过。所以群里「你看看这个」+ 引用发过来，
        鱼只看见三个字。

        引用图片 / 引用聊天记录时 message_str 只有「[图片]」「[聊天记录]」这种占位符，
        等于没引用——这时候回查原消息，图片直接走识图链看一眼。
        """
        parts = []
        for comp in (event.message_obj.message or []):
            kind = type(comp).__name__.lower()
            if kind == "reply":
                qt = str(getattr(comp, "message_str", "") or "").strip()
                who = str(getattr(comp, "sender_nickname", "") or "").strip()
                if not qt or qt in self._QUOTE_PLACEHOLDERS:
                    # 占位符 = 框架没给正文，回查原消息
                    logger.info("[fish] 引用只有占位符 %r，回查原消息", qt)
                    qt = await self._quote_detail(event, comp)
                    if not qt and str(getattr(comp, "id", "")):
                        logger.info("[fish] 引用回查空（消息 %s 可能已撤回/超时）",
                                    str(getattr(comp, "id", "")))
                if qt:
                    parts.append(f"【他引用了{who or '某人'}的消息】{qt[:400]}")
            elif kind in ("forward", "nodes", "node"):
                fid = str(getattr(comp, "id", "") or getattr(comp, "message_id", "") or "")
                txt = await self._forward_text(event, fid)
                if txt:
                    parts.append(f"【他发了一份聊天记录】{txt[:400]}")
                else:
                    # 内容取不到的转发别写成「（内容没取到）」：模型看见这句话会
                    # 顺着评一句「发的什么玩意」，等于对着空白瞎评价。直接当没看见
                    logger.info("[fish] 合并转发内容取不到（id=%s），这条不喂给模型", fid[:24])
        return "\n".join(parts)

    @staticmethod
    def _flatten_forward(res) -> str:
        """把 get_forward_msg 的节点列表拍成「昵称：内容 / 昵称：内容」。"""
        data = (res or {}).get("data") if isinstance(res, dict) else None
        data = data or res or {}
        if not isinstance(data, dict):
            return ""
        nodes = data.get("messages") or data.get("message") or []
        lines = []
        for node in nodes[:12]:
            if not isinstance(node, dict):
                continue
            sender = (node.get("sender") or {}).get("nickname") or node.get("nickname") or ""
            segs = node.get("message") or node.get("content") or []
            if isinstance(segs, str):
                segs = [{"type": "text", "data": {"text": segs}}]
            buf = []
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                st = seg.get("type")
                d = seg.get("data") or {}
                if st == "text":
                    buf.append(str(d.get("text") or ""))
                elif st == "image":
                    buf.append("[图片]")
                elif st in ("face", "mface"):
                    buf.append("[表情]")
                elif st == "at":
                    buf.append("@" + str(d.get("qq") or ""))
            t = "".join(buf).strip()
            if t:
                lines.append(f"{sender}：{t[:80]}")
        return " / ".join(lines)

    @staticmethod
    def _forward_first_image(res) -> str:
        """合并转发的节点里第一张图的 url。纯图转发全靠它。"""
        data = (res or {}).get("data") if isinstance(res, dict) else None
        data = data or res or {}
        if not isinstance(data, dict):
            return ""
        for node in (data.get("messages") or data.get("message") or [])[:12]:
            if not isinstance(node, dict):
                continue
            segs = node.get("message") or node.get("content") or []
            if isinstance(segs, str):
                continue
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "image":
                    d = seg.get("data") or {}
                    u = str(d.get("url") or d.get("file") or "")
                    if u:
                        return u
        return ""

    async def _image_desc(self, gid: str, event) -> str:
        """这条消息里的图，识别一次就够了。

        时间线回填和「要不要回这条」会各要一次：不共享任务的话每次多烧一次
        视觉调用，而视觉本来就慢（12 秒超时），重复调更容易撞 429。
        """
        key = self._msg_key(gid, event)
        task = self._vision_jobs.get(key) if key else None
        if task is None:
            try:
                imgs = [c for c in event.message_obj.message if isinstance(c, Image)]
            except Exception:  # noqa: BLE001
                return ""
            if not imgs:
                return ""
            task = asyncio.create_task(vision.from_components(imgs, self.cfg, logger=logger))
            if key:
                self._vision_jobs[key] = task
                # 别让字典无界长：消息 id 只增不减
                if len(self._vision_jobs) > 64:
                    for _k in list(self._vision_jobs)[:32]:
                        self._vision_jobs.pop(_k, None)
        try:
            return await task or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("[fish] 识图失败：%s", e)
            return ""

    async def _backfill_image_desc(self, gid: str, event, msg_entry: dict, dlg_entry: dict) -> None:
        """识图完把描述写回时间线那两条记录，后续消息就能看见图里是什么。"""
        try:
            desc = (await self._image_desc(gid, event) or "").strip()
            if not desc:
                return
            shown = f"[图片：{desc[:80]}]"
            msg_entry["text"] = shown
            dlg_entry["text"] = shown
            logger.info("[fish] 群 %s 图片回填时间线：%s", gid, desc[:60])
        except Exception as e:  # noqa: BLE001
            logger.debug("[fish] 图片回填失败：%s", e)
