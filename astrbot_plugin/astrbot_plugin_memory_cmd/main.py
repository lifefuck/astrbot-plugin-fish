# -*- coding: utf-8 -*-
"""/memary 指令（原小鲸鱼同款）。

单独成插件而不是塞进 astrbot_plugin_fish，是为了让「聊天」和「指令」互不牵连：
聊天插件改了配置、重启、甚至整个删掉，这条指令都还能用。

数据来源就是 astrbot_plugin_fish 写的那个 fish_memory.db，只读打开，不写一行。
"""

import asyncio
import os
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Node, Nodes, Plain

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认指向聊天插件的记忆库；聊天和指令装在不同地方时在面板里改这一项
DEFAULT_DB = os.path.join(
    os.path.dirname(PLUGIN_DIR), "astrbot_plugin_fish", "data", "fish_memory.db"
)

# 20 秒内重复发同一个指令直接复用上次结果：一次查询要把每个群友的记忆都拉一遍，
# 群里几个人同时发的话没必要查三遍
_CACHE: dict[str, tuple[float, str, list[str]]] = {}
_CACHE_TTL = 20.0
TYPE_LABEL = {
    "event": "经历",
    "preference": "喜好",
    "fact": "事实",
    "relation": "关系",
    "emotion": "情绪",
}


@register(
    "astrbot_plugin_memory_cmd",
    "life",
    "把原小鲸鱼的 /memary 搬成独立指令插件：按群隔离查看记忆库，只读不改数据。",
    "1.0.0",
)
class MemoryCmdPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        logger.info("[memary] 记忆库：%s", self._db_path())

    def _db_path(self) -> str:
        return str(self.cfg.get("db_path") or DEFAULT_DB)

    # ---------------- 权限 ----------------
    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """群管/群主/机器人管理员能看全群，普通群友只能看自己那份。

        以前对所有人开放，等于把别人的印象标签和记忆内容公开枚举。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        raw = getattr(event.message_obj, "raw_message", None)
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        return str(sender.get("role") or "") in ("owner", "admin")

    # ---------------- 取数 ----------------
    def _query(self, group_id: str, only_uid: str | None, per_person: int):
        """只读查询。用 URI 模式打开，避免和聊天插件的写事务抢锁。"""
        path = self._db_path()
        if not os.path.exists(path):
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
        try:
            conn.row_factory = sqlite3.Row
            params: list = [group_id]
            sql = (
                "SELECT person_id, title, content, memory_type, importance, updated_at "
                "FROM fish_memory_records WHERE group_id=? AND is_deleted=0"
            )
            if only_uid:
                sql += " AND person_id=?"
                params.append(str(only_uid))
            # 重要的、新的排前面；同一个人只取前 N 条，不然一个人就能占满整屏
            sql += " ORDER BY person_id, importance DESC, updated_at DESC"
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        people: dict[str, list] = {}
        for r in rows:
            bucket = people.setdefault(r["person_id"], [])
            if len(bucket) < per_person:
                bucket.append(r)
        return people

    async def _nick(self, event: AstrMessageEvent, group_id: str, uid: str) -> str:
        """拿群名片/昵称。协议端不支持或超时就退回 QQ 号，不能让一条指令卡住。"""
        if not group_id:
            return ""
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return ""
            info = await asyncio.wait_for(
                bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(uid),
                    no_cache=False,
                ),
                timeout=4.0,
            )
            return str((info or {}).get("card") or (info or {}).get("nickname") or "")
        except Exception:
            return ""

    async def _entries(self, event: AstrMessageEvent, group_id: str,
                       only_uid: str | None) -> tuple[str, list[str]] | None:
        per_person = int(self.cfg.get("per_person") or 5)
        people = await asyncio.to_thread(self._query, group_id, only_uid, per_person)
        if people is None:  # 库不存在
            return None
        if not people:
            return ("", [])

        # 记忆多的排前面；主人那份固定第一
        admin_id = str(self.cfg.get("owner_qq") or "")
        rows = sorted(people.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if admin_id:
            rows.sort(key=lambda kv: 0 if kv[0] == admin_id else 1)

        entries: list[str] = []
        for uid, items in rows:
            nick = await self._nick(event, group_id, uid)
            head = f"{nick}（QQ {uid}）" if nick else f"QQ {uid}"
            lines = [f"{head} · {len(items)} 条"]
            for r in items:
                label = TYPE_LABEL.get(r["memory_type"], r["memory_type"] or "记忆")
                text = " ".join(str(r["content"] or "").split())
                if len(text) > 80:
                    text = text[:80] + "…"
                lines.append(f"  [{label}] {text}")
            entries.append("\n".join(lines))

        name = str(self.cfg.get("group_alias") or "") or (
            f"群 {group_id}" if group_id else "私聊"
        )
        if only_uid:
            title = f"你的记忆 · {name}"
        else:
            title = f"群友记忆 · {name} · {len(rows)} 人" if group_id else f"私聊记忆 · {len(rows)} 人"
        return (title, entries)

    # ---------------- 指令 ----------------
    @filter.command("memary", alias={"memory"})
    async def memary(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return
        group_id = str(event.get_group_id() or "")
        uid = str(event.get_sender_id())
        only_uid = None if (self._is_privileged(event) or not group_id) else uid
        if not group_id:  # 私聊看的就是私聊那条线
            only_uid = None

        cache_key = f"{group_id or 'private'}:{only_uid or 'all'}"
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            title, entries = hit[1], hit[2]
        else:
            got = await self._entries(event, group_id, only_uid)
            if got is None:
                yield event.plain_result(
                    "记忆库还没建起来，先跟它聊几句再查"
                )
                return
            title, entries = got
            if not entries:
                yield event.plain_result(
                    "当前群聊暂无群友记忆记录" if group_id else "私聊暂无记忆记录"
                )
                return
            _CACHE[cache_key] = (time.time(), title, entries)

        logger.info("[memary] %s 查询 %s：%d 人", uid, cache_key, len(entries))

        # 合并转发优先：一屏塞不下几十个人，转发成一条聊天记录才不刷屏
        if self.cfg.get("forward", True):
            nodes = [Node(content=[Plain(f"{title}\n" + "\n\n".join(entries))], name=title, uin="0")]
            yield event.set_result(MessageEventResult(chain=[Nodes(nodes)]))
            return

        # 协议端不支持合并转发时退回分页文本，功能不能因为展示方式没了
        page = max(1, int(self.cfg.get("page_size") or 6))
        for i in range(0, len(entries), page):
            chunk = entries[i : i + page]
            head = title if i == 0 else f"{title}（{i + 1}-{i + len(chunk)}）"
            yield event.plain_result(head + "\n\n" + "\n\n".join(chunk))
            if i + page < len(entries):
                await asyncio.sleep(0.3)
