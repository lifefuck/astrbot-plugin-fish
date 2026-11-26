"""「去骂他」这类派活：从旧 bot 的 handlers/actions.py 搬过来的动作层。

旧版那套是完整 bot 里的一等公民（自带 ws、自带群成员表、自带发送），
在 AstrBot 插件里这些全得换掉，所以这里只保留「判断」和「生成」两段纯函数，
真正发消息交给 main.py（它才有 event 和 provider）。

跟提示词那套的分工：
  · 提示词 / quarrel.py 管「别人骂我，我怎么回」
  · 这里管「群友让我去骂人」——会被 @ 到别人头上，所以门槛更硬：
    不许骂群管、不许骂主人、不许一次点太多人、同群一分钟只干一次
"""

from __future__ import annotations

import random
import re

# main.py 用 _load() 按文件路径加载模块，相对 import 在这套加载器下不成立，
# 而且重复加载会让「五分钟两次」的冷却状态变成两份。这里留个空位，由 main 注入同一个实例
quarrel = None

# 无差别攻击：群嘲、挨个骂、随机挑人
# 「全体」「全场」这两个裸词不能单独算派活：群友正常吐槽里到处是它们
# （「公司全体脑子进水」「全场都在挂机」），以前命中就直接挑三个人开骂，
# 把一整段聊天当指令执行过一次线上事故。现在必须和骂人动词同句才算。
RE_ATTACK_ALL = re.compile(
    r"(无差别|群嘲|挨个|一个一个|随机挑|挑几个|随便骂|都骂一|都喷一)"
    r"|(?:骂|喷|怼|羞辱|收拾|教训|开骂|开喷)[^。！？\n]{0,8}"
    r"(?:全体|全场|所有人|全部人?|大家|全群|群里所有人)"
    r"|(?:全体|全场|所有人|全部人?|大家|全群)[^。！？\n]{0,8}(?:都骂|都喷|挨个骂|挨个喷)")

# 指名：必须是「命令」才认。以前只要句子里出现「骂」就抓后面的字，
# 于是「天天骂我」「他骂我两句」全被当成派活，它反而不吭声
RE_TARGET = re.compile(
    r"(?:给我|帮我|替我|你去|快去|去|来)\s*(?:骂|喷|怼|羞辱|收拾|教育)"
    r"|(?:骂|喷|怼)(?:一下|两句|一顿|几句|他|她|这货|那个)"
    # @ 只有跟在骂人动词后面才算靶子。以前这里挂着一个裸的 `|@`，
    # 于是群里任何一条「@张三 在吗」都被当成「派我去骂张三」——
    # 适配器把 @ 渲染成「 @昵称(QQ) 」，find_by_name 还真能从里面抠出人
    r"|(?:骂|喷|怼|羞辱|收拾|教育)\s*(?:一下|两句|一顿)?\s*@")

# 反向：求饶、制止、自嘲都不算派活
RE_NOT = re.compile(
    r"(别骂|不要骂|不许骂|骂他干嘛|骂我干嘛|别喷|不要喷|骂得好|该骂|欠骂|活该被骂|被骂了)")

_TARGET_BAN = ("主人", "老板", "群主", "管理", "管理员", "life")

# 生成嘴替时给模型的约束。字数放宽到 8~30：旧版写死 6~14，
# 稍微复杂一点的损法（「装懂装到骨头里去了」）会被截断成半句
_GEN_SYSTEM = """你在帮一个嘴很利的群友写怼人短句。只输出短句本身，一行一句，最多 3 行。
要求：8~30 个字；只损对方的说法、逻辑、装的样子；不许带家人、身体、诅咒、脏字；
不许讲大道理，不许「首先其次」这种结构；说完就停，不要解释你在做什么。"""


def parse(text: str) -> dict | None:
    """这句是不是派活。返回 {"kind": "all"|"target", "who": 原始字串} 或 None。"""
    raw = str(text or "").strip()
    if not raw or RE_NOT.search(raw):
        return None
    if RE_ATTACK_ALL.search(raw):
        return {"kind": "all", "who": ""}
    m = RE_TARGET.search(raw)
    if not m:
        return None
    tail = raw[m.end():]
    # 「骂一下所有人」「教育教育这个群」：以前会把「一下所有人」整个当成人名，
    # find_by_name 找不到就交回普通回复，等于这条指令废了
    if re.match(r"\s*(?:一下|下|两句|几句|一顿)?\s*(?:所有人|全部人?|全体|大家|全群|群里所有人)", tail):
        return {"kind": "all", "who": ""}
    who = re.match(r"\s*(@?[^\s，。！？]{1,16})", tail)
    who = (who.group(1) if who else "").strip(" @:：,，。！？")
    # 「骂一下张三」剩下的「一下张三」：动词性量词不是名字
    who = re.sub(r"^(?:一下|下|两句|几句|一顿|这个群|群里|群里的)\s*", "", who) or ""
    if who in ("我", "我们", "你", "你自己", "自己", "他吗"):
        return None
    if not who or any(b in who for b in _TARGET_BAN):
        # 「骂主人」这种一律当没听见：这条是旧 bot 被群友拿来借刀骂人之后加的
        return None
    return {"kind": "target", "who": who}


def pick_targets(members: list[dict], recent: list[dict], *, self_uid: str,
                 owner_uid: str = "", want: int = 3) -> list[dict]:
    """从群成员/最近发言人里挑靶子，去掉自己、主人、群管，按最近发言优先。"""
    ban_role = ("admin", "owner")

    def ok(m: dict) -> bool:
        if str(m.get("uid") or "") in (str(self_uid), str(owner_uid or "\0")):
            return False
        return str(m.get("role") or "member") not in ban_role

    picked, seen = [], set()
    for m in recent:            # 最近说过话的优先，骂「空气」很蠢
        u = str(m.get("uid") or "")
        if not u or u in seen or not ok(m):
            continue
        seen.add(u)
        picked.append({"uid": u, "nick": m.get("nick") or u, "role": m.get("role") or "member"})
        if len(picked) >= want:
            return picked
    for m in members:           # 群里还没人说话（刚开局）才回落到成员表
        u = str(m.get("uid") or "")
        if not u or u in seen or not ok(m):
            continue
        seen.add(u)
        picked.append({"uid": u, "nick": m.get("nick") or u, "role": m.get("role") or "member"})
        if len(picked) >= want:
            break
    return picked


def find_by_name(members: list[dict], who: str, *, self_uid: str,
                 owner_uid: str = "") -> list[dict]:
    """按 @ / QQ 号 / 昵称找靶子。昵称用包含匹配，群里带后缀的「小明(摸鱼中)」也能对上。"""
    who = str(who or "").lstrip("@").strip()
    if not who:
        return []
    # @ 在日志/文本里常渲染成「我是神人(1234567890)」，先抠出 QQ 号，
    # 否则整串拿去比昵称永远比不中——22:50 那两次「找不到靶子」就是这么来的
    _uid_hit = ""
    _m = re.search(r"[\(（](\d{5,})[\)）]", who)
    if _m:
        _uid_hit = _m.group(1)
    elif who.isdigit() or (who.lstrip("@").isdigit() and len(who) >= 5):
        _uid_hit = who.lstrip("@")
    if _uid_hit:
        who = _uid_hit
    hit = []
    for m in members:
        uid = str(m.get("uid") or "")
        nick = str(m.get("nick") or "")
        if not uid or uid in (str(self_uid), str(owner_uid or "\0")):
            continue
        if str(m.get("role") or "member") in ("admin", "owner"):
            continue
        if uid == who or (_uid_hit and uid == _uid_hit) or who in nick or (nick and nick in who):
            hit.append({"uid": uid, "nick": nick or uid, "role": m.get("role") or "member"})
    # 完全同名的只留一个，别一 @ 一大串
    seen, out = set(), []
    for h in hit:
        if h["uid"] in seen:
            continue
        seen.add(h["uid"])
        out.append(h)
    return out[:2]


def gen_prompt(target_nick: str, context: str) -> str:
    ctx = " ".join(str(context or "").split())[:200]
    return (f"要怼的人是「{target_nick}」。他最近说的话是：{ctx or '（没说什么，就当他欠骂）'}\n"
            "写 2 行怼他的短句，直接输出短句。")


def clean_lines(text: str, *, limit: int = 2) -> list[str]:
    """把模型返回的东西收拾成能直接 @ 出去的短句：去编号、去引号、限长、过谐音降级。"""
    out = []
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("\"'“”‘’「」【】 ")
        line = re.sub(r"^\s*(?:\d+[.、)]|[-*•])\s*", "", line)
        line = re.sub(r"^[^\u4e00-\u9fa5A-Za-z0-9]+|[^\u4e00-\u9fa5A-Za-z0-9！？。…]+$", "", line)
        if not line or len(line) < 5:
            continue
        if len(line) > 40:          # 长句在群里根本不是怼人，是演讲
            line = line[:40]
        if "首先" in line or "其次" in line or "总的" in line:
            continue
        if quarrel is not None:
            line = quarrel.soften(line)
        if line and line not in out:
            out.append(line)
        if len(out) >= limit:
            break
    fallback = quarrel._QUIPS if quarrel is not None else ("急得跳脚像个小丑",)
    return out or [random.choice(fallback)]
