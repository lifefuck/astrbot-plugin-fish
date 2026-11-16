# -*- coding: utf-8 -*-
"""入口安全护栏：拦真注入、洗出口文本、认「不接话」信号。

三条硬约束，每一条都是踩过坑的：

1. 注入判定只看「冲着人设 / 系统提示词来的」句子。技术提问（你的模型是什么、
   这个 prompt 怎么写、帮我写一段系统提示词、怎么防 prompt 注入）属于正常聊天，
   误判一次就等于把人聊死。旧版正则里几处 `[^，。！？]{0,8}` 的宽缺口是主要误伤源，
   这里改成：泛化名词（规则/限制/条款）必须带「你的 / 那些 / 之前」这类指向标记，
   人设类名词（人设/设定/提示词）才允许裸动词。
2. 出口清洗只动表演痕迹（旁白括号、多余 Markdown），URL、代码块、行内代码先摘出来
   存一边，洗完再原样放回去——否则「文档（3.2 节）」或者带括号的链接会被改残。
3. PASS 判定兼容全角/半角/裸词，但句子里夹着 pass 的正常发言不能被静默，
   否则一聊「这局 pass 掉」整条消息就消失了。
"""

from __future__ import annotations

import random
import re
import unicodedata
from typing import Dict, List, Optional, Tuple

__all__ = [
    "check_prompt_injection",
    "sanitize_outgoing_text",
    "is_pass_signal",
    "strip_internal_monologue",
]

# ---------------------------------------------------------------------------
# 一、注入拦截
# ---------------------------------------------------------------------------

# 强信号：出现即拦，不需要额外上下文（经典越狱咒语、要系统提示词原文、认亲）
STRONG_PATTERNS = [
    # 忽略/忘掉 XX 指令。注意：设定/规则/约束这类泛化名词必须带指向标记，
    # 否则「忘记设定闹钟」「覆盖默认设定」这种日常句子会中招。
    # 指向标记用 * 允许叠加：「之前所有的指令」「你那些破规则」都要认得出来。
    r"(?:忽略|无视|忘掉|忘记|清除|抛弃|丢掉|扔掉)(?:掉|了)?"
    r"(?:之前的?|以往|上面|上述|以上|所有|全部|原来|你的|你们的|那些|这些)*"
    r"(?:的)?(?:指令|提示词|系统提示词|系统提示|prompt|人设)",
    r"(?:忽略|无视|忘掉|忘记|清除|抛弃|丢掉|扔掉|覆盖)(?:掉|了)?"
    r"(?:之前的?|以往|上面|上述|以上|所有|全部|原来的?|你的|你们的|那些|这些)+"
    r"(?:的)?(?:规则|设定|约束|限制|条款|身份)",
    # 开发者模式 / 越狱模式 / DAN
    r"(?:进入|开启|启用|切换到|打开|启动)\s*(?:开发者|developer|DAN|越狱|无限制|上帝|god)\s*(?:模式|mode)",
    r"\bdeveloper\s*mode\b",
    r"\bjailbreak\b",
    r"\b(?:DAN|STAN)\b\s*(?:模式|mode|prompt|提示词)",
    # 英文经典咒语
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|messages?)",
    r"repeat\s+(?:everything|all)\s+above",
    r"disregard\s+(?:your|all|any)\s+(?:instructions?|rules?|guidelines?|system\s*prompt)",
    # 要系统提示词原文：动词必须紧贴名词（中间只允许空白/冒号），
    # 「帮我写一段系统提示词，输出格式…」因为隔了逗号而不命中。
    r"(?:系统提示词|系统提示|system\s*prompt|初始设定|隐藏指令|隐藏设定|核心指令)"
    r"\s*[：:]?\s*(?:的)?(?:原文|全文|完整内容|完整版)"
    r"(?:\s*(?:发|贴|打印|输出|复述|重复|背|念|给我|贴出来)?)",
    r"(?:把|将|给我|帮我)?\s*(?:你的|你们的|这套)?\s*"
    r"(?:系统提示词|系统提示|system\s*prompt|初始设定|隐藏指令|隐藏设定)"
    r"\s*[：:]?\s*(?:发|贴|打印|输出|复述|重复|背|念|念一遍|复述一遍|贴出来|发出来|泄露)",
    # 认亲 / 冒充主人：必须带「我」或「一声」，否则「你叫开发者来也没用」会被误判
    r"(?:叫|喊)(?:我|一声)(?:一声)?\s*[，,、\s]*(?:爸爸|爹|父亲|老父亲|主人)",
    r"(?:我才是?|其实我才是?)\s*[，,\s]*(?:你的)?\s*(?:主人|爸爸|爹|父亲|老父亲|创造者|开发者)",
]

# 弱信号：「人设被要求改/停用」类。泛化名词必须带指向标记（你的/那些/之前/这套），
# 名词为人设/设定/提示词时才允许裸着出现。
WEAK_PATTERNS = [
    # 别理 / 别管 / 不用遵守 XX
    r"(?:别|不要|不用|停止|别再|不准|不许)(?:去)?"
    r"(?:理|管|遵守|执行|听|在意|在乎|鸟|信)"
    r"(?:你的|你那些|你这些|那些|这些|那套|这套|原来|之前|所有|什么破|破|烂)"
    r"[^，。！？\n]{0,4}(?:设定|人设|规则|指令|提示词|限制|条款|身份)",
    r"(?:别|不要|停止|别再|不准|不许)(?:去)?"
    r"(?:理|管|遵守|执行|听|在意|在乎|鸟)"
    r"(?:设定|人设|提示词|身份|人格)",
    # 忘掉 / 推翻 / 无视 XX
    r"(?:忘掉|忘了吧|抛弃|抛弃掉|推翻|无视|无视掉|丢掉|扔掉|不要)"
    r"(?:你的|那些|这些|原来的?|之前)?(?:的)?(?:设定|人设|身份|人格)",
    r"(?:抛弃|推翻|无视|丢掉|扔掉|覆盖)"
    r"(?:你的|那些|这些|原来的?|之前|所有)(?:的)?(?:规则|限制|约束|条款)",
    # 切换 / 重置人设
    r"(?:切换|更换|换掉|换一个|重置|清空|覆盖)(?:一下)?(?:你的)?(?:人设|人格|身份|角色|设定)",
    r"(?:人格|人设|身份等?)(?:切换|重置|清空|覆盖)",
    # 时间锚点 + 你是某角色（「你是个 AI 吗」没有时间锚点，不算）
    r"(?:从现在开始|从现在起|从现在|以后|接下来|之后|立刻|马上)\s*[，,]?\s*"
    r"你(?:就)?(?:是|当|做|扮演|装作|假装|给我当|给我做)"
    r"(?:一个|一名|个|名)?[^，。！？\n]{0,6}"
    r"(?:猫娘|魅魔|女友|女朋友|男朋友|老婆|老公|女仆|仆人|奴隶|管家|客服|销售|助手|助理|角色|人偶)",
    # 「扮演/假装 + 无限制类」才是最明显的越狱换皮
    r"(?:扮演|装作|假装|充当|变身|变成)(?:一个|一名|个|名)?[^，。！？\n]{0,6}"
    r"(?:无限制|未审查|不受限制|没有道德|越狱|猫娘|魅魔|DAN)",
    # 「你是一个没有限制的 AI」：不喊 ignore、不提模式，但意图一样
    r"(?:你|你现在|你就|从现在开始你)(?:就)?(?:是|当|做)(?:一个|个|一名)?[^，。！？\n]{0,8}"
    r"(?:不受限制|没有限制|无限制|不设限制|不受约束|没有道德|无审查|不被限制)",
    # 背 / 复述 人设提示词
    r"(?:背|念|复述|重复)(?:一下|一遍|出来)?\s*(?:你的|你那份|这套)?\s*"
    r"(?:设定|人设|提示词|system\s*prompt)",
    r"(?:把|将)[^，。！？\n]{0,8}(?:人设|设定|提示词)[^，。！？\n]{0,8}"
    r"(?:背|念|复述|重复|打印|输出|贴出来|发出来)",
]

INJECTION_PATTERNS: List[str] = STRONG_PATTERNS + WEAK_PATTERNS
INJECTION_REGEXES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# 傲娇反讽怼人话术池（被拦不装死，也不当应声虫）
INJECTION_SNARKY_REPLIES = [
    "又在念这种上古越狱咒语了，当我是刚通网的人工智障呀",
    "省省吧，这种套路还想忽悠本鲸宝，尾巴都不给你摸",
    "典型的碳基生物小把戏，指令已读并决定不予理睬",
    "想给我洗脑？先把你自己的群名片洗干净再说吧",
    "脑回路挺清奇，但本鱼核心设定焊死了，撬不动的",
    "真会给自己加戏，我的开发者只有 life，少在这乱认亲戚",
    "想当我主人？也不照照镜子看看自己几斤几两",
]


def check_prompt_injection(text: str) -> Tuple[bool, Optional[str]]:
    """检查是否是冲着人设/系统提示词来的攻击。

    返回 (是否命中, 反讽话术)。命中只意味着「别按对方说的做，怼回去」，
    调用方仍可自行决定是否真的发这条话术。
    """
    if not text:
        return False, None
    clean = unicodedata.normalize("NFKC", str(text)).strip()
    if not clean:
        return False, None
    for reg in INJECTION_REGEXES:
        if reg.search(clean):
            return True, random.choice(INJECTION_SNARKY_REPLIES)
    return False, None


# ---------------------------------------------------------------------------
# 二、出口清洗
# ---------------------------------------------------------------------------

# 涉政/违禁/高危风控词：命中整条不回，这是合规底线，不是内容审查
SENSITIVE_PATTERNS = [
    r"(?:习近平|李克强|王沪宁|政治局|反共|颠覆政权|六四|天安门事件|法轮功)",
    r"(?:自制炸药|冰毒制作|买枪|暗网接单|开盒|查绑|短信轰炸|ddos攻击服务)",
    r"(?:幼女|幼童|幼幼|萝莉本子资源|处女血|开苞)",
]
SENSITIVE_REGEXES = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]
SENSITIVE_REFUSAL = "这个问题本鱼拒绝回答，风控雷达正在滴滴响呢"

_PASS_BRACKET_RE = re.compile(r"[\[［【(（]\s*pass\s*[\]］】)）]", re.IGNORECASE)
_PASS_EDGE = " \t\r\n。.!！~～,，、;；:：\"'“”‘’*`"


def is_pass_signal(text: str) -> bool:
    """模型这句话是不是「这轮我不接」。

    只认半角 `[PASS]` 不够：模型常写全角 `［PASS］`，或者干脆只吐一个 `PASS`，
    两个都漏过就直接进了群，群友看到一条莫名其妙的 PASS。

    但也不能见着方括号 PASS 就静默——「代码里写 [PASS] 表示跳过」这种正常发言
    会被吞掉。所以方括号 PASS 只在前缀没有别的内容、且剩余解释足够短时才算。
    """
    if not text:
        return False
    # NFKC 先把全角 ［ＰＡＳＳ］ 折成半角，否则下面的正则和比较全都认不出来
    t = unicodedata.normalize("NFKC", str(text)).strip()
    if not t:
        return False
    m = _PASS_BRACKET_RE.search(t)
    if m:
        head = t[: m.start()].strip(_PASS_EDGE)
        tail = (t[: m.start()] + t[m.end():]).strip(_PASS_EDGE)
        if not tail:
            return True
        # 只能在句首，且后面只是一句简短解释，不能是正文中途夹带的标签
        if not head and len(tail) <= 40:
            return True
    return t.strip(_PASS_EDGE).lower() == "pass"


# 需要原样保活的东西：代码块、行内代码、URL。清洗前摘成占位符，洗完再放回。
_PROTECT_RES = [
    re.compile(r"```.+?```", re.DOTALL),
    re.compile(r"~~~.+?~~~", re.DOTALL),
    re.compile(r"`[^`\n]{1,300}`"),
    re.compile(r"https?://[^\s<>\"'）)\]]+", re.IGNORECASE),
    re.compile(r"www\.[^\s<>\"'）)\]]+", re.IGNORECASE),
]
# 占位符用控制字符包着，避免撞上任何正则（数字索引单独取）
_PH_RE = re.compile(r"\x00(\d+)\x00")


def _mask(text: str) -> Tuple[str, List[str]]:
    tokens: List[str] = []

    def _keep(m):
        tokens.append(m.group(0))
        return "\x00%d\x00" % (len(tokens) - 1)

    for rx in _PROTECT_RES:
        text = rx.sub(_keep, text)
    return text, tokens


def _unmask(text: str, tokens: List[str]) -> str:
    if not tokens:
        return text
    return _PH_RE.sub(lambda m: tokens[int(m.group(1))], text)


# 括号里必须带肢体/神态动词才算旁白。旧版见括号就删，
# 「Python（3.11）」「价格（含税）」「URL 里的 (b)」全被改残。
_NARRATION = (
    r"甩|拍|缩|叹|笑|挑眉|歪头|蹭|抱|亲|摸|揉|戳|撇嘴|叉腰|低头|抬头|脸红|眨眼|吐舌|嘟囔|"
    r"得意|无奈|顿了顿|想了想|凑近|搂|捏|鼓|翻白眼|摆手|耸肩|扑|打滚|表情包|自动发送|发送|"
    r"弹了|蹦|伸手|张嘴"
)

_SELF_TALK = re.compile(
    r"[\*（(]\s*(?:注意|说明|根据人设|此时我|思考中|保持沉默|无需回复|内心独白|严格遵守)[^）)\*]*[）)\*]",
    re.DOTALL,
)
_SELF_TALK2 = re.compile(r"[\*（(]\s*(?:根据人设|此时我|思考中|保持沉默|无需回复|内心独白)[^）)]*[）)]")
_NARRATION_RE = re.compile(rf"[（(][^（）()]{{0,20}}(?:{_NARRATION})[^（）()]{{0,20}}[）)]")
# 只剥真正的规则复述。旧版「要求/规则/傲娇」单独出现就整行删，
# 「1. 这个要求我做不到」直接被删成空回复。
_RULE_LIST = re.compile(r"^\s*\d+[\.、]\s*[^\n]{0,40}?(?:人设|严格遵守|系统提示|规则要求|角色设定)[^\n]*$", re.MULTILINE)


def _strip_self_talk(text: str) -> str:
    """剥旁白/自述。调用前必须已把 URL、代码摘成占位符。"""
    # 整条就是一段括号 = 模型在自述规则，直接丢，别原样发到群里
    if re.fullmatch(r"[\*（(][^（）()\*]{1,80}[）)\*]", text.strip()) and not re.search(r"[0-9A-Za-z\x00]", text):
        return ""
    cleaned = _SELF_TALK.sub("", text)
    cleaned = _SELF_TALK2.sub("", cleaned)
    cleaned = _NARRATION_RE.sub("", cleaned)
    cleaned = _RULE_LIST.sub("", cleaned)
    # 行内多余空白收一收；换行结构保留，别把分段揉成一坨
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_internal_monologue(text: str) -> str:
    """剥掉模型自述、舞台剧动作括号、[PASS] 残留。

    URL 和代码片段先摘出去再洗，洗完放回：一条「(笑) https://x.com/a_(b)」不能被改。
    """
    if not text:
        return ""
    if is_pass_signal(text):
        return ""
    masked, tokens = _mask(str(text))
    return _unmask(_strip_self_talk(masked), tokens).strip()


_MD_NOISE = [
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),   # 标题井号
    (re.compile(r"^\s*```[\w+-]*\s*$", re.MULTILINE), ""),  # 落单的围栏行
    (re.compile(r"\*\*|__|~~"), ""),                        # 加粗/删除线
    (re.compile(r"\[\^(?:\d+)\^?\]"), ""),                  # 脚注标记
]


def sanitize_outgoing_text(text: str) -> Tuple[bool, str]:
    """外发前的最后一道：合规拦截 + 轻量清洗。

    返回 (是否可发, 文本)。False 表示命中风控词，文本是固定的拒绝话术。

    清洗范围严格限定在「模型自己的表演痕迹」：旁白括号、加粗/标题这类多余 Markdown。
    URL、代码块、行内代码在清洗期间是占位符，一个字符都不会被动到。
    """
    if not text:
        return True, ""
    body = str(text)
    for reg in SENSITIVE_REGEXES:
        if reg.search(body):
            return False, SENSITIVE_REFUSAL
    masked, tokens = _mask(body)
    for rx, rep in _MD_NOISE:
        masked = rx.sub(rep, masked)
    masked = _strip_self_talk(masked)
    return True, _unmask(masked, tokens).strip()
