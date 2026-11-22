#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在场模式单测：什么算在场、配额、间隔、以及不许跟在自己话后面补表情。

跑法：python3 scripts/test_presence.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"

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


P = _load("presence", CORE / "presence.py")

CFG = {"presence_enabled": True, "presence_gap_sec": 120, "presence_per_10min": 2}
G = "100000001"
T0 = 1_700_000_000.0


def sig(text="", images=0, **kw):
    base = {"text": text, "images": [object()] * images if images else None,
            "is_at": False, "is_quote_bot": False, "is_wake": False,
            "is_follow": False, "target_uid": "", "muted": False, "banned": False,
            "is_bot": False}
    base.update(kw)
    return SimpleNamespace(**base)


# 1 开关
ok("1 关掉就完全不在场", not P.judge(sig("哈哈哈哈"), None, {"presence_enabled": False}, T0)[0])

# 2-3 有指向的不算在场
ok("2 @鱼不算在场", not P.judge(sig("哈哈哈哈", is_at=True), None, CFG, T0)[0])
ok("3 在跟别人说话不算", not P.judge(sig("哈哈哈哈", target_uid="someone_else"), None, CFG, T0)[0])

# 4-5 要回答的不算在场
ok("4 问句不算", not P.judge(sig("这报错咋整？"), None, CFG, T0)[0])
ok("5 链接不算", not P.judge(sig("看这个 https://x.cn/a"), None, CFG, T0)[0])

# 6-8 内容判定
okok, scene, _w = P.judge(sig("哈哈哈哈笑死"), None, CFG, T0)
ok("6 大笑算接梗", okok and scene == "接梗", (okok, scene))
okok, scene, _w = P.judge(sig("", images=2), None, CFG, T0)
ok("7 连发两张图算接梗", okok and scene == "接梗", (okok, scene))
okok, scene, _w = P.judge(sig("这张", images=1), None, CFG, T0)
ok("8 单图加个短词算冷场垫图", okok and scene == "冷场", (okok, scene))

# 9 配额：10 分钟 2 次
P._state.clear()
P.mark(G, "草", now=T0)
P.mark(G, "确实", now=T0 + 130)
qok, why = P.quota_ok(G, None, CFG, T0 + 300)
ok("9 十分钟内第三次被配额挡住", not qok and "配额" in why, why)

# 10 间隔不足
P._state.clear()
P.mark(G, "草", now=T0)
qok, why = P.quota_ok(G, None, CFG, T0 + 60)
ok("10 间隔不足 120 秒被挡", not qok and "间隔" in why, why)

# 11 不许跟在自己上一句后面
P._state.clear()
dlg = [{"me": True, "ts": T0 - 20, "text": "刚说了一句"}]
qok, why = P.quota_ok(G, dlg, CFG, T0)
ok("11 刚说过话不补表情", not qok and "刚说过话" in why, why)

# 12 中间隔了人说话就无所谓
dlg = [{"me": True, "ts": T0 - 20, "text": "刚说了一句"},
       {"me": False, "ts": T0 - 10, "text": "哈哈哈哈"}]
qok, _why = P.quota_ok(G, dlg, CFG, T0)
ok("12 中间有人说话就可以在场", qok, _why)

# 13 前后都在问问题，不凑热闹
P._state.clear()
dlg = [{"me": False, "ts": T0 - 5, "text": "这个怎么弄？"},
       {"me": False, "ts": T0 - 3, "text": "哈哈哈哈"}]
okok, _s, _w = P.judge(sig("哈哈哈哈"), dlg, CFG, T0)
ok("13 有人在求助就不凑热闹", not okok, _w)

# 15 问过但鱼已经答了，就不该再挡着
P._state.clear()
dlg = [{"me": False, "ts": T0 - 30, "text": "这个怎么弄？"},
       {"me": True, "ts": T0 - 20, "text": "填默认值"},
       {"me": False, "ts": T0 - 10, "text": "哈哈哈哈"}]
okok, _s, _w = P.judge(sig("哈哈哈哈"), dlg, CFG, T0)
ok("15 答过之后可以照常在场", okok, _w)

# 14 try_present 一整套
P._state.clear()
got = P.try_present(G, sig("哈哈哈哈"), None, CFG, T0)
ok("14 在场返回场合和短词", bool(got) and got[0] == "接梗" and got[1], got)

print("在场 %d 例：%d 通过 / %d 失败" % (15, 15 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
