#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆补全的一次性迁移：给存量记忆补出处，并把老行标成 said_legacy。

为什么必须真标：新逻辑里「能不能主动提」只看 source_type + anchor_mid。
如果只靠 anchor_text 为空来挡，那么任何一个手贱补过锚点的脚本、
或者面板编辑过内容的行，都会悄悄变回「可以主动提」——
出处是身份，不是副产品，得写死在行上。

    python3 scripts/migrate_memory_source.py --dry-run   # 只看会改多少行
    python3 scripts/migrate_memory_source.py             # 迁移（先自动备份库）
    python3 scripts/migrate_memory_source.py --rollback  # 用最近一次备份覆盖回来

库路径：默认 <插件>/data/fish_memory.db，可用 FISH_MEMORY_DB 覆盖。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "astrbot_plugin", "astrbot_plugin_fish", "fish_core"))

import fish_memory as FM  # noqa: E402

BACKUP_DIR = os.path.expanduser("~/.astrbot/backups")


def db_path() -> str:
    return FM.DEFAULT_DB_PATH


def cols(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(fish_memory_records)")}


def ensure_cols(conn) -> list:
    have = cols(conn)
    added = []
    for name, decl in FM._MIGRATE_COLS:
        if name not in have:
            conn.execute("ALTER TABLE fish_memory_records ADD COLUMN %s %s" % (name, decl))
            added.append(name)
    conn.commit()
    return added


def backup_db(path: str) -> str:
    """整库文件备份（带 -wal/-shm）。

    用 sqlite 的 backup API 而不是 cp：线上库一直在写，直接拷贝可能拿到半页数据；
    backup API 会拿一致性快照
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, "fish_memory-pre-step5-%s.db" % ts)
    src = sqlite3.connect(path, timeout=5.0)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def migrate(dry: bool) -> dict:
    path = db_path()
    if not os.path.exists(path):
        print("库不存在：%s（没什么可迁的）" % path)
        return {"ok": False}

    bak = ""
    if not dry:
        bak = backup_db(path)

    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        added = ensure_cols(conn)
        # 只动「没有锚点、且还没被标过」的存量行。
        # 这里必须排除 source_type='said_legacy'：UPDATE 不写 anchor_mid，
        # 只按 anchor_mid 为空来筛的话，第二次跑会把整批行的 confidence 再重置一遍，
        # 把用户手工纠正过的分数抹掉——迁移脚本必须跑几次结果都一样。
        rows = conn.execute(
            "SELECT memory_id, content, memory_type, memory_type AS mt, confidence, source_type "
            "FROM fish_memory_records WHERE (anchor_mid='' OR anchor_mid IS NULL) "
            "AND COALESCE(source_type,'') IN ('', 'said')"
        ).fetchall()

        plan = []
        for r in rows:
            mt = (r["memory_type"] or "event").strip()
            conf = {"fact": 0.6, "preference": 0.6, "note": 0.6,
                    "promise": 0.55, "plan": 0.45, "event": 0.45,
                    "relationship": 0.5}.get(mt, 0.45)
            body = str(r["content"] or "")
            plan.append({
                "id": r["memory_id"], "type": mt, "conf": conf,
                "topic": FM.topics_of(body), "anchor_text": body[:80],
                "old_source": r["source_type"] or "",
            })

        print("库：%s" % path)
        print("补列：%s" % (", ".join(added) if added else "无（已是最新）"))
        print("待处理存量行：%d" % len(plan))
        for kind in sorted({p["type"] for p in plan}):
            n = len([p for p in plan if p["type"] == kind])
            print("  %-13s %d 条 → 置信度 %.2f" % (kind, n, [p["conf"] for p in plan if p["type"] == kind][0]))
        if dry:
            print("\n--dry-run：一条都没写。确认无误去掉 --dry-run 再跑。")
            return {"ok": True, "dry": True, "rows": len(plan), "added": added}

        for p in plan:
            conn.execute(
                "UPDATE fish_memory_records SET source_type='said_legacy', confidence=?, "
                "topic=CASE WHEN topic='' THEN ? ELSE topic END, "
                "anchor_text=CASE WHEN anchor_text='' THEN ? ELSE anchor_text END, "
                "updated_at=updated_at WHERE memory_id=?",
                (p["conf"], p["topic"], p["anchor_text"], p["id"])
            )
        conn.commit()

        left = conn.execute(
            "SELECT COUNT(*) FROM fish_memory_records WHERE source_type='said_legacy'"
        ).fetchone()[0]
        manifest = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                    "db": path, "backup": bak, "rows": len(plan),
                    "legacy_total": int(left), "added": added}
        man_path = os.path.join(os.path.dirname(bak), "fish_memory-pre-step5-%s.json"
                                % os.path.basename(bak).split("-pre-step5-")[-1][:-3])
        with open(man_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print("\n迁移完成：%d 行标为 said_legacy（库内共 %d 条旧记忆）" % (len(plan), left))
        print("备份：%s" % bak)
        print("清单：%s" % man_path)
        return {"ok": True, **manifest}
    finally:
        conn.close()


def rollback() -> int:
    mans = sorted(glob.glob(os.path.join(BACKUP_DIR, "fish_memory-pre-step5-*.json")))
    if not mans:
        print("没有找到迁移备份清单（%s/fish_memory-pre-step5-*.json）" % BACKUP_DIR)
        return 1
    man = json.load(open(mans[-1], encoding="utf-8"))
    src = man.get("backup") or ""
    if not src or not os.path.exists(src):
        print("清单里的备份文件不存在：%s" % src)
        return 1
    dst = man["db"]
    # 覆盖前先给当前库再存一份：回滚本身也可能是错的，得能再滚回去
    keep = dst + ".pre-rollback-%s" % _dt.datetime.now().strftime("%H%M%S")
    if os.path.exists(dst):
        shutil.copy2(dst, keep)
    conn = sqlite3.connect(src)
    out = sqlite3.connect(dst)
    try:
        conn.backup(out)
    finally:
        out.close()
        conn.close()
    for suf in ("-wal", "-shm"):
        p = dst + suf
        if os.path.exists(p):
            os.remove(p)
    print("已回滚到 %s（迁移时间 %s，%d 行）" % (src, man.get("ts"), man.get("rows", 0)))
    print("回滚前的库另存：%s" % keep)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆补全迁移：补出处、老行标 said_legacy")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写入")
    ap.add_argument("--rollback", action="store_true", help="用最近一次备份覆盖当前库")
    a = ap.parse_args()
    if a.rollback:
        return rollback()
    res = migrate(a.dry_run)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
