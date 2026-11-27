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
import re
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
            ("memory_detail", self.memory_detail, ["GET"], "单人记忆明细"),
            ("events", self.events, ["GET"], "最近交互事件"),
            ("favor", self.favor, ["GET"], "群友好感度"),
            ("group", self.update_group, ["POST"], "改群级开关"),
            ("group_reset", self.group_reset, ["POST"], "删群级 override"),
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

    # ---------------- 生效状态 ----------------
    def _whitelist_groups(self) -> list[str]:
        """配置里的群白名单，统一成去空白的字符串列表。

        配置项允许写成 "[\\"*\\"]" 也可能被人手写成 "123,456" 逗号串（main.py 的
        _group_allowed 两种都认），面板要做权限判断就必须用同一套解析，
        否则会出现「面板说没权限、插件其实在接话」的错位。
        """
        groups = self.p.cfg.get("enabled_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        return [str(g).strip() for g in groups if str(g).strip()]

    def _in_whitelist(self, gid: str) -> bool:
        groups = self._whitelist_groups()
        return "*" in groups or str(gid) in groups

    def _group_state(self, gid: str) -> dict:
        """把「静默 / override / 白名单」三处状态合成本群真正生效的结果。

        main.py 里这三处判断是分开的：_is_muted 先短路，_group_allowed 再看
        override.enabled，最后才退回白名单。面板如果只把 _group_allowed 当状态，
        群里 /off 过的群会照样显示「接话中」——开关看着坏了，其实是两个状态源没合并。
        """
        ov = (getattr(self.p, "_group_overrides", {}) or {}).get(str(gid), {}) or {}
        whitelisted = self._in_whitelist(gid)
        muted = bool(self.p._is_muted(gid))
        allowed = bool(self.p._group_allowed(gid))
        enabled = allowed and not muted
        if muted:
            # 静默优先级最高：/off 之后连 @ 都不回
            reason = "muted"
        elif ov.get("enabled") is False:
            reason = "override_off"
        elif enabled and not whitelisted:
            # 历史脏数据：override 里存着 enabled=true，效果是绕过白名单偷偷接话。
            # 单独报出来，让人知道这个群为什么会「莫名在说话」
            reason = "override_on_unlisted"
        elif enabled:
            reason = "on"
        else:
            reason = "not_whitelisted"
        return {"enabled": enabled, "muted": muted,
                "whitelisted": whitelisted, "reason": reason}

    def _known_groups(self) -> list[str]:
        """运行期见过的群 + 白名单写的群 + 有 override 的群，合起来才是「面板该显示的群」。"""
        seen: list[str] = []
        for src in (
            list(getattr(self.p, "_groups", {}).keys()),
            list(self._affect_state().keys()),
            list(getattr(self.p, "_group_overrides", {}).keys()),
            # 只在群里 /off 过、之后再没动静的群，不加这一行面板里就找不到它了
            list(getattr(self.p, "_mute_state", {}).keys()),
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
            # 但面板上明明列着八个群，显示 0 会让人以为白名单没生效。
            # 用的是合并后的生效状态：/off 静默中的群不算在生效里，否则跟群列表打架
            "groups_runtime": len([g for g in self._known_groups() if self._group_state(g)["enabled"]]),
            "memory": mem,
            "memes": memes,
            "peers": len(getattr(self.p, "peers", []) or []),
            "cross_topics": len(getattr(self.p, "cross").topics) if getattr(self.p, "cross", None) else 0,
            "switches": {
                "interject_desire": float(cfg.get("interject_desire", 0.3) or 0.3),
                "interject_cooldown_sec": int(cfg.get("interject_cooldown_sec", 120) or 120),
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
            state = self._group_state(gid)
            interject_last_hour = 0
            if st is not None:
                interject_last_hour = len([t for t in st.interject_times if now - t < 3600])
            out.append({
                "group_id": gid,
                # enabled 给的是「合并后真正生效」的结果（静默 + override + 白名单），
                # 不是单看你写没写过 override：前端拿它当开关状态，两个来源分开给就会打架
                "enabled": state["enabled"],
                "muted": state["muted"],
                "whitelisted": state["whitelisted"],
                "state_reason": state["reason"],
                "override": {
                    "enabled": ov.get("enabled"),
                    "interject_desire": ov.get("interject_desire"),
                    "note": ov.get("note") or "",
                },
                # 有没有 override 决定面板要不要放「删 override」按钮：没东西可删就别给按钮
                "has_override": bool(ov),
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
        # 取 title 不取 content：记忆正文可能是整段转发或长粘贴，
        # 面板是概览，不该把原文原样摊在群号旁边
        sql = ("SELECT group_id, person_id, COUNT(*) AS cnt, AVG(importance) AS imp, "
               "MAX(updated_at) AS ts, MAX(title) AS sample, MAX(memory_type) AS kind "
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
            "sample": (r["sample"] or "")[:40],
            "type": r["kind"] or "event",
        } for r in rows]
        return _json_ok({"group_id": group_id, "people": people,
                         "total": len(people), "now": now})

    async def memory_detail(self):
        """单个人的全部记忆明细——这里才给正文。

        排行接口只给一条样本（面板不摊原文），但群里 /memory 也只给条数了，
        总得有个地方能看全，就是这里：点开某个人，逐条列 title + content + 标签。
        """
        group_id = (request.query.get("group_id") or "").strip()
        person_id = (request.query.get("person_id") or "").strip()
        if not person_id:
            return _json_err("缺 person_id")
        try:
            limit = min(300, max(1, int(request.query.get("limit") or 100)))
        except ValueError:
            limit = 100
        db = self._mem_db()
        if not db.is_file():
            return _json_ok({"items": [], "person_id": person_id})
        sql = ("SELECT memory_id, group_id, person_id, title, content, memory_type, "
               "importance, updated_at FROM fish_memory_records "
               "WHERE is_deleted = 0 AND person_id = ?")
        args: list = [person_id]
        if group_id:
            sql += " AND group_id = ?"
            args.append(group_id)
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
                tags: dict[str, list] = {}
                if rows:
                    ids = [r["memory_id"] for r in rows]
                    ph = ",".join("?" * len(ids))
                    for mid, val in conn.execute(
                        f"SELECT memory_id, tag_value FROM fish_memory_tags "
                        f"WHERE memory_id IN ({ph})", ids):
                        tags.setdefault(str(mid), []).append(str(val))
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("[fish] 控制台读记忆明细失败：%s", e)
            return _json_err(f"读记忆库失败：{e}")
        now = time.time()
        items = [{
            "memory_id": r["memory_id"],
            "group_id": r["group_id"],
            "title": r["title"] or "",
            "content": r["content"] or "",
            "type": r["memory_type"] or "",
            "importance": round(float(r["importance"] or 0), 2),
            "updated_ago": _ago(float(r["updated_at"] or 0), now),
            "tags": tags.get(str(r["memory_id"]), []),
        } for r in rows]
        return _json_ok({"person_id": person_id, "group_id": group_id,
                         "items": items, "total": len(items), "now": now})

    def _favor_state(self) -> dict:
        f = self._data_dir / "favor_state.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def favor(self):
        """好感度排行：分数低的排前面，最该看一眼的是那些被怼到低分的人。"""
        gid = (request.query.get("group_id") or "").strip()
        state = self._favor_state()
        now = time.time()
        rows = []
        for g, persons in state.items():
            if gid and g != gid:
                continue
            if not isinstance(persons, dict):
                continue
            for uid, rec in persons.items():
                if not isinstance(rec, dict):
                    continue
                # 状态文件是手改得动的，一个 "s": "未知" 就能让整个 /api/favor 500
                try:
                    score = float(rec.get("s", 50))
                except (TypeError, ValueError):
                    score = 50.0
                rows.append({
                    "group_id": g, "person_id": uid, "score": round(score, 1),
                    "tier": "亲近" if score >= 80 else "熟人" if score >= 60 else "中立" if score >= 40 else "嫌弃",
                    "reason": str(rec.get("n") or ""),
                    "updated_ago": _ago(float(rec.get("t") or 0), now),
                })
        rows.sort(key=lambda r: r["score"])
        return _json_ok({"people": rows, "now": now})

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

        # 白名单闸门。override 的语义是「覆盖白名单里的某个群」，不是「白名单之外还能开后门」：
        # 不拦的话，谁打开面板就能把一个没上线的群点亮，等于 enabled_groups 这条安全线形同虚设。
        # 只拦「要让它生效」的动作（打开 / 调意愿），关闭、备注、删 override 一律放行，
        # 否则历史脏数据就再也清不掉了。
        if not self._in_whitelist(gid) and (key == "interject_desire" or (key == "enabled" and value is True)):
            return _json_err(
                f"群 {gid} 不在 enabled_groups 里（也没有 \"*\"），面板不能给它单独配置。"
                "要让它在群里接话，先把群号加进插件配置的 enabled_groups；"
                "如果这是以前留下的 override，点这张卡上的「重置」就能清掉"
            )
        if key == "enabled" and value is True:
            # 静默状态存在 group_muted.json，是群内 /off /on 的地盘：面板直接清掉会漏掉
            # 「恢复时是否要加回云崽白名单」那一位，那样开出来的群在云崽侧还是哑的
            if self.p._is_muted(gid):
                return _json_err(
                    f"群 {gid} 正处于 /off 静默，面板改不了静默状态；请先在该群发 /on 解除"
                )

        groups = dict(getattr(self.p, "_group_overrides", {}) or {})
        item = dict(groups.get(gid) or {})
        if key == "enabled" and value is True:
            # 开启 = 回到「跟随白名单」，而不是往 override 里塞一条 enabled=true。
            # 塞 true 等于把这个群钉死在白名单之外：以后就算把它从白名单删掉，
            # _group_allowed 仍会因为这条 override 继续接话（问题 3 的根源）
            item.pop("enabled", None)
        else:
            item[key] = value
        if item:
            groups[gid] = item
        else:
            # 关掉之后 override 空成 {}：空条目会让人以为「这个群单独配过」，直接删掉
            groups.pop(gid, None)
        try:
            self.p.save_group_overrides(groups)
        except Exception as e:
            logger.warning("[fish] 控制台写群配置失败：%s", e)
            return _json_err("写文件失败，细节看服务端日志")
        self.p.log_event("panel", gid, f"面板改了 {key} = {value}")
        return _json_ok({
            "group_id": gid, "key": key, "value": value, "override": item,
            # 回写生效状态：前端刷新前就能知道这次改动到底让群开起来了没有
            "state": self._group_state(gid),
        })

    async def group_reset(self):
        """删掉群级 override（整条，或只删某个字段）。

        加这个接口是因为 override 调歪了原来只能手改 data/group_overrides.json，
        面板上的「意愿」滑块也没有归零入口。只删 override，不动 group_muted.json：
        静默的解除要连带把云崽白名单恢复回去，那属于群内 /on 的职责，面板越俎代庖会把状态搞歪。
        """
        body = await request.json({}) or {}
        gid = str(body.get("group_id") or "").strip()
        key = str(body.get("key") or "").strip()
        if not gid.isdigit():
            return _json_err("group_id 必须是纯数字群号")
        groups = dict(getattr(self.p, "_group_overrides", {}) or {})
        if gid not in groups:
            # 没 override 不算错：面板可能停在旧数据上，直接返回当前生效状态就行
            return _json_ok({"group_id": gid, "removed": [],
                             "state": self._group_state(gid)})
        item = dict(groups.get(gid) or {})
        if key:
            # 指定字段就只删这个字段，别的设置（比如备注）保留
            removed = [key] if key in item else []
            item.pop(key, None)
        else:
            removed = list(item.keys())
            item = {}
        if item:
            groups[gid] = item
        else:
            groups.pop(gid, None)
        try:
            self.p.save_group_overrides(groups)
        except Exception as e:
            logger.warning("[fish] 控制台删群 override 失败：%s", e)
            return _json_err("写文件失败，细节看服务端日志")
        self.p.log_event("panel", gid, f"面板删了 override：{'、'.join(removed) or '无'}")
        return _json_ok({"group_id": gid, "removed": removed,
                         "override": item, "state": self._group_state(gid)})
