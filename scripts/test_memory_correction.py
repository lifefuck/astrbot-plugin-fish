#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆纠错单测：什么时候算纠正、纠正后旧条怎么处理、低置信度怎么降。

跑法：python3 scripts/test_memory_correction.py
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

_TMP = tempfile.mkdtemp(prefix="fishmem-fix-")
_DB = os.path.join(_TMP, "m.db")
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
G, U = "100000001", "200000001"

# 先落一条「两只猫」，当作上一轮召回给模型的记忆
rec = E.add_memory(G, U, "我养了两只猫，一只叫豆豆", memory_type="fact",
                   anchor_mid="m1", anchor_text="我养了两只猫")
recalled = [{"id": rec["id"], "content": "我养了两只猫，一只叫豆豆",
             "type": "fact", "source_type": "said", "confidence": 0.9,
             "anchor_text": "我养了两只猫", "topic": "养猫"}]

# ---- 判纠正 ----
hit = E.detect_correction("不是两只，是三只", recalled)
ok("1 数字改口算纠正", hit is not None and hit.get("id") == rec["id"], hit)
ok("2 没有纠正话术不算", E.detect_correction("今天天气不错", recalled) is None)
ok("3 有「不是」但不沾边不算",
   E.detect_correction("不是这个意思，我说的是游戏", recalled) is None)
ok("4 没有召回上下文不算", E.detect_correction("不是两只，是三只", []) is None)

# ---- 落纠正 ----
fix = E.apply_correction(G, U, hit, "不是两只，是三只，第三只是捡的", by_uid=U, anchor_mid="m2")
ok("5 返回旧条和新条", bool(fix.get("old")) and bool(fix.get("new")), fix)

with sqlite3.connect(_DB) as c:
    c.row_factory = sqlite3.Row
    old = c.execute("SELECT confidence, is_deleted FROM fish_memory_records WHERE memory_id=?",
                    (rec["id"],)).fetchone()
    corr = c.execute("SELECT old_content, new_content, by_uid FROM fish_memory_corrections "
                     "WHERE memory_id=?", (rec["id"],)).fetchall()
    newrow = c.execute("SELECT content, source_type, confidence, is_deleted FROM fish_memory_records "
                       "WHERE memory_id=?", (fix["new"],)).fetchone()

ok("6 旧条作废且置信度归零", old and old["is_deleted"] == 1 and float(old["confidence"]) == 0.0,
   dict(old) if old else None)
ok("7 纠正记录留了旧内容", bool(corr) and "两只猫" in corr[0]["old_content"], [dict(x) for x in corr])
ok("8 纠正记录留了新内容", bool(corr) and "三只" in corr[0]["new_content"], [dict(x) for x in corr])
ok("9 新条是本人亲口且高置信",
   newrow and newrow["source_type"] == "said" and float(newrow["confidence"]) >= 0.9,
   dict(newrow) if newrow else None)
ok("10 新条内容带更正后的数字", newrow and "三只" in newrow["content"], dict(newrow) if newrow else None)

# ---- 低置信度降级 ----
low = E.add_memory(G, U, "他好像在做游戏相关的活", memory_type="event",
                   source_type="heard", confidence=0.45, anchor_text="好像在做游戏")
with sqlite3.connect(_DB) as c:
    # 衰减判据是 updated_at（同一条一天只降一次），这里把两个时间都推老
    c.execute("UPDATE fish_memory_records SET last_activated_at=?, updated_at=? WHERE memory_id=?",
              (time.time() - 40 * 86400, time.time() - 40 * 86400, low["id"]))
    c.commit()
n = E.decay_low_confidence(days=30)
with sqlite3.connect(_DB) as c:
    v = c.execute("SELECT confidence FROM fish_memory_records WHERE memory_id=?",
                  (low["id"],)).fetchone()[0]
ok("11 30 天没印证会被降权", n >= 1 and float(v) < 0.45, (n, v))

# 降到 0.2 以下就软删（行还在，可人工捞回）
with sqlite3.connect(_DB) as c:
    c.execute("UPDATE fish_memory_records SET confidence=0.21, last_activated_at=?, updated_at=? "
              "WHERE memory_id=?",
              (time.time() - 40 * 86400, time.time() - 40 * 86400, low["id"]))
    c.commit()
E.decay_low_confidence(days=30)
with sqlite3.connect(_DB) as c:
    r = c.execute("SELECT confidence, is_deleted FROM fish_memory_records WHERE memory_id=?",
                  (low["id"],)).fetchone()
ok("12 降到 0.2 以下转软删", r and int(r[1]) == 1, tuple(r) if r else None)

# ---- 纠正记录不参与清理 ----
before = 0
with sqlite3.connect(_DB) as c:
    before = c.execute("SELECT COUNT(*) FROM fish_memory_corrections").fetchone()[0]
E.prune(grave_days=0)
with sqlite3.connect(_DB) as c:
    after = c.execute("SELECT COUNT(*) FROM fish_memory_corrections").fetchone()[0]
ok("13 清理不动纠正记录", before == after and before >= 1, (before, after))

print("记忆纠错 %d 例：%d 通过 / %d 失败" % (13, 13 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
