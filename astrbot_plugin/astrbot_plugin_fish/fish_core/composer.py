# -*- coding: utf-8 -*-
"""把「小鲸鱼」原项目的 prompt 拼装逻辑搬过来。

原项目在 services/llm.py 的 call_deepseek 里，把时间线、记忆、心境、群表达样例、
云崽指令清单、时间戳一段段贴到 user prompt 末尾。这里保持同样的顺序和措辞，
只把 broker（bot 全局对象）换成显式入参，好让它能被单测和注入法验证。
"""

import datetime
import json
import os
import random
import re
import time

_STYLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "style_fewshot.json")
_style_cache = {"mtime": 0.0, "data": []}

# 云崽面板指令清单：只有真在问用法时才带，平时一个字都不发
# 群里别 @ 别人：行首/空格后的 @昵称整段削掉。中文名和后面的正文之间常常没空格，
# 所以只吃「@ + 最多 12 个非空白字符」，剩下的正文留给模型原话来接
# 只在 @昵称 后面紧跟空白/标点/行尾时才削。以前允许吃到 12 个字，
# 「@张三你说得对」会被整句删空，然后 main 回退发原文，等于白削
_AT_MENTION = re.compile(r"(^|[\s，,])[@＠][^\s，。！？、,.!?：:]{1,12}(?=[\s，。！？、,.!?：:]|$)")
# 「我没艾特你啊」「不是要艾特」这类自我辩解的整句，直接删。它是在解释格式，
# 不是在跟人说话；越解释越显得机器
# 断句符里必须带逗号：不加的话「我不是要艾特你，别误会」会连后半句一起删掉
_AT_META_SENTENCE = re.compile(r"[^。！？!?\n，,、]{0,20}?(?:我)?(?:没有|没|不是)[^。！？!?\n，,、]{0,3}艾特[^。！？!?\n，,、]{0,20}[。！？!?]?")

_GAME_WORDS = ("原神", "星铁", "崩铁", "绝区零", "鸣潮", "异环")
_HOW_WORDS = ("怎么", "如何", "在哪", "哪里", "帮助")


def needs_games_kb(text: str) -> bool:
    """这句是不是在问云崽面板的用法（prompts.needs_games_kb 的薄封装）。"""
    if not text:
        return False
    low = text.lower()
    if any(w in low for w in ("绑定", "面板", "体力", "深渊", "剧透", "抽卡", "签到",
                              "帮助", "菜单", "指令", "命令", "点歌", "锅巴", "云崽")):
        return True
    entity = any(g in low for g in _GAME_WORDS) or "云崽" in low or "yunzai" in low
    return entity and any(w in low for w in _HOW_WORDS)


def style_samples_hint(group_id) -> str:
    """从本群真实聊天里抽几组「有人说→别人怎么接」，给模型一个语感锚点。

    形容词教不会语气，示例才教得会；每次随机换一批，避免它把某一组背成口头禅。
    """
    if not group_id:
        return ""
    try:
        mtime = os.path.getmtime(_STYLE_PATH)
        if _style_cache["mtime"] != mtime:
            with open(_STYLE_PATH, encoding="utf-8") as f:
                _style_cache["data"] = json.load(f)
            _style_cache["mtime"] = mtime
    except Exception:
        return ""
    pool = [x for x in _style_cache["data"] if str(x.get("gid")) == str(group_id)]
    if len(pool) < 3:
        return ""
    picked = random.sample(pool, min(5, len(pool)))
    body = "\n".join("「" + str(x["a"]) + "」→「" + str(x["b"]) + "」" for x in picked)
    return ("\n【这个群真实的接话长这样】别照抄内容，学的是「随口一句、踩在具体那件事上」的劲。\n"
            "这些都是别人的旧对话，里面的人名、群名、事都跟现在无关，别拿来当现在的人说：\n" + body)


def time_hint(now: float | None = None) -> str:
    """把真实时间贴在 prompt 末尾（放开头会破 KV cache 前缀命中）。"""
    now_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hour = now_dt.hour
    if 5 <= hour < 9:
        period = "清晨/早晨"
    elif 9 <= hour < 12:
        period = "上午"
    elif 12 <= hour < 14:
        period = "中午"
    elif 14 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "深夜/凌晨"
    weekday = "一二三四五六日"[now_dt.weekday()]
    return f"\n【当前真实时间】：{now_dt.strftime('%Y-%m-%d %H:%M')} 星期{weekday}（{period}）\n"


def build_system_prompt(base_persona: str, bot_nick: str, bot_qq: str, group_id: str, is_private: bool) -> str:
    """人设 + 身份锚点。

    身份必须写死：本来人设里叫「小鲸鱼」，AstrBot 侧账号昵称可能完全不同，
    模型会把两个名字当成两个人。
    """
    where = "私聊" if is_private else f"群 {group_id}"
    identity = (
        # 别用「账号」「对话」这种系统口吻：它越觉得自己是个程序，回话越容易端着
        f"（你是「{bot_nick}」，一条住在群里的鲸鱼娘。群里叫的傻鱼、大肥鱼、鲸宝也都是你，"
        f"不是别人也不是山寨货。你的 QQ 号是 {bot_qq}，现在在{where}说话）\n"
    )
    # 这段只留会当场露馅的硬东西：标签、Markdown、内部概念都会原样出现在群里。
    # 写成「不要×N」的清单时，模型每句话前都在核对，回话明显发钝（实测一段 17 个「不要」）；
    # 换成正面描述加后果说明，它自己就知道该写什么。
    rule = (
        "\n【输出】写进群里那句台词本身，直接说事。群友看到的是原样文本："
        "标签、Markdown、英文思考、关于怎么发消息的说明都会露出来。不 @ 人。"
        "好感度、记忆、插件、模型、AI 这些词不进正文，你就是群里的人。"
        "开场「咋了」「在」「喂」一场最多一次。\n"
    )
    return f"{identity}{base_persona}{rule}"


def build_user_prompt(
    *,
    timeline: list[str],
    latest_line: str,
    sender_nick: str,
    sender_uid: str,
    memory_lines: list[str],
    affect_parts: list[str],
    style_hint: str,
    anti_repeat_hint: str,
    games_kb: str,
    is_at: bool,
    is_private: bool,
    now: float | None = None,
    vision_desc: str = "",
    media_hint: str = "",
    social_hint: str = "",
    cross_hint: str = "",
    quarrel_hint: str = "",
    include_timeline: bool = True,
) -> str:
    """按原项目顺序拼 user prompt。"""
    parts = []
    if not include_timeline:
        # 多轮上下文模式：前面的记录已经作为 messages 传上去了，这里不再重拼一遍。
        # 以前每轮把整段群聊记录重新拼进 user prompt，滑窗一动整段从头变，
        # 前缀缓存一个 token 都命不中（Magpie 那边命中率只有 28% 就是这么来的）
        parts.append("【这是接着上面的对话往下说，前面的记录都在上文里】")
    elif is_private:
        parts.append("【对话记录】（这是私聊）")
    else:
        parts.append("【群聊记录】（从旧到新，最后一行是刚发生的）")
    if include_timeline:
        parts.extend(timeline or ["（群里暂时没别人说话）"])

    if vision_desc:
        # 图里说了什么必须摆在「当前这句」后面：放最前面模型会当成背景设定，答的时候当没看见
        parts.append(f"【你自己看到的画面】（就当是自己亲眼看见的，别复述画面内容，直接说你的反应）：{vision_desc}")

    if media_hint:
        # 链接/视频解析结果单独一块。以前塞进 vision_desc，模型读到的是
        # 「他这张图里：【这条链接的内容】BV 视频《PCL不能下载》」，语义拧着，
        # 于是它宁可问「这BV号是啥视频」也不肯用已经查到的标题
        parts.append(f"【他发的这条链接的内容】（已经解析出来了，直接当已知信息用，别再问是什么）：{media_hint}")

    parts.append(f"\n【当前这句】：{sender_nick}（{sender_uid}）：{latest_line or '（只@了你，没打字）'}")
    if is_at:
        parts.append("（他是在直接跟你说话，必须回）")
    else:
        parts.append("（没人叫你，这句是群友之间的闲聊；插话就一两句，接着话头说）")

    if memory_lines:
        parts.append("\n【你还记得的关于他/她的事】：")
        parts.extend("- " + m for m in memory_lines)

    if affect_parts:
        parts.append("")
        parts.extend(affect_parts)

    if style_hint:
        parts.append(style_hint)

    if anti_repeat_hint:
        parts.append(anti_repeat_hint)

    if games_kb:
        parts.append("\n【云崽指令清单（群友在问用法，按这个答）】：\n" + games_kb)

    if social_hint:
        # 第一步（social_read）的结论，只给角度不给原文，免得模型把「该怎么接」复述出来
        parts.append(social_hint)

    if cross_hint:
        parts.append(cross_hint)

    if quarrel_hint:
        # 有人刚骂了它/它主人。这段只在真正触发的那一轮出现，绝不进常驻人设：
        # 常驻一条「被骂就喷回去」会让它把调侃也当挑衅，闲聊全毁
        parts.append("\n" + quarrel_hint)

    parts.append(time_hint(now))
    return "\n".join(parts)


def anti_repeat_hint(recent_bot_replies: list[str]) -> str:
    """把刚说过的几句摆回模型面前当负样本，比堆「禁止词表」有效。"""
    recent = [r for r in (recent_bot_replies or []) if r][-4:]
    if not recent:
        return ""
    return "【你刚在群里说过这些话，别原样重复，换个说法】：\n" + "\n".join(f"- {r}" for r in recent)


# 模型偶尔会模仿工具调用语法，把整段 [send_message_to_user]…[/send_message_to_user] 吐进正文
_FAKE_TOOL_TAG = re.compile(r"\[/?(?:send_message_to_user|send_msg|send_group_msg|reply|message)\s*[^\]]*\]", re.I)
_BRACKET_ACTION = re.compile(r"[（(]([^（()）]{1,12})[)）]")
# 只有这些「动作旁白」词才算噪音。以前按长度无差别删，把
# 「Python（3.11）」「我（真的）觉得（不是）这样」这种正常括号也削了
_BRACKET_WORDS = (
    "笑", "叹", "摊手", "耸肩", "扶额", "白眼", "无语", "沉默", "思考", "点头",
    "摇头", "皱眉", "撇嘴", "瞪", "咳", "摸头", "捂脸", "鼓掌", "比心", "眨眼",
    "歪头", "挠头", "尴尬", "汗", "佩服", "愣", "顿", "抱", "哭", "气",
)
_MEME_LEFTOVER = re.compile(r"[\[【［]\s*MEME\s*[:：][^\]】］\n]*[\]】］]?", re.IGNORECASE)

# 出口兜底：模型手滑写出来的表情标签五花八门——[MEME]、[MEME 嘲讽]、[MEME_嘲讽]、
# [色:嘲讽]、(MEME:嘲讽)、[开心]、以及嵌套括号留下的 ] / []。上游正则永远追不完，
# 所以在最后一道清洗里按「括号 + 内容像情绪标签」整段削掉。
_MOOD_WORDS = "卖萌|得意|嘲讽|震惊|愤怒|委屈|无奈|干饭|开心|高兴|难过|生气|色|汗|笑|害羞|期待"
_NESTED_TAG = re.compile(r"[\[【［][^\[\]【】［］\n]{0,16}[\[【［][^\[\]【】［］\n]{0,16}[\]】］][\]】］]")
_LOOSE_TAG = re.compile(
    r"[\[【［(（]\s*(?:(?:MEME|MEMO|EMOJI|EMOTION|EMOTE)[\s:：_\-]*)?"
    r"(?:%s)(?:[\s:：_\-]*(?:%s)?)?\s*[\]】］)）]" % (_MOOD_WORDS, _MOOD_WORDS),
    re.IGNORECASE,
)
_BARE_TAG = re.compile(r"[\[【［(（]\s*(?:MEME|MEMO|EMOJI|EMOTION|EMOTE)\s*[\]】］)）]", re.IGNORECASE)
_EMPTY_BRACKET = re.compile(r"[\[【［]\s*[\]】］]")


def _strip_meme_junk(text: str) -> str:
    """把各种写歪的表情标签削干净，不留 ] / [] 这种残渣。

    只认「括号里只有情绪词或 MEME 前缀」的，正常括号（Python（3.11）、（笑）之外的
    技术括号）不会被误伤：括号里还有别的字就不匹配。
    """
    t = text or ""
    for _ in range(3):   # 嵌套的可能要削好几轮
        before = t
        t = _NESTED_TAG.sub("", t)
        t = _LOOSE_TAG.sub("", t)
        t = _BARE_TAG.sub("", t)
        t = _EMPTY_BRACKET.sub("", t)
        if t == before:
            break
    # 削完标签留下的孤立右括号：「就这？]」这种；整条只剩括号的也一并清掉
    t = re.sub(r"(?<=[？?！!。.…~～])\s*[\]】］]+", "", t)
    if not re.sub(r"[\s\[\]【】［］()（）]", "", t):
        return ""
    return t


def _drop_english_meta(text: str) -> str:
    """删掉模型漏出来的英文思考行。

    中文群里几乎不会有整行英文；出现「Wait — I shouldn't use the tool…」这种，
    是模型在自言自语，发出去就是事故。按「ASCII 字母占比 > 60% 且够长」判断，
    比维护一串 meta 词表稳。
    """
    kept = []
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) >= 12:
            has_cjk = any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" for ch in s)
            letters = sum(1 for ch in s if ch.isascii() and ch.isalpha())
            # 只要句子里有中文就是人话（「这个 bug 是 NullPointerException 导致的」），
            # 只有整行纯英文才判成模型自言自语
            if not has_cjk and letters / len(s) > 0.6:
                continue
        kept.append(line)
    return "\n".join(kept)


def _strip_bracket_actions(text: str) -> str:
    """只削动作旁白括号，正常括号原样留着。"""
    def _repl(m):
        inner = m.group(1)
        if len(inner) <= 6 and any(w in inner for w in _BRACKET_WORDS):
            return ""
        return m.group(0)
    return _BRACKET_ACTION.sub(_repl, text)


def sanitize_reply(text: str) -> str:
    """出口清洗：去掉 markdown 残余、括号动作、残留的表情标签。

    模型偶尔会写 **粗体**、`（笑）`、`[MEME:得意]`，原项目在这一步统一削掉，
    这里保持同样的处理顺序。
    """
    t = text or ""
    # 先削嵌套/变体再削标准标签：反过来的话 [MEME:[嘲讽]] 会被削成孤零零的 ]
    t = _strip_meme_junk(t)
    t = _MEME_LEFTOVER.sub("", t)
    t = _FAKE_TOOL_TAG.sub("", t)
    t = _drop_english_meta(t)
    if not t.strip():
        # 整条都是英文独白：这种确实一个字的不能发，直接判空
        return ""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    # 标签嵌在粗体里时（**[MEME:得意]**），剥完标签会剩下孤零零的 ****，
    # 先按粗体替换一次之后还剩连续星号就整段删掉
    t = re.sub(r"\*{2,}", "", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = _strip_bracket_actions(t)
    # 群聊里不接受 @ 别人：模型学到的群表达样例里没有 @，但它会自作聪明加一个
    # 「@张三 你说的对」，在群里就是强行点名，很烦。行首、行尾、整句的 @ 一起削
    t = _AT_MENTION.sub("", t)
    t = _AT_META_SENTENCE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    # 连续换行折掉：分条交给 splitter，出口不留空行
    t = re.sub(r"\n{2,}", "\n", t)
    # 削掉句子后留下的孤立标点：「我不是要艾特你，别误会」会剩「，别误会」
    t = re.sub(r"^[，,、。；;：:\s]+", "", t)
    out = t.strip()
    if out:
        return out
    # 削到一字不剩说明规则打偏了，不是模型的问题：退回「只剥标签」的版本。
    # 之前这里返回空串，main 又拿未清洗原文兜底，@ 和英文独白反而漏了出去
    fallback = _FAKE_TOOL_TAG.sub("", _MEME_LEFTOVER.sub("", text or ""))
    fallback = re.sub(r"\*{2,}", "", fallback).strip()
    # 只剩星号/标点等于什么都没有，别把 **** 发进群
    return fallback if re.search(r"\w", fallback) else ""
