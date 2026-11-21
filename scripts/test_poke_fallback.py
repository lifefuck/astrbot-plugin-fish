#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""戳一戳：模型优先、本地兜底的回归用例。

正常情况走模型（build_prompt 带「戳了你一下」），本地池只在模型超时/失败时兜底。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "astrbot_plugin"))
from astrbot_plugin_fish.fish_core import poke_context

fail = 0


def check(name, cond, detail=""):
    global fail
    if cond:
        print("PASS " + name)
    else:
        fail += 1
        print(f"FAIL {name} {detail}")


# 1 兜底池不超过 5 句
check("兜底池 <=5", len(poke_context.FALLBACK_POOL) <= 5, str(len(poke_context.FALLBACK_POOL)))
check("兜底池非空", len(poke_context.FALLBACK_POOL) >= 1)

# 2 兜底会避开最近说过的
recent = [poke_context.FALLBACK_POOL[0], poke_context.FALLBACK_POOL[1]]
picked = poke_context.pick_fallback(recent)
check("兜底避开刚说过的", picked not in recent, picked)
check("全都说过也能出话", bool(poke_context.pick_fallback(list(poke_context.FALLBACK_POOL))))

# 3 自定义池生效（配置覆盖）
check("自定义池生效", poke_context.pick_fallback([], ["只此一句"]) == "只此一句")

# 4 模型 prompt：纯手欠
info = {"pending": "", "timeline": ["甲：今天好热"], "quiet_min": 5}
pr = poke_context.build_prompt(info, "戳你的是群友「甲」", repeat=1, last_said=[])
check("prompt 带被戳", "戳了你一下" in pr and "纯手欠" in pr)
check("prompt 一句话约束", "只输出" in pr)

# 5 模型 prompt：有未回的留言要补
info2 = {"pending": "你刚才怎么不理我", "timeline": [], "quiet_min": None}
pr2 = poke_context.build_prompt(info2, "戳你的是群友「甲」", repeat=1, last_said=[])
check("prompt 补回未回", "没接" in pr2 or "没被回" in pr2, pr2)

# 6 连戳：告诉模型别重复上一句
pr3 = poke_context.build_prompt(info, "戳你的是群友「甲」", repeat=3, last_said=["手欠"])
check("连戳给负样本", "手欠" in pr3 and "3 下" in pr3)

print("\n" + ("全部通过" if not fail else f"{fail} 个失败"))
sys.exit(1 if fail else 0)
