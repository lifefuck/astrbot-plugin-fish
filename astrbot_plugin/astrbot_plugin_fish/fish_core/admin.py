# -*- coding: utf-8 -*-
"""群管理与风控：白名单、身份、禁言、告状（admin.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class AdminMixin:
    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        """@ 我、或者引用回复我。@全体 不算点名（群里一天能 @ 全体几十回）。"""
        self_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
            if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                return True
        # 只有 @ 没有正文时，AstrBot 的唤醒检查可能把那个 At 从组件里摘掉，
        # 光看组件就漏判成「没被点名」→ 插件让路 → 框架用人格外的口气回一句
        # 「你@了我但没说话～」。所以再翻一遍原始事件里的 message 段
        raw = getattr(event.message_obj, "raw_message", None)
        segs = getattr(raw, "message", None)
        if segs is None and isinstance(raw, dict):
            segs = raw.get("message")
        for seg in (segs or []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                if str((seg.get("data") or {}).get("qq")) == self_id:
                    return True
        return False

    @staticmethod
    def _at_others(event: AstrMessageEvent) -> bool:
        """这条是不是 @ 了别人（不是 @全体、也不是 @我）。

        真实翻车：lifefuck 发「@没长大的御姐 我真求你了 你是女的吗」，
        机器人因为刚跟他说过话、处在追问窗口里，抢答成「没叫你吧，这是又求助御姐什么」——
        人家明明在问御姐。@ 了别人就说明这句不是冲它来的。
        """
        try:
            self_id = str(event.get_self_id())
            for comp in (event.message_obj.message or []):
                if isinstance(comp, At) and str(getattr(comp, "qq", "") or "") not in ("", "all", self_id):
                    return True
            raw = getattr(event.message_obj, "raw_message", None)
            segs = getattr(raw, "message", None)
            if segs is None and isinstance(raw, dict):
                segs = raw.get("message")
            for seg in (segs or []):
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str((seg.get("data") or {}).get("qq") or "")
                    if qq and qq not in ("all", self_id):
                        return True
        except Exception:
            return False
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

    def _private_allowed(self, uid: str) -> bool:
        """私聊白名单：默认谁私聊都不理，只有列进去的人和主人才生效。"""
        if not self.cfg.get("enabled", True) or not self.cfg.get("reply_private", True):
            return False
        uid = str(uid).strip()
        owner = str(self.cfg.get("owner_qq") or "").strip()
        if uid and uid == owner:
            return True
        users = self.cfg.get("enabled_private_users") or []
        if isinstance(users, str):
            users = [u for u in re.split(r"[,，\s]+", users) if u]
        return uid in [str(u).strip() for u in users]

    def _order_watch(self, gid: str) -> bool:
        """哪些群开了主动巡检。名单空 = 一个都不查，免得装完就在别人的群里告状。"""
        if not self.cfg.get("order_guard_enabled", True):
            return False
        groups = self.cfg.get("tattle_watch_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        return str(gid) in [str(g).strip() for g in groups]

    async def _report_order(self, event, gid, nick, uid, text, label) -> str:
        """把一条违规私聊上报主人，返回要在群里说的那句话（上报失败返回空串）。"""
        now = time.time()
        gap = float(self.cfg.get("order_guard_cooldown_sec", 600) or 600)
        last = self._order_last.get(str(gid), 0.0)
        if now - last < gap:
            # 同一个群十分钟内只惊动主人一次，不然刷屏的会把你手机打爆
            logger.info("[fish] 群 %s %d 秒前刚上报过，这条不再打扰主人", gid, int(now - last))
            return ""
        # 先占位再 await：下面最长等 30 秒，等回来才写的话，
        # 同一条违规被巡检和模型自报同时触发时，主人会收到两条一样的私聊
        self._order_last[str(gid)] = now
        inst = self._tattle_plugin()
        if inst is None:
            logger.warning("[fish] 巡检命中「%s」，但告状插件不在", label)
            return ""
        content = f"【{label}】{nick}（{uid}）在群 {gid} 说：\n{text}"
        try:
            result = await asyncio.wait_for(
                inst.complain_to_receivers(
                    event,
                    content=content,
                    accused=str(uid),
                    reason=label,
                    # 只上报，不触发插件的自动封禁：罚不罚、怎么罚由主人定
                    sender_at_fault=False,
                ),
                30,
            )
        except Exception as e:
            logger.warning("[fish] 巡检上报失败：%r", e)
            self._order_last.pop(str(gid), None)  # 没送出去，占位还回去
            return ""
        logger.info("[fish] 巡检上报群 %s（%s）：%s", gid, label, str(result)[:100])
        # 插件返回的是 "Complaint finished: 1 sent, 0 failed." 这种串，
        # 一个收件人（主人自己）就是成功；0 sent 才算没送出去，那就别在群里吹
        m = re.search(r"(\d+)\s*sent", str(result) or "", re.I)
        if m and int(m.group(1)) == 0:
            logger.warning("[fish] 巡检上报没送出去（0 sent），群里不吭声")
            self._order_last.pop(str(gid), None)
            return ""
        return random.choice((
            "这条我记下了",
            "行，记一笔",
            "这个我记住了，别当我没听见",
        ))

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

    def _at_bot(self, event) -> bool:
        """这条消息有没有直接 @ 机器人。判定「骂的是不是它」时很关键：
        QQ 里「这傻逼 bot 好烦」@ 的是别人，其实是在跟群友吐槽。"""
        try:
            segs = getattr(event.message_obj, "message", None) or []
            self_id = str(event.get_self_id())
            for seg in segs:
                if getattr(seg, "type", "") == "at" and str(getattr(seg, "qq", "") or "") == self_id:
                    return True
        except Exception:
            pass
        return False

    def _anger_unlimited(self, gid: str) -> bool:
        """群级红温开关：anger_overrides.json 里列了这个群就放开次数上限。

        旧 bot 的 anger_overrides.json 同款。改文件即生效，不用重启——
        想现场试「骂到底能多凶」的时候，改这一行比改代码快。
        """
        p = DATA_DIR / "anger_overrides.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            self._anger_cache = (0.0, set())
            return False
        cache = getattr(self, "_anger_cache", (0.0, set()))
        if cache[0] != mtime:
            gs = set()
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                gs = {str(x).strip() for x in (data.get("groups") or []) if str(x).strip()}
            except Exception as e:
                logger.warning("[fish] anger_overrides.json 读不了，按正常阈值处理：%s", e)
            self._anger_cache = (mtime, gs)
        return str(gid) in self._anger_cache[1]

    async def _group_members(self, event, gid: str) -> list[dict]:
        """群成员表，缓存 10 分钟。拿不到就退回「最近说过话的人」，不能让骂人卡住。"""
        now = time.time()
        cached = getattr(self, "_members_cache", {})
        ts, rows = cached.get(gid, (0.0, []))
        if rows and now - ts < 600:
            return rows
        rows = []
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            data = await api.call_action("get_group_member_list", group_id=int(gid),
                                         **{"self_id": int(event.get_self_id())})
            for m in data or []:
                rows.append({"uid": str(m.get("user_id") or ""),
                             "nick": m.get("card") or m.get("nickname") or "",
                             "role": m.get("role") or "member"})
        except Exception as e:
            logger.warning("[fish] 取群成员表失败（%s），用最近发言人兜底", e)
        if rows:
            cached[gid] = (now, rows)
            self._members_cache = cached
        return rows

    async def _do_insult(self, event, gid: str, text: str, uid: str, st, act: dict) -> bool:
        """群友让它去骂人。返回 True 表示这条已经处理完，别再走正常回复。"""
        now = time.time()
        cd = float(self.cfg.get("insult_cooldown_sec", 60) or 60)
        last = getattr(self, "_insult_ts", {}).get(gid, 0.0)
        if now - last < cd:
            return False
        self_uid = str(event.get_self_id())
        owner = str(self.cfg.get("owner_qq") or "").strip()
        members = await self._group_members(event, gid)
        if not members:
            # 取不到成员表（接口超时/没权限）时别干瞪眼：用最近说过话的人兜底，
            # 否则群里一句「骂 xxx」只会在日志留一行「找不到靶子」，人看着就是犯傻
            members = [{"uid": str(x.get("uid") or ""), "nick": x.get("nick") or "",
                        "role": "member"} for x in list(st.msgs)[-12:]]

        if act.get("kind") == "target":
            who = act.get("who", "")
            if who.endswith(("他", "她", "它", "这货", "那货")):
                # 「教育教育他」这种，指的是上一句那个人，不是叫「他」的群友
                who = ""
            targets = insult_actions.find_by_name(members, who,
                                                  self_uid=self_uid, owner_uid=owner) if who else []
            if not targets and not who:
                _r = [{"uid": m.get("uid"), "nick": m.get("nick")}
                      for m in list(st.msgs)[-4:]][::-1]
                targets = insult_actions.pick_targets(members, _r, self_uid=self_uid,
                                                      owner_uid=owner, want=1)
            if not targets:
                # 以前这里回「你说的是谁啊，群里没这号人」，被旁人一句随口的
                # 「骂一句」就钓出来复读。现在宁可不说，也不当复读机
                # 以前这里 return True：指令没落地、事件被 stop，群里一个字都没有。
                # 用户发了明确指令却得到彻底沉默，只能理解成「没听见」。
                # 交回普通流程，让它自己说一句「群里没这人」
                logger.info("[fish] 群 %s 派活但找不到靶子（%r），交回普通回复", gid, act.get("who"))
                return False
        else:
            recent = [{"uid": m.get("uid"), "nick": m.get("nick")}
                      for m in list(st.msgs)[-12:]][::-1]
            targets = insult_actions.pick_targets(
                members, recent, self_uid=self_uid, owner_uid=owner,
                want=int(self.cfg.get("insult_max_targets", 3) or 3))
        if not targets:
            return False

        self._insult_ts = {**getattr(self, "_insult_ts", {}), gid: now}
        _said: list[str] = []
        for t in targets:
            ctx = " ".join(str(m.get("text") or "") for m in list(st.msgs)[-6:]
                           if str(m.get("uid")) == t["uid"])
            try:
                raw = await self._llm_once(insult_actions.gen_prompt(t["nick"], ctx),
                                           insult_actions._GEN_SYSTEM, 25.0)
            except Exception as e:
                logger.warning("[fish] 骂人短句生成失败：%s", e)
                raw = ""
            for line in insult_actions.clean_lines(raw, limit=2):
                try:
                    await event.send(MessageChain(chain=[At(qq=str(t["uid"])), Plain(" " + line)]))
                except Exception as e:
                    logger.warning("[fish] 骂人短句发不出去：%s", e)
                    break
                _said.append(f"{t['nick']}：{line}")
                await asyncio.sleep(0.8)
        logger.info("[fish] 群 %s 执行骂人（%s，靶子 %d 个）", gid, act.get("kind"), len(targets))
        if _said:
            # 骂完也要记账：不然下一轮它不知道自己刚骂过人，可能又好声好气接话
            self._record_spoke(gid, uid, "[骂人] " + " / ".join(_said), reason="骂人")
        return True
