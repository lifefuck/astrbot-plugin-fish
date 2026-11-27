# -*- coding: utf-8 -*-
"""调度器：只说「这轮要不要开口」，不碰生成。

和 speak_decision.decide() 的分工：
- decide() 管「多该说」——被@/引用/追问/主动的优先级，以及收件人、密度、冷却这些闸。
- scheduler 管「配不配说」——默认**不说**，只有四类触发才进入候选，再过一道发言率上限。

为什么要多这一层：收件人闸砍掉了「不是冲我说的」，但只要判官给了分数、或者来的是
链接，decide() 仍会放行，群里还是会出现「没人问它、它一句句往外倒」。群聊里最烦的
不是答错，是话太多。所以默认值反过来：没有明确理由就闭嘴。

四类触发（其余一律 silent）：
    must   被 @ / 被引用 / 唤醒前缀 / 被骂（有指向证据）、追问窗口内
    maybe  话题卡住（10 分钟内同一话题被问 ≥2 次且无人接，90 秒内还热着）
    maybe  明确求助（求操作/求解答的措辞）+ 收件人分 ≥40
    silent 其它一切，含纯分享链接、判官高分但没指向

发言率上限（只管 maybe，must 不受限：人点了名必须应声）：
    - 占比：窗口内「鱼已经说了几轮」+1 不能超过 round(活人条数 / 活跃人数)，
      等价于鱼发言量不超过人均，占比 ≤ 1/N
    - 连续：窗口末尾连着两条鱼的发言中间没人说话，第三条不许
    - 频率：主动发言 10 分钟内最多 2 轮，两次之间至少 proactive_gap_sec

纯函数：不读文件、不改状态、不调模型，方便单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Tuple

TIER_MUST = "must"
TIER_MAYBE = "maybe"
TIER_SILENT = "silent"
# 在场：不说话，只给一张表情或一个短词。不算发言轮次，配额单独一套（见 presence.py）
TIER_PRESENT = "present"

# 求助/求解的措辞。「怎么办」这种既可能是自嘲也可能是求助，所以还要靠收件人分兜一层
_HELP_RE = re.compile(
    r"怎么|如何|为啥|为什么|报错|崩了|跑不起来|装不上|求助|请问|帮我看|咋办|怎么办|谁懂|有人知道|"
    r"配置|设置|填什么|啥意思")
# 问句：带问号，或者带疑问词。用来判断「话题卡住」
_ASK_RE = re.compile(r"[?？]|怎么|如何|为啥|为什么|报错|求助|谁[会懂知道]|请问|能不能|"
                     r"是不是|有没有|哪里|哪个|咋")

# 判定阈值（口径，不进配置：没人会去调「连续说话算几条」）
_STALL_WINDOW_SEC = 600.0     # 卡话题看 10 分钟
_STALL_MIN_ASKS = 2           # 同一话题至少被问两次才算「卡住」
_STALL_HOT_SEC = 120.0        # 最后一次问出来 2 分钟内才算热着
_STALL_QUIET_SEC = 60.0       # 鱼在这段时间内没说过话，才轮到它救场

# 热聊插话档：两人一来一回时 addressee 会把收件人分扣到 0，卡话题那档又只认问句，
# 结果群里越热闹它越插不上。这一档不看问句，只看热度和自己沉默了多久。
_WARM_WINDOW_SEC = 180.0      # 只看最近 3 分钟
_WARM_MIN_MSGS = 6            # 3 分钟内至少 6 条人话才算热聊
_WARM_MIN_SPEAKERS = 2        # 至少两个人在说，一个人自言自语不掺和
_WARM_QUIET_SEC = 240.0       # 自己至少 4 分钟没开口才补这一句
# 收件人分门槛分两档：「求助」要 40（灰区下限），「卡话题」放到 30。
# 卡话题的信号本来就弱（一群人干瞪眼没人答），再用 40 卡等于永远触发不了——
# 实测「这个配置项你们怎么填的？」这种泛称撞不到代词判定，只有 0~30 分
_ADD_MIN_HELP = 40
_ADD_MIN_STALL = 30

_SEQ = {"n": 0}

# 在场也要让路的硬状态：这些不是「话多话少」，是根本不该出现
_HARD_BLOCK = ("别的机器人", "已 /off", "被禁言", "说的是别人")


def _hard_blocked(reason: str) -> bool:
    r = str(reason or "")
    return any(r.startswith(p) for p in _HARD_BLOCK)


@dataclass
class Intent:
    """一次调度结论。"""

    speak: bool = False
    tier: str = TIER_SILENT
    reason: str = ""
    mode: str = ""
    priority: int = 0
    intent_id: int = 0
    trigger: str = ""                     # at/quote/wake/follow/stall/help/-
    stats: Dict[str, Any] = field(default_factory=dict)

    def log_line(self) -> str:
        s = self.stats or {}
        stat = ""
        if "H" in s:
            stat = " H=%s N=%s B=%s" % (s.get("H"), s.get("N"), s.get("B"))
        return "speak=%s tier=%s trigger=%s reason=%s%s" % (
            self.speak, self.tier or "-", self.trigger or "-", self.reason or "-", stat)


def _num(cfg: Optional[Dict[str, Any]], key: str, default: float) -> float:
    try:
        v = (cfg or {}).get(key)
        return float(default if v is None else v)
    except Exception:
        return float(default)


def _next_id() -> int:
    _SEQ["n"] += 1
    return _SEQ["n"]


def _entry_ts(e: Dict[str, Any]) -> float:
    try:
        return float(e.get("ts") or 0.0)
    except Exception:
        return 0.0


def _entry_is_bot(e: Dict[str, Any]) -> bool:
    return bool(e.get("me"))


def windowed(dialogue: Iterable[Dict[str, Any]], now: float, window: float,
             limit: int) -> list:
    """取窗口内的最后 limit 条：先按时间裁，再按条数裁。"""
    # 未来时间戳（时钟跳变/别的进程写脏）以前也能通过 `now - ts <= window`
    # 并且排序后永远排在最后，等于一条假消息长期霸占 rows[-1] 的位置。
    # 给 60 秒容差挡住它，正常消息不受影响
    rows = [e for e in (dialogue or ())
            if 0 < _entry_ts(e) <= now + 60.0 and now - _entry_ts(e) <= window]
    rows.sort(key=_entry_ts)
    return rows[-limit:] if limit and limit > 0 else rows


def rate_stats(rows: list) -> Tuple[int, int, int]:
    """(活人条数 H, 活跃人数 N, 鱼说了几轮 B)。"""
    humans = [e for e in rows if not _entry_is_bot(e)]
    h = len(humans)
    n = len({str(e.get("uid") or e.get("who") or "") for e in humans})
    b = len([e for e in rows if _entry_is_bot(e)])   # dialogue 每条鱼消息 = 一轮，拆条不重复登
    return h, max(n, 1), b


def trailing_bot_turns(rows: list) -> int:
    """鱼最近一次发言之前，紧挨着几句自己的话（中间没人说话）。

    不能只看窗口末尾：鱼连说两句之后往往正是有人提问，末尾一定是人的话；
    要判「连说」，得从最后一条鱼消息往回数。
    """
    idx = 0
    for i in range(len(rows) - 1, -1, -1):
        if _entry_is_bot(rows[i]):
            idx = i
            break
    else:
        return 0
    c = 0
    for i in range(idx, -1, -1):
        if not _entry_is_bot(rows[i]):
            break
        c += 1
    return c


def _answered(rows: list, since_ts: float) -> bool:
    """since_ts 之后有没有人给出像回答的长句（问句不算答案）。"""
    for e in rows:
        if _entry_is_bot(e) or _entry_ts(e) <= since_ts:
            continue
        t = str(e.get("text") or "")
        if len(t) >= 25 and not _ASK_RE.search(t[:12]):
            return True
    return False


def answered_recently(rows: list, now: float, sec: float = 300.0) -> bool:
    """最近的问句之后，是不是已经有人答过了。答过就不轮到来补一句。"""
    asks = [e for e in rows if not _entry_is_bot(e)
            and now - _entry_ts(e) <= sec and _ASK_RE.search(str(e.get("text") or ""))]
    if not asks:
        return False
    return _answered(rows, _entry_ts(asks[-1]))


def stalled(rows: list, now: float) -> Tuple[bool, str]:
    """话题卡住没有：最近 10 分钟里同一个话题被问过 ≥2 次、没人接、还热着。"""
    asks = [e for e in rows
            if not _entry_is_bot(e) and now - _entry_ts(e) <= _STALL_WINDOW_SEC
            and _ASK_RE.search(str(e.get("text") or ""))]
    if len(asks) < _STALL_MIN_ASKS:
        return False, "问句不足 %d 条" % _STALL_MIN_ASKS
    last_ask = asks[-1]
    if now - _entry_ts(last_ask) > _STALL_HOT_SEC:
        return False, "上一问已经 %.0fs 前" % (now - _entry_ts(last_ask))
    # 鱼在这期间说过话，说明已经有人接了，不该再补一刀
    bot_after = [e for e in rows
                 if _entry_is_bot(e) and _entry_ts(e) > _entry_ts(asks[0])
                 and now - _entry_ts(e) <= _STALL_QUIET_SEC]
    if bot_after:
        return False, "鱼 %.0fs 内刚说过" % (now - _entry_ts(bot_after[-1]))
    # 有人给出像回答的长句，也算接住了
    if _answered(rows, _entry_ts(asks[0])):
        return False, "已经有人接了"
    return True, "%d 次问句没人接" % len(asks)


_BOT_SILENT_MARKS = (
    "[图片]", "[表情]", "[动图]", "[语音]", "[文件]", "[链接]",
    "[视频]", "[卡片]", "[斗图]", "[动画表情]", "[PASS]",
)


def _bot_spoke_ts(rows: list) -> float:
    """鱼最后一次「真的说了话」的时间。

    纯表情不算说话：它甩一张图交差，却会重置沉默计时，等于一张图把接下来
    4 分钟的插话机会一起堵死——实测 23:11-23:17 那场热聊，warm 档一次没触发，
    就因为中间鱼跟了两张纯图。

    排除表按前缀判，不能只列白名单：respond 的记账串是 `[只发图:嘲讽]` 这种
    带参数的形态，全等比较会漏，等于纯表情照样重置冷却（实测漏了 7 种）。
    """
    ts = 0.0
    for e in rows:
        if not _entry_is_bot(e):
            continue
        t = str(e.get("text") or "").strip()
        if not t or t in _BOT_SILENT_MARKS or t.startswith("[只发图"):
            continue
        ts = max(ts, _entry_ts(e))
    return ts


def warm_chat(rows: list, now: float, cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """群里正聊得热、鱼却半天没出声：给它一次插话机会。

    上面每一档主动触发都要过收件人分，而群里两个人一来一回时 addressee 会按
    「A↔B在聊」把分扣到 0，判定层说得再对也轮不到它。实测一场 13 分钟的对话
    它一个字没说，判定层十几次都判「接（0.64 > 0.62）」，全死在调度层。
    这一档只看两件事：群够不够热、鱼沉默够不够久。发言率和主动冷却照旧管它。
    """
    if (cfg or {}).get("warm_chat_enabled", True) is False:
        return False, "热聊插话关着"
    win = _num(cfg, "warm_window_sec", _WARM_WINDOW_SEC)
    need = int(_num(cfg, "warm_min_msgs", float(_WARM_MIN_MSGS)))
    quiet = _num(cfg, "warm_quiet_sec", _WARM_QUIET_SEC)
    recent = [e for e in rows if not _entry_is_bot(e) and now - _entry_ts(e) <= win]
    if len(recent) < need:
        return False, "热聊条数 %d <%d" % (len(recent), need)
    # 当前这条是图就别接：对着表情插话，模型十有八九再甩一张图，然后沉默 4 分钟
    cur = str((rows[-1] if rows else {}).get("text") or "").strip()
    if cur in ("", "[图片]", "[表情]", "[动图]", "[语音]"):
        return False, "最新一条是图，不对着图说话"
    speakers = {str(e.get("uid") or e.get("who") or "") for e in recent}
    speakers.discard("")
    if len(speakers) < _WARM_MIN_SPEAKERS:
        return False, "只有一个人在说"
    # 光有热度不够，还得有话可接：群里连刷几张图时插进去，模型只会再甩一张图
    said_aloud = [e for e in recent
                  if now - _entry_ts(e) <= 60
                  and str(e.get("text") or "").strip() not in ("", "[图片]", "[表情]", "[动图]", "[语音]")]
    if not said_aloud:
        return False, "最近 1 分钟只有图，没话可接"
    last_bot = _bot_spoke_ts(rows)
    if last_bot and now - last_bot < quiet:
        return False, "鱼 %.0fs 前说过话" % (now - last_bot)
    return True, "%d 条热聊·鱼沉默 %s" % (len(recent), "很久" if not last_bot else "%.0fs" % (now - last_bot))


def help_asked(text: str) -> bool:
    """这句是不是在求操作/求解答。"""
    return bool(_HELP_RE.search(text or ""))


def rate_guard(rows: list, cfg: Optional[Dict[str, Any]]) -> Tuple[bool, str, Dict[str, Any]]:
    """发言率闸。返回 (能不能说, 理由, 统计)。"""
    h, n, b = rate_stats(rows)
    stats = {"H": h, "N": n, "B": b}
    if not (cfg or {}).get("rate_guard_enabled", True):
        return True, "", stats
    quota = max(1, int(round(h / n))) if n else 1
    stats["quota"] = quota
    if b + 1 > quota:
        return False, "发言率满（已说 %d 轮 / 额度 %d）" % (b, quota), stats
    trail = trailing_bot_turns(rows)
    stats["trail"] = trail
    if trail >= 2:
        return False, "连着说了 %d 条没人接" % trail, stats
    return True, "", stats


def schedule(sig: Any, cfg: Optional[Dict[str, Any]] = None, *, decision: Any = None,
             dialogue: Iterable[Dict[str, Any]] = (),
             present: Optional[Tuple[str, str]] = None) -> Intent:
    """调度总入口。

    decision 传 speak_decision.decide() 的结果（不传就自己算），这样优先级链、
    收件人闸、密度、冷却、判官逻辑都还在原位，这里只做「该不该由它说」的加减法。
    """
    from . import speak_decision as _sd  # 延迟导入，避免 kernel 加载期的循环引用

    d = decision or _sd.decide(sig, cfg)
    iid = _next_id()

    # 1) 判定链说「不是说给鱼的」时不急着返回：卡话题/明确求助这两种强信号能翻案。
    #    理由很实在——一群人连问两遍没人答，鱼就在群里，还端着「没人叫我」就不像人了。
    #    翻不了案再原样透传，理由一个字不丢
    denied = not d.speak

    # 2) 被点名类 / 追问窗口：直接 must。人点了名必须应声，不吃发言率
    if not denied and d.priority >= _sd.PRIO_FOLLOW:
        trig = ("follow" if d.priority == _sd.PRIO_FOLLOW
                else (getattr(d, "mode", "") or "called"))
        return Intent(speak=True, tier=TIER_MUST, reason=d.reason, mode=d.mode,
                      priority=d.priority, intent_id=iid, trigger=trig)

    # 3) 主动路径：默认不说，除非话题卡住 / 明确求助
    if not denied and cfg is not None and not cfg.get("scheduler_default_silent", True):
        return Intent(speak=True, tier=TIER_MAYBE, reason=d.reason or "关掉了默认不说",
                      mode=d.mode, priority=d.priority, intent_id=iid, trigger="legacy")

    now = float(getattr(sig, "now", 0.0) or 0.0)
    win = _num(cfg, "rate_window_sec", 600.0)
    lim = int(_num(cfg, "rate_window_msgs", 50))
    rows = windowed(dialogue, now, win, lim)
    add = int(getattr(sig, "addressee_score", 0) or 0)

    trigger = ""
    why = ""
    # 明确在跟别人说话（@某人 / 回某人）时，卡话题和求助都不算——那不是没人答，
    # 是有人正在被问。这一条护栏挡住「@甲」被 stall 误翻案的情况
    _to_other = bool(str(getattr(sig, "target_uid", "") or ""))
    ok, stall_why = stalled(rows, now) if not _to_other else (False, "在跟别人说话")
    # 求助只在「还没人回答」时才算数：别人已经答了再补一句就是抢话
    want_help = (not _to_other and help_asked(getattr(sig, "text", "") or "")
                 and not answered_recently(rows, now))
    # 热聊档：判定链已经说「这条能接」，只是被收件人分/问句条件挡住
    warm_ok, warm_why = ((False, "在跟别人说话") if _to_other
                         else warm_chat(rows, now, cfg))
    if ok and add >= _ADD_MIN_STALL:
        trigger, why = "stall", stall_why
    elif want_help and add >= _ADD_MIN_HELP:
        trigger, why = "help", "明确求助"
    elif warm_ok and not _hard_blocked(d.reason or ""):
        # 以前这里还有 `and not denied`，而 denied 对「没点名鱼」的普通群聊几乎恒为 True，
        # 于是热聊档形同虚设（16:34 那场 67 条回放 warm 0 触发）。热聊档存在的意义就是
        # 「没人点名但聊得热、鱼可以搭一句」，只靠 warm_chat 自己的门槛（沉默≥180s、
        # 近段≥6 条且≥2 人）和主动插话的每小时上限兜底
        # 但硬拦必须留：/off、被禁言、在跟别人说话、别的机器人在场，
        # 以前一条都不查，warm 能把 /off 直接翻案成「说」（静默群里照样开口）
        trigger, why = "warm", warm_why
    if not trigger:
        if ok:
            why = "%s；收件人 %d < %d" % (stall_why, add, _ADD_MIN_STALL)
        elif help_asked(getattr(sig, "text", "") or ""):
            why = "求助但收件人 %d < %d" % (add, _ADD_MIN_HELP)
        elif warm_ok:
            why = warm_why
        else:
            why = stall_why
        # 什么都不值得说的时候还有最后一档：在场。群里在笑、在摆烂、在抛梗，
        # 不需要回答，但「嗯」一声比彻底消失好。产物是一张表情或一个短词，
        # 不调模型，也不占说话的配额
        # 在场不看「判定链让不让说话」：本来就不该说话的消息才轮得到在场。
        # 只认四种硬拦（别的机器人/已关闭/被禁言/在跟别人说话），
        # 其余理由（冷却、条数、质量分、冷场退避）都是「说话」的闸，跟在场无关
        if present and not _hard_blocked(d.reason or ""):
            scene, short = present[0], present[1]
            return Intent(speak=True, tier=TIER_PRESENT, reason="在场·%s" % scene,
                          mode=d.mode, priority=d.priority, intent_id=iid,
                          trigger=scene, stats={"short": short})
        reason = d.reason if denied else "没有触发条件·默认不说（%s）" % why
        return Intent(speak=False, tier=TIER_SILENT, reason=reason,
                      mode=d.mode, priority=d.priority, intent_id=iid, trigger="-")

    # 4) 触发成立，再过发言率。连续发言先判：理由比「冷却剩几秒」更好懂
    trail = trailing_bot_turns(rows)
    if (cfg or {}).get("rate_guard_enabled", True) and trail >= 2:
        return Intent(speak=False, tier=TIER_SILENT,
                      reason="连着说了 %d 条没人接" % trail, mode=d.mode,
                      priority=d.priority, intent_id=iid, trigger=trigger,
                      stats={"trail": trail})
    allowed, rate_why, stats = rate_guard(rows, cfg)
    gap = _num(cfg, "proactive_gap_sec", 90.0)
    # 纯表情不算「刚说过」：它甩一张图就沉默 90 秒，等于一张图吃掉一次正经接话机会
    last_bot = _bot_spoke_ts(rows)
    if last_bot and now - last_bot < gap:
        allowed, rate_why = False, "主动发言冷却剩 %.0fs" % (gap - (now - last_bot))
    if not allowed:
        return Intent(speak=False, tier=TIER_SILENT, reason=rate_why, mode=d.mode,
                      priority=d.priority, intent_id=iid, trigger=trigger, stats=stats)
    return Intent(speak=True, tier=TIER_MAYBE, reason=why or trigger, mode=d.mode,
                  priority=d.priority, intent_id=iid, trigger=trigger, stats=stats)
