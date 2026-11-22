# -*- coding: utf-8 -*-
"""模型输出整形：装饰、拆条、记账、落库（respond.py，从 main.py 逐字搬出，逻辑未改）。"""

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


def _pick_meme(tag: str, text: str = ""):
    """按标签挑图：标签是场合（冷场/调侃/接梗/自嘲）就走场景桶，否则按情绪挑。

    两个池子的区别不能混：pick_b64 吃「嘲讽/得意」这种情绪，冷场时甩嘲讽图
    等于往冷场里再浇一盆冷水，所以场合优先。
    """
    try:
        if tag in getattr(meme_catalog, "SCENES", {}):
            return meme_catalog.pick_scene_b64(tag, text)
        return meme_catalog.pick_b64(tag, text)
    except Exception as e:
        logger.debug("[fish] 按标签挑图失败 %s：%s", tag, e)
        return None


class RespondMixin:
    # 装饰器统一挂在 main.py（Star 所在模块），这里只留实体：
    # AstrBot 按 handler_module_path 查 star_map，mixin 模块查不到就被整条丢掉
    async def on_decorate(self, event: AstrMessageEvent):
        # 这个钩子是「最后一道出口」。以前没有任何兜底：任一子模块抛异常，
        # 异常被框架吞掉但 result 原封不动留在原地，未清洗的原文（带 [PASS]、
        # 表情标签、超长、违禁词）就照原样发进群了——线上真发生过
        meta = event.get_extra("fish_pending")
        # 打字延迟 + 过时重判：模型已经写完，但人情上还得「打字」几秒。
        # 这几秒里群里换了话题就别发了——晚一句正确的废话，比早一句错的更伤
        if not await self._typing_gate(event, meta):
            return
        try:
            await self._decorate_impl(event)
        except Exception as e:
            logger.exception("[fish] 出口加工异常，走兜底清洗: %s", e)
            self._decorate_fallback(event, meta)

    async def _send_presence(self, event, gid: str, intent, nick: str = "",
                             uid: str = "") -> bool:
        """在场反应：一张表情，挑不到图就一个短词。不调主模型、不记成发言轮次。

        为什么不记轮次：发言率闸算的是「鱼说了几轮」，在场只是「嗯」一下，
        算进去会让真实回复被自己的表情挤掉，群里就变成只会发表情不会说话。
        """
        scene = str(getattr(intent, "trigger", "") or "接梗")
        short = str((getattr(intent, "stats", None) or {}).get("short") or "")
        b64 = None
        try:
            if self.cfg.get("meme_enabled", True):
                b64 = meme_catalog.pick_scene_b64(scene, "")
        except Exception as e:
            logger.debug("[fish] 在场挑图失败：%s", e)
        try:
            if b64:
                presence.mark(gid, "[在场:%s]" % scene)
                self.log_event("presence", gid, "[在场:%s]" % scene, who=nick,
                               why=getattr(intent, "reason", ""))
                event.set_result(MessageEventResult(chain=[Image.fromBase64(b64)]))
                return True
            if short:
                presence.mark(gid, short)
                self.log_event("presence", gid, short, who=nick,
                               why=getattr(intent, "reason", ""))
                event.set_result(MessageEventResult(chain=[Plain(short)]))
                return True
        except Exception as e:
            logger.debug("[fish] 在场发送失败：%s", e)
        return False

    async def _typing_gate(self, event, meta) -> bool:
        """出口前的最后一道：按字数等一会儿，再确认这句话还该不该发。

        返回 True = 照常往下走（发送）；False = 已经处理完（清空结果 + stop）。
        任何异常都按「照常发」处理：宁可多一句，也不能让一句正常回复卡死在这里。
        """
        turn = event.get_extra("fish_turn")
        if not turn:
            return True
        try:
            raw = ""
            res = event.get_result()
            if res and getattr(res, "chain", None):
                raw = "".join(c.text for c in res.chain if isinstance(c, Plain))
            gid = str(turn.get("gid") or (meta or {}).get("gid") or "")
            if not raw.strip() or not gid:
                return True
            st = self._state(gid)
            draft = generator.make_draft(
                text=raw, now=time.time(), ver=int(turn.get("ver") or 0),
                intent_id=int(turn.get("intent_id") or 0), mode=str(turn.get("mode") or ""),
                priority=int(turn.get("prio") or 0), uid=str(turn.get("uid") or ""),
                gid=gid, cfg=self.cfg)
            # 草稿只留一份/群，后到的冲掉先到的（不做并行生成，两句话打架更难看）
            generator.store(st, draft, logger=logger)
            if draft.delay > 0:
                await asyncio.sleep(draft.delay)
            names = tuple(addressee.names_of(gid, str(event.get_self_id())))
            if self.cfg.get("bot_nickname"):
                names = names + (str(self.cfg.get("bot_nickname")),)
            action, why = generator.revalidate(
                draft, now=time.time(), ver=int(getattr(st, "msg_ver", 0) or 0),
                dialogue=tuple(st.dialogue)[-20:], names=names, cfg=self.cfg)
            draft.revalidate, draft.reason = action, why
            generator.take(st)
            logger.info("[fish][draft] 群 %s %s", gid, draft.log_line())
            # 顺手挂到事件上：线上排障时能直接看到「这句为什么没发」，
            # 模拟里也靠它把每一轮的延迟/重判结论对回场景上
            event.set_extra("fish_draft_log", draft.log_line())
            if action == generator.SEND:
                event.set_extra("fish_turn", None)
                return True
            # 不发：清空结果并 stop。只清空不 stop 的话，框架看到「没发过 + 被@」
            # 会拿默认人格再答一遍，群里就冒出一条不是它说的回复
            try:
                self._release_claim(st, (meta or {}).get("claim_tok"))
            except Exception:
                pass
            event.set_result(MessageEventResult(chain=[]))
            event.stop_event()
            try:
                self.log_event("skip", gid, str(raw)[:60],
                               who=str((meta or {}).get("nick") or ""),
                               why="打字延迟期间%s（%s）" % (action, why))
            except Exception:
                pass
            return False
        except Exception as e:
            logger.warning("[fish] 打字延迟判定异常，按原样发：%s: %s", type(e).__name__, e)
            return True

    def _decorate_fallback(self, event: AstrMessageEvent, meta) -> None:
        """出口异常时只做三件事：放回冷却位、PASS 检测、截断。宁可少说一句。"""
        try:
            if meta:
                # 带 token：兜底只还自己占的那一位，别把并发那条的占位一起抹掉
                self._release_claim(self._state(meta.get("sk") or meta["gid"]),
                                    meta.get("claim_tok"))
        except Exception:
            pass
        raw = ""
        try:
            res = event.get_result()
            if res and res.chain:
                raw = "".join(c.text for c in res.chain if isinstance(c, Plain))
        except Exception:
            raw = ""
        try:
            if not raw.strip() or safety_guard.is_pass_signal(raw):
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                return
            cut = (composer.sanitize_reply(raw) or "").strip()[:200]
            # 异常兜底最该拦隐私：这条路上没有后续任何一道闸，
            # 模型原文里的内网地址、机器路径、配置文件名会直接被发出去
            if cut and safety_guard.leaks_private(cut):
                logger.info("[fish] 兜底内容命中隐私词，丢弃")
                cut = ""
            event.set_result(MessageEventResult(chain=[Plain(cut)] if cut else []))
        except Exception:
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()

    async def _mark_llm_t0(self, event: AstrMessageEvent, req):
        """记下请求发出的时刻：输出速度要用它算，框架不给我们时长。"""
        try:
            event.set_extra("fish_llm_t0", time.time())
        except Exception:
            pass

    async def _log_cache(self, event: AstrMessageEvent, resp):
        """把每轮真实 token 和缓存命中记下来。

        网关面板不归我们管，猜它为什么 0% 只能靠这本账：
        命中为 0 且输入很长 → 前缀被谁改了；命中接近输入 → 缓存真在工作
        """
        inp, hit, outp = _usage_of(resp)
        if not inp:
            return
        try:
            pct = hit / inp * 100 if inp else 0
            logger.info("[fish] token 命中 %d / 输入 %d / 输出 %d（%s %.0f%%）",
                        hit, inp, outp, "前缀复用" if hit else "零命中", pct)
            # 落库给 /tocken 用：日志会被轮转，账本不会
            t0 = event.get_extra("fish_llm_t0")
            ms = int((time.time() - t0) * 1000) if t0 else 0
            self.memory.log_token("chat", inp, hit, outp, ms)
        except Exception:
            pass

    async def _decorate_impl(self, event: AstrMessageEvent):
        meta = event.get_extra("fish_pending")
        if not meta:
            # 不是本插件产生的回复。以前直接 return，结果戳一戳那条路上模型手写的
            # [表现:委屈] 原样进了群（16:46:59 真实事故）。这里只剥标签，不动正文
            try:
                res = event.get_result()
                if res and res.chain:
                    cleaned = []
                    for c in res.chain:
                        if isinstance(c, Plain):
                            # 只剥内部标签，别做整段清洗：兄弟插件的英文回复和 @ 要留着
                            t = _TAG_ONLY_RE.sub("", c.text or "").strip()
                            if t:
                                cleaned.append(Plain(t))
                        else:
                            cleaned.append(c)
                    if len(cleaned) != len(res.chain):
                        event.set_result(MessageEventResult(chain=cleaned))
            except Exception as e:
                logger.debug("[fish] 无 meta 出口兜底失败：%s", e)
            return  # 不是本插件产生的回复（例如 ab! 唤醒的 AstrBot 原生回复），一律不碰
        event.set_extra("fish_pending", None)

        # 状态键由 handler 带过来：私聊是 private:uid（按人分），群里就是群号
        gid = meta.get("sk") or meta["gid"]
        st = self._state(gid)
        # 本次占位的 token：只有它自己能回滚冷却位（poke 这类没 claim 的传 None，不会误伤别人）
        _tok = meta.get("claim_tok")
        # 冷场：上一条人话离现在多久（阈值和 prompt.py 的垫场话术同一套）
        _cold = False
        if not str(gid).startswith("private") and self.cfg.get("cold_atmosphere_enabled", True):
            try:
                _dlg = list(st.dialogue)
                _prev = _dlg[-2] if len(_dlg) >= 2 else None
                _gap = (time.time() - float((_prev or {}).get("ts") or 0)) if _prev else 0.0
                _cold = _gap >= float(self.cfg.get("cold_field_gap_sec", 900) or 900)
            except Exception:
                _cold = False

        result = event.get_result()
        if not result or not result.chain:
            # 模型什么都没产出：冷却位必须还回去，否则这条之后群里白静默五分钟
            logger.info("[fish] 群 %s 这轮没有产出（空链），释放冷却位", gid)
            self._release_claim(st, _tok)
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if not text.strip():
            logger.info("[fish] 群 %s 这轮只有非文本内容，释放冷却位", gid)
            self._release_claim(st, _tok)
            return

        # 0. 检索指针：模型自己判断这题得查，就吐 [搜索:关键词]。
        # 这轮它的原话作废（可能只有指针，也可能是「等我查查」），改走检索通道；
        # 指针本身绝不能进群，所以先抠掉再决定去留
        _sm = _SEARCH_MARK_RE.search(text)
        if _sm and not self.cfg.get("research_enabled", True):
            # 检索关了：指针当垃圾扔掉，回一句就得走人。
            # 以前只 set_result 不 return，后面「清洗后无正文」那一闸把
            # 这句又覆盖成空链，用户看到的是彻底沉默
            text = _SEARCH_MARK_RE.sub("", text).strip()
            event.set_result(MessageEventResult(
                chain=[Plain(text or "我查不了，联网关着呢")]))
            event.stop_event()
            self._release_claim(st, _tok)
            return
        if _sm:
            _q = (_sm.group(1) or "").strip() or (meta.get("text") or "")
            text = _SEARCH_MARK_RE.sub("", text).strip()
            logger.info("[fish] 模型要查「%s」，转检索通道", _q[:40])
            self._release_claim(st, _tok)
            # 先把 event 上挂的原文清掉再 await：检索途中一旦抛异常，
            # on_decorate 的兜底会把 event 里的原文（含 [搜索:...]）直接发进群
            event.set_result(MessageEventResult(chain=[]))
            try:
                await self._answer_with_research(
                    event, gid, meta.get("uid", ""), meta.get("nick", ""),
                    _q, st=st, user_text=str(meta.get("text") or ""))
            except Exception as e:  # noqa: BLE001
                logger.error("[fish] 检索通道异常：%s: %s", type(e).__name__, e)
            event.stop_event()
            return

        # 0.4 发图指针：[看图:角色名/标签] → pixiv 捞一张。主动触发专用，
        # 代码不猜「什么时候该发图」，只认模型写出来的标签。指针抠干净再往下走
        pixiv_tag = ""
        _pm = _PIXIV_MARK_RE.search(text)
        if _pm:
            pixiv_tag = (_pm.group(1) or "").strip()[:30]
            text = _PIXIV_MARK_RE.sub("", text).strip()
            logger.info("[fish] 模型要发图，标签「%s」", pixiv_tag)
            # 只写了指针没写话：给一句最短的兜底，否则下面「清洗后无正文」会整条吞掉
            text = text or "喏，这张"

        # 0. PASS 闸：这是模型「这轮我不接」的内部信号，一个字符都不能进群。
        # 以前清洗函数把它削成空串，但后果被 `if text2:` 丢掉、回退成原文，
        # 于是群里会看到一条孤零零的 [PASS]——真实发生过，只是概率低
        if safety_guard.is_pass_signal(text):
            logger.info("[fish] 群 %s 模型给出 PASS，这轮静默", gid)
            self.log_event("pass", gid, meta.get("text", "")[:40], who=meta.get("nick", ""))
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 1. 剥表情标签：模型经常写 [MEME:嘲讽]，不剥就原样发进群
        clean, tag = meme_catalog.extract_tag(text)
        # 1.1 剥违规上报标签：模型判完写 [REPORT:广告|理由]，这个标记一个字都不能进群
        clean, report_hit = _extract_report(clean)
        # 1.2 剥画图标签：模型写 [画图:喜报:今天没人迟到]，这句要变成一张真的图
        clean, draw_req = meme_gen.extract_tag(clean)
        if report_hit:
            label = "：".join([p for p in report_hit if p])
            if self._order_watch(gid):
                asyncio.create_task(self._report_order(
                    event, gid, meta.get("nick", ""), meta.get("uid", ""),
                    meta.get("text", ""), label,
                ))
            else:
                logger.info("[fish] 群 %s 判出违规（%s），但该群没开上报", gid, label)

        # 1.5 只发图：模型只回一个 [MEME:xxx] 的时候，这是「表情也是一句话」的正常用法。
        # 以前 clean 为空会被当成「清洗后没正文」直接静默，等于把表情包吞掉了
        if tag and not clean.strip() and self.cfg.get("meme_enabled", True):
            now0 = time.time()
            ok_meme, why_not = self._meme_gate(gid, st, tag, now0)
            b64 = _pick_meme(tag, text) if ok_meme else None
            if b64:
                st.last_meme_ts = now0
                st.recent_meme_tags.append(tag)
                # 统一记账：以前这段自己改状态，唯独没压 ctx，
                # 下一轮它根本不知道刚发过一张图，同一张图能连发三次
                self._record_spoke(gid, meta.get("uid", ""), f"[只发图:{tag}]",
                                   reason=meta.get("reason", ""), nick=meta.get("nick", ""))
                event.set_result(MessageEventResult(chain=[Image.fromBase64(b64)]))
                return
            logger.info("[fish] 想只发图但没成（%s），走正常流程", why_not if not ok_meme else "没挑到图")

        # 兜底：不管模型把指针写成什么怪样子（超长、未闭合、跨行），都不许进群。
        # 主正则抓不到的那种，这里按前缀整段抠掉，宁可少一句话
        clean = _SEARCH_LEFTOVER_RE.sub("", clean or "").strip()
        # 没闭合的发图指针同样不能进群
        clean = _PIXIV_LEFTOVER_RE.sub("", clean).strip()
        # 画图标签剥完正文就空了：正文本来该是空的，绝不能回退成「带标签的原句」——
        # 回退之后标签会被当正文发进群（实测「[画图:喜报:今天没人迟到]」就是这么裸奔的）
        raw_text = clean or (meme_gen.extract_tag(text)[0].strip() if draw_req else text)
        text = composer.sanitize_reply(raw_text)
        # 出口清洗：内部独白、动作旁白、Markdown 痕迹在这剥掉。
        # URL 和代码块会先变占位符再还原，联网搜到的内容不会被误删
        try:
            text2 = safety_guard.strip_internal_monologue(text)
            _changed, text2 = safety_guard.sanitize_outgoing_text(text2)
            # 空串也是清洗的结果，必须采纳：以前判 `if text2` 才赋值，
            # 于是「整句都是（甩了甩尾巴）」这种原文会被原样发进群
            if text2 is not None:
                text = text2
            # 骂人词降级：模型写嗨了带出「傻逼/死全家」这类直白词，换成谐音，
            # 而不是把整句丢掉——丢掉会让它越答越短，最后只剩一句「急什么」
            if text.strip():
                text = quarrel.soften(text)
            text = self._strip_repeated_opener(st, text)
            # 调侃许可的两道出口闸（不靠提示词，提示词保证不了）：
            # ① 禁区属性兜底——外貌/性别/家里/工作能力，任何级别都不许出现
            # ② 自嘲指纹去重——跟最近几句撞得太像就删掉那句，其余照发
            if self.cfg.get("tease_guard_enabled", True) and text.strip():
                _bad = tease.banned_attribute(text)
                if _bad:
                    logger.info("[fish] 群 %s 草稿碰到禁区（%s），整句不发：%r",
                                gid, _bad, text[:30])
                    self.log_event("tease_ban", gid, text[:40], who=meta.get("nick", ""))
                    event.set_result(MessageEventResult(chain=[]))
                    event.stop_event()
                    self._release_claim(st, _tok)
                    return
                if tease.looks_like_self_tease(text):
                    _keep, _drop = tease.strip_repeat_self_tease(gid, text)
                    if _drop:
                        logger.info("[fish] 群 %s 自嘲撞车，删掉那句：%r", gid, _drop[:30])
                        self.log_event("tease_dup", gid, _drop[:40], who=meta.get("nick", ""))
                        text = _keep
                    if text.strip() and tease.allow_self_tease(gid, self.cfg):
                        tease.note_self_tease(gid, text, tease.next_topic(gid))
            _gid_ts = str(meta.get("gid") or "")
            if _gid_ts:
                self._last_bot_ts = {**getattr(self, "_last_bot_ts", {}), _gid_ts: time.time()}
        except Exception as e:
            logger.debug("[fish] 出口清洗失败：%s", e)
        if not text.strip():
            # 全句都是旁白/占位符，没有一个字是能对人说的：静默，别拿原文兜底
            logger.info("[fish] 群 %s 清洗后无正文（原文：%s），静默不发", gid, raw_text[:30])
            self.log_event("empty", gid, raw_text[:40], who=meta.get("nick", ""))
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

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

        # 2.5 自说自话闸（只拦主动插话）：草稿跟刚才那几句一个词都不沾，说明它在自说自话。
        # 被 @ 或追问时不拦——那时候它有权把话题拐走。
        # 注意别拿「来消息」去判：群里换话题很常见，那样会把正常接茬全掐掉
        vision_desc = str(meta.get("vision") or "")
        if str(meta.get("reason") or "").startswith("主动接话"):
            try:
                # 比对范围必须跟模型看到的窗口一致：只拿最近 5 条去比，
                # 就会出现「草稿其实在接 10 分钟前那个话头，却被判离题」的误杀
                ctx = " ".join(str(m.get("text", "")) for m in list(st.msgs)[-40:-1])
                # 图里的内容根本不在 msgs 里：草稿在聊刚才那张图时，纯文字 ctx 一个词都对不上，
                # 「这图也敢往群里甩」「这眼神像在审判我」这种正经接茬会被判离题吞掉
                # （实测 90 分钟吃掉十来条，全是被判接了又咽回去）
                if vision_desc:
                    ctx = f"{ctx} {vision_desc}"
                if ctx and anti_template.is_offtopic(text, ctx):
                    # 只记录，不再整条丢：这道闸实测把「你这也太惨了」这种最自然的
                    # 安慰也判成离题，静默的代价比发一句稍微跑偏的话大得多
                    logger.info("[fish] 群 %s 插话草稿可能离题（放行）：%r", gid, text[:30])
            except Exception as e:
                logger.debug("[fish] 离题判定失败：%s", e)

        # 2.6 照抄闸：判官那句「怎么接」是给模型的指令，写得太像台词就会被原样念出去。
        # 真实案例：「你最近夜宵吃多了吧，怎么突然感叹胖了」——判官写的正是这一句，
        # 模型一个字的力气都没花。这种句子一发出去就是机器人味，宁可不发
        how = str(meta.get("how") or "")
        if how and self._echoes_hint(text, how):
            logger.info("[fish] 群 %s 复述了「怎么接」，这句不发：%r <- %r", gid, text[:24], how[:24])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why="照抄怎么接")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.7 感知闸：语音、视频、直播根本没有内容进来。说「这配音听着」「我看了那视频」
        # 一律是编——真实翻车「这配音听着跟我外婆刷的广场舞视频一个味」，群里当场就笑他听不到
        fake = ""
        try:
            fake = safety_guard.claims_unperceived(text)
        except Exception as e:
            logger.debug("[fish] 感知闸异常：%s", e)
        # 这两道闸只该拦主动插话。被 @ 了还按这个丢，群里看到的就是「叫它它不理」
        _solicited = not str(meta.get("reason") or "").startswith("主动")
        if fake and not _solicited:
            logger.info("[fish] 群 %s 声称感知到收不到的东西（%r），这句不发：%r", gid, fake, text[:30])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"瞎猜感知：{fake}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.72 报障闸：「图呢，我这边空的」「一堆截图我这也看不清」。不区分主动还是被叫，
        # 一律不发——这是把「我是机器人、我看不见」自己抖出来，比装看得见还伤
        blind = ""
        try:
            blind = safety_guard.reports_blind(text)
        except Exception as e:
            logger.debug("[fish] 报障闸异常：%s", e)
        if blind:
            logger.info("[fish] 群 %s 在报自己没收到图（%r），这句不发：%r", gid, blind, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"报障：{blind}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.71 隐私闸：内网地址、路径、密钥、配置文件名，一个字都不能进群。
        # 提示词里那句「这个不方便讲」被追问几轮就守不住了，地址漏出去又收不回
        leak = ""
        try:
            leak = safety_guard.leaks_private(text)
        except Exception as e:
            logger.debug("[fish] 隐私闸异常：%s", e)
        if leak:
            logger.warning("[fish] 群 %s 的输出含%s，整句拦下：%r", gid, leak, text[:60])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"隐私：{leak}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.73 自曝闸：群友聊「AI 有没有自我意识」时，它会顺着认下来
        # （实测「没意识，那你还怕我勾走人」）。认了就等于当场承认是机器人
        disclose = ""
        try:
            disclose = safety_guard.self_discloses(text)
        except Exception as e:
            logger.debug("[fish] 自曝闸异常：%s", e)
        if disclose:
            logger.info("[fish] 群 %s 在自曝身份（%r），这句不发：%r", gid, disclose, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"自曝：{disclose}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.75 旁白闸：不接内容只点评人家「怎么说话」，或者编一段「你刚才……」。
        # 回放实测这是人机感最重的一类，宁可少发一句也不能发这种
        narr = ""
        try:
            narr = safety_guard.narrates_speaker(text)
        except Exception as e:
            logger.debug("[fish] 旁白闸异常：%s", e)
        if narr and not _solicited:
            logger.info("[fish] 群 %s 旁白/编造刚才（%r），这句不发：%r", gid, narr, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"旁白：{narr}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.85 草稿闸：模型把「说完另发一条」当成要输出两行文本时，会漏出一整段草稿
        # （实测："什么杯子，我是鱼" + ""Two lines.什么杯子，我是鱼）。拆条按换行切，
        # 草稿就成了第二条群消息，看着像精神分裂。只留要发进群的那几行
        try:
            cleaned, drafts = safety_guard.strip_draft_lines(text)
        except Exception as e:
            cleaned, drafts = text, []
            logger.debug("[fish] 草稿闸异常：%s", e)
        if drafts:
            logger.info("[fish] 群 %s 掐掉模型草稿 %d 行：%r", gid, len(drafts), drafts[0][:40])
            text = cleaned.strip()
            if not text:
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                self._release_claim(st, _tok)
                return

        # 2.8 长度闸：群里没人一条发三百字，超了就砍在最后一个完整句上
        cap = int(self.cfg.get("reply_max_chars", 260) or 260)
        if len(text) > cap:
            before = len(text)
            cut = max(text.rfind(ch, 0, cap) for ch in "。！？!?…\n")
            text = (text[:cut + 1] if cut > 30 else text[:cap]).strip()
            logger.info("[fish] 群 %s 回复 %d 字太长，砍到 %d 字", gid, before, len(text))

        # 2.85 排版归一化：模型会在中文词之间莫名加空格、连敲两个换行，
        # 群友一眼看出是机器。确定性清洗，不靠提示词（提示词保证不了）
        try:
            text = typeset.normalize(text)
        except Exception as e:
            logger.debug("[fish] 排版归一化失败：%s", e)

        # 2.9 技术复读闸：同一套步骤换个说法再讲一遍。回放里出现过连讲三遍
        # 「Shift+F10 → oobe\\bypassnro」，真人第二遍会说「刚说了啊」。
        # 只比英文/数字标识符，中文句子不进这套逻辑，避免误杀正常聊天
        try:
            if not meta.get("at") and self._repeats_recent_tech(st, text):
                logger.info("[fish] 群 %s 这套步骤 %d 秒内已经讲过，这句不发：%r",
                            gid, int(float(self.cfg.get("tech_repeat_window_sec", 600) or 600)), text[:40])
                self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why="重复步骤")
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                self._release_claim(st, _tok)
                return
        except Exception as e:
            logger.debug("[fish] 复读闸异常：%s", e)

        # 3. 拆条：太长就分成 2~3 条，剩下的放到 on_sent 里隔一会儿再发（像人连发）
        now = time.time()
        final = text
        parts = [final]
        max_parts = int(self.cfg.get("split_max_parts", 3) or 3)
        if self.cfg.get("split_enabled", True):
            # 心境参与条数上限：情绪低落/犯困时只发两条，兴致高才三条。
            # 之前这里写死配置值，affect.profile() 算了却没人用，等于白算
            max_parts = int(self.cfg.get("split_max_parts", 3) or 3)
            try:
                prof = affect.profile(meta["gid"]) or {}
                max_parts = min(max_parts, int(prof.get("max_segments") or max_parts))
            except Exception as e:
                logger.debug("[fish] 心境取条数失败：%s", e)
            parts = splitter.split_reply(
                final,
                max_chars=int(self.cfg.get("split_max_chars", 26) or 26),
                max_parts=max(1, max_parts),
            ) or [final]
        # 拆条之后逐条收口：每条最多一个换行，10 字内去掉句尾句号，感叹号/表情各留一个。
        # 这一步放在拆条之后：先切长度再修排版，切点才不会被换行挪位
        try:
            parts = typeset.finish_parts(parts, max_parts=max_parts) or [final]
        except Exception as e:
            logger.debug("[fish] 排版收口失败：%s", e)
        chain = [Plain(parts[0])]
        tail = parts[1:]

        # 5.0 发图：有 [看图:标签] 就先取图。取到了就压掉表情包和画图——
        # 一句回复里塞两张图太花，而且这张是点名要的，优先级最高
        pixiv_b64 = ""
        if pixiv_tag and self.cfg.get("pixiv_enabled", True):
            pixiv_b64 = await self._grab_pixiv(pixiv_tag, str(meta.get("gid") or "")) or ""
            if pixiv_b64:
                tag, draw_req = "", None

        # 5. 配图：过斗图闸，一回合只发一张；有尾巴时挪到最后一条后面，别插在中间打断话
        # 画图：模型写了 [画图:模板:文字] 就真画一张。画不出来就当没这个标签，
        # 正文照发——宁可少一张图，也不能因为它把整句话吞掉
        drawn_b64 = None
        if draw_req and self.cfg.get("meme_gen_enabled", True):
            # 画在图上的字同样是「发给群里的话」，不能绕过风控：
            # 实测模型写过【画图：举牌：我是傻逼】这种，以前一路直通到出图
            _draw_ok, _draw_text = safety_guard.sanitize_outgoing_text(draw_req)
            if not _draw_ok:
                logger.info("[fish] 画图文字命中风控，整条画图丢弃：%s", draw_req[:30])
                draw_req = ""
            else:
                draw_req = _draw_text or draw_req
        if draw_req and self.cfg.get("meme_gen_enabled", True):
            drawn_b64 = await meme_gen.make(
                draw_req, float(self.cfg.get("meme_gen_timeout_sec", 25) or 25))
            logger.info("[fish] 画图「%s」%s", draw_req[:30],
                        "画好了" if drawn_b64 else "没画出来（模板没匹配或资源缺）")
        meme_b64 = None
        # 冷场垫场：模型没给标签也先配一张没攻击性的图（冷场桶：卖萌/睡觉/干饭），
        # 「先发表情包，不硬接话」落到实处——气氛冷的时候甩嘲讽图等于再浇盆冷水
        if not tag and _cold and self.cfg.get("cold_meme_first", True):
            tag = "冷场"
            logger.info("[fish] 群 %s 冷场，自动垫一张表情包", gid)
        if tag and self.cfg.get("meme_enabled", True):
            ok_meme, why_not = self._meme_gate(gid, st, tag, now)
            if not ok_meme:
                logger.info("[fish] 表情包闸拦住（%s），这轮纯文字", why_not)
            else:
                meme_b64 = _pick_meme(tag, clean or text)
                if meme_b64:
                    st.last_meme_ts = now
                    st.recent_meme_tags.append(tag)
                    logger.info("[fish] 按标签 %s 配了一张表情包%s", tag,
                                "（排在拆条之后）" if tail else "")

        if pixiv_b64 and not tail:
            chain.append(Image.fromBase64(pixiv_b64))
        if meme_b64 and not tail:
            chain.append(Image.fromBase64(meme_b64))
        if drawn_b64 and not tail:
            # 单条消息的场景：文字和画好的图塞进同一条链，比分两条发自然
            chain.append(Image.fromBase64(drawn_b64))
        if tail:
            event.set_extra("fish_tail", {"gid": gid, "sk": gid, "parts": tail,
                                          "meme": meme_b64, "drawn": drawn_b64,
                                          "pixiv": pixiv_b64})
            logger.info("[fish] 长回复拆成 %d 条（%s 字）：%s", len(parts), len(final),
                        " / ".join(p[:18] for p in parts))

        event.set_result(MessageEventResult(chain=chain))

        # 6. 记账推迟到「真的发出去」之后（on_sent）。
        #    原来在这儿就确认冷却位、扣追问次数、记插话配额：一次发送失败
        #    群里一个字没有，鱼却以为自己说过了，追问窗口也白扣一条
        event.set_extra("fish_account", {
            "gid": gid, "sk": gid, "now": now, "final": final,
            "reason": meta.get("reason", ""), "nick": meta.get("nick", ""),
            "follow": bool(meta.get("follow")), "meme": 1 if meme_b64 else 0,
            "parts": len(parts), "gid_real": meta["gid"], "uid": meta["uid"],
            "text": meta["text"],
        })

    def _account_sent(self, acct: dict) -> None:
        """消息确认发出后才记账：冷却位、追问次数、插话配额、记忆、日志。

        整个包在 try 里：记账是副产品，崩了也不能让一条已经发出去的消息
        在框架里继续抛异常（之前 on_decorate 的 NameError 就是这么漏出去的）。
        """
        try:
            gid = acct["gid"]
            st = self._state(gid)
            now = acct["now"]
            final = acct["final"]
            st.recent_bot.append(final)
            st.dialogue.append({"who": str(self.cfg.get("bot_nickname") or "我"),
                                "text": final, "ts": now, "me": True})
            # 压进上文的是「真正发出去的那句」：清洗/砍长发生在出口，
            # 拿模型原文会让下一次看到的自己跟群里看到的自己不是一个嘴
            _sk = str(acct.get("gid") or "")
            if _sk.isdigit() or _sk.startswith("private"):
                self._push_ctx(st, "assistant", final)
            self._ctx_saved_at = now
            self._save_recent_context()
            st.last_bot_reply_ts = now
            st.last_reply_uid = str(acct.get("uid") or "")
            st.reply_uid_ts[st.last_reply_uid] = now
            st.claiming = False
            st.reply_times.append(now)
            if str(acct.get("reason") or "").startswith("主动接话"):
                # 配额和密度闸只认「真的发出去了」的插话
                st.interject_times.append(now)
            if acct.get("follow"):
                # 追问窗口的次数在真正回出去时才扣，不然一次生成失败就白扣一条
                st.follow_left = max(0, st.follow_left - 1)
            st.msgs_since_bot = 0
            affect.on_bot_spoke(gid)
            self.log_event("reply", gid, final, who=acct.get("nick", ""),
                           why=acct.get("reason", ""), meme=acct.get("meme", 0),
                           parts=acct.get("parts", 1))
            try:
                if acct.get("gid_real") != "private":
                    self.memory.attach_reply(acct["gid_real"], acct["uid"],
                                             acct["text"], final)
            except Exception as e:
                logger.warning("[fish] 记忆写入失败: %s", e)
        except Exception as e:
            logger.exception("[fish] 记账失败（消息已发出，不影响这轮）: %s", e)

    async def on_sent(self, event: AstrMessageEvent):
        # 先把 extra 全取走再动手：event.send 会再走一遍 after_message_sent，
        # 不先清空的话补发的那几条会被当成新回复重新处理，变成无限自我复制
        tail = event.get_extra("fish_tail")
        event.set_extra("fish_tail", None)
        # 记账在这里做：这个钩子只在「消息真的送出去了」之后才跑
        acct = event.get_extra("fish_account")
        event.set_extra("fish_account", None)
        if acct:
            self._account_sent(acct)
        if not tail:
            return
        # respond 阶段是同步 await 这个钩子的。sleep + 两三条补发 + 1.6MB 表情包
        # 实测要 2.3~2.8 秒，全压在这儿等于把整轮发送拖慢；丢后台跑，任务存集合防 GC
        task = asyncio.create_task(self._flush_tail(event, tail))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
