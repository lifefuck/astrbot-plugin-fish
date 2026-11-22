#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目的地记忆单测：说过什么、说过几次、跨人跨群隔离、定期裁剪。

跑法：python3 scripts/test_memory_said.py
"""
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"

_TMP = tempfile.mkdtemp(prefix="fishmem-said-")
os.environ["FISH_MEMORY_DB"] = os.path.join(_TMP, "m.db")

FAIL = []


def ok(name, cond, got=""):
    if cond:
        print("ok", name)
    else:
        print("FAIL", name, "|", got)
        FAIL.append(name)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FM = _load("fish_memory", CORE / "fish_memory.py")
E = FM.FishMemoryEngine(os.path.join(_TMP, "said.db"))

G, U, V = "100000001", "200000001", "200000002"
G2 = "100000002"
TOPIC = FM.topics_of("这游戏太劝退了，肝不动")

# ---- 计数 ----
r1 = E.record_said(G, U, "这游戏太劝退了，肝不动了兄弟", to_nick="甲")
ok("1 首次记一条", r1.get("repeat") == 1, r1)
r2 = E.record_said(G, U, "这游戏太劝退了，真的肝不动", to_nick="甲")
ok("2 同话题第二次合并计数", r2.get("repeat") == 2, r2)
r3 = E.record_said(G, U, "这游戏太劝退了，别肝了", to_nick="甲")
ok("3 第三次到 3", r3.get("repeat") == 3, r3)

n, gist = E.said_repeats(G, U, TOPIC)
ok("4 查到说过三次", n == 3, n)
ok("5 带回上次原话", bool(gist), gist)

# ---- 隔离 ----
n_v, _ = E.said_repeats(G, V, TOPIC)
ok("6 另一个人不算说过", n_v == 0, n_v)
n_g2, _ = E.said_repeats(G2, U, TOPIC)
ok("7 别的群不算说过", n_g2 == 0, n_g2)

# ---- 空内容 / 只发图不记 ----
ok("8 只发图不记", E.record_said(G, U, "[只发图:嘲讽]").get("skipped") is True)
ok("9 空文本不记", E.record_said(G, U, "   ").get("skipped") is True)

# ---- 跨窗口不合并：7 天前的同类不算今天这次 ----
old_ts = time.time() - 8 * 86400
E.record_said(G, V, "这游戏太劝退了，我卸载了", to_nick="乙", ts=old_ts)
n_new = E.record_said(G, V, "这游戏太劝退了，又装回来了", to_nick="乙")
ok("10 隔了 8 天算新的一轮", n_new.get("repeat") == 1, n_new)

# ---- 裁剪：60 天前的目的地记忆被清掉 ----
E.record_said(G2, V, "很久以前说过的一句话啊", to_nick="丙", ts=time.time() - 90 * 86400)
# 查询默认只看 7 天，这里故意放宽窗口，才看得到 90 天前那条
_topic = FM.topics_of("很久以前说过的一句话啊")
before = E.said_repeats(G2, V, _topic, within_days=200)[0]
res = E.prune()
after = E.said_repeats(G2, V, _topic, within_days=200)[0]
ok("11 裁剪前查得到", before >= 1, before)
ok("12 裁剪清掉过期目的地记忆", res.get("said", 0) >= 1, res)
ok("13 裁剪后查不到", after == 0, after)

print("目的地记忆 %d 例：%d 通过 / %d 失败" % (13, 13 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
