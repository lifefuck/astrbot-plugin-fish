# -*- coding: utf-8 -*-
"""开口判定的行为测试。不连网络、不读线上配置，纯合成信号。

跑法：python3 scripts/test_speak_decision.py
任何一个用例不通过就以非 0 退出，方便上灰度前当门禁。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "astrbot_plugin"))

from astrbot_plugin_fish.fish_core import speak_decision as sd  # noqa: E402

CFG = {"density_guard": True, "density_window_sec": 120, "density_limit": 2,
       "cooldown_sec": 5, "follow_min_gap_sec": 8, "cold_shoulder_strikes": 2,
       "judge_inline": True}

CASES = []


def case(name, expect_speak, expect_reason_has="", **kw):
    CASES.append((name, expect_speak, expect_reason_has, kw))


# 1 普通空气消息：没人叫、没质量分 -> 不该说
case("空气消息不接", False, "没质量分")
# 2 @ 了机器人：必须应声
case("被@必回", True, "被@", is_at=True, text="在吗")
# 3 @ 但刚回过，冷却没到：仍然要等 —— 优先级不能跳过硬闸
case("被@仍受冷却", False, "冷却", is_at=True, text="在吗", now=100.0, last_bot_reply_ts=97.0)
# 4 被骂但没指向证据：不算被骂，退回普通
case("骂人无指向不算被骂", False, "没质量分", abused=True, abuse_evidence="", text="这破软件")
# 5 被骂且带指向：要接，但冷却照样算
case("被骂带指向但冷却未到", False, "冷却", abused=True, abuse_evidence="@我了还骂", text="你个傻子",
     now=100.0, last_bot_reply_ts=98.0)
# 6 主动插话撞密度闸
case("密度闸拦主动插话", False, "已主动说", text="这个我懂", is_link=True,
     now=200.0, interject_times=(190.0, 195.0))
# 7 追问窗口不受密度闸限制
case("追问窗口豁免密度", True, "追问", is_follow=True, text="然后呢",
     now=200.0, interject_times=(190.0, 195.0))
# 8 冷场退避：主动插话被压住
case("冷场退避", False, "冷场", text="哈哈哈", is_link=True, now=100.0, silent_until=400.0)
# 9 说的是别人：不抢话
case("说的是别人", False, "说的是别人", text="@张三 你昨天那个", at_targets=("999",),
     target_uid="999", uid="111", address_evidence="@张三")
# 10 判官坏了：默认不接（不是放行）
case("判官坏了默认不接", False, "判官不可用", is_follow=True, text="继续",
     judge_broken=True)
# 11 判官低分但被 @：仍然要应声
case("判官低分仍应声", True, "被@", is_at=True, text="在吗", judge_score=0.1, judge_threshold=0.5)
# 12 判官低分 + 追问：不接
case("判官低分不插话", False, "判官", is_follow=True, text="哦", judge_score=0.2, judge_threshold=0.5)
# 13 默认路径走 inline [PASS]
case("默认路径inline_PASS", True, "", text="今天好热", is_link=True, judge_score=0.9, judge_threshold=0.48)
# 14 转发没人叫：不接
case("转发不接", False, "转发", has_forward=True, text="")

# 14.5 引用我发的消息：等于在跟我说话，要接
case("引用我要接", True, "引用我", text="你看这个", is_quote_bot=True, reply_to_uid="111", uid="222")
# 14.6 引用我 + 同时 @ 了别人：仍然要接，不能判成「说的是别人」
case("引用我且@别人也接", True, "引用我", text="你看这个",
     is_quote_bot=True, reply_to_uid="111", uid="222",
     at_targets=("999",), target_uid="", address_evidence="@别人")

# 15 主动质量分不达标：不接（影子比对抓到的真实 bug：带分就一路放行）
case("主动质量不达标", False, "主动质量", text="今天好热", judge_score=0.47, judge_threshold=0.48)
# 16 主动质量分达标：走 inline PASS
case("主动质量达标", True, "", text="今天好热", judge_score=0.62, judge_threshold=0.48)

# 17 旁观态准入门槛：打分器给了结论，对象按它压主动路径
case("旁观准入门槛", False, "刚说过话", text="今天好热", judge_score=0.7, ambient_gate_blocked="刚说过话，冷却中")
# 18 追问窗口总配额：已说 3 条（上限 3）不再接
case("追问配额用完", False, "追问窗口内已说", text="然后呢", is_follow=True, follow_left=2, recent_replies=3)
# 19 追问配额未满：正常接
case("追问配额未满", True, "", text="然后呢", is_follow=True, follow_left=2, recent_replies=1)

# 20 裸 @ 45 秒内已应过：不再应
case("裸@去重", False, "裸@ 45 秒内已应过", text="", is_at=True, empty=True, replied_recently=True)
# 21 裸 @ 首次：应声（本地，不调模型）
case("裸@首次", True, "", text="", is_at=True, empty=True, replied_recently=False)

fail = 0
for name, exp, rsub, kw in CASES:
    sig = sd.Signals(**{k: v for k, v in kw.items() if k != "inline_pass"})
    d = sd.decide(sig, CFG)
    ok = (d.speak == exp) and (rsub in d.reason)
    if name == "默认路径inline_PASS":
        ok = ok and d.inline_pass is True
    if not ok:
        fail += 1
        print("FAIL %-18s expect speak=%s/%s got: %s" % (name, exp, rsub, d.log_line()))
    else:
        print("PASS %-18s %s" % (name, d.log_line()))

# 旁路：三条前置动作必须显式登记 kind
for kind in ("inject_guard", "insult_action", "admin_action"):
    b = sd.bypass("命中" + kind, kind)
    if b.bypass != kind or b.speak:
        fail += 1
        print("FAIL 旁路 %s 登记错误" % kind)
    else:
        print("PASS 旁路 %-14s %s" % (kind, b.log_line()))

print("\n%d 个用例，失败 %d" % (len(CASES) + 3, fail))
sys.exit(1 if fail else 0)
