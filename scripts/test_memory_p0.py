#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 记忆回归：衰减幂等、召回算激活、召回缓存按人不串档、纠错归属断言。

跑法：python3 scripts/test_memory_p0.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"

_TMP = tempfile.mkdtemp(prefix="fishmem-p0-")
_DB = os.path.join(_TMP, "m.db")
# 必须在 import 之前设：模块顶层有单例，否则会在仓库里就地建库
os.environ["FISH_MEMORY_DB"] = _DB

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
E = FM.FishMemoryEngine(_DB)
G = "100000001"
A, B = "200000001", "200000002"


def _row(mid):
    with sqlite3.connect(_DB) as c:
        return c.execute(
            "SELECT confidence, is_deleted, activation_count, last_activated_at, updated_at "
            "FROM fish_memory_records WHERE memory_id=?", (mid,)).fetchone()


# ---- 1. 衰减幂等：一天跑八次，只能降一档 ----
m = E.add_memory(G, A, "他好像在做游戏相关的活", memory_type="event",
                 source_type="heard", confidence=0.45, anchor_text="好像在做游戏")
with sqlite3.connect(_DB) as c:
    c.execute("UPDATE fish_memory_records SET updated_at=? WHERE memory_id=?",
              (time.time() - 40 * 86400, m["id"]))
    c.commit()
first = E.decay_low_confidence(days=30)
v1 = float(_row(m["id"])[0])
for _ in range(7):
    E.decay_low_confidence(days=30)
v8 = float(_row(m["id"])[0])
ok("1 第一次降权真的降了", first >= 1 and abs(v1 - 0.3825) < 0.001, (first, v1))
ok("2 同一天再跑七次不再打折（重启不再掉记忆）", abs(v8 - v1) < 1e-9, (v1, v8))
ok("3 没被打进软删", int(_row(m["id"])[1]) == 0, _row(m["id"]))

# 过了 30 天再跑一次才会继续降
with sqlite3.connect(_DB) as c:
    c.execute("UPDATE fish_memory_records SET updated_at=? WHERE memory_id=?",
              (time.time() - 40 * 86400, m["id"]))
    c.commit()
E.decay_low_confidence(days=30)
v9 = float(_row(m["id"])[0])
ok("4 隔了 30 天才会再降一档", v9 < v8 - 1e-6, (v8, v9))

# ---- 5. 召回命中 = 激活，衰减不再当它「没人提」----
m2 = E.add_memory(G, A, "他养了两只猫", memory_type="fact",
                  source_type="said", confidence=0.6, anchor_text="养了猫")
with sqlite3.connect(_DB) as c:
    c.execute("UPDATE fish_memory_records SET last_activated_at=?, activation_count=0 "
              "WHERE memory_id=?", (time.time() - 40 * 86400, m2["id"]))
    c.commit()
hits = E.recall(G, A, "我养了两只猫，你呢", top_k=3)
r = _row(m2["id"])
ok("5 召回能命中", any(h["id"] == m2["id"] for h in hits), [h["id"] for h in hits])
ok("6 命中后激活时间被刷新", r[3] > time.time() - 60, r)
ok("7 命中后计数 +1", int(r[2]) >= 1, r)
ok("8 召回结果带归属人（纠错断言要用）",
   all("person_id" in h for h in hits) and any(h.get("person_id") == A for h in hits), hits[0])

# ---- 9. 召回缓存按 (群, 人) 分开：B 拿不到 A 的召回 ----
store = {}
FM.put_recalled(store, G, A, [{"id": "x1", "content": "他养了两只猫"}])
ok("9 A 能取回自己的", len(FM.take_recalled(store, G, A)) == 1)
ok("10 B 取不到 A 的（这就是串档的入口）", FM.take_recalled(store, G, B) == [])
ok("11 键是 (群, 人)", (G, A) in store, list(store))
# 300 秒 TTL
FM.put_recalled(store, G, A, [{"id": "x1"}], now=time.time() - 301)
ok("12 超过 300 秒的召回不算数", FM.take_recalled(store, G, A) == [])
FM.put_recalled(store, G, A, [{"id": "x1"}], now=time.time() - 299)
ok("13 299 秒内仍然有效", len(FM.take_recalled(store, G, A)) == 1)

# ---- 14. 纠错归属断言：拿着 A 的条目去改 B，直接放弃 ----
foreign = {"id": m2["id"], "person_id": A, "content": "他养了两只猫"}
fix = E.apply_correction(G, B, foreign, "不是他养的，是我养的", by_uid=B)
ok("14 归属不符时不动手", fix == {} or not fix.get("new"), fix)
ok("15 A 的旧条没被作废", int(_row(m2["id"])[1]) == 0, _row(m2["id"]))
with sqlite3.connect(_DB) as c:
    n = c.execute("SELECT COUNT(*) FROM fish_memory_corrections WHERE person_id=?", (B,)).fetchone()[0]
ok("16 B 的档案里没留假纠正", n == 0, n)

# 归属一致就正常改
own = {"id": m2["id"], "person_id": A, "content": "他养了两只猫"}
fix2 = E.apply_correction(G, A, own, "不是两只，是三只", by_uid=A)
ok("17 归属一致时照常纠正", bool(fix2.get("new")) and int(_row(m2["id"])[1]) == 1, fix2)

# ---- 18. 老数据没有 person_id 时不误伤（兼容旧召回条目）----
legacy = {"id": "no-such-id", "content": "随便"}
ok("18 缺 person_id 不抛异常", isinstance(E.apply_correction(G, A, legacy, "嗯"), dict))

print()
print("P0 记忆回归 %d 例：%d 通过 / %d 失败" % (18, 18 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
