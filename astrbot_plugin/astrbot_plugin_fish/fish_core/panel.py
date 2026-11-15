"""大肥鱼控制台的后端：把老面板那几块数据做成插件自带的 Web API。

为什么不用老项目的 http.server：老面板是自己起一个端口 + 自己一套密码，
迁过来只会多一个要记的地址和一处要维护的鉴权。AstrBot 4.x 支持插件在面板里挂页面
（pages/ 目录 + register_web_api），页面跑在面板自己的登录态和同源策略里，
不用另开端口，也不用再存一份密码。

接口分两类：
    只读  总览、群列表、记忆排行、最近事件
    受控写 只改群级开关（enabled / interject_desire / note），走白名单字段，
          写在插件自己的 data/group_overrides.json，不碰 AstrBot 的全局配置。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import request

PLUGIN_NAME = "astrbot_plugin_fish"
API_PREFIX = f"/{PLUGIN_NAME}/panel"
# 插件目录下的 data/：这里不依赖插件实例上的属性，少一处猜错就少一处空面板
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 面板能改的群级字段：白名单，键名随便传会把脏数据写进 overrides 文件
WRITABLE_KEYS = ("enabled", "interject_desire", "note")


def _json_ok(data) -> dict:
    # 和面板其它接口一样的返回形状：前端会先看 status，再取 data
    return {"status": "ok", "data": data}


def _json_err(msg: str) -> dict:
    return {"status": "error", "message": msg, "data": None}


def _ago(ts: float, now: float) -> float | None:
    """给前端一个「多少秒前」，别让它自己算时区。"""
    if not ts or ts <= 0:
        return None
    return round(max(0.0, now - ts), 1)


class PanelApi:
    def __init__(self, plugin) -> None:
        self.p = plugin

    # ---------------- 注册 ----------------
    def register(self) -> None:
        ctx = self.p.context
        routes = [
            ("overview", self.overview, ["GET"], "控制台总览"),
            ("groups", self.groups, ["GET"], "群列表与冷热场"),
            ("memories", self.memories, ["GET"], "群友记忆排行"),
            ("events", self.events, ["GET"], "最近交互事件"),
            ("group", self.update_group, ["POST"], "改群级开关"),
        ]
        for name, handler, methods, desc in routes:
            ctx.register_web_api(f"{API_PREFIX}/{name}", handler, methods, desc)
        logger.info("[fish] 控制台接口已注册 %d 个：%s/*", len(routes), API_PREFIX)

    # ---------------- 数据工具 ----------------
    @property
    def _data_dir(self) -> Path:
        return DATA_DIR

    def _mem_db(self) -> Path:
        return self._data_dir / "fish_memory.db"

    def _affect_state(self) -> dict:
        """心境直接读落盘文件，不去碰 affect 模块的内部状态。"""
        f = self._data_dir / "affect_state.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _memory_stats(self, group_id: str | None = None) -> dict:
        """记忆条数、人数、类型分布。库不存在或正被写都当成空，面板不能因此报错。"""
        db = self._mem_db()
        if not db.is_file():
            return {"total": 0, "people": 0, "groups": 0, "types": {}}
        sql = ("SELECT group_id, person_id, memory_type, COUNT(*) FROM fish_memory_records "
               "WHERE is_deleted = 0")
        args: list = []
        if group_id:
            sql += " AND group_id = ?"
            args.append(str(group_id))
        sql += " GROUP BY group_id, person_id, memory_type"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                rows = conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("[fish] 控制台读记忆库失败：%s", e)
            return {"total": 0, "people": 0, "groups": 0, "types": {}}
        # 按人去重，不按「群+人」组合：同一个人在三个群各有记忆，面板写「3 人」才不算骗人
        people = {r[1] for r in rows}
        types: dict[str, int] = {}
        for r in rows:
            types[r[2]] = types.get(r[2], 0) + int(r[3])
        return {
            "total": sum(int(r[3]) for r in rows),
            "people": len(people),
            "groups": len({r[0] for r in rows}),
            "types": types,
        }

    def _known_groups(self) -> list[str]:
        """运行期见过的群 + 白名单写的群 + 有 override 的群，合起来才是「面板该显示的群」。"""
        seen: list[str] = []
        for src in (
            list(getattr(self.p, "_groups", {}).keys()),
            list(self._affect_state().keys()),
            list(getattr(self.p, "_group_overrides", {}).keys()),
        ):
            for g in src:
                g = str(g)
                if g and g != "private" and g not in seen:
                    seen.append(g)
        return seen

    # ---------------- 只读接口 ----------------
    async def overview(self):
        now = time.time()
        cfg = self.p.cfg
        memes = 0
        try:
            memes = sum(int(v) for v in self.p.meme_stats().values())
        except Exception:
            pass
        mem = self._memory_stats()
        return _json_ok({
            "uptime": round(now - float(getattr(self.p, "_started", now)), 1),
            "started_at": float(getattr(self.p, "_started", now)),
            "bot_nickname": str(cfg.get("bot_nickname") or ""),
            # 「生效群」按白名单口径算：_groups 只有收到过消息才会建，进程刚起时是 0，
            # 但面板上明明列着八个群，显示 0 会让人以为白名单没生效
            "groups_runtime": len([g for g in self._known_groups() if self.p._group_allowed(g)]),
            "memory": mem,
            "memes": memes,
            "peers": len(getattr(self.p, "peers", []) or []),
            "cross_topics": len(getattr(self.p, "cross").topics) if getattr(self.p, "cross", None) else 0,
            "switches": {
                "interject_desire": float(cfg.get("interject_desire", 0.3) or 0.3),
                "interject_cooldown_sec": int(cfg.get("interject_cooldown_sec", 300) or 300),
                "interject_hourly_cap": int(cfg.get("interject_hourly_cap", 8) or 8),
                "social_read": bool(cfg.get("social_read_enabled", True)),
                "split": bool(cfg.get("split_enabled", True)),
                "vision": bool(cfg.get("vision_enabled", True)),
                "poke_back": bool(cfg.get("poke_back", True)),
                "style_learning": bool(cfg.get("style_learning", True)),
            },
        })

    async def groups(self):
        now = time.time()
        affect_state = self._affect_state()
        out = []
        for gid in self._known_groups():
            st = getattr(self.p, "_groups", {}).get(gid)
            a = affect_state.get(gid, {}) or {}
            ov = getattr(self.p, "_group_overrides", {}).get(gid, {}) or {}
            interject_last_hour = 0
            if st is not None:
                interject_last_hour = len([t for t in st.interject_times if now - t < 3600])
            out.append({
                "group_id": gid,
                "enabled": self.p._group_allowed(gid),
                "override": {
                    "enabled": ov.get("enabled"),
                    "interject_desire": ov.get("interject_desire"),
                    "note": ov.get("note") or "",
                },
                "buffered_msgs": len(st.msgs) if st is not None else 0,
                "msgs_since_bot": st.msgs_since_bot if st is not None else 0,
                "interject_last_hour": interject_last_hour,
                "last_msg_ago": _ago(st.last_msg_ts if st else 0.0, now),
                "last_bot_reply_ago": _ago(st.last_bot_reply_ts if st else 0.0, now),
                "mind": {
                    "valence": round(float(a.get("valence", 0) or 0), 3),
                    "arousal": round(float(a.get("arousal", 0) or 0), 3),
                    "pout": round(float(a.get("pout", 0) or 0), 3),
                    "care": len(a.get("care") or []),
                    "updated_ago": _ago(float(a.get("updated", 0) or 0), now),
                },
                "memory": self._memory_stats(gid)["total"],
            })
        # 最近有动静的排前面，一屏能看到正在用的群
        out.sort(key=lambda g: (g["last_msg_ago"] is None, g["last_msg_ago"] or 1e18))
        return _json_ok({"groups": out, "now": now})

    async def memories(self):
        group_id = (request.query.get("group_id") or "").strip()
        try:
            limit = min(200, max(1, int(request.query.get("limit") or 40)))
        except ValueError:
            limit = 40
        db = self._mem_db()
        if not db.is_file():
            return _json_ok({"group_id": group_id, "people": [], "total": 0})
        # 列要起别名：行是 dict，靠下标取会直接 IndexError → 接口 500
        sql = ("SELECT group_id, person_id, COUNT(*) AS cnt, AVG(importance) AS imp, "
               "MAX(updated_at) AS ts, MAX(content) AS sample "
               "FROM fish_memory_records WHERE is_deleted = 0")
        args: list = []
        if group_id:
            sql += " AND group_id = ?"
            args.append(group_id)
        sql += " GROUP BY group_id, person_id ORDER BY COUNT(*) DESC LIMIT ?"
        args.append(limit)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("[fish] 控制台读记忆排行失败：%s", e)
            return _json_err(f"读记忆库失败：{e}")
        now = time.time()
        people = [{
            "group_id": r["group_id"],
            "person_id": r["person_id"],
            "count": int(r["cnt"]),
            "importance": round(float(r["imp"] or 0), 2),
            "updated_ago": _ago(float(r["ts"] or 0), now),
            "sample": (r["sample"] or "")[:60],
        } for r in rows]
        return _json_ok({"group_id": group_id, "people": people,
                         "total": len(people), "now": now})

    async def events(self):
        try:
            limit = min(200, max(1, int(request.query.get("limit") or 60)))
        except ValueError:
            limit = 60
        buf = list(getattr(self.p, "_events", []) or [])
        return _json_ok({"events": list(reversed(buf))[:limit], "now": time.time()})

    # ---------------- 受控写 ----------------
    async def update_group(self):
        body = await request.json({}) or {}
        gid = str(body.get("group_id") or "").strip()
        key = str(body.get("key") or "").strip()
        if not gid.isdigit():
            return _json_err("group_id 必须是纯数字群号")
        if key not in WRITABLE_KEYS:
            return _json_err(f"字段只能改这几个：{'、'.join(WRITABLE_KEYS)}")

        value = body.get("value")
        if key == "enabled":
            if not isinstance(value, bool):
                return _json_err("enabled 只接受 true / false")
        elif key == "interject_desire":
            try:
                value = float(value)
            except (TypeError, ValueError):
                return _json_err("interject_desire 得是 0~1 之间的小数")
            if not (0.0 <= value <= 1.0):
                return _json_err("interject_desire 得在 0~1 之间")
        else:  # note
            value = str(value or "")[:40]

        groups = dict(getattr(self.p, "_group_overrides", {}) or {})
        item = dict(groups.get(gid) or {})
        item[key] = value
        groups[gid] = item
        try:
            self.p.save_group_overrides(groups)
        except Exception as e:
            logger.warning("[fish] 控制台写群配置失败：%s", e)
            return _json_err(f"写文件失败：{e}")
        self.p.log_event("panel", gid, f"面板改了 {key} = {value}")
        return _json_ok({"group_id": gid, "key": key, "value": value, "override": item})
