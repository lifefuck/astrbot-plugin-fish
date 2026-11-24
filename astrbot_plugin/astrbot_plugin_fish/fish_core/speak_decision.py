# -*- coding: utf-8 -*-
"""开口判定：所有「这条消息要不要说话」的唯一入口。

背景（为什么要这个东西）：
原来的开口判定散在 on_group 里 —— 被 @ 走一条、手打前缀走一条、追问窗口又一条、
主动插话再一条，每条各自判密度、各自判冷却。结果是同一句话能不能说，取决于它先撞上
哪个 if：被骂能绕过冷却和密度，追问窗口能绕过密度，主动插话两个都要过。
改一个地方必然漏掉另一处，这是「不想理它还一直说」和「有人骂它却装死」同时存在的根因。

这个模块把这些信号收成一个对象，一次判完：
- 输入：当前消息 + 群状态视图 + 时间线里的事实（谁 @ 谁、刚说过什么）
- 输出：说不说 / 为什么 / 什么模式（被叫·追问·主动·被骂）/ 这条是说给谁的

三条前置动作（注入拦截、骂人动作、处置人动作）不参与打分，但必须显式登记成旁路，
每次都要留下「哪条旁路、为什么」，不允许静默 return。

设计约束（来自实际踩坑）：
1. 优先级只提高「容忍度」，不跳过硬闸。被 @ 也要过冷却和判官，只是冷却更短。
2. 默认路径是主模型自判 [PASS]（inline），不是独立判官。独立判官只在被叫类路径作参考。
3. 判官坏了默认「不接」——坏掉的判官等于没有依据，宁可安静也别乱说。
4. 冷却、密度、冷场退避的数字一律从配置读，模块里不留任何常量默认值兜底业务口径
   （只有类型兜底，且必须在日志里体现实际生效值）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 优先级：数字越大越「该说」。注意它只影响冷却长度和密度豁免，不影响硬闸。
PRIO_AMBIENT = 0      # 没人叫，空气里的一句
PRIO_PROACTIVE = 1    # 主动插话（含贴链接）
PRIO_FOLLOW = 2       # 追问窗口内
PRIO_CALLED = 3       # 被 @ / 唤醒前缀 / 被骂到头上

MODE_AT = "被@"
MODE_QUOTE = "引用我"
MODE_WAKE = "手打前缀"
MODE_ABUSE = "被骂"
MODE_FOLLOW = "追问窗口"
MODE_PROACTIVE = "主动插话"


@dataclass
class Signals:
    """一条消息能提供的全部判定输入。只放事实，不放结论。"""

    group_id: str = ""
    uid: str = ""
    nick: str = ""
    text: str = ""
    now: float = 0.0

    # —— 指向信号：谁在跟谁说话 ——
    is_at: bool = False                 # @ 了机器人
    is_wake: bool = False               # 手打了唤醒前缀（前缀已剥掉）
    is_quote_bot: bool = False          # 引用的是机器人自己的话
    at_targets: tuple = ()              # 这条消息 @ 到的其他人（uid 列表）
    reply_to_uid: str = ""              # 引用/回复的是谁
    target_uid: str = ""                # 时间线推断出的「这轮说给谁」（由外部填）
    address_evidence: str = ""          # 指向证据的文字说明，进日志用

    # —— 被骂 ——
    abused: bool = False                # 判定为「骂到头上」
    abuse_evidence: str = ""            # 必须非空才算数：带指向的骂才算骂

    # —— 窗口 / 冷场 ——
    is_follow: bool = False
    follow_left: int = 0
    pending_interject_ts: float = 0.0
    unanswered_interjects: int = 0
    silent_until: float = 0.0
    last_bot_reply_ts: float = 0.0
    last_interject_ts: float = 0.0
    interject_times: tuple = ()          # 近窗内的主动插话时间戳
    recent_replies: int = 0              # 追问窗口内最近已回几条（旧 follow_reply_cap 口径）

    # —— 收件人：这条到底说给谁（addressee.py 打分）——
    # 默认满分 = 不改变既有行为。只有主路径会传入真实分数，裸 @ / 私聊等
    # 走 _sd_signals 的入口拿默认值，等于这道闸对它们不生效
    addressee_score: int = 100
    addressee_reason: str = ""

    # —— 内容形态 ——
    has_image: bool = False
    has_media: bool = False
    has_forward: bool = False            # 合并转发：读不懂，没人叫别接
    is_link: bool = False
    empty: bool = False                  # 没有可回的文本（纯表情/卡片等）

    # —— 判官 ——
    judge_score: Optional[float] = None  # None = 没跑或坏了
    judge_threshold: float = 0.0
    judge_broken: bool = False           # 调用了但失败/返回空
    # 旁观态的准入门槛（按人冷却、小时配额、复读、质量分 vs 意愿门槛）由
    # interject 那一套算完后塞进来：它只提供「结论+理由」，最终说不说仍由本对象定
    ambient_gate_blocked: str = ""
    # 主动插话闸（质量分 × 时机 × 按人冷却 × 小时配额）已经放行。
    # 两道闸问的是同一个问题「这句是不是说给我」，插话闸放过了就不在收件人闸
    # 再按「没人叫我」一刀切——实测 77 条决策里 44 条死在这里，成了只会答话的应答机
    interject_ok: bool = False
    # 裸 @（只有 @、没文字）在 45 秒内已经应过一声：同一句「在呢」连发两遍很蠢
    replied_recently: bool = False

    # —— 硬状态 ——
    muted: bool = False
    banned: bool = False
    peer_bot: bool = False
    quiet_hours: bool = False


@dataclass
class SpeakDecision:
    speak: bool
    reason: str
    mode: str = ""
    priority: int = PRIO_AMBIENT
    target_uid: str = ""
    bypass: str = ""            # 非空 = 这条走了有意旁路，主流程必须记日志
    inline_pass: bool = False   # 是否还需主模型自判 [PASS]
    judge_source: str = ""      # inline / judge / none
    notes: Dict[str, Any] = field(default_factory=dict)

    def log_line(self) -> str:
        return ("speak=%s reason=%s mode=%s prio=%d target=%s bypass=%s "
                "judge=%s inline_pass=%s" % (
                    self.speak, self.reason, self.mode, self.priority,
                    self.target_uid or "-", self.bypass or "-",
                    self.judge_source or "-", self.inline_pass))

    def as_dict(self) -> Dict[str, Any]:
        return {"speak": self.speak, "reason": self.reason, "mode": self.mode,
                "priority": self.priority, "target_uid": self.target_uid,
                "bypass": self.bypass, "inline_pass": self.inline_pass,
                "judge_source": self.judge_source, "notes": self.notes}



def _cfg_num(cfg, key, fallback, notes):
    """读数字型配置。业务口径必须有显式配置；这里只兜类型，并把生效值记进 notes。"""
    raw = (cfg or {}).get(key, None)
    val = fallback
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
    elif raw is not None:
        notes["配置类型异常:%s" % key] = repr(raw)
    notes["生效:%s" % key] = val
    return val


def bypass(dec_reason: str, kind: str) -> SpeakDecision:
    """三条前置旁路的统一出口：明确登记，不许静默 return。"""
    return SpeakDecision(speak=False, reason=dec_reason, bypass=kind,
                         notes={"有意旁路": kind})


def decide(sig: Signals, cfg: Optional[Dict[str, Any]] = None) -> SpeakDecision:
    """唯一入口。纯函数：不读文件、不改状态、不调模型。"""
    cfg = cfg or {}
    notes: Dict[str, Any] = {}

    # ---------- 0. 硬状态：这些连旁路都不算，是「不该处理」 ----------
    if sig.peer_bot:
        return SpeakDecision(False, "别的机器人", notes={"硬状态": "peer_bot"})
    if sig.muted:
        return SpeakDecision(False, "已 /off", notes={"硬状态": "muted"})
    if sig.banned:
        return SpeakDecision(False, "被禁言", notes={"硬状态": "banned"})
    if sig.has_forward and not (sig.is_at or sig.is_wake):
        # 合并转发是 OCR 式转述，读不懂就只能瞎评。没人叫就当没看见
        return SpeakDecision(False, "转发·没人叫不接", priority=PRIO_PROACTIVE)

    # ---------- 1. 优先级：每个信号只决定「多该说」，不决定「能不能说」 ----------
    # 被骂必须先有指向证据才算数：光判断「这句话有攻击性」不够，
    # 群里骂别人的时候它也当成骂自己，就会凑上去找骂
    abused_ok = bool(sig.abused and (sig.abuse_evidence or "").strip())
    prio = PRIO_AMBIENT
    mode = ""
    if abused_ok:
        prio, mode = PRIO_CALLED, MODE_ABUSE
    elif sig.is_at:
        prio, mode = PRIO_CALLED, MODE_AT
    elif sig.is_quote_bot:
        # 引用我发的消息 = 在跟我说话，哪怕句子里同时 @ 了别人。
        # 以前这种情况走「@了别人→说的是别人」被吞掉，明明是在让我看东西
        prio, mode = PRIO_CALLED, MODE_QUOTE
    elif sig.is_wake:
        prio, mode = PRIO_CALLED, MODE_WAKE
    elif sig.is_follow:
        prio, mode = PRIO_FOLLOW, MODE_FOLLOW
    elif sig.is_link:
        prio, mode = PRIO_PROACTIVE, MODE_PROACTIVE
    notes["被@"] = sig.is_at
    notes["引用我"] = sig.is_quote_bot
    notes["@了"] = list(sig.at_targets)
    notes["手打前缀"] = sig.is_wake
    notes["被骂"] = abused_ok
    notes["追问"] = sig.is_follow

    # ---------- 1.4 收件人闸：这句是不是冲我说的 ----------
    # 优先级只回答「多该说」，不回答「是不是说给我」。两个人聊得正热时按话题相邻
    # 抢一句，比漏接更伤。被 @ / 引用 / 唤醒在优先级里已是 CALLED 且满分，拦不到它们；
    # 这道闸主要拦「话题相邻就想插嘴」和「@ 了别人还凑上去」。
    # 灰区（gray ≤ 分 < hard）默认不是，由判官定：过线才放。以前额外要求必须在
    # 追问窗口内，结果「话题相邻（S_NEAR）」这档分数再高也永远进不来。
    # 插话闸已放行（sig.interject_ok）时整道跳过：它已经按质量分/时机/按人冷却/
    # 小时配额判过一遍「这句值不值得接」，这里再问「有人叫你吗」等于只许当应答机。
    if (cfg.get("addressee_guard_enabled", True) and prio < PRIO_CALLED
            and not abused_ok and not sig.interject_ok):
        hard = _cfg_num(cfg, "addressee_hard_threshold", 70, notes)
        gray = _cfg_num(cfg, "addressee_gray_threshold", 40, notes)
        sc = int(sig.addressee_score)
        notes["收件人"] = sc
        if sc < hard:
            # 质量分没跑（None）按「没有反对证据」处理：追问路径本来就把最终
            # 说不说交给主模型自判 [PASS]，这里再卡一刀等于把追问全掐死
            thr = sig.judge_threshold or _cfg_num(cfg, "judge_threshold", 0.48, notes)
            quality_ok = sig.judge_score is None or sig.judge_score >= thr
            if not (sc >= gray and quality_ok):
                return SpeakDecision(
                    False, "收件人不是鱼（%d %s）" % (sc, sig.addressee_reason or "无信号"),
                    mode=mode, priority=prio, notes=notes)
            notes["灰区放行"] = sc

    # ---------- 1.5 旁观态准入：由 interject 打分器给的结论，只压主动路径 ----------
    if prio <= PRIO_PROACTIVE and (sig.ambient_gate_blocked or "").strip():
        return SpeakDecision(False, sig.ambient_gate_blocked, mode=mode, priority=prio, notes=notes)

    # ---------- 1.6 裸 @ 的去重：刚应过就不再应 ----------
    if prio >= PRIO_CALLED and sig.empty and sig.replied_recently:
        return SpeakDecision(False, "裸@ 45 秒内已应过", mode=mode, priority=prio, notes=notes)

    # ---------- 2. 指向：说给别人听的话，即使被叫也别抢 ----------
    if sig.target_uid and sig.uid and sig.target_uid != sig.uid and prio <= PRIO_PROACTIVE:
        return SpeakDecision(False, "说的是别人（%s）" % (sig.address_evidence or "时间线"),
                             mode=mode, priority=prio, target_uid=sig.target_uid, notes=notes)

    # ---------- 3. 冷场退避：连插两条没人接，安静一段时间 ----------
    strikes = int(_cfg_num(cfg, "cold_shoulder_strikes", 2, notes))
    if prio <= PRIO_PROACTIVE and sig.silent_until > sig.now:
        return SpeakDecision(False, "冷场退避剩 %.0fs" % (sig.silent_until - sig.now),
                             mode=mode, priority=prio, notes=notes)

    # ---------- 4. 密度闸：只管主动插话，被叫和追问不占这条 ----------
    if prio <= PRIO_PROACTIVE and cfg.get("density_guard", True):
        win = _cfg_num(cfg, "density_window_sec", 120.0, notes)
        lim = int(_cfg_num(cfg, "density_limit", 2, notes))
        recent = [t for t in (sig.interject_times or ()) if sig.now - t <= win]
        if len(recent) >= lim:
            return SpeakDecision(False, "%.0f 秒内已主动说 %d 条" % (win, len(recent)),
                                 mode=mode, priority=prio, notes=notes)
        if sig.unanswered_interjects >= strikes:
            return SpeakDecision(False, "冷场计数 %d 次" % sig.unanswered_interjects,
                                 mode=mode, priority=prio, notes=notes)

    # ---------- 4.5 追问窗口内的回复总配额 ----------
    # 追问不是「每句必回」的通行证：实测一个人 7 分钟换来 18 条回复，比他自己说话还密。
    # 旧代码把这条写在 main.py 的 is_follow 分支里，切换后必须一起收进来，否则配额失效
    if prio == PRIO_FOLLOW:
        cap = int(_cfg_num(cfg, "follow_reply_cap", 3, notes))
        if cap and sig.recent_replies >= cap:
            return SpeakDecision(False, "追问窗口内已说 %d 条（上限 %d）" % (sig.recent_replies, cap),
                                 mode=mode, priority=prio, notes=notes)

    # ---------- 5. 冷却：所有人都要过，被叫只是冷却更短 ----------
    # 旧代码里 forced（被骂）完全绕过冷却，于是同一人 90 秒被回两次后还被追着说
    gap_key = "cooldown_sec" if prio >= PRIO_CALLED else "follow_min_gap_sec"
    gap = _cfg_num(cfg, gap_key, 0.0, notes)
    if prio >= PRIO_FOLLOW and gap > 0 and sig.last_bot_reply_ts:
        real_gap = sig.now - sig.last_bot_reply_ts
        if real_gap < gap:
            return SpeakDecision(False, "冷却剩 %.1fs（%s）" % (gap - real_gap, gap_key),
                                 mode=mode, priority=prio, notes=notes)

    # ---------- 5.5 主动插话的前置门槛 ----------
    # 空气里的一句要有「凭什么接」的证据才往下走：要么有质量分（便宜判官打的），
    # 要么是明确在给人看东西的链接。都没有就静音——否则每条消息都去问一次主模型
    # 要不要 [PASS]，token 会烧在这里，而且模型多半会挑一句无关的接上
    if prio <= PRIO_PROACTIVE and sig.judge_score is None and not sig.is_link:
        return SpeakDecision(False, "没质量分·不主动插话", mode=mode, priority=prio, notes=notes)
    # 有分但不达标同样不接。之前漏了这一条：空气消息只要带了分就一路走到 speak=True，
    # 影子日志里立刻暴露（旧=略过 0.47/0.48，新=接话），等于把「略过」全改成「接」
    if prio <= PRIO_PROACTIVE and sig.judge_score is not None:
        thr0 = sig.judge_threshold or _cfg_num(cfg, "judge_threshold", 0.48, notes)
        if sig.judge_score < thr0:
            return SpeakDecision(False, "主动质量 %.2f < %.2f" % (sig.judge_score, thr0),
                                 mode=mode, priority=prio, judge_source="inline", notes=notes)
        notes["主动质量"] = round(sig.judge_score, 2)

    # ---------- 6. 判官：只在被叫类路径作参考；默认路径走 inline [PASS] ----------
    inline_enabled = bool(cfg.get("judge_inline", True))
    need_judge = prio >= PRIO_FOLLOW
    if need_judge and sig.judge_broken:
        # 判官坏了 = 没有依据。默认不接，不是放行
        pol = str(cfg.get("judge_broken_policy") or "skip")
        if pol != "reply":
            return SpeakDecision(False, "判官不可用·默认不接", mode=mode, priority=prio,
                                 judge_source="judge", notes=notes)
        notes["判官坏了但配置为 reply"] = True
    if need_judge and sig.judge_score is not None:
        thr = sig.judge_threshold or _cfg_num(cfg, "judge_threshold", 0.0, notes)
        if sig.judge_score < thr:
            # 被 @ 时不因低分闭嘴：人点名了必须应声，低分只降级成短回
            if prio < PRIO_CALLED:
                return SpeakDecision(False, "判官 %.2f < %.2f" % (sig.judge_score, thr),
                                     mode=mode, priority=prio, judge_source="judge", notes=notes)
            notes["判官低分但被叫"] = round(sig.judge_score, 2)
        judge_source = "judge"
    else:
        judge_source = "inline" if inline_enabled else "none"

    # ---------- 7. 空内容兜底 ----------
    if sig.empty and prio < PRIO_CALLED:
        return SpeakDecision(False, "没有可回的文本", mode=mode, priority=prio, notes=notes)

    return SpeakDecision(True, mode or "接话", mode=mode, priority=prio,
                         target_uid=sig.target_uid, inline_pass=(judge_source == "inline"),
                         judge_source=judge_source, notes=notes)


def infer_target(sig: Signals, relations_summary: str = "") -> str:
    """推断「这轮话是说给谁的」。

    顺序：@了人 → 那个人；引用了某人 → 那个人；追问窗口 → 窗口主人；
    否则看时间线里最近一条 @ 关系（cross_interaction 已采，别重复采）。
    relations_summary 是 cross_interaction.get_group_relations_summary 的输出，
    只在最后兜底时用，命中就写进 address_evidence。
    """
    if sig.at_targets:
        return str(sig.at_targets[0])
    if sig.reply_to_uid:
        return str(sig.reply_to_uid)
    if sig.is_follow and sig.target_uid:
        return str(sig.target_uid)
    if relations_summary:
        try:
            data = json.loads(relations_summary) if isinstance(relations_summary, str) else relations_summary
        except Exception:
            return sig.target_uid
        pairs = (data or {}).get("pairs") or []
        if pairs:
            top = pairs[0]
            if isinstance(top, dict):
                return str(top.get("target_id") or top.get("target") or "")
    return sig.target_uid
