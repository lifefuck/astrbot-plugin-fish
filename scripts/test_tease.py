#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调侃许可单测：三级、升降、ban、自嘲节流与去重、禁区兜底。

跑法：python3 scripts/test_tease.py
"""
import builtins
import importlib.util
import sys
import tempfile
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


# tease 里是 `import backup`（跟 favor 一样，靠 kernel 把 fish_core 塞进 sys.path）。
# 单测里直接把它钉进 sys.modules，别去模拟包结构
BK = _load("backup", CORE / "backup.py")
sys.modules["backup"] = BK
T = _load("tease", CORE / "tease.py")

CFG = {"tease_high_favor": 80, "tease_low_favor": 60, "tease_ban_days": 30,
       "self_tease_per_hour": 1}
G, A, B = "100000001", "111", "222"
DAY = 86400.0
T0 = 1_700_000_000.0

# 1-3 三级由好感分给上限
ok("1 新人默认无许可", T.level(G, A, 50, CFG, T0) == 0, T.level(G, A, 50, CFG, T0))
ok("2 好感 60 给低许可", T.level(G, A, 60, CFG, T0) == 1, T.level(G, A, 60, CFG, T0))
ok("3 好感 80 给高许可", T.level(G, A, 85, CFG, T0) == 2, T.level(G, A, 85, CFG, T0))

# 4-8 ban 与锁死
T.refuse(G, A, "别开这种玩笑", now=T0, cfg=CFG)
row = T._state["people"][G][A]
ok("4 拒绝后归零并记 ban", row["level"] == 0 and row["ban_until"] > T0, row)
ok("5 ban 期内好感再高也是无许可", T.level(G, A, 95, CFG, T0 + DAY) == 0,
   T.level(G, A, 95, CFG, T0 + DAY))
# 期满只是「可以重新攒」，不会直接跳回原来的级别：刚被拒绝过的人
# 隔 30 天自动变熟，那 ban 就等于没记
ok("6 ban 期满仍要从零攒", T.level(G, A, 85, CFG, T0 + 31 * DAY) == 0,
   T.level(G, A, 85, CFG, T0 + 31 * DAY))
T.refuse(G, B, "别拿我说事", now=T0, cfg=CFG)
T.refuse(G, B, "别拿我说事", now=T0, cfg=CFG)
T.refuse(G, B, "别拿我说事", now=T0, cfg=CFG)
ok("7 累计三次锁死", bool(T._state["people"][G][B].get("locked")),
   T._state["people"][G][B])
ok("8 锁死后 ban 期满仍无许可", T.level(G, B, 95, CFG, T0 + 400 * DAY) == 0,
   T.level(G, B, 95, CFG, T0 + 400 * DAY))
T.force(G, B, 0, "人工解除", unlock=True)
ok("8b 人工可解锁", not T._state["people"][G][B].get("locked"))

# 9 升级过期回落一档
T2 = "333"
T._state["people"].setdefault(G, {})[T2] = {"level": 2, "points": 0, "since": T0,
                                            "last": T0, "strikes": 0, "ban_until": 0,
                                            "locked": False, "why": "", "evidence": []}
ok("9 升级 31 天不用回落一档", T.level(G, T2, 90, CFG, T0 + 31 * DAY) == 1,
   T.level(G, T2, 90, CFG, T0 + 31 * DAY))

# 10 三级 prompt 文案各不相同，且 L0 明确禁止自嘲
m0 = T.memo(G, "444", 50, CFG, T0)
m1 = T.memo(G, "444", 65, CFG, T0)
m2 = T.memo(G, "444", 90, CFG, T0)
ok("10 三级文案不同且 L0 禁自嘲",
   m0 != m1 and m1 != m2 and "别自嘲" in m0 and "自己开涮" in m1 and "挤对" in m2,
   (m0, m1, m2))

# 11-12 自嘲每小时一次
G2 = "100000002"
ok("11 第一次允许自嘲", T.allow_self_tease(G2, CFG, T0), T.allow_self_tease(G2, CFG, T0))
T.note_self_tease(G2, "我怎么这么蠢", "犯蠢", now=T0)
ok("12 一小时内第二次不许", not T.allow_self_tease(G2, CFG, T0 + 600),
   T.allow_self_tease(G2, CFG, T0 + 600))
ok("12b 一小时后恢复", T.allow_self_tease(G2, CFG, T0 + 3700),
   T.allow_self_tease(G2, CFG, T0 + 3700))

# 13 主题轮换：同一主题 3 天内不用
_t1 = T.next_topic(G2, T0)
T.note_self_tease(G2, "我怎么这么蠢", _t1, now=T0)
_t2 = T.next_topic(G2, T0 + DAY)
ok("13 同一主题三天内不重复", _t1 != _t2, (_t1, _t2))

# 14-15 指纹去重
G3 = "100000003"
T.note_self_tease(G3, "我怎么这么蠢啊", "犯蠢", now=T0)
keep, drop = T.strip_repeat_self_tease(G3, "我怎么这么蠢啊。这报错我知道。")
ok("14 撞车的自嘲句被删、其余保留",
   drop == "我怎么这么蠢啊。" and "报错" in keep, (keep, drop))
keep2, drop2 = T.strip_repeat_self_tease(G3, "我穷得只能吃土了。")
ok("15 换了说法的自嘲照发", drop2 == "" and "吃土" in keep2, (keep2, drop2))

# 16 禁区兜底
ok("16a 禁区命中外貌", T.banned_attribute("你好胖啊") != "", T.banned_attribute("你好胖啊"))
ok("16b 正常话不误伤", T.banned_attribute("你脸上写着不想干") == "",
   T.banned_attribute("你脸上写着不想干"))
ok("16c 禁区命中工作能力", T.banned_attribute("你就是废物") != "",
   T.banned_attribute("你就是废物"))

# 17 拒绝词不错杀
ok("17a 拒绝词认得出", T.refuse_matched("别拿我说事"))
ok("17b 普通抱怨不误判", not T.refuse_matched("这破闹钟又响了"))

# 18 落盘与重载
with tempfile.TemporaryDirectory() as td:
    p = str(Path(td) / "tease.json")
    def _w(path, data):
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    T.attach(writer=_w, path=p)
    T.refuse(G, "555", "别开这种玩笑", now=T0, cfg=CFG)
    T.save(force=True)
    T._state = {"people": {}, "self": {}}
    T.attach(writer=_w, path=p)
    ok("18 落盘后重载 ban 还在", T.level(G, "555", 95, CFG, T0 + DAY) == 0,
       T._state.get("people", {}).get(G, {}).get("555"))

print("调侃许可 %d 例：%d 通过 / %d 失败" % (18, 18 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
