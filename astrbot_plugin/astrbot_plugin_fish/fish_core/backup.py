# -*- coding: utf-8 -*-
"""数据备份：JSON 快照 + sqlite 整库备份。

老仓库那套是 `backup_json(path, keep=3, min_interval=600)` + `load_json_with_recovery`：
覆盖前留快照、同一个文件十分钟内只留一份、主文件坏了自动回退最近的备份。
这里保留同样的语义，另外补一个 sqlite 备份——记忆库是本次迁移新增的落盘资产，
只做 JSON 快照它一条都不会被保住。

为什么用 sqlite3 的 backup API 而不是 cp：库是热文件，可能正处在 WAL 中间态，
直接拷出来的副本有机会打不开。backup() 会走事务，拿到一致快照。
"""

import glob
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger("astrbot")


def snapshot_json(path: str, keep: int = 3, min_interval: float = 600.0) -> None:
    """覆盖前留快照。同一条文件 min_interval 内只留一份，防止刷出一堆备份。"""
    if not path or not os.path.exists(path):
        return
    try:
        olds = sorted(glob.glob(f"{path}.bak-*"))
        if olds and time.time() - os.path.getmtime(olds[-1]) < min_interval:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = f"{path}.bak-{stamp}"
        shutil.copy2(path, bak)
        try:
            # copy2 连 mtime 一起搬，源文件几周没动过 → 下一次判断「刚才才备过」永远不成立，
            # 快照就会在每次保存时重刷。把 mtime 拨回当下，节流才真正生效
            os.utime(bak, None)
            os.chmod(bak, 0o600)   # copy2 原样搬权限，源文件是 664 备份也是
        except OSError:
            pass
        for old in sorted(glob.glob(f"{path}.bak-*"))[:-max(1, keep)]:
            os.remove(old)
    except Exception as e:
        logger.warning("[备份] 快照 %s 失败：%s", os.path.basename(path), e)


def load_json_recover(path: str):
    """读 JSON，主文件坏了就回退最近的备份。

    读不出来返回 None，绝不返回空字典：调用方拿空字典一保存，
    磁盘上整份档案就被抹掉了（老仓库在这上面栽过）。
    """
    if not path:
        return None
    for candidate in [path] + sorted(glob.glob(f"{path}.bak-*"), reverse=True):
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.error("[备份] 读 %s 失败：%s", os.path.basename(candidate), e)
            continue
        if not isinstance(data, dict):
            logger.error("[备份] %s 结构异常（不是对象），跳过", os.path.basename(candidate))
            continue
        if candidate != path:
            logger.warning("[备份] 主文件 %s 损坏，已从 %s 恢复",
                           os.path.basename(path), os.path.basename(candidate))
            try:
                shutil.copy2(candidate, path)
            except Exception as e:
                logger.warning("[备份] 回写 %s 失败：%s", path, e)
        return data
    return None


def backup_db(db_path: str, dest_dir: str, keep: int = 7,
              min_interval: float = 86400.0) -> str:
    """把 sqlite 库备份到 dest_dir，返回备份路径（没做就返回空串）。

    默认一天一次（min_interval=86400），留最近 keep 份。
    """
    if not db_path or not os.path.exists(db_path):
        return ""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        olds = sorted(glob.glob(os.path.join(dest_dir, "fish_memory-*.db")))
        if olds and time.time() - os.path.getmtime(olds[-1]) < min_interval:
            return ""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir, f"fish_memory-{stamp}.db")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # 备份也留个自检：打不开就等于没备
        chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True, timeout=5.0)
        try:
            n = chk.execute("SELECT COUNT(1) FROM fish_memory_records").fetchone()[0]
        finally:
            chk.close()
        # 源库是 WAL，备份过程会在目标旁边留下 -wal/-shm 残渣。
        # 备份目录里留着这俩，以后用它们去恢复会被当成「没提交的事务」，
        # 备份就不干净了，删掉
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(dest + suffix)
            except FileNotFoundError:
                pass
        for old in sorted(glob.glob(os.path.join(dest_dir, "fish_memory-*.db")))[:-max(1, keep)]:
            os.remove(old)
        logger.info("[备份] 记忆库已备份 %s（%d 条记录）", os.path.basename(dest), n)
        return dest
    except Exception as e:
        logger.warning("[备份] 记忆库备份失败：%s", e)
        return ""
