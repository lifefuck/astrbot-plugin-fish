#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调度器单测：默认不说 / 四类触发 / 发言率上限。

跑法：python3 scripts/test_scheduler.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SCH = _load("scheduler", CORE / "scheduler.py")
SD = _load("speak_decision", CORE / "speak_decision.py")
# scheduler 里 `from . import speak_decision` 要求包结构，测试里直接钉进 sys.modules 的假包
import types
pkg = types.ModuleType("fishcore")
pkg.speak_decision = SD
sys.modules["fishcore"] = pkg
SCH.__dict__["__package__"] = "fishcore"
SCH.speak_decision = SD
import importlib
_orig_import = __import__


def _patch_relimport():
    """把 `from . import speak_decision` 解析到测试里加载的那个模块。"""
    import builtins
    real = builtins.__import__

    def hook(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and fromlist and "speak_decision" in fromlist:
            return SimpleNamespace(speak_decision=SD)
        return real(name, globals, locals, fromlist, level)

    builtins.__import__ = hook
    return lambda: setattr(builtins, "__import__", real)


CFG = {
    "scheduler_default_silent": True,
    "rate_guard_enabled": True,
    "rate_window_sec": 600,
    "rate_window_msgs": 50,
    "proactive_gap_sec": 90,
    "typing_delay_enabled": True,
}

NOW = 1_800_000_000.0


def sig(**kw):
    base = dict(now=NOW, text="", addressee_score=0, is_at=False, is_quote_bot=False,
                is_wake=False, is_follow=False, at_targets=(), reply_to_uid="",
                target_uid="", address_evidence="", uid="111", group_id="1",
                has_forward=False, has_image=False, has_media=False, is_link=False,
                empty=False, abused=False, abuse_evidence="", muted=False, banned=False,
                silent_until=0.0, last_bot_reply_ts=0.0, last_interject_ts=0.0,
                interject_times=(), recent_replies=0, unanswered_interjects=0,
                pending_interject_ts=0.0, follow_left=0, judge_score=None,
                judge_threshold=0.0, judge_broken=False, ambient_gate_blocked="",
                peer_bot=False, replied_recently=False)
    base.update(kw)
    return SimpleNamespace(**base)


def dec(speak=True, priority=SD.PRIO_PROACTIVE, reason="", mode="主动插话"):
    return SimpleNamespace(speak=speak, reason=reason, priority=priority, mode=mode)


def dlg(*rows):
    """rows: (uid, ts, text, me)"""
    out = []
    for uid, ts, text, me in rows:
        out.append({"who": uid, "uid": uid, "text": text, "ts": ts, "me": me})
    return out


CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), detail))


def main():
    restore = _patch_relimport()
    try:
        # 1 判定链说不说：透传
        r = SCH.schedule(sig(), CFG, decision=dec(False, reason="收件人不是鱼"))
        case("1 判定链说不说就透传", (not r.speak) and r.tier == SCH.TIER_SILENT
             and "收件人" in r.reason, r.log_line())

        # 2 被 @ → must
        r = SCH.schedule(sig(is_at=True), CFG, decision=dec(True, SD.PRIO_CALLED, mode="被@"))
        case("2 被@是 must", r.speak and r.tier == SCH.TIER_MUST, r.log_line())

        # 3 追问窗口 → must
        r = SCH.schedule(sig(is_follow=True), CFG, decision=dec(True, SD.PRIO_FOLLOW, mode="追问窗口"))
        case("3 追问是 must", r.speak and r.tier == SCH.TIER_MUST, r.log_line())

        # 4 主动路径没有任何触发 → 默认不说
        rows = dlg(("222", NOW - 30, "今天真热", False), ("333", NOW - 20, "是啊", False))
        r = SCH.schedule(sig(text="今天真热", addressee_score=30), CFG,
                         decision=dec(True), dialogue=rows)
        case("4 没触发默认不说", (not r.speak) and "默认不说" in r.reason, r.log_line())

        # 5 卡话题 + 收件人 45 → maybe
        rows = dlg(("222", NOW - 200, "这个配置项怎么填？", False),
                   ("333", NOW - 120, "这个配置项到底怎么填啊？", False))
        r = SCH.schedule(sig(text="这个配置项到底怎么填啊？", addressee_score=45), CFG,
                         decision=dec(True), dialogue=rows)
        case("5 卡话题放行", r.speak and r.tier == SCH.TIER_MAYBE and r.trigger == "stall",
             r.log_line())

        # 6 同样场景、收件人 30 → 放行（拍板：卡话题这一档降到 30，求助仍是 40）
        r = SCH.schedule(sig(text="这个配置项怎么填？", addressee_score=30), CFG,
                         decision=dec(True), dialogue=rows)
        case("6a 卡话题收件人 30 放行", r.speak and r.trigger == "stall", r.log_line())

        # 6b 收件人 20 → 还是不说
        r = SCH.schedule(sig(text="这个配置项怎么填？", addressee_score=20), CFG,
                         decision=dec(True), dialogue=rows)
        case("6b 卡话题收件人 20 不说", not r.speak and "收件人" in r.reason, r.log_line())

        # 6c 判定链已否（收件人闸 70 未过）但卡话题成立 → 能翻案
        r = SCH.schedule(sig(text="这个配置项怎么填？", addressee_score=60), CFG,
                         decision=dec(False, reason="收件人不是鱼（60 代词）"), dialogue=rows)
        case("6c 卡话题能翻回判定链的否决", r.speak and r.trigger == "stall", r.log_line())

        # 6d 没有触发条件时，判定链的否决原样透传
        rows_q = dlg(("222", NOW - 30, "这也太离谱了吧？", False))
        r = SCH.schedule(sig(text="这也太离谱了吧？", addressee_score=30), CFG,
                         decision=dec(False, reason="收件人不是鱼（30 无信号）"), dialogue=rows_q)
        case("6d 无触发时否决原样透传", (not r.speak) and r.reason == "收件人不是鱼（30 无信号）",
             r.reason)

        # 6e 正在 @ 别人时，卡话题不许翻案（那不是没人答，是有人正被问）
        r = SCH.schedule(sig(text="这个配置项怎么填？", addressee_score=60, target_uid="333"),
                         CFG, decision=dec(False, reason="收件人不是鱼（0 @别人）"), dialogue=rows)
        case("6e @别人时卡话题不翻案", not r.speak and "别人" in r.reason, r.log_line())

        # 7 明确求助 + 45 → maybe
        rows2 = dlg(("222", NOW - 40, "这报错咋办", False))
        r = SCH.schedule(sig(text="这报错咋办", addressee_score=45), CFG,
                         decision=dec(True), dialogue=rows2)
        case("7 明确求助放行", r.speak and r.trigger == "help", r.log_line())

        # 8 求助但收件人 20 → 不说
        r = SCH.schedule(sig(text="这报错咋办", addressee_score=20), CFG,
                         decision=dec(True), dialogue=rows2)
        case("8 求助但没人冲着它说·不说", not r.speak, r.log_line())

        # 9 发言率：鱼已说 2 轮、活人 3 条 2 人 → 额度 2，第 3 轮被压
        rows = dlg(("bot", NOW - 300, "第一句", True), ("222", NOW - 250, "嗯", False),
                   ("bot", NOW - 200, "第二句", True), ("333", NOW - 150, "哈哈", False),
                   ("222", NOW - 140, "这个配置项填啥", False),
                   ("333", NOW - 100, "这个配置项到底填啥啊", False))
        r = SCH.schedule(sig(text="这个配置项到底填啥啊", addressee_score=45), CFG,
                         decision=dec(True), dialogue=rows)
        case("9 发言率额满被压", not r.speak and "发言率满" in r.reason, r.log_line())

        # 10 连续两条没人接 → 第三条不说
        rows = dlg(("222", NOW - 300, "这个配置项填啥", False),
                   ("bot", NOW - 200, "填 model 那行", True),
                   ("bot", NOW - 150, "上面那个别动", True),
                   ("333", NOW - 40, "这个配置项到底填啥啊", False))
        r = SCH.schedule(sig(text="这个配置项到底填啥啊", addressee_score=45), CFG,
                         decision=dec(True), dialogue=rows)
        case("10 连着说两句没人接就不说", not r.speak and "连着说了" in r.reason, r.log_line())

        # 11 主动冷却：鱼 30 秒前刚说过
        rows = dlg(("bot", NOW - 30, "刚说完", True),
                   ("222", NOW - 25, "这个配置项填啥", False),
                   ("333", NOW - 20, "这个配置项到底填啥啊", False))
        r = SCH.schedule(sig(text="这个配置项到底填啥啊", addressee_score=45), CFG,
                         decision=dec(True), dialogue=rows)
        case("11 主动冷却没到不说", not r.speak and "冷却" in r.reason, r.log_line())

        # 12 must 不吃发言率：被 @ 时额度再满也说
        rows = dlg(("bot", NOW - 100, "a", True), ("bot", NOW - 90, "b", True),
                   ("bot", NOW - 80, "c", True))
        r = SCH.schedule(sig(is_at=True), CFG, decision=dec(True, SD.PRIO_CALLED, mode="被@"),
                         dialogue=rows)
        case("12 被@不吃发言率", r.speak and r.tier == SCH.TIER_MUST, r.log_line())

        # 13 关掉默认不说 → 退回旧口径
        cfg2 = dict(CFG, scheduler_default_silent=False)
        r = SCH.schedule(sig(text="今天真热", addressee_score=30), cfg2,
                         decision=dec(True), dialogue=rows)
        case("13 关掉开关退回旧口径", r.speak and r.tier == SCH.TIER_MAYBE, r.log_line())

        # 14 只有一条问句不算卡住
        rows = dlg(("222", NOW - 30, "这也太离谱了吧？", False))
        r = SCH.schedule(sig(text="这也太离谱了吧？", addressee_score=50), CFG,
                         decision=dec(True), dialogue=rows)
        case("14 一条问句不算卡住", (not r.speak) and "问句不足" in r.reason, r.log_line())

        # 15 有人接了长句 → 不算卡住
        rows = dlg(("222", NOW - 200, "这个配置项怎么填？", False),
                   ("333", NOW - 150, "到底怎么填？", False),
                   ("444", NOW - 100,
                    "填 model 那一行就行，别管上面那个默认值，那个是给旧版本留的", False))
        r = SCH.schedule(sig(text="到底怎么填？", addressee_score=50), CFG,
                         decision=dec(True), dialogue=rows)
        case("15 已经有人接了就不救场", not r.speak, r.log_line())
    finally:
        restore()

    bad = [c for c in CASES if not c[1]]
    for name, ok, detail in CASES:
        print(("  ok  " if ok else "  FAIL") + " " + name + ("  ← " + detail if not ok else ""))
    print("\n调度器 %d 例：%d 通过 / %d 失败" % (len(CASES), len(CASES) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
