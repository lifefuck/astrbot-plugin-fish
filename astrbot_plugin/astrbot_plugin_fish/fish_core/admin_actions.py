# -*- coding: utf-8 -*-
"""自然语言群管理动作：禁言 / 踢人。

从老仓库 `handlers/actions.py` 搬过来的是**处置能力这一段**（`_resolve_target`、
`_parse_duration`、`_is_imperative`、`_do_ban`、`_do_kick` 和它们的保护规则），
骂人/无差别攻击、B站热榜、识图、发表情包那几段没搬——新插件里这些各有更完整的实现
（connectors / media_resolver / vision / meme_catalog），重复搬会互相打架。

保留下来的几条硬规则（都是踩过坑写的，别删）：
- 讨论句不是命令：「该不该禁言他」「踢不踢」不许真动手
- 只能处置比自己低的人：群主、管理员、主人、机器人自己都动不了
- 数字兜底要跳过带量词的句子：「我踢了 80000 步」不能把步数当 QQ 号踢人
"""

import logging
import re
import time

logger = logging.getLogger("astrbot")

# 「该不该禁言」「踢不踢」这种讨论句不是命令
_ASK_WORDS = re.compile(r"[?？]|吗|呢|吧|该不该|要不要|是不是|能不能|可不可以|如果|假如|要不|是否|该禁|该踢")
# 「不要禁言他」「别踢老王」是反话。只排疑问词挡不住，实测会把人真禁掉
_NEG_WORDS = re.compile(r"(别|不要|不用|甭|禁止|不许|不准|先别|不能|没必要)")
# 「把小明的禁言解了」方向相反，绝不能当成再禁一次
_UNBAN_WORDS = re.compile(r"(解禁|解除|取消禁言|撤销|解开|放出来|别禁了|不用禁了|可以说话了)")
# 数字兜底会把「我踢了80000步」的步数当 QQ 号
_NUM_UNIT = re.compile(r"\d{5,12}\s*(?:步|元|块|个|人|分|秒|天|年|岁|次|条|页|米|度|斤|万|千|层|抽|连)")
_RE_AT = re.compile(r"\[CQ:at,qq=(\d+)\]")

_ADMIN_CACHE: dict = {}
_ADMIN_TTL = 60.0


def is_imperative(body: str) -> bool:
    b = body or ""
    if _ASK_WORDS.search(b):
        return False
    # 反话最难防：一句话里只要有否定词就不动手，宁可漏一次也别误禁人
    if _NEG_WORDS.search(b):
        return False
    return True


def parse_duration(text: str) -> tuple[int, str]:
    """「10分钟」「半小时」「1小时」→ 秒；解析不出按 10 分钟。返回 (秒, 去掉时长后的文本)。"""
    if "半小时" in text or "半钟头" in text:
        # 「半钟头」也走这条：老版只判「半小时」，半钟头会掉进默认 10 分钟
        return 1800, text.replace("半小时", "").replace("半钟头", "")
    m = re.search(r"(\d+)\s*(秒|分钟|分|小时|钟头|天)", text)
    if not m:
        return 600, text
    n, unit = int(m.group(1)), m.group(2)
    mult = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "钟头": 3600, "天": 86400}[unit]
    return max(60, min(n * mult, 12 * 3600)), (text[:m.start()] + text[m.end():])


def resolve_target(text: str, nick_map: dict, at_ids: list | None = None):
    """把「小明」「老王」「@张三」对回 QQ 号。

    nick_map: {uid: 昵称}，调用方用最近发言的人拼出来即可，不必拉全群成员。
    at_ids:   消息里 @ 到的人（有它就最准，直接命中）。
    """
    body = re.sub(r"[\s，。！？!?~～]", "", text or "")
    if at_ids:
        for uid in at_ids:
            if uid and uid.isdigit():
                return uid, nick_map.get(uid) or uid
    # 长的先匹配，避免「阿明」命中「阿明明」
    for uid, nick in sorted(nick_map.items(), key=lambda kv: -len(str(kv[1]))):
        if nick and str(nick) in body:
            return str(uid), str(nick)
    # 数字兜底：句子里有量词就不认，免得把「80000 步」当 QQ 号
    if not _NUM_UNIT.search(text or ""):
        m = re.search(r"(\d{5,12})", body)
        if m:
            return m.group(1), m.group(1)
    return None


async def _is_group_admin(bot, gid: str, uid: str) -> bool:
    """是不是群管。缓存 60 秒——无权限的人每刷一次都白等一次 API，消息链路跟着卡。"""
    key = (gid, uid)
    hit = _ADMIN_CACHE.get(key)
    if hit and time.time() - hit[1] < _ADMIN_TTL:
        return hit[0]
    ok = False
    try:
        info = await bot.call_action("get_group_member_info", group_id=int(gid),
                                     user_id=int(uid), no_cache=True)
        ok = str((info or {}).get("role") or "") in ("admin", "owner")
    except Exception as e:
        logger.debug("[动作层] 查群管身份失败 %s/%s：%s", gid, uid, e)
    _ADMIN_CACHE[key] = (ok, time.time())
    return ok


def _ok(res) -> bool:
    """AstrBot 的 call_action 直接返回 data；有的适配器包一层 status。"""
    if res is None:
        return False
    if isinstance(res, dict):
        st = res.get("status")
        if st in ("failed", "error"):
            return False
        if "retcode" in res and int(res.get("retcode") or 0) != 0:
            return False
    return True


async def do_ban(bot, gid: str, sender_id: str, nick_map: dict, at_ids: list,
                 text: str, owner_qq: str, self_id: str) -> str:
    if not await _is_group_admin(bot, gid, sender_id) and sender_id != owner_qq:
        return "你说话不好使"
    secs, rest = parse_duration(text)
    target = resolve_target(rest, nick_map, at_ids)
    if not target:
        return "禁谁啊，说个名字"
    qq, nick = target
    if qq in (self_id, owner_qq):
        return "这个不行，换个人"
    # 借机器人的号去处置别的管理/群主，矛盾会直接烧到机器人身上
    if qq != sender_id and await _is_group_admin(bot, gid, qq):
        return "他也是管理，我不掺和"
    try:
        res = await bot.call_action("set_group_ban", group_id=int(gid),
                                    user_id=int(qq), duration=secs)
    except Exception as e:
        logger.warning("[动作层] 禁言异常：%s", e)
        return "我禁不动他，看看我是不是管理员"
    if _ok(res):
        logger.info("[动作层] 群 %s 禁言 %s(%s) %d 秒", gid, nick, qq, secs)
        span = f"{secs // 3600} 小时" if secs >= 3600 else f"{max(1, secs // 60)} 分钟"
        return f"{nick} 闭嘴 {span}"
    logger.warning("[动作层] 禁言失败：%s", res)
    return "我禁不动他，看看我是不是管理员"


async def do_kick(bot, gid: str, sender_id: str, nick_map: dict, at_ids: list,
                  text: str, owner_qq: str, self_id: str) -> str:
    if not await _is_group_admin(bot, gid, sender_id) and sender_id != owner_qq:
        return "你说话不好使"
    target = resolve_target(text, nick_map, at_ids)
    if not target:
        return "踢谁，说清楚"
    qq, nick = target
    if qq in (self_id, owner_qq):
        return "这个不行，换个人"
    if qq != sender_id and await _is_group_admin(bot, gid, qq):
        return "他也是管理，我不掺和"
    try:
        res = await bot.call_action("set_group_kick", group_id=int(gid),
                                    user_id=int(qq), reject_add_request=False)
    except Exception as e:
        logger.warning("[动作层] 踢人异常：%s", e)
        return "踢不动，权限不够"
    if _ok(res):
        logger.info("[动作层] 群 %s 踢出 %s(%s)", gid, nick, qq)
        return f"{nick} 送走了"
    logger.warning("[动作层] 踢人失败：%s", res)
    return "踢不动，权限不够"


async def maybe_run(event, bot, gid: str, sender_id: str, self_id: str,
                    owner_qq: str, nick_map: dict, text: str,
                    addressed: bool, is_admin: bool) -> str | None:
    """识别到处置意图就执行并返回要发的话；不是命令返回 None。

    只处理禁言/踢人：这两件事群里真的需要有人能执行，其余动作（换名片、
    撤回）不在老仓库里，别顺手加。
    """
    body = (text or "").strip()
    if not body or len(body) > 20:
        return None
    # 没被叫到、自己又不是群管、也不是主人：不理会
    # （主人可能只在群里挂个普通成员身份，不给这条口子就得每次先 @ 一下）
    if not addressed and not is_admin and sender_id != owner_qq:
        return None
    if not is_imperative(body):
        return None

    at_ids = []
    try:
        for comp in event.message_obj.message:
            qq = str(getattr(comp, "qq", "") or "")
            if qq.isdigit() and qq != self_id:
                at_ids.append(qq)
    except Exception:
        pass
    # CQ 码兜底：有的适配器把 @ 塞在 raw_message 里
    at_ids += [m for m in _RE_AT.findall(str(getattr(event.message_obj, "raw_message", "") or ""))
               if m != self_id]

    if _UNBAN_WORDS.search(body):
        # 解禁要走管理面板/人工，不能顺手套到"再禁一次"上
        return None
    wants_ban = "禁言" in body or "闭嘴" in body
    wants_kick = bool(re.search(r"(踢|踹)", body))
    if not (wants_ban or wants_kick):
        return None

    # 权限判断放在最前面：普通人真的在指名道姓指挥时直接回绝。
    # 但目标都解析不出来，很可能只是顺口一说（「再聊下去我就踢人了」），
    # 这种情况不接，留给模型正常聊天
    if sender_id != owner_qq and not await _is_group_admin(bot, gid, sender_id):
        return "你说话不好使" if resolve_target(body, nick_map, at_ids) else None

    if wants_ban:
        return await do_ban(bot, gid, sender_id, nick_map, at_ids, body, owner_qq, self_id)
    return await do_kick(bot, gid, sender_id, nick_map, at_ids, body, owner_qq, self_id)
