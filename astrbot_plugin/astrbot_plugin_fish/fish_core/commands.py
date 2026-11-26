# -*- coding: utf-8 -*-
"""指令：tocken、开关、表情、点歌、告状（commands.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class CommandsMixin:
    # 解除静默的说法。只放整词，不做前缀匹配：「on my way 马上到」这类正常聊天
    # 不能被当成 /on，否则静默期间随便一句英文就能把群叫醒
    _UNMUTE_TEXTS = {"on", "恢复", "开启", "解除静默", "unmute"}

    def _is_unmute_cmd(self, event: AstrMessageEvent) -> bool:
        """这句是不是在喊恢复。

        静默分支必须放行它：cmd_on 是后注册的独立 handler，on_group 里一
        stop_event，框架的 star_request 循环直接 break，cmd_on 永远轮不到，
        静默就成了不可逆的（重启也救不回来，只能手改 group_muted.json）。
        """
        text = str(getattr(event, "message_str", "") or "").strip()
        # 框架通常已经剥掉唤醒前缀；这里再削一遍斜杠/井号，兼容手动写的 /on
        word = text.lstrip("/／#!！").strip()
        return word in self._UNMUTE_TEXTS

    async def muted_branch(self, event: AstrMessageEvent, gid: str):
        """静默期间收到群消息：只有 /on 放行，其余一个字都不回。

        stop_event 留给调用方（on_group）做，这里只管「要不要处理」——
        把这段逻辑放在 Mixin 里而不是 main.py，是为了能在没有 astrbot 的
        环境里跑回归（tests 自带最小桩）
        """
        if not self._is_unmute_cmd(event):
            return
        async for r in self.cmd_on(event):
            yield r

    async def cmd_tocken(self, event: AstrMessageEvent):
        """/tocken：token 用量账本。只给主人，免得群里谁都能查这只鱼的账单。"""
        # 别名里有 "token"，私聊里「token 超了怎么办」这种正常聊天会被当成指令吃掉，
        # 还会 stop_event 让 on_private 再也收不到。只认真·指令形态。
        _raw = (event.message_str or "").strip()
        if not re.match(r"^[/／]?(tocken|token|用量|token用量)(\s+文字)?$", _raw, re.I):
            # 写了 /tocken 但参数不认识：以前直接放行给 LLM，群里看到的是
            # 一句莫名其妙的闲聊回复。明确拒绝比装作没看见好
            if re.match(r"^[/／](tocken|token|用量)", _raw, re.I):
                event.stop_event()
                yield event.plain_result("用法：/tocken（发图）或 /tocken 文字")
            return
        try:
            owner = str(self.cfg.get("owner_qq") or "").strip()
            if not owner or str(event.get_sender_id()) != owner:
                event.stop_event()
                yield event.plain_result("⚠️ 这条只有主人能看")
                return
        except Exception:
            event.stop_event()
            return
        try:
            st = self.memory.token_stats()
        except Exception as e:  # noqa: BLE001
            logger.warning("[fish] /tocken 统计失败：%s", e)
            event.stop_event()
            yield event.plain_result("⚠️ 账本读不出来，日志里有原因")
            return
        # 默认发图：文字表格在手机 QQ 里必然折行，图没有这个限制。
        # 想看文字版就 /tocken 文字。
        card = None
        if "文字" not in (event.message_str or ""):
            try:
                card = token_card.make(st, str(DATA_DIR / "cache"))
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] /tocken 画卡失败，退回文字版：%s", e)
        event.stop_event()
        if card:
            try:
                yield event.image_result(card)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] /tocken 发图失败，退回文字版：%s", e)
        yield event.plain_result(token_report.render(st))

    async def cmd_off(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            # 私聊里「on my way 马上到」这种正常聊天会被短别名当指令吃掉，
            # 以前还会 stop_event，owner 私聊从此不再回话。只有真写了 /off 才拦。
            if not (event.message_str or "").lstrip().startswith("/"):
                return
            event.stop_event()
            yield event.plain_result("⚠️ 静默只能在群里发")
            return
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        yaml_path = str(self.cfg.get("yunzai_other_yaml") or "").strip()
        # 路径必须显式配：以前缺省会回退到硬编码的云崽路径，等于 /off 去改
        # 另一个服务（trss-yunzai）的配置——审查时实测把别的群从 whiteGroup 删了
        sync_ok = bool(yaml_path) and bool(self.cfg.get("yunzai_sync", True))
        was_listed = yunzai_link.in_whitelist(yaml_path, int(gid)) if sync_ok else False
        self._set_muted(gid, True, restore_yunzai=was_listed)
        if sync_ok:
            # 只关我自己没用：云崽那边的插件还在群里动，「全关」就是句空话
            yunzai_link.sync_whitelist(yaml_path, int(gid), False)
        elif self.cfg.get("yunzai_sync", True):
            logger.warning("[fish] 群 %s 静默：没配 yunzai_other_yaml，跳过云崽同步", gid)
        logger.info("[fish] 群 %s 静默（%s 触发）", gid, event.get_sender_id())
        self.log_event("mute", gid, "静默", who=event.get_sender_name())
        event.stop_event()
        yield event.plain_result("全关了，从现在起群里我就当没听见，发 /on 才回来")

    async def cmd_on(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            # 同上：私聊「on my way」不该被当指令，也不该把事件吞掉
            if not (event.message_str or "").lstrip().startswith("/"):
                return
            event.stop_event()
            yield event.plain_result("⚠️ 这条只能在群里发")
            return
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        restore_yunzai = self._mute_restore(gid)
        self._set_muted(gid, False)
        # 面板/旧数据可能在 override 里留了 enabled=false。只清静默不清它，
        # 就会出现「群里发了 /on、提示恢复了、实际还是不接话」的鬼打墙
        ov = dict(self._group_overrides.get(str(gid)) or {})
        if "enabled" in ov:
            ov.pop("enabled", None)
            try:
                if ov:
                    self._group_overrides[str(gid)] = ov
                else:
                    self._group_overrides.pop(str(gid), None)
                self.save_group_overrides(self._group_overrides)
                logger.info("[fish] 群 %s /on 顺带清掉 override.enabled", gid)
            except Exception as e:
                logger.warning("[fish] 群 %s 清理 override 失败：%s", gid, e)
        _yp = str(self.cfg.get("yunzai_other_yaml") or "").strip()
        if _yp and self.cfg.get("yunzai_sync", True) and restore_yunzai:
            # 只有当 /off 之前它本来在白名单里才加回去，否则就是凭空塞群号
            yunzai_link.sync_whitelist(_yp, int(gid), True)
        # 禁言缓存也清掉：刚解禁的那一刻不该还拿旧记录装死
        self._ban_expire.pop(gid, None)
        self._ban_checked.pop(gid, None)
        logger.info("[fish] 群 %s 恢复（%s 触发）", gid, event.get_sender_id())
        # 不 stop 的话框架默认 LLM 会拿「/on」再回一句闲聊
        event.stop_event()
        yield event.plain_result("回来了，该说说该闹闹")

    async def cmd_memes(self, event: AstrMessageEvent):
        """手动重载热梗池和本群黑话，改完 json 不用重启 AstrBot。"""
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        try:
            self.memes.load_pool(force=True)
            self.memes.load_group_slang()
        except Exception as e:
            # 原始异常里常带绝对路径和 json 行号，发到群里等于把自己的目录结构送人
            logger.warning("[fish] 重载热梗失败：%s", e)
            yield event.plain_result("⚠️ 重载失败，细节在日志里")
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

    def _tattle_plugin(self):
        try:
            from astrbot.core.star.star import star_map
            for md in star_map.values():
                if "tattle" in str(getattr(md, "name", "")):
                    inst = getattr(md, "star_cls", None)
                    if inst is not None and hasattr(inst, "complain_to_receivers"):
                        return inst
        except Exception as e:
            logger.debug("[fish] 找不到告状插件：%s", e)
        return None

    async def _tattle_intent(self, text: str, nick: str):
        """这句话是「让我去告状」还是「在聊告状这功能」？判不了返回 None。"""
        try:
            ans = await self._llm_once(
                f"{nick}：{text}",
                order_watch.TATTLE_SYSTEM,
                float(self.cfg.get("tattle_intent_timeout_sec", 10) or 10),
            )
        except Exception as e:
            logger.warning("[fish] 告状意图判定失败：%s", e)
            return None
        up = str(ans or "").strip().upper()
        if up.startswith("YES"):
            return True
        if up.startswith("NO"):
            return False
        return None

    def _maybe_patrol(self, event, gid: str, text: str, nick: str, uid: str,
                      recent: list) -> None:
        """秩序巡检入口：命中粗筛就起一个后台判定任务。

        跟接话判定完全解耦——判「不接」的那些消息（@的是别人、质量分不够、冷却中）
        原来一次模型都不调，等于把对着骂和刷广告的全放过去了。
        """
        if not self._order_watch(gid):
            return
        if str(uid) == str(event.get_self_id()):
            return
        # 主人自己骂人不用上报：接收人就是他，等于把他的话再发回给他一遍，
        # 群里还会多一句「已经上报了」，纯属自导自演
        owner = str(self.cfg.get("owner_qq") or "").strip()
        if owner and str(uid) == owner:
            return
        hint = order_watch.suspicious(text, recent)
        if not hint:
            return
        now = time.time()
        gap = float(self.cfg.get("patrol_judge_gap_sec", 45) or 45)
        if now - self._order_last.get("judge:" + str(gid), 0.0) < gap:
            return
        self._order_last["judge:" + str(gid)] = now
        asyncio.create_task(
            self._patrol(event, gid, text, nick, uid, hint, self._at_bot(event)))

    async def _patrol(self, event, gid: str, text: str, nick: str, uid: str, hint: str,
                      at_bot: bool = False) -> None:
        """粗筛命中 → 模型判一次 → 真要报就私聊 + 群里说一句。"""
        try:
            ans = await self._llm_once(
                order_watch.judge_prompt(hint, nick, text, self.cfg.get("bot_nickname", "机器人"),
                                         at_bot),
                order_watch.JUDGE_SYSTEM,
                float(self.cfg.get("patrol_judge_timeout_sec", 12) or 12),
            )
        except Exception as e:
            logger.warning("[fish] 巡检判定失败：%s", e)
            return
        if "YES" not in str(ans or "").upper()[:8]:
            logger.info("[fish] 群 %s 粗筛命中「%s」但模型判不算（%s），不报：%s",
                        gid, hint, str(ans)[:20], text[:30])
            return
        line = await self._report_order(event, gid, nick, uid, text, hint)
        if not line:
            return
        try:
            await event.send(MessageChain(chain=[Plain(line)]))
            logger.info("[fish] 群 %s 巡检已在群里提示：%s", gid, line)
        except Exception as e:
            logger.warning("[fish] 巡检群里提示没发出去：%s", e)

    def _tattle_allowed(self, gid: str, uid: str, now: float) -> bool:
        """告状转达的频率闸：每人 5 分钟一次、每群每小时 3 次。

        每次转达 = 一次模型判定 + 一条私聊 + 一张证据卡，不设限的话任何人连发
        五遍就能把接收人私聊刷满。返回 False 表示这次不转，调用方按「已处理」走。
        """
        log = getattr(self, "_tattle_log", None)
        if log is None:
            log = self._tattle_log = {}
        if now - float(log.get("u:" + uid, 0.0)) < 300:
            logger.info("[fish] 群 %s 用户 %s 告状太频，本轮不转", gid, uid[:8])
            return False
        gkey = "g:" + gid
        stamps = [t for t in (log.get(gkey) or []) if now - float(t) < 3600]
        if len(stamps) >= 3:
            logger.info("[fish] 群 %s 一小时内告状已达 3 次，本轮不转", gid)
            log[gkey] = stamps
            return False
        log["u:" + uid] = now
        log[gkey] = stamps + [now]
        return True

    async def _try_tattle(self, event, text: str) -> bool:
        """群友说要告状，就把原话私发给配置里的接收人（群里不出声）。

        带冷却：每次转达都是一条模型判定 + 一条私聊 + 一张证据卡，不设限的话
        任何群员连发五遍就能把主人私聊刷满
        """
        gid, uid = str(event.get_group_id() or ""), str(event.get_sender_id() or "")
        if not self._tattle_allowed(gid, uid, time.time()):
            return True
        inst = self._tattle_plugin()
        if inst is None:
            return False
        try:
            # sender_at_fault 恒为 False：这里只是替人传话，
            # 不能因为谁喊了一句告状就把人拉进自动封禁的计数器
            result = await asyncio.wait_for(
                inst.complain_to_receivers(
                    event, content=text, accused="", reason="群友要求转达", sender_at_fault=False
                ),
                30,
            )
        except Exception as e:
            logger.warning("[fish] 告状转达失败：%r", e)
            return False
        # 别拿 "failed" 当关键字：正常返回串是 "1 sent, 0 failed."，一出现 failed 就判失败，
        # 结果每条都记成「告状转达失败」，实际早就送到了
        m = re.search(r"(\d+)\s*sent", str(result) or "", re.I)
        ok = bool(m and int(m.group(1)) > 0) if m else True
        logger.info("[fish] 告状转达%s：%s", "成功" if ok else "失败", str(result)[:120])
        return ok

    def _song_service(self):
        """点歌服务：复用 listen-music 插件的 B 站客户端，发语音由我们自己发。"""
        if self._song_svc is None:
            try:
                from .song import SongService
                media_dir = str(DATA_DIR / "song_media")
                os.makedirs(media_dir, exist_ok=True)
                self._song_svc = SongService(media_dir)
            except Exception as e:
                logger.warning("[fish] 点歌服务装不起来：%s", e)
        return self._song_svc

    async def _try_song_request(self, event, gid: str, text: str) -> str:
        """自然语言点歌：识别意图 → 搜索 → 直接发语音。

        返回 ""（这压根不是点歌）/ "ok"（语音发出去了）/ "fail"（接了但没成）。
        三种结果分开才能给对反馈：成了别啰嗦，没成得说一声，别装作没听见。
        """
        title = _parse_song_request(text)
        if not title:
            return ""
        svc = self._song_service()
        if svc is None:
            return ""
        # 有没有真的挑中过曲目：决定失败后该说「没搜到」还是「下不动」
        hit = {"v": False}

        async def _stage(found_title: str) -> None:
            # 曲目一挑中就先报一声，剩下的下载上传要二三十秒。
            # 报用户点的那个名字就好——站上的标题带一堆【无损音质】【4K】装饰，念出来很蠢
            hit["v"] = True
            await svc.say(event, gid, f"找到了，《{title}》，拉着呢")

        try:
            ok = await svc.play(
                event, title, event.unified_msg_origin, gid, on_stage=_stage
            )
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            # CancelledError 不是 Exception，混在一起看才知道是超时还是被掐了
            logger.warning("[fish] 点歌失败「%s」：%s %r", title, type(e).__name__, e)
            return "fail" if hit["v"] else "notfound"
        if ok == "uncertain":
            # 语音多半已经进群，只是没等到回包。说什么都可能自相矛盾，就闭嘴
            logger.info("[fish] 群 %s 点歌「%s」发送结果不确定，不作声", gid, title)
            return "ok"
        if ok:
            logger.info("[fish] 群 %s 自然语言点歌「%s」已发送", gid, title)
            return "ok"
        return "fail" if hit["v"] else "notfound"

    async def _answer_with_research(self, event: AstrMessageEvent, gid: str,
                                    uid: str, nick: str, question: str,
                                    st=None, user_text: str = "") -> None:
        """查完再答。这条路不发 request_llm，自己 send，所以不经过拆条。

        为什么要先发一句：检索实测 20~40 秒，群里静默这么久像是挂了。
        为什么答案整条发：这类答案的价值全在「第几点 + 出处链接」，切开
        就没法看了——链接断在两条消息中间，谁也点不着。
        """
        from . import research, safety_guard

        announce = random.choice(("我去查下", "等我看看", "查一下哈", "我搜搜"))
        try:
            await event.send(MessageChain(chain=[Plain(announce)]))
        except Exception as e:
            logger.warning("[fish] 检索预告发不出去：%s", e)
        prov = None
        try:
            prov = await self.context.get_using_provider_async()
        except Exception as e:
            logger.warning("[fish] 拿不到 provider：%s", e)
        text = ""
        res = {}
        if prov is not None:
            try:
                res = await research.run(
                    question, provider=prov, http=await self._http(),
                    max_rounds=int(self.cfg.get("research_max_rounds", 4) or 4),
                    budget_sec=float(self.cfg.get("research_budget_sec", 150) or 150),
                    proxy=(self.cfg.get("search_proxy") or None),
                )
                text = (res.get("text") or "").strip()
                _u = res.get("usage") or {}
                if _u.get("input"):
                    # 联网那条路的输入是聊天的好几倍，单独一类记账，
                    # 混进「单条聊天用量」会把均值拉得没法看
                    self.memory.log_token("search", _u.get("input") or 0,
                                          _u.get("hit") or 0, _u.get("output") or 0,
                                          _u.get("ms") or 0)
            except Exception as e:
                logger.warning("[fish] 检索失败：%s", e)
        # 出口闸照过：这条路是唯一「长文本 + 整段抄网页原文」的出口，
        # 以前只做了 sanitize_outgoing_text，于是 PASS、内网地址、机器路径
        # 都能原样跟着答案进群——偏偏这条路的原文最脏
        text = _SEARCH_LEFTOVER_RE.sub("", text or "").strip()
        text = _PIXIV_LEFTOVER_RE.sub("", text).strip()
        if text and safety_guard.is_pass_signal(text):
            text = ""
        ok, text = safety_guard.sanitize_outgoing_text(text)
        if not ok:
            text = ""
        leak = safety_guard.leaks_private(text) if text else ""
        if leak:
            # 抄到网页里的内网地址/机器路径：整段不发，也不拿它去猜
            logger.info("[fish] 检索答案命中隐私词（%s），整段丢弃", leak)
            text = ""
        if text:
            try:
                text = quarrel.soften(text)
            except Exception:  # noqa: BLE001
                pass
        if not text:
            text = "查了一圈没查到能确认的说法，别照网上的教程乱刷，容易变砖"
        sent = False
        try:
            await self._send_long(event, gid, text)
            sent = True
            logger.info("[fish] 群 %s 检索回答已发（%s，%d 字，%s 轮，%s ms，来源 %d 个）",
                        gid, nick, len(text), res.get("rounds"), res.get("ms"),
                        len(res.get("sources") or []))
        except Exception as e:
            logger.warning("[fish] 检索回答发不出去：%s", e)
        # 记账：不发这一步，这一轮在状态机眼里等于没发生——冷却位没动、
        # reply_times 没加，模型下一轮的 ctx 里也没有自己刚答过的那段
        if sent and st is not None:
            try:
                self._account_sent({"gid": gid, "uid": str(uid), "now": time.time(),
                                    "final": text, "reason": "检索回答",
                                    "follow": False})
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] 检索记账失败：%s", e)
        # 记忆里记的必须是用户原话：以前传的是检索关键词，
        # 于是「这卡多少瓦」在记忆里变成了一条「用户说：RTX 5090 显卡 功耗」
        await self._remember(gid, uid, user_text or question)
