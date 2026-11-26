# -*- coding: utf-8 -*-
"""提示词拼装：把状态摊成 system + user（prompt.py，从 main.py 逐字搬出，逻辑未改）。"""

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

from . import fish_memory, huoren
from .kernel import *  # noqa: F401,F403


class PromptMixin:
    async def _compose(self, gid, uid, nick, text, is_at, st: GroupState, is_private: bool = False,
                       ctx_line: str = "",
                       self_id: str = "", vision_desc: str = "", media_hint: str = "",
                       social_hint: str = "",
                       cross_hint: str = "",
                       quarrel_hint: str = ""):
        affect.tick(gid)
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), bot_qq=self_id,
            group_id=gid or "-", is_private=is_private,
        )
        # 大肥鱼 humanizer 的 STYLE_REMINDER，原句搬过来挂在人设最后。
        # 为什么放在末尾：开头的位置已经被身份锚点占了，结尾这一句离生成最近，压得住腔调
        if humanize.STYLE_REMINDER.strip() not in system_prompt:
            system_prompt = system_prompt + humanize.STYLE_REMINDER
        # 生成前禁忌（salttove DEFAULT_GUARD 原句）：告诉它别写成什么，比事后删更省事。
        # 和 STYLE_REMINDER 同一位置——末尾离生成最近，压得住
        if humanize.GUARD.strip() not in system_prompt:
            system_prompt = system_prompt + "\n\n" + humanize.GUARD
        # 心流（对照 dsh-huoren 的 mood.js）：energy/warmth 按会话落盘，被催/被骂/深夜
        # 掉，被谢涨，每轮向基线（60/55）回落 6%，最后压成一句话挂在人设末尾。
        # 只在这句话有内容时才加——状态正常就别白占 token
        if self.cfg.get("huoren_mood", True):
            try:
                _mk = "%s-%s" % (gid or "private", uid or "-")
                _mood_st = huoren.advance(huoren.load(str(DATA_DIR), _mk), text)
                huoren.save(str(DATA_DIR), _mk, _mood_st)
                _mood_line = huoren.describe(_mood_st)
                if _mood_line:
                    system_prompt = system_prompt + "\n\n【此刻状态】" + _mood_line
            except Exception as e:
                logger.debug("[fish] 心流状态失败：%s", e)

        # 记忆召回：按当前这句检索这个人在这个群的历史记忆
        memory_lines = []
        if gid and gid != "private":
            try:
                _req = bool(self.cfg.get("memory_source_required", True))
                # 置信度门槛要在检索那一步就生效，不能只在最后的格式化里过滤
                _mconf = self.cfg.get("memory_min_confidence")
                _mconf = 0.5 if _mconf is None else float(_mconf)
                recalled = await asyncio.to_thread(
                    self.memory.recall, gid, uid, text,
                    cfg_int(self.cfg, "memory_recall_topk", 4), 14.0,
                    "active" if _req else "passive", _mconf)
                # 主动召回没命中、而这句又是在问事：放宽到「有话题重合就许引用」。
                # 只降一档、不放开无来源的条目——问到才提，跟前一条规则不冲突
                if not recalled and _req and re.search(r"[?？]|吗|么|啥|什么|几|哪|怎么|为什么", text or ""):
                    recalled = await asyncio.to_thread(
                        self.memory.recall, gid, uid, text,
                        cfg_int(self.cfg, "memory_recall_topk", 4), 14.0, "passive", _mconf)
                    if recalled:
                        logger.info("[fish] 记忆改用被动召回：%d 条", len(recalled))
                # 过滤碎片、近重去重、带出处，全在 fish_memory.shape_recalled 一处
                # 主动引用的置信度门槛走配置（0 是合法值，不能用 or 兜底）
                _min_conf = _mconf
                memory_lines = fish_memory.shape_recalled(
                    recalled, nick=nick, text=text,
                    fmt_when=self._fmt_mem_when, limit=2,
                    mode="active" if _req else "passive", min_conf=_min_conf)
                # 留一份给下一轮的纠正检测：他说「你记错了」时要能对上是哪条
                # 按 (群, 人) 存：纠正检测只认「说话人自己看到的召回」，
                # 否则群里 B 的一句否定会改到 A 的档案上
                fish_memory.put_recalled(self._last_recalled, str(gid), str(uid), recalled)
            except Exception as e:
                logger.warning("[fish] 记忆召回失败: %s", e)

        # 心境：只给体感，不给数字（数字会被模型念出来）
        affect_parts = []
        memo = affect.prompt_memo(gid)
        if memo:
            affect_parts.append(memo)
        # 主人先算出来：好感分要跳过，主人骂两句不该把关系算凉
        _owner = str(self.cfg.get("owner_qq") or "").strip()
        _is_owner = bool(_owner) and str(uid) == _owner
        favor_memo = favor.prompt_memo(gid, uid) if (not is_private and not _is_owner) else ""
        # 有人直接问「我多少分」的时候才把数字给它：平时不报分数，
        # 免得每句话都带一句「我对你 87 分」，那比机器人还机器人
        if not is_private and _ASK_FAVOR_RE.search(text or ""):
            try:
                _cur = favor.score(gid, uid)
                favor_memo = ((favor_memo + "\n") if favor_memo else "") + (
                    f"【他刚在问自己在这儿多少分：现在 {_cur:.0f} 分（0~100）。"
                    "要问就直接报这个数，顺口带句你的态度；别解释怎么算的，别报别人的分】"
                )
            except Exception as e:
                logger.debug("[fish] 取好感分失败：%s", e)
        if favor_memo:
            affect_parts.append(favor_memo)
        # 调侃许可：好感分只说明「他最近对鱼怎么样」，说明不了「能不能拿他开玩笑」。
        # 新人和熟人都是 50 分，但只有后者经得起挤对。三级各一句，不堆
        if not is_private and not _is_owner and self.cfg.get("tease_guard_enabled", True):
            try:
                _fz = favor.score(gid, uid)
                _tm = tease.memo(gid, uid, _fz, self.cfg)
                # 自嘲还有次数限制：一小时一次，用完了这轮就别再自嘲，免得变成口头禅
                if tease.level(gid, uid, _fz, self.cfg) >= tease.LEVEL_LOW and \
                        not tease.allow_self_tease(gid, self.cfg):
                    _tm += "（这会儿别再拿自己开涮了，刚开过。）"
                if _tm:
                    affect_parts.append(_tm)
            except Exception as e:
                logger.debug("[fish] 调侃许可注入失败：%s", e)
        # 主人频道：跟 owner 说话时的倾向排在好感分后面，压过任何「戒备/冷淡」。
        # 好感分算的是他最近对鱼怎么样，拿它对待做出的那个人，等于最熟的反倒最生分
        if _is_owner:
            affect_parts.append(
                "【说话的人】：life，是把它做出来的主人。跟他说话松、软、爱黏，"
                "小声带点谄媚也不害臊；被他骂不真记仇，嘴硬一句就顺回去，不用装客气也不用端着"
            )
        care = affect.care_hint(gid)
        if care:
            affect_parts.append(care)
        if not is_private:
            variant = affect.style_variant()
            if variant:
                affect_parts.append(f"【这次换个说法】：{variant}")

        # 印象原子：去过重、衰减之后留下的几句话，比整句记忆耐看，也不会重复提同一件事
        try:
            atoms_raw = self._atoms_of(gid, uid)
            if atoms_raw:
                atoms = memory_atoms.apply_decay(atoms_raw)
                atom_text, _used = memory_atoms.render(atoms)
                # 被注入 = 模型想起过一次，强度该回升。不写回的话，
                # 用得最多的印象也会照常衰减到被 trim 掉
                if _used:
                    memory_atoms.mark_used(atoms, _used)
                if atom_text:
                    # 印象原子是鱼自己总结的（inferred），按第 5 步的规矩只能被动提：
                    # 这句跟它沾边才出现，否则闭嘴——主动提自己的猜想最容易说错人
                    if (not self.cfg.get("memory_source_required", True)
                            or fish_memory.topic_overlap(
                                {"content": atom_text, "topic": fish_memory.topics_of(atom_text)},
                                text or "")):
                        memory_lines.append(atom_text[:260])
                # apply_decay 返回的是新列表（淘汰过的），元素还是同一批 dict 且原地
                # 刷新了 d/d_at；用 == 比较两个列表比的是元素内容，恒等 ⇒ 永远不落盘，
                # 用得最多的印象照样被当没人用而衰减掉
                if atoms is not atoms_raw:
                    self._atoms_all[f"{gid}:{uid}"] = atoms
                    self._save_atoms()
        except Exception as e:
            logger.debug("[fish] 印象原子渲染失败：%s", e)

        # 关系网：谁最近爱跟谁搭话，只给一句体感，不给统计表
        try:
            if gid and gid.isdigit():
                rel = cross_interaction.get_group_relations_summary(int(gid))
                if rel:
                    memory_lines.append(rel[:120])
        except Exception as e:
            logger.debug("[fish] 关系网摘要失败：%s", e)

        # 目的地记忆：这件事跟这个人说过两回了，第三次先自查再开口。
        # 不是禁止说，是让它自己决定「补新的 / 推进 / 承认重复」
        if not is_private and self.cfg.get("memory_source_required", True):
            try:
                _tp = fish_memory.topics_of(text or "")
                _n, _last = self.memory.said_repeats(gid, uid, _tp)
                _warn = int(self.cfg.get("memory_said_repeat_warn", 2) or 2)
                if _n >= _warn and _last:
                    memory_lines.insert(0, 
                        f"【这事你已经跟他说过 {_n} 次了（上次：「{_last[:30]}」）："
                        f"别再从头讲，要么补一句新的，要么直接问他办没办】")
                    logger.info("[fish][mem][repeat] 群 %s 重复第 %d 次：%s", gid, _n, _last[:20])
            except Exception as e:
                logger.debug("[fish] 目的地记忆查询失败：%s", e)

        # 有人问「X 是什么梗」：本地词库查不到就去梗百科，查到的只当资料、带来源
        ask_term = _meme_ask_term(text or "")
        if ask_term:
            try:
                from . import meme_wiki
                entry = await meme_wiki.lookup(ask_term)
                if entry:
                    logger.info("[fish] 查梗「%s」命中：%s", ask_term, entry.get("title"))
                else:
                    logger.info("[fish] 查梗「%s」没查到", ask_term)
                wiki_hint = meme_wiki.render(entry) if entry else ""
            except Exception as e:
                logger.warning("[fish] 查梗失败「%s」：%s", ask_term, e)
                wiki_hint = ""
        else:
            wiki_hint = ""

        # 热梗：常驻核心梗 + 这句沾边的梗，调味用，命中不了就空着
        meme_hint = ""
        try:
            # group_id 必传：本群黑话是逐群挖的，传错群等于把别人家的梗塞给它
            meme_hint = self.memes.get_prompt_snippet(text or "", group_id=gid)
        except Exception as e:
            logger.debug("[fish] 热梗取词失败：%s", e)

        # 本群自己的梗：这句用到了群里的说法就点一下（比通用热梗接得准）
        atmosphere_hint = ""
        if not is_private and self.cfg.get("group_memes_enabled", True):
            try:
                _hit = group_memes.match_hint(gid, text)
                if _hit:
                    atmosphere_hint = _hit
            except Exception as e:
                logger.debug("[fish] 本群梗命中失败：%s", e)
        # 冷场：上一条人话离现在多久。太久没人吭声就别硬接上一轮的话头，
        # 先甩张表情包垫场，再拿本群的老梗递一句 —— 荤段子一律不要
        if not is_private and self.cfg.get("cold_atmosphere_enabled", True):
            try:
                _dlg = list(st.dialogue)
                _prev = _dlg[-2] if len(_dlg) >= 2 else None
                _gap = max(0.0, time.time() - float((_prev or {}).get("ts") or 0)) if _prev else 0.0
                _need = float(self.cfg.get("cold_field_gap_sec", 900) or 900)
                if _gap >= _need:
                    atmosphere_hint = group_memes.cold_hint(gid, silent_min=_gap)
                    logger.info("[fish] 群 %s 冷场 %.0f 分钟，走垫场话术", gid, _gap / 60)
            except Exception as e:
                logger.debug("[fish] 冷场判定失败：%s", e)

        # 时间线走 dialogue 而不是 msgs：模型得看见自己刚说过什么，
        # 否则它会把同一句换个说法又来一遍，或者接话接到自己头上。
        # 条数窗在热闹的群里只等于「最近三分钟」：刚聊透的「神话保底 2000」早就滚出去了，
        # 于是它过一会儿又问一遍「神话是啥」。改成以时间窗为主、条数为上限，
        # 冷清时能一直看到几十分钟前的话，刷屏时也不会把 prompt 撑爆
        window_sec = float(self.cfg.get("context_window_sec", 1800) or 1800)
        size = int(self.cfg.get("timeline_size", 40) or 40)
        raw = list(st.dialogue)[:-1]  # 最后一条是当前这句，单独摆在「当前这句」块里
        now_ref = time.time()
        window = [m for m in raw if now_ref - float(m.get("ts") or 0) <= window_sec]
        if not window:
            window = raw[-size:]   # 太久没人说话时别让模型眼前一片空白
        elif len(window) > size:
            window = window[-size:]
        timeline = []
        for m in window:
            # 我的话可能是拆条后的两段，直接塞进去会多出没署名的半行，
            # 模型会当成另一个人接的话
            _t = " ".join(str(m["text"]).split())
            timeline.append(f"{m['who']}：{_t}")

        _multi = bool(self.cfg.get("multi_turn_contexts", True))
        # 用 ingest 时压进去的那一行，别用出口改写后的 text（否则弹不掉本轮这句）
        _ctx = self._ctx_of(st, ctx_line or text) if _multi else []
        if _multi and not _ctx:
            # 还没攒下上文（刚重启/新群）：这一轮继续用整段时间线，别让它眼前一片空白
            _multi = False
        user_prompt = composer.build_user_prompt(
            timeline=timeline,
            include_timeline=not _multi,
            latest_line=text,
            sender_nick=nick,
            sender_uid=uid,
            memory_lines=memory_lines,
            affect_parts=affect_parts,
            style_hint=(composer.style_samples_hint(
                gid, int(self.cfg.get("prompt_budget_style_max", 5) or 5))
                if gid != "private" else ""),
            meme_hint=meme_hint,
            wiki_hint=wiki_hint,
            atmosphere_hint=atmosphere_hint,
            budget={
                "memory": int(self.cfg.get("prompt_budget_memory_max", 2) or 0),
                "memory_chars": int(self.cfg.get("memory_prompt_budget", 160) or 0),
                "style": int(self.cfg.get("prompt_budget_style_max", 5) or 0),
                "hint": int(self.cfg.get("prompt_budget_hint_max", 2) or 0),
            },
            anti_repeat_hint=composer.anti_repeat_hint(list(st.recent_bot)),
            games_kb=prompts.GAMES_KB if prompts.needs_games_kb(text) else "",
            is_at=is_at,
            is_private=is_private,
            vision_desc=vision_desc,
            media_hint=media_hint,
            social_hint=social_hint,
            cross_hint=cross_hint,
            quarrel_hint=quarrel_hint,
        )
        if self.cfg.get("debug_dump_prompt"):
            try:
                body = "=== system ===\n" + system_prompt + "\n\n=== user ===\n" + user_prompt
                _lp = DATA_DIR / "last_prompt.txt"
                _lp.write_text(body, encoding="utf-8")
                # 整个系统提示词（含记忆和别人原话）落在盘上，umask 002 下是 664，
                # 机器上其他账号能直接读
                os.chmod(_lp, 0o600)
                # 每次留一份带时间戳的档：last_prompt 只留最后一次，回头看「心境/记忆到底进没进」
                # 就没证据了，尤其是要贴给别人的时候
                arch = DATA_DIR / "prompt_archive"
                arch.mkdir(exist_ok=True)
                (arch / f"{time.strftime('%Y%m%d-%H%M%S')}_{uid}.txt").write_text(body, encoding="utf-8")
                for old in sorted(arch.iterdir())[:-50]:
                    old.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("[fish] prompt 落盘失败: %s", e)
        logger.info("[fish] 上文 %d 条（%s），本轮 user 只 %d 字", len(_ctx),
                    "多轮·缓存友好" if _multi else "单轮·整段重拼", len(user_prompt))
        logger.debug("[fish] system_prompt=%r\nuser_prompt=%r", system_prompt[:80], user_prompt[:200])
        return system_prompt, user_prompt, _ctx
