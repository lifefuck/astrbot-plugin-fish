# -*- coding: utf-8 -*-
"""开口判定：该不该说话、说什么（decision.py，从 main.py 逐字搬出，逻辑未改）。"""

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


class DecisionMixin:
    # 开口就是「催什么」「急什么」这类问句：同一天说第四遍时群里一眼看出是复读机。
    # 同样是拆分时留在旧 main 里的类常量，没跟过来就 AttributeError
    _OPENER_RE = re.compile(r"^(催什么|急什么|叫什么叫|戳什么|问什么|又怎么了)[，,。！!？?\s]*")
    @staticmethod
    def _echoes_hint(text: str, how: str) -> bool:
        """回复是不是把「怎么接」逐字念了一遍。

        判官那行是给模型的指令，不是给人看的话。真发生过：判官写
        「问一句他最近是不是夜宵吃多了」，模型就发「你最近夜宵吃多了吧」。
        字面重合到这个程度，说明它只是在复读指令，不是在接话——宁可不发。
        """
        def norm(s: str) -> str:
            return re.sub(r"[\s，。！？、；：\"'「」（）()~～…]+", "", str(s or ""))

        a, b = norm(text), norm(how)
        if not a or len(b) < 6:
            return False
        if b in a:
            return True
        # 短角度（「吐槽他这操作」）本来就该被用上，只钉长句照抄
        return len(b) >= 10 and SequenceMatcher(None, a, b).ratio() >= 0.7

    @staticmethod
    def _tech_tokens(t: str) -> set:
        """抽出句子里像命令行/文件名/型号的英文数字串，用来判断「这套步骤是不是刚讲过」。"""
        return {x.lower().rstrip(".,;:") for x in _TECH_TOKEN_RE.findall(str(t or ""))}

    def _repeats_recent_tech(self, st: GroupState, text: str) -> bool:
        """自己 10 分钟内讲过同一套技术步骤就算复读。

        真实翻车：先说「输 bypassnro 就跳过去了」，下一句又把 shift+f10 全流程讲一遍。
        判据两条：重合两个以上标识符；或者重合一个「强标识符」（4 个字母以上的英文专名，
        像 bypassnro、oobe、cmd），后者是因为换个说法常常只剩一个专名还能对上。
        中文不参与比较，避免把正常接话误判成复读。
        """
        # 只比「像命令/文件/键位」的串：带数字、下划线、斜杠、点、加号，
        # 或者长到不像普通英文单词（驼峰专名）。
        # 以前只卡长度≥4，结果「GPLv2 / MIT / linux」这种技术名词一重合就判复读——
        # 用户接着问 GPLv2 和 MIT 的区别，那句正常回答被整条吞了
        def cmdish(t: str) -> bool:
            return bool(re.search(r"[0-9_\\/.+:]", t)) or (t.isalpha() and len(t) >= 7)

        mine = {t for t in self._tech_tokens(text) if cmdish(t)}
        if not mine:
            return False
        win = float(self.cfg.get("tech_repeat_window_sec", 600) or 600)
        now_t = time.time()
        for m in list(st.dialogue):
            if not m.get("me"):
                continue
            if now_t - float(m.get("ts") or 0) > win:
                continue
            common = mine & {t for t in self._tech_tokens(str(m.get("text") or "")) if cmdish(t)}
            # 两个以上对得上才算「同一套步骤」；一个长专名（bypassnro 这种）也够
            if len(common) >= 2 or any(len(t) >= 6 for t in common):
                return True
        return False

    def _density_blocked(self, st: "GroupState", now: float) -> bool:
        """两分钟里已经主动说了两条就闭嘴。连刷换禁言，比少接一句贵多了。"""
        if not self.cfg.get("density_guard", True):
            return False
        window = float(self.cfg.get("density_window_sec", 120) or 120)
        limit = int(self.cfg.get("density_limit", 2) or 2)
        # 只数「主动插话」：被 @ 和追问窗口是别人点名的，把它们算进来会让
        # 密度闸在正常问答后立刻锁死插话
        q = st.interject_times
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    def _sd_signals(self, gid, st, now, **kw):
        """把群状态摊成 Signals。裸 @ 和主路径共用，避免两处各写一套字段。"""
        from . import speak_decision as _sd
        base = dict(
            group_id=str(gid), now=now,
            pending_interject_ts=float(getattr(st, "pending_interject_ts", 0.0) or 0.0),
            unanswered_interjects=int(getattr(st, "unanswered_interjects", 0) or 0),
            silent_until=float(getattr(st, "silent_until", 0.0) or 0.0),
            last_bot_reply_ts=float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0),
            last_interject_ts=float(getattr(st, "last_interject_ts", 0.0) or 0.0),
            interject_times=tuple(getattr(st, "interject_times", ()) or ()),
            is_follow=bool(getattr(st, "follow_left", 0)),
            followed_left_hint=0,
        )
        base.pop("followed_left_hint", None)
        base.update(kw)
        return _sd.Signals(**base)

    def _addressed_to_me(self, text: str) -> bool:
        """没 @ 但明显在叫它：只有外号才算，别拿单个「鱼」字乱认。"""
        t = text or ""
        return any(k in t for k in ("小鲸鱼", "鲸宝", "大肥鱼", "肥鱼", "傻鱼", "鲸娘", "鱼娘", "笨鱼"))

    def _sd_shadow(self, gid, st, sig_kw, old_ok, old_reason):
        """开口判定对象（speak_decision）的影子比对：只记录，不改行为。

        灰度期靠这条日志判断「新判定会不会把该说的吞掉、把不该说的放出来」，
        对比完再切 enforce。任何异常都吞掉——影子日志绝不能影响线上回话。
        """
        if not self.cfg.get("speak_decision_shadow", False):
            return
        gs = self.cfg.get("speak_decision_shadow_groups") or []
        if gs and str(gid) not in [str(x) for x in gs]:
            return
        import time as _t
        from . import speak_decision as _sd
        kw = dict(sig_kw)
        kw.setdefault("now", _t.time())
        kw.setdefault("group_id", str(gid))
        kw["interject_times"] = tuple(getattr(st, "interject_times", ()) or ())
        kw["pending_interject_ts"] = float(getattr(st, "pending_interject_ts", 0.0) or 0.0)
        kw["unanswered_interjects"] = int(getattr(st, "unanswered_interjects", 0) or 0)
        kw["silent_until"] = float(getattr(st, "silent_until", 0.0) or 0.0)
        kw["last_bot_reply_ts"] = float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0)
        d = _sd.decide(_sd.Signals(**kw), self.cfg)
        if old_ok is None:
            logger.info("[fish][sd] 群 %s 新判定 %s", gid, d.log_line())
        else:
            logger.info("[fish][sd] 群 %s 旧=%s/%s 新=%s %s", gid, old_ok, old_reason,
                        d.log_line(), "一致" if bool(old_ok) == bool(d.speak) else "不一致")

    def _should_interject(self, gid, st, text, now, has_image, has_media, uid: str = "") -> tuple[bool, str]:
        """旁观态接话闸门：质量分 × 时机修正 vs 意愿门槛。"""
        # adjust() 的「刚说过就 ×0.55」读的是模块里的 COOLDOWN_SEC，不是参数，
        # 不把配置同步进去，会出现「硬冷却放行了、软权重还在按 300 秒打折」的矛盾
        interject_planner.COOLDOWN_SEC = float(self.cfg.get("interject_cooldown_sec", 120))
        _cd = float(self.cfg.get("interject_cooldown_sec", 120))
        _el = now - st.last_bot_reply_ts
        _gap = float(self.cfg.get("chat_min_gap_sec", 2) or 0)
        _owner = str(self.cfg.get("owner_qq") or "")
        _aimed = self._addressed_to_me(text) or (bool(_owner) and str(uid) == _owner)
        _follow = bool(uid) and str(getattr(st, "last_reply_uid", "")) == str(uid)
        # 冷却按「人」算，不按群算。
        # 23:17 那次：我刚回完 lifefuck，十一秒后「我是神人」接了一句，
        # 被整群共用的 22 秒冷却掐掉——同一个群里换个人说话也算「刚说过话」，
        # 在多人聊天里就是集体装聋。现在只对「这个人自己刚被回过」生效。
        _same = now - float((getattr(st, "reply_uid_ts", {}) or {}).get(str(uid), 0.0))
        if _el < _gap or (_same < _cd and not (_aimed or _follow)):
            logger.info("[fish] 群 %s 冷却拦住：%s 距上次回话 %.1fs / 距他被回 %.0fs（gap=%s 叫他=%s 接着聊=%s）",
                        gid, str(uid)[-4:], _el, _same, _gap, _aimed, _follow)
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
        # 群级 override 优先于全局意愿：一个群吵、一个群安静，只能分别调
        ov_desire = self._group_overrides.get(str(gid), {}).get("interject_desire")
        desire = float(self.cfg.get("interject_desire", 0.30) if ov_desire is None else ov_desire)
        threshold = interject_score.interject_threshold(desire)
        passed = score >= threshold
        # 影子比对要的就是这个分：判定对象按「没分就不主动插话」处理，
        # 少这一行会把所有主动插话都记成「没质量分」，灰度对比全是噪声
        self._last_quality = float(score)
        # 注意不在这里记 quota：判定通过不代表真发得出去（social_read 可能改判、
        # 生成可能失败）。真正的计数在 on_decorate 发出去那一刻
        logger.info(
            "[fish] 群 %s 接话质量 %.2f vs 门槛 %.2f（%s | 时机 %s）-> %s",
            gid, score, threshold, why, timing or "无修正", "接" if passed else "略过",
        )
        return passed, f"主动接话 {score:.2f}/{threshold:.2f}"

    def _strip_repeated_opener(self, st, text: str) -> str:
        """开场白最近八条里出现过两次以上，就把这个开场削掉，留后半句。"""
        m = self._OPENER_RE.match(text or "")
        if not m:
            return text
        word = m.group(1)
        used = sum(1 for t in list(st.recent_bot)[-8:] if word in (t or ""))
        if used < 2:
            return text
        rest = text[m.end():].lstrip("，,。 ")
        return rest or text
