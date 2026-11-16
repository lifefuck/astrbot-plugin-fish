# -*- coding: utf-8 -*-
"""`/memory`、`/memary` 指令：群友记忆库与好感度总结。

这一版是照老仓库 `qq-deepseek-bot/handlers/admin.py::handle_memory_summary_command`
一比一搬过来的——排版、字段、排序、合并转发都一样，别再自己改样式：

    群友记忆 · 群名 · 34 人          ← 标题节点（机器人自己的号）
    群友A  QQ 10001                ← 每人一个节点，挂**真实 QQ**，折叠记录里显示本人头像
    群员
    好感 41/100 普通｜对线吵架、善意调侃 （已沉淀 7 条深层旧事）

数据来源：
- `user_memories.json`（老机器人的档案，只读）：印象 atoms、兜底好感、兜底昵称
- `favor_state.json`（聊天插件实时好感，只读）：有就用它，比老档案新
- `fish_memory.db`（只读）：每人已沉淀多少条深层记忆
- 协议端 API：群名、群名片、群内角色、头衔
"""

import asyncio
import json
import os
import sqlite3
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# 默认指向聊天插件的数据目录；聊天和指令装在不同地方时在面板里改
DEFAULT_DATA = os.path.join(os.path.dirname(PLUGIN_DIR), "astrbot_plugin_fish", "data")
DEFAULT_DB = os.path.join(DEFAULT_DATA, "fish_memory.db")
DEFAULT_FAVOR = os.path.join(DEFAULT_DATA, "favor_state.json")
# 老机器人的档案默认在同级目录的旧仓库里，找不到就只显示实时好感
DEFAULT_OLD_MEM = os.path.expanduser("~/projects/qq-deepseek-bot/user_memories.json")

_MEMORY_CACHE_TTL = 20.0
_MEMORY_PER_PAGE = 12
_ROLE_LABEL = {"owner": "群主", "admin": "管理员"}

# 结果缓存：20 秒内同一个人重复发，直接用上次算好的
_CACHE: dict[str, tuple[float, str, list]] = {}
# 群成员列表缓存：一次拉全群，别每人一次 API
_MEMBER_CACHE: dict[str, tuple[float, dict]] = {}
_MEMBER_TTL = 300.0
_GROUP_NAME_CACHE: dict[str, str] = {}
# json 文件按 mtime 缓存
_JSON_CACHE: dict[str, tuple[float, dict]] = {}


def _safe_int(value, default: int = 50) -> int:
    """记忆里的 favor 可能被手改成 None/字符串，直接 int() 会整条指令崩掉。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _favor_tier(favor: int) -> str:
    if favor >= 80:
        return "恋慕"
    if favor >= 65:
        return "亲近"
    if favor >= 40:
        return "普通"
    return "炸毛"


def _dedupe_notes(notes) -> str:
    """印象是多轮打分攒的，「爱对线」这种会重复好几遍，显示前按顿号切开去重。"""
    seen, out = set(), []
    for item in notes or []:
        text = item.get("text") if isinstance(item, dict) else item
        for part in str(text or "").replace("，", "、").split("、"):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return "、".join(out)


def _load_json(path: str) -> dict:
    """按 mtime 缓存读取 json；文件不在或读坏了都当空表，不能让指令挂掉。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
        cached = _JSON_CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        _JSON_CACHE[path] = (mtime, data)
        return data
    except Exception as e:
        logger.debug("[memary] 读 %s 失败：%s", path, e)
        return {}


@register(
    "astrbot_plugin_memory_cmd",
    "life",
    "把原小鲸鱼的 /memary 原样搬过来：群友记忆与好感度总结，"
    "每人一张折叠节点，普通群友只能看自己那份。只读不改数据。",
    "1.2.0",
)
class MemoryCmdPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = config or {}
        logger.info("[memary] 记忆库：%s", self._db_path())

    # ---------------- 路径 ----------------
    def _db_path(self) -> str:
        return str(self.cfg.get("db_path") or DEFAULT_DB)

    def _favor_path(self) -> str:
        return str(self.cfg.get("favor_path") or DEFAULT_FAVOR)

    def _old_mem_path(self) -> str:
        return str(self.cfg.get("user_mem_path") or DEFAULT_OLD_MEM)

    # ---------------- 权限 ----------------
    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """群管/群主能看全群，普通群友只能看自己那份。

        跟老仓库一致：别人的好感数值和印象标签不是公开信息。
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
    async def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        if not group_id:
            return "私聊"
        if group_id in _GROUP_NAME_CACHE:
            return _GROUP_NAME_CACHE[group_id]
        name = f"群 {group_id}"
        try:
            bot = getattr(event, "bot", None)
            if bot is not None:
                info = await asyncio.wait_for(
                    bot.call_action("get_group_info", group_id=int(group_id)), timeout=4.0
                )
                got = str((info or {}).get("group_name") or "").strip()
                if got:
                    name = got
        except Exception as e:
            logger.debug("[memary] 拉群名失败（用群号）: %s", e)
        _GROUP_NAME_CACHE[group_id] = name
        return name

    async def _members(self, event: AstrMessageEvent, group_id: str) -> dict:
        """全群成员信息（名片/昵称/角色/头衔），5 分钟缓存。"""
        if not group_id:
            return {}
        now = time.time()
        cached = _MEMBER_CACHE.get(group_id)
        if cached and now - cached[0] < _MEMBER_TTL:
            return cached[1]
        info: dict[str, dict] = {}
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return {}
            raw = await asyncio.wait_for(
                bot.call_action("get_group_member_list", group_id=int(group_id)), timeout=8.0
            )
            for m in raw or []:
                uid = m.get("user_id")
                if uid:
                    info[str(uid)] = m
        except Exception as e:
            logger.warning("[memary] 拉群成员列表失败（排版降级）: %s", e)
        _MEMBER_CACHE[group_id] = (now, info)
        return info

    def _favor_of(self, group_id: str, uid: str) -> int:
        """好感：优先聊天插件的实时值，没有就退回老档案。"""
        live = _load_json(self._favor_path()).get(group_id, {}).get(uid) or {}
        if isinstance(live, dict) and live.get("s") is not None:
            try:
                return _safe_int(live.get("s"))
            except Exception:
                pass
        old = _load_json(self._old_mem_path()).get(group_id, {}).get(uid) or {}
        return _safe_int(old.get("favor", 50))

    def _deep_count(self, uid: str) -> int:
        path = self._db_path()
        if not os.path.exists(path):
            return 0
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
            try:
                row = conn.execute(
                    "SELECT COUNT(1) FROM fish_memory_records "
                    "WHERE person_id=? AND is_deleted=0",
                    (str(uid),),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[memary] 查深层记忆条数失败：%s", e)
            return 0

    def _scope(self, group_id: str, only_uid: str | None,
               member_ids: set[str] | None = None) -> dict:
        """这一屏要列哪些人：老档案里有记录的，加只在实时好感里的新人。

        实时好感里混着调试注入留下的假 QQ（10011、800000001 这种），
        所以只认「确实是本群成员」的那部分，否则人数虚高、还平白多出十几个查无此人。
        """
        scope = dict(_load_json(self._old_mem_path()).get(group_id, {}) or {})
        for uid in (_load_json(self._favor_path()).get(group_id, {}) or {}):
            uid = str(uid)
            if member_ids and uid not in member_ids:
                continue
            scope.setdefault(uid, {})
        if only_uid:
            scope = {only_uid: scope.get(only_uid, {})} if only_uid in scope else {}
        return scope

    # ---------------- 排版 ----------------
    def _format_member(self, uid: str, mem: dict, member: dict, group_id: str) -> str:
        """一个人三行：名字+QQ号 / 身份+头衔 / 好感+印象。折叠里一行塞太满看不清。"""
        card = (member.get("card") or "").strip()
        qq_nick = (member.get("nickname") or "").strip()
        fallback = (mem.get("nickname") or "").strip()
        name = card or qq_nick or fallback

        # 昵称拿不到时老档案会存成 QQ 号，别让它在同一行里出现两遍
        if not name or name == uid:
            head = f"QQ {uid}"
        else:
            head = name
            if card and qq_nick and card != qq_nick:
                head += f"（QQ昵称 {qq_nick}）"
            head += f"  QQ {uid}"

        role = _ROLE_LABEL.get(str(member.get("role") or ""), "群员")
        title = (member.get("title") or "").strip()
        meta = role + (f" · 头衔「{title}」" if title else "")

        favor = self._favor_of(group_id, uid)
        notes = _dedupe_notes(mem.get("notes")) or "无印象"
        deep = self._deep_count(uid)
        extra = f"（已沉淀 {deep} 条深层旧事）" if deep > 0 else ""
        tail = f"好感 {favor}/100 {_favor_tier(favor)}｜{notes} {extra}".strip()
        return "\n".join([head, meta, tail])

    async def _build(self, event: AstrMessageEvent, group_id: str, only_uid: str | None):
        """返回 (标题, [(QQ, 昵称, 正文), ...])；没数据返回 (None, [])。"""
        members = await self._members(event, group_id) if group_id else {}
        scope = await asyncio.to_thread(
            self._scope, group_id, only_uid, set(members) or None
        )
        if not scope:
            return None, []
        # 好感高的排前面，主人固定第一
        rows = sorted(
            scope.items(),
            key=lambda kv: (-self._favor_of(group_id, kv[0]), kv[0]),
        )
        owner = str(self.cfg.get("owner_qq") or "")
        if owner:
            rows.sort(key=lambda kv: 0 if kv[0] == owner else 1)

        group_name = await self._group_name(event, group_id)
        if only_uid:
            title = f"你的记忆 · {group_name}"
        else:
            title = f"群友记忆 · {group_name} · {len(scope)} 人"

        entries = []
        for uid, mem in rows:
            body = await asyncio.to_thread(
                self._format_member, uid, mem or {}, members.get(uid, {}), group_id
            )
            nick = (members.get(uid, {}).get("card")
                    or members.get(uid, {}).get("nickname")
                    or (mem or {}).get("nickname") or uid)
            entries.append((uid, nick, body))
        return title, entries

    # ---------------- 发送 ----------------
    async def _send_forward(self, event: AstrMessageEvent, group_id: str,
                            title: str, entries: list) -> bool:
        """合并转发：标题节点挂机器人号，每人节点挂本人 QQ（折叠里显示本尊头像）。"""
        bot = getattr(event, "bot", None)
        if bot is None or not entries:
            return False
        bot_qq = str(event.get_self_id() or "")
        nodes = [{
            "type": "node",
            "data": {
                "user_id": bot_qq,
                "nickname": str(self.cfg.get("bot_nick") or "小鲸鱼"),
                "content": [{"type": "text", "data": {"text": title}}],
            },
        }]
        for uid, nick, body in entries:
            nodes.append({
                "type": "node",
                "data": {
                    "user_id": str(uid),
                    "nickname": str(nick or uid),
                    "content": [{"type": "text", "data": {"text": body}}],
                },
            })
        try:
            res = await asyncio.wait_for(
                bot.call_action("send_group_forward_msg", group_id=int(group_id),
                                messages=nodes),
                timeout=8.0,
            )
            if isinstance(res, dict) and res.get("status") == "failed":
                logger.warning("[memary] 合并转发失败：%s", res)
                return False
            logger.info("[memary] 群 %s 合并转发 %d 条", group_id, len(nodes))
            return True
        except Exception as e:
            logger.warning("[memary] 合并转发异常：%s", e)
            return False

    @staticmethod
    def _pages(title: str, entries: list) -> list[str]:
        """协议端不支持合并转发时退回分页文本，功能不能因此没了。"""
        pages = []
        for start in range(0, len(entries), _MEMORY_PER_PAGE):
            chunk = entries[start:start + _MEMORY_PER_PAGE]
            lines = [title] if start == 0 else [f"{title}（{start + 1}-{start + len(chunk)}）"]
            lines += [body for _uid, _nick, body in chunk]
            pages.append("\n\n".join(lines))
        return pages

    # ---------------- 指令 ----------------
    @filter.command("memary", alias={"memory"})
    async def memary(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return
        group_id = str(event.get_group_id() or "")
        uid = str(event.get_sender_id())
        # 群管/群主看全群；普通群友只看自己
        only_uid = None if (self._is_privileged(event) or not group_id) else uid

        cache_key = f"{group_id or 'private'}:{only_uid or 'all'}"
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < _MEMORY_CACHE_TTL:
            title, entries = hit[1], hit[2]
        else:
            title, entries = await self._build(event, group_id, only_uid)
            if title is None:
                tip = "当前群聊暂无群友记忆记录" if group_id else "私聊暂无记忆记录"
                event.stop_event()
                yield event.plain_result(tip)
                return
            _CACHE[cache_key] = (time.time(), title, entries)

        logger.info("[memary] %s 查询 %s（%d 人）", uid, cache_key, len(entries))

        if self.cfg.get("forward", True) and group_id:
            if await self._send_forward(event, group_id, title, entries):
                event.stop_event()
                return

        # 合并转发不可用：分页纯文本顶上
        event.stop_event()
        for i, page in enumerate(self._pages(title, entries)):
            if i:
                await asyncio.sleep(0.25)
            yield event.plain_result(page)
