#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词瘦身 + 拼装预算 + 空上下文判定 的回归用例。

不碰线上：纯字符串进、纯字符串出。
"""
import sys, os, re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "astrbot_plugin"))
from astrbot_plugin_fish.fish_core import composer, prompts

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print(f"PASS {name}")
    else:
        fail += 1
        print(f"FAIL {name} {detail}")


# 1 人设长度与内容
# 人设放长是批准的（对照梗鲸母本重写），这里只守住「结构还在、不是被截断」
check("人设已展开 >2000 字", len(prompts.SYSTEM_PROMPT) > 2000, f"实际 {len(prompts.SYSTEM_PROMPT)}")
check("人设含 20 组正反例", prompts.SYSTEM_PROMPT.count("反例：") == 20, f"实际 {prompts.SYSTEM_PROMPT.count('反例：')}")
check("人设保留输出约定", all(k in prompts.SYSTEM_PROMPT for k in ("[PASS]", "[搜索:", "[看图:", "[MEME:")))
check("荤话段已砍", "荤话" not in prompts.SYSTEM_PROMPT and "骚话" not in prompts.SYSTEM_PROMPT)

# 2 记忆预算：给 4 条只留 2 条
up = composer.build_user_prompt(
    timeline=["甲：今天好热", "乙：是啊"], latest_line="热死了", sender_nick="甲", sender_uid="1",
    memory_lines=["甲（昨天）说过：我养猫", "甲（3 天前）说过：怕冷", "乙（昨天）说过：加班", "乙（今天）说过：下雨"],
    affect_parts=[], style_hint="", anti_repeat_hint="", games_kb="", is_at=True, is_private=False,
    budget={"memory": 2, "style": 3, "hint": 2},
)
check("记忆留 2 条", up.count("说过：") == 2, f"实际 {up.count('说过：')}")
check("记忆带时间", "（昨天）" in up)

# 3 风格样例预算：5 条只留 3 条
style = "【模仿这些说话的样子】：\n- a\n- b\n- c\n- d\n- e"
up2 = composer.build_user_prompt(
    timeline=["甲：x"], latest_line="y", sender_nick="甲", sender_uid="1",
    memory_lines=[], affect_parts=[], style_hint=style, anti_repeat_hint="",
    games_kb="", is_at=False, is_private=False, budget={"memory": 2, "style": 3, "hint": 2},
)
check("风格留 3 组", sum(1 for ln in up2.split("\n") if ln.startswith("- ")) == 3, up2)

# 4 hint 预算：6 条提示只留 2 条，且优先留吵架/跨群
up3 = composer.build_user_prompt(
    timeline=["甲：x"], latest_line="y", sender_nick="甲", sender_uid="1",
    memory_lines=[], affect_parts=[], style_hint="", games_kb="", is_at=False, is_private=False,
    anti_repeat_hint="【别重复】- q", social_hint="【社交】s", cross_hint="【跨群】c",
    quarrel_hint="【被骂】k", meme_hint="【表情包】m", wiki_hint="【百科】w",
    budget={"memory": 2, "style": 3, "hint": 2},
)
check("hint 留 2 条", ["【被骂】" in up3, "【跨群】" in up3].count(True) == 2, up3)
check("低优先 hint 被丢", "【社交】" not in up3 and "【百科】" not in up3)

# 5 空上下文判定
check("时间线空 → empty", composer.timeline_is_thin([], "今天好热") == "empty")
check("单条无关 → thin", composer.timeline_is_thin(["甲：明天考试"], "这个显卡多少钱") == "thin")
check("单条相关 → 正常", composer.timeline_is_thin(["甲：显卡涨价了"], "显卡多少钱") == "")
check("多条 → 正常", composer.timeline_is_thin(["甲：a", "乙：b"], "随便说") == "")

print(f"\n{'全部通过' if not fail else str(fail) + ' 个失败'}")
sys.exit(1 if fail else 0)
