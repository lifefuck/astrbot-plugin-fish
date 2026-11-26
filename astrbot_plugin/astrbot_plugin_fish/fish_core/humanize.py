# -*- coding: utf-8 -*-
"""人味化后处理：把模型腔调压成真人聊天。

规则不是拍脑袋写的，两段来源：
- acceleratorcxy/astrbot_plugin_dafeiyu_core 的 humanizer_feature.py：
  `_clean_text`（think 块 / Markdown / 括号旁白 / AI 腔整句）、
  `_split_segments` + `_chunk_text`（拆短气泡）、STYLE_REMINDER。
- WiseXin/MaiBot-Character-Disguise 的 cleaner.py：
  `strip_markdown`、`clean_ai_tropes`、`strip_periods`、`strip_xml_tags`、
  静默标签识别。

改动过的部分只有两处，其余按原样搬：
1. 阈值跟着 fish 走（单条 45 字、最多 2 条），不套用大肥鱼的 30 字/3 条；
2. 多了一条「不是……而是……」的整句删除（fish 最典型的八股句式），
   只删整句、不改写，跟 `_strip_ai_sentences` 同一套切句逻辑。

为什么做成确定性规则而不是写进提示词：提示词里写「不要用不是…而是…」等于
把句式又喂了一遍（粉红大象），模型反而更容易照着写。出口削掉最省事。
"""

from __future__ import annotations

import json
import os
import re

# ---------------------------------------------------------------- 常量

# 大肥鱼原句，一字未改。挂在系统提示词末尾，比在正文里堆十条禁令有效
STYLE_REMINDER = (
    "\n\n【聊天风格】像真人用 QQ 聊天：一次回一两句话就行，不要用 markdown、列表、标题，"
    "不要写括号动作旁白或内心戏，不要自称 AI/助手/模型，不要输出思考过程。"
)

SENT_END = "。！？!?~…"
# 按句末标点切句，保留标点本身（原实现同一套）
SPLIT_RE = re.compile(r"[^。！？!?~…\n]*[。！？!?~…]+|[^。！？!?~…\n]+")
SEG_BREAKS = set("，,。.！!？?；;：:、~～…）)」』】 \t\u3000")
PAREN_RE = re.compile(r"[（(]([^（）()]{0,120})[）)]")
ACTION_WORDS = (
    "叹", "笑", "摸", "歪", "看", "盯", "眨", "甩", "伸", "摊", "挠", "抱", "捏", "愣",
    "沉默", "无奈", "小声", "嘀咕", "翻白眼", "挑眉", "耸肩", "低头", "抬头", "脸红",
    "嘴角", "尾巴", "耳朵", "鲸", "鱼", "挥舞", "拍", "戳", "咬", "嚼", "摇", "蹦",
    "转圈", "趴", "躺", "跪", "捂", "扶额", "目光", "眼神", "动作", "表情", "心里",
    "内心", "心想", "想着", "随后", "于是", "然后", "顿了顿", "轻笑", "哼了",
)
AI_SENT_RE = re.compile(
    "(作为(一个)?(?:AI|人工智能|语言模型|助手|机器人|虚拟助手)"
    "|我是(一个)?(?:AI|人工智能|语言模型|AI助手)"
    "|有什么(?:可以|能)帮|需要我(?:帮忙|帮你|帮)|能帮到你|希望对你有帮助"
    "|很高兴(?:能)?为你|还有什么(?:可以|需要)|您可以(?:随时)?(?:告诉|询问|联系))",
    re.I,
)
GREETING_PREFIX_RE = re.compile(r"^(?:好的|好滴|好呀|收到|收到啦|明白了|了解|没问题|当然可以)[，,。！!～~]\s*")

# fish 自己的八股：转折句式。只删整句，不做改写——
# 改写会把「不是不想去，是不敢去」这种正常句子也拧坏，删掉反而干净。
# 降档（A 组）：只在**句首**命中才算八股，且整条回复最多删 1 句。
# 原来是一句里出现「不是…而是」就整句删，结果「这事不是我的问题，而是他先动的」
# 这种正常反驳也被削掉，还会连着删两三句，读起来像漏字。
CONTRAST_RE = re.compile(r"^\s*(?:其实|所以|说到底)?\s*不是[^。！？!?~…\n]{1,60}?[，,]?\s*而是")
CONTRAST_MAX_HITS = 1
# 盖章收尾：整句就是一句自我确认，删掉不影响上下文
STAMP_RE = re.compile(
    r"^(?:这就够了|这就行了|我记下了|我记住了|记住了|就这样吧|够了够了|懂了吧|明白了吧)"
    r"[。！？!?~…～]*$"
)

# MaiBot 的静默标签，外加 fish 自己的 [NO_REPLY]
SILENT_PATTERNS = (
    re.compile(r"<\s*silent\s*/?\s*>", re.IGNORECASE),
    re.compile(r"<\s*message\s*>\s*<\s*/\s*message\s*>", re.IGNORECASE),
    re.compile(r"^\[silent\]$", re.IGNORECASE),
    re.compile(r"^\(silent\)$", re.IGNORECASE),
    re.compile(r"^\[NO_REPLY\]$", re.IGNORECASE),
    re.compile(r"^【NO_REPLY】$", re.IGNORECASE),
    re.compile(r"^（沉默）$"),
    re.compile(r"^（潜水）$"),
)

AI_LEADING_PATTERNS = (
    re.compile(r"^(好的|好的呢|明白啦|了解啦|收到)[，,！!~ ]*", re.IGNORECASE),
    re.compile(r"^(作为|作为一个)[^，,。！？\n]{1,20}[，,]", re.IGNORECASE),
    re.compile(r"^(很高兴为你解答|关于这个问题|我来给你(解答|分析|说明)一下)[，,：:\s]*", re.IGNORECASE),
)

AI_TRAILING_PATTERNS = (
    re.compile(
        r"[，, ]*(希望(能|可以|对你)?(有(所)?)?帮(助|到)(你)?|有什么问题随时问我"
        r"|祝你游戏愉快|祝你玩得开心|祝你生活愉快)[！!。.~]*$",
        re.IGNORECASE,
    ),
)


# ---------------------------------------------------------------- 清洗

_CJK_PAUSE_RE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u00a0\u3000]+(?=[\u4e00-\u9fff])")


def pause_to_comma(text: str) -> str:
    """汉字之间的空格换成逗号。

    fish 的 typeset 会**删掉**汉字之间的空格（它当那是模型手抖的噪音），
    但现在模型学会了用空格断句——"十点也算早的了 赶紧躺平"，删完就是一坨
    "十点也算早的了赶紧躺平"。这里先把空格转成逗号，typeset 就不会吃掉了；
    真人打字也确实是逗号断句，比空格更符合 QQ 的气泡观感。
    中英文之间的空格不动（显卡 4090 不能被改成 显卡，4090）。
    """
    t = str(text or "")
    if not t:
        return t
    t = _CJK_PAUSE_RE.sub("，", t)
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"(^|[。！？!?…\n])，", r"\1", t)
    t = re.sub(r"，(?=[。！？!?…\n]|$)", "", t)
    return t


def is_silent(text: str) -> bool:
    """整条回复就是「这轮不说话」。空串不算。"""
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    return any(p.search(s) for p in SILENT_PATTERNS)


def strip_xml_tags(text: str) -> str:
    """剥掉残留的 <message>/<quote>/<think> 这类伪标签，别让它们裸奔进群。"""
    t = str(text or "")
    t = re.sub(
        r"<\s*/?\s*(?:message|quote|output|thought|think|action)(?:\s+[^>]*)?>",
        "", t, flags=re.IGNORECASE,
    )
    return t.strip()


def strip_markdown(text: str) -> str:
    """Markdown → 纯文本。跟 composer 那套重复的部分保持幂等，不会互相打脸。"""
    t = str(text or "")
    t = re.sub(r"\*\*\*(.*?)\*\*\*", r"\1", t)
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"\*(.*?)\*", r"\1", t)
    t = re.sub(r"~~(.*?)~~", r"\1", t)
    t = re.sub(r"```[\w]*\n(.*?)```", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"#+\s*", "", t)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*>\s*", "", t, flags=re.MULTILINE)
    return t


def clean_ai_tropes(text: str) -> str:
    """削客服腔的首尾：「好的，」「希望能帮到你」。"""
    t = strip_xml_tags(str(text or "")).strip()
    for pat in AI_LEADING_PATTERNS:
        t = pat.sub("", t).strip()
    for pat in AI_TRAILING_PATTERNS:
        t = pat.sub("", t).strip()
    return t


def strip_periods(text: str) -> str:
    """去句号化：句末句号剥掉，句中句号换成逗号（省略号保留）。

    手机上没人一句话敲三个句号；留着就是「文档腔」。
    MaiBot 原版把句中句号换成空格，fish 这边不行——typeset 会把中文之间的空格
    当成模型乱加的空格吃掉，结果是「下班到家」糊成一坨。改成逗号，读起来还是人话。
    """
    t = str(text or "").strip()
    t = re.sub(r"。+$", "", t).strip()
    if not t.endswith("..."):
        t = re.sub(r"(?<!\.)\.{1,2}$", "", t).strip()
    t = re.sub(r"。[ \t]*", "，", t)
    t = re.sub(r"[，,]{2,}", "，", t)
    t = re.sub(r"[，,]([！？!?~～])", r"\1", t)
    t = re.sub(r" {2,}", " ", t)
    return t.strip(" ，,")


def truncate_smart(text: str, max_chars: int) -> str:
    """按长度截断，优先断在标点上，别切半句。"""
    t = str(text or "")
    if len(t) <= max_chars:
        return t
    candidate = t[:max_chars]
    last_break = -1
    for mark in "，, ！!？?~～ \n；;、":
        pos = candidate.rfind(mark)
        if pos > last_break:
            last_break = pos
    truncated = candidate[:last_break] if last_break >= int(max_chars * 0.55) else candidate
    truncated = re.sub(r"[，,、；; \-]+$", "", truncated).strip()
    return truncated


def strip_parens(text: str) -> str:
    """括号旁白：动作/表情/图片说明删，正常的括注留着。"""

    def _repl(m):
        inner = (m.group(1) or "").strip()
        if not inner:
            return ""
        if re.search(r"(?:图片|表情|发送|image|img)", inner, re.I):
            return ""
        if any(w in inner for w in ACTION_WORDS):
            return ""
        before = text[:m.start()].rstrip()
        at_sentence_start = not before or before[-1] in SENT_END + "\n"
        if len(inner) <= 12 and at_sentence_start:
            return ""
        return m.group(0)

    try:
        return PAREN_RE.sub(_repl, text)
    except Exception:
        return text


def strip_bad_sentences(text: str) -> str:
    """整句删除内容型八股：AI 腔、转折句式、盖章收尾。

    只删命中句子，不动别的句子，也不改写——改写会改出模型没说过的话。
    """
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？!?~…\n])", str(text))
    kept = []
    contrast_used = 0
    for p in parts:
        core = p.strip()
        if not core:
            continue
        if AI_SENT_RE.search(core):
            continue
        # 降档后仍限量：整条只削一句「不是A而是B」，多删会让回复残缺
        if contrast_used < CONTRAST_MAX_HITS and CONTRAST_RE.search(core):
            contrast_used += 1
            continue
        if STAMP_RE.match(core):
            continue
        kept.append(p)
    out = "".join(kept).strip()
    return out


def clean_text(text: str) -> str:
    """出口主清洗：think 块、Markdown、括号旁白、AI 腔、八股句、残留标签。"""
    if not text:
        return ""
    t = str(text).replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"^\s*🤔\s*思考[:：][\s\S]*?────\s*", "", t)
    t = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", t, flags=re.I)
    t = re.sub(r"</?think(?:ing)?>", "", t, flags=re.I)
    t = t.replace("────", "")
    t = strip_markdown(t)
    t = strip_parens(t)
    t = strip_bad_sentences(t)
    t = clean_ai_tropes(t)
    t = pause_to_comma(t)
    t = re.sub(r"[\[【](?:图片|表情包?|语音|视频|文件|动画表情|image|img|sticker)[\]】]", "", t, flags=re.I)
    t = re.sub(r"&&[^&\n]{1,20}&&", "", t)
    t = re.sub(r"(?<![A-Za-z0-9]):[a-zA-Z0-9_+-]{1,20}:(?![A-Za-z0-9])", "", t)
    t = re.sub(r"[ \t\u3000]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    early = GREETING_PREFIX_RE.sub("", t, count=1)
    if early.strip():
        t = early
    return t.strip()


def clean_for_history(text: str, limit: int = 300) -> str:
    """写进上下文之前过一遍：清洗 + 限长。

    上下文里要是留着模型自己的八股原话，下一轮它就会照着学——
    这是「越聊越像机器人」最直接的来源。
    """
    t = clean_text(text)
    if limit and len(t) > limit:
        t = truncate_smart(t, limit)
    return t.strip()


# ---------------------------------------------------------------- 拆条

def chunk_text(text: str, max_chars: int) -> list[str]:
    """单段太长时按字数切，尽量断在标点/空格后面。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    out, rest = [], text
    floor = max(1, max_chars // 2)
    while len(rest) > max_chars:
        cut = 0
        for idx in range(max_chars - 1, floor - 1, -1):
            if rest[idx] in SEG_BREAKS:
                cut = idx + 1
                break
        if cut <= 0:
            cut = max_chars
        head = rest[:cut].strip()
        if head:
            out.append(head)
        rest = rest[cut:].lstrip()
    if rest.strip():
        out.append(rest.strip())
    return out


def split_segments(text: str, max_seg: int = 2, max_chars: int = 45) -> list[str]:
    """按句末标点断句，再把超长段切短，最后均分成 max_seg 条。

    阈值用 fish 自己的（45 字 / 2 条），不套大肥鱼的 30 字 / 3 条：
    群里连发三条已经是刷屏，两条刚好。
    """
    max_seg = max(1, int(max_seg or 1))
    parts = [p.strip() for p in SPLIT_RE.findall(text or "") if p and p.strip()]
    if not parts:
        return [text.strip()] if (text or "").strip() else []
    pieces: list[str] = []
    for part in parts:
        pieces.extend(chunk_text(part, int(max_chars or 0)))
    pieces = [p for p in pieces if p and p.strip()]
    if not pieces:
        return [text.strip()] if (text or "").strip() else []
    if len(pieces) <= max_seg:
        return pieces
    total = sum(len(p) for p in pieces)
    target = max(1, -(-total // max_seg))
    groups, buf = [], ""
    for piece in pieces:
        if buf and len(buf) + len(piece) > target:
            groups.append(buf)
            buf = piece
        else:
            buf += piece
    if buf:
        groups.append(buf)
    while len(groups) > max_seg:
        tail = groups.pop()
        if not groups:
            groups = [tail]
            break
        groups[-1] = groups[-1] + tail
    return [g for g in groups if g]

# ================================================================ 鱼八骨词表
# 来源：salttove/-remove-rigid-express-of-QQ-ai-chatbot 的 lexicon.json + filter_core.py。
# 按原仓库的分层原样搬，只做两处 fish 适配：
#   1. 命中整条删空时原仓库回一句 fallback（「嗯，我在听。」），fish 不这么干——
#      fallback 本身又是一句固定话，等于把八股换成另一种八股；fish 这边返回空，
#      由 respond 走「这轮不说话」，沉默比假话强。
#   2. not_but（不是…而是…）这一层不在这里开，交给 strip_bad_sentences 的
#      降档版（只禁段首、全文限 1 次）——两处都删会重复计数，也压不住次数。

# 生成前禁忌：原仓库 DEFAULT_GUARD 原句。挂提示词末尾，和 STYLE_REMINDER 同位置
GUARD = (
    "【表达禁忌】直接说事，别用鱼八股：不要只回一个字/两个字（好、行、行吧、你定、我不理）；"
    "不要认错官腔（这条我认、我记下了、我记住了）；"
    "不要先分点再总结再拔高（讲的其实是、本质上、说到底）；"
    "不要把工具状态或备选回复发出来（已记录、记进待办、以下是几个版本）；"
    "不要固定句式（不是……而是……、总的来说、值得一提的是、某种程度上）；"
    "不要给对方的发言打分，不要宣告“我在/我守着”，不要问“你认吗/行不行/你说呢”。"
    "短句，口语，别每句都收尾。"
)

HARD_PHRASES = (
    "我记下了", "我记得了", "我记住了", "这个账我记住了",
    "这个账我记下了", "你这句话我记着", "这话我记下了", "这话我收下了",
    "这话我留着", "这话我接着了", "行，我认", "认下来",
    "认得实", "我认这个", "认你这个", "你自己掂量",
    "你掂量", "问别人还是问自己", "这话是ta告诉你的，还是你自己想明白的", "这话是别人告诉你的，还是你自己想明白的",
    "什么心思都写在", "说的比谁都响", "这句话我不接", "我不接这句",
    "睡吧，我守着", "你去，我跟", "我不催你", "这句话稳",
    "这句话接得漂亮", "这句话接的漂亮", "声音没抖，但比刚才稳", "他记得这事",
    "收不回去", "比之前的都重", "比之前的都轻", "比之前的都强",
    "比刚才稳", "比刚才轻", "比刚才重", "比刚才平",
    "没接你这句", "认这个", "这条我认", "我认这条",
    "已记录", "已记下", "记进待办", "记进待办事项",
    "加入待办", "已加入待办", "备选回复", "以下是几个版本",
    "以下是几种回复", "回复选项", "如果需要我可以", "需要的话我可以",
    "讲的其实是",
)
# 词表里有 8 条是模板（"没接你的xxx"、"xx比xx重"），原仓库直接当字面量，永远匹配不到。
# 这里把 xx 当「一小段话」处理，才算真生效；其余条目逐字照搬
HARD_TEMPLATES = (
    "没接你的xxx", "没接你那句xx", "行，xxx", "xx就是认了",
    "xx比xx沉", "xx比xx重", "xx比xx稳", "xx比xx轻",
)
SOFT_PHRASES = (
    "稳", "很平", "很直", "漂亮",
    "接得好", "够劲", "收下了", "接住了",
    "收着", "认你", "我认", "我不拦",
    "我跟", "不催", "回去睡",
)
# 单字硬切：原仓库默认开，fish 默认关（见 clean_yubagu 的 short_cut 参数注释）
SHORT_CUT = (
    "好", "行", "行吧", "我不嫌",
    "你定", "我不理",
)
OPENER_REPLACE = (
    "好", "行", "行吧",
)
FICTION_PHRASES = (
    "嘴角勾起一丝弧度", "嘴角勾起一抹弧度", "嘴角微微上扬", "倒吸一口凉气",
    "眼中闪过一丝", "眼底闪过一丝", "瞳孔骤缩", "指尖蜷缩",
    "空气凝固", "空气突然安静", "时间仿佛静止", "似笑非笑",
    "挑了挑眉", "微微颔首", "若有所思", "意味深长",
    "不易察觉", "几不可闻", "若有若无", "低沉悦耳",
    "嗓音低沉", "像受惊的小兽", "如同受惊的小兽", "推了推眼镜",
    "推了推并不存在的眼镜", "投入石子", "空气冻结", "令人牙酸",
)
TRANSITION_PREFIX = (
    "首先|其次|最后|再者|此外|然而|总之|总而言之|综上所述|总的来说|值得注意的是|值得一提的是|需要指出的是|不难看出|由此可见|换句话说|简而言之|更重要的是|不可否认|毋庸置疑|众所周知|不言而喻|显而易见|事实上|客观来说|客观地讲|理性来看|说到底|归根结底|本质上|说白了|总结一下|简单总结|概括一下|某种程度上|从某种程度上|某种意义上|从某种意义上|总体而言|就目前而言|需要强调的是|不可忽视的是",
)
FILLER_SRC = "作为一个?(?:AI|人工智能|语言模型)|希望(?:这|这些)?对(?:你|您)有(?:所)?帮助|如果(?:你|您)?还有(?:其他|别的)?问题|如有(?:其他|别的)?问题|欢迎随时(?:提问|问我)|随时(?:告诉|问)我"
ERA_SRC = "(?:随着|在当今|在这个)[^。！？\\n]{0,40}?(?:时代|背景下|发展中|的发展|的进步|的演进|的趋势)(?:的(?:发展|进程|趋势|变化|演进))?"
LET_US_SRC = "让我们[^。！？\\n]{0,30}"
FILLER_RE = re.compile(rf"^(?:{FILLER_SRC})[^。！？\n]*[。！？]?$", re.I)
ERA_RE = re.compile(rf"^(?:{ERA_SRC})[，,、：:\s]*", re.I)
LET_US_RE = re.compile(rf"^(?:{LET_US_SRC})[。！？]?$", re.I)
AI_SELF_RE = re.compile(r"^(?:作为一个?(?:AI|人工智能|语言模型))[，,、：:\s]*", re.I)
TRANSITION_RE = re.compile(rf"^(?:{TRANSITION_PREFIX})[，,、：:\s]*", re.I)
def _template_re(t: str) -> re.Pattern:
    """把「没接你的xxx」这类模板转成正则：xx 当一小段话（不超过 8 字）。

    原仓库把这些模板当字面量匹配，永远命中不了——照搬等于白搬
    """
    return re.compile(re.escape(t).replace("xx", "[^，。！？!?\n]{0,8}"))


_HARD_WILD_RES = tuple(_template_re(t) for t in HARD_TEMPLATES)

_SENTENCE_RE = re.compile(r"(?<=[。！？!?…\n])")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://[^\s]+")
_PLACEHOLDER_RE = re.compile("\u0000(\\d+)\u0000")
_PUNCT_EDGE = "，,、；;：:。！？!?… \t\r\n"


def _norm_cmp(text: str) -> str:
    return re.sub(r"[\s。！？!?…，,、；;：:]+", "", text or "")


def _protect(text: str) -> tuple[str, list[str]]:
    """代码块/行内代码/链接先藏起来，别被句子过滤切坏。"""
    stash: list[str] = []

    def _repl(m):
        stash.append(m.group(0))
        return "\u0000%d\u0000" % (len(stash) - 1)

    text = _CODE_FENCE_RE.sub(_repl, text)
    text = _INLINE_CODE_RE.sub(_repl, text)
    text = _URL_RE.sub(_repl, text)
    return text, stash


def _restore(text: str, stash: list[str]) -> str:
    def _repl(m):
        i = int(m.group(1))
        return stash[i] if 0 <= i < len(stash) else ""

    return _PLACEHOLDER_RE.sub(_repl, text)


def _remove_span(sentence: str, start: int, end: int) -> str:
    """按区间删一段：首尾命中保剩下半句，夹在中间整句作废。"""
    before, after = sentence[:start], sentence[end:]
    if not before.strip():
        return after.lstrip(_PUNCT_EDGE)
    if not after.strip():
        return before.rstrip(_PUNCT_EDGE)
    return ""


def _remove_phrase(sentence: str, phrase: str) -> str:
    """删一处词：它在句首/句尾时保住剩下那半句，夹在中间就整句作废。

    中间命中不能只抠掉词本身——「我记下了你说的」抠完剩「你说的」还算句人话，
    但「这话我记下了，明天再说」抠完成了「这话，明天再说」，语义变了。
    原仓库的选择是中间命中整句删，跟着走。
    """
    idx = sentence.find(phrase)
    if idx < 0:
        return sentence
    before, after = sentence[:idx], sentence[idx + len(phrase):]
    if not before.strip():
        return after.lstrip(_PUNCT_EDGE)
    if not after.strip():
        return before.rstrip(_PUNCT_EDGE)
    return ""


_HARD_SORTED = tuple(sorted(HARD_PHRASES, key=len, reverse=True))
_SOFT_SET = {_norm_cmp(x) for x in SOFT_PHRASES}
_SHORT_CUT_SET = {_norm_cmp(x) for x in SHORT_CUT}


def clean_yubagu(text: str, short_cut: bool = False, fiction: bool = False) -> str:
    """鱼八骨清理：逐句过词表，删空就删空（不回 fallback）。

    short_cut 默认关：fish 里「好」「行」经常是一条正经回复，全删等于对 @
    它的人装死；原仓库敢开是因为它有大肥鱼那套兜底话术。要开由配置显式开。
    """
    t = str(text or "")
    if not t.strip():
        return t
    protected, stash = _protect(t)
    out: list[str] = []
    for sentence in _SENTENCE_RE.split(protected):
        if sentence == "":
            continue
        s = sentence.strip()
        if not s:
            continue
        # 开头「好，」「行吧，」这类应答词剥掉，露出来的正文才是内容
        for opener in OPENER_REPLACE:
            m = re.match(rf"^{re.escape(opener)}[，,、：:\s]+", s)
            if m:
                s = s[m.end():].strip()
                break
        if not s:
            continue
        # 说教开的头：去 AI 自称、去「随着…时代」、循环去转折词
        m = AI_SELF_RE.match(s)
        if m and m.end():
            s = s[m.end():].lstrip()
        while s:
            m = ERA_RE.match(s) or TRANSITION_RE.match(s)
            if not m or not m.end():
                break
            s = s[m.end():].lstrip()
        if not s:
            continue
        if FILLER_RE.fullmatch(s) or LET_US_RE.fullmatch(s):
            continue
        for rx in _HARD_WILD_RES:
            m = rx.search(s)
            if m:
                s = _remove_span(s, m.start(), m.end())
                break
        for phrase in _HARD_SORTED:
            if s and phrase in s:
                s = _remove_phrase(s, phrase)
                if not s.strip():
                    break
                break
        if not s.strip():
            continue
        if fiction and any(p in s for p in FICTION_PHRASES):
            continue
        n = _norm_cmp(s)
        if not n:
            continue
        if short_cut and n in _SHORT_CUT_SET:
            continue
        if n in _SOFT_SET:
            continue
        out.append(s + (sentence[len(sentence.rstrip()):] if sentence.endswith("\n") else ""))
    res = "".join(out)
    res = _restore(res, stash)
    return re.sub(r"\n{3,}", "\n\n", res).strip()


# ================================================================ 逗号拆条
# 来源：dafeiyu-qq-bot 的 plugins/dsh-human。原逻辑是「≤26 字、按逗号拆成
# 两条、0.7 概率」，和 fish 现有阈值不冲突：fish 的 splitter 是「>45 字才拆、
# 最多 2 条」，两边都是上限 2 条，所以是互补而不是打架——短句补上「一句话拆两口气」
# 这一档，长句还是走 fish 原来那套。
HUMAN_MAX_LEN = 26
HUMAN_MIN_PART = 3
HUMAN_MIN_RIGHT = 2
HUMAN_SPLIT_RATE = 0.7
_INTERJ_RE = re.compile(
    r"^(?:嗯+|哦+|噢+|啊+|欸+|诶+|哎+|唉+|呃+|额+|对+|是+|行+|好+|草+|靠+|"
    r"懂了|明白|收到|好吧|行吧|得了|算了|不是|没有|没事|真的|确实)$"
)
_SENT_END_RE = re.compile(r"(?:啊|呀|吧|呢|嘛|哦|喔|噢|啦|咯|嘞|哈|哟|唷|欸)$")
_HUMAN_SKIP_RE = re.compile(r"https?://|www\.|\[贴纸[:：]|\[CQ:|```|\n|\d[,，]\d")
_HARD_TAIL_RE = re.compile(
    r"(?:和|跟|与|或|及|把|被|让|给|对|向|从|在|是|有|想|要|会|能|就|还|也|但|而|"
    r"因为|所以|如果|虽然|不但|一边|又|为了|的话)$"
)
_SOFT_TAIL_RE = re.compile(r"(?:的|地|得)$")
_QUESTION_WORD_RE = re.compile(r"谁|什么|啥|哪|怎|多少|几|吗|呢")
_SHI_DE_RE = re.compile(r"是[^，,]*的$")
_DEPENDENT_HEAD_RE = re.compile(r"^(?:的|地|得|了|着|吗|呢|吧|啊|呀|嘛)")
_FW_SPLIT_RE = re.compile(r"[。？！~…][^。？！~…]*\S")


def left_can_stand(left: str) -> bool:
    """左半能不能单独成一条。全是结构性判据，不靠句子白名单。"""
    if not left:
        return False
    if _INTERJ_RE.match(left):
        return len(left) >= 2
    complete = bool(_SENT_END_RE.search(left) or _QUESTION_WORD_RE.search(left))
    if len(left) < (2 if complete else HUMAN_MIN_PART):
        return False
    if _HARD_TAIL_RE.search(left):
        return False
    if _SOFT_TAIL_RE.search(left) and not (
        _QUESTION_WORD_RE.search(left) or _SHI_DE_RE.search(left)
    ):
        return False
    return True


def split_at_comma(text: str) -> tuple[str, str] | None:
    """只在第一个合格的逗号处拆一次；拿不准返回 None。"""
    t = (text or "").strip()
    if not t or len(t) > HUMAN_MAX_LEN:
        return None
    if _HUMAN_SKIP_RE.search(t):
        return None
    if _FW_SPLIT_RE.search(t):
        # 。？！~… 后面还有内容，fish 的 splitter 本来就会拆，再补一刀变 3 条
        return None
    for m in re.finditer(r"[，,]", t):
        left, right = t[:m.start()].strip(), t[m.end():].strip()
        if not left_can_stand(left) or len(right) < HUMAN_MIN_RIGHT:
            continue
        if _DEPENDENT_HEAD_RE.match(right):
            continue
        if "，" in left or "," in left:
            continue
        return left, right
    return None


def humanize_split(text: str, roll: float | None = None) -> list[str]:
    """把一条回复变成 1~2 条真人形状的消息（句末句号顺手去掉）。"""
    import random as _random

    t = (text or "").strip()
    if not t:
        return []
    if roll is None:
        roll = _random.random()
    pair = split_at_comma(t)
    if pair is None or roll >= HUMAN_SPLIT_RATE:
        return [re.sub(r"。+$", "", t)]
    left, right = pair
    return [re.sub(r"。+$", "", left), re.sub(r"。+$", "", right)]


# ================================================================ AI 味动态拦截
# 来源：dafeiyu-qq-bot 的 plugins/dsh-aiflavour。三层：静态强词（剥）、静态影子
# （只记日志）、词根热度学习（同群累计够次数才开剥，久不命中自动降级）+ 口癖
# （只剥不刹）+ 会话刹车（窗口内又说教 → 整条不发）。
# 状态落 data/aiflavour_dyn.json，跟 style_fewshot.json 同目录，重启不丢。

FLAVOUR_STRONG = {
    "解释一下", "我解释一下", "我来解释", "简单解释", "解释解释",
    "总结一下", "总结来说", "总结起来", "总的来说",
    "简单来说", "简单点说", "说白了", "讲白了", "换句话说", "换而言之",
    "可以这样理解", "可以理解为", "让我解释", "具体来说", "详细说下",
    "综上所述", "总而言之", "作为一个语言模型", "作为AI", "作为 AI",
    "希望这个回答对你有帮助", "有什么可以帮您", "随时找我",
    "这是一个好问题", "这是一个值得思考的问题", "我理解你的感受",
    "值得注意的是", "值得一提", "由此可见", "不难发现", "显而易见",
    "需要指出的是", "需要强调的是", "值得强调的是",
}
FLAVOUR_WEAK = {
    "首先", "其次", "最后", "让我们", "本质上", "说到底",
    "不仅仅是", "不是", "专家认为", "研究表明",
    "前所未有", "前景光明", "标志着", "其实", "就是说", "就是这样",
}
FLAVOUR_ROOTS = {
    "解释", "总结", "说明", "意味着", "简单来说", "换句话说", "换而言之",
    "综上所述", "总而言之", "由此可见", "不难发现", "显而易见",
    "值得一提", "值得注意", "需要指出", "需要说明", "强调一下",
    "所以说", "简单说", "就是说", "也就是", "其实", "本质上",
}
FLAVOUR_TICS = {"大半夜的", "大半夜"}

_FLAVOUR_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "aiflavour_dyn.json"
)
_flavour_state: dict | None = None
_flavour_last_block: dict[str, float] = {}
_flavour_cfg = {"learn_min": 3, "deactivate_days": 7, "short_window": 180.0}


def _flavour_load() -> dict:
    global _flavour_state
    if _flavour_state is not None:
        return _flavour_state
    try:
        with open(_FLAVOUR_STATE_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        _flavour_state = raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        _flavour_state = {}
    except Exception:
        # 读坏了不能当空表继续跑：下一次 save 会把学到的名单整个覆盖掉
        _flavour_state = {}
    return _flavour_state


def _flavour_save() -> None:
    try:
        os.makedirs(os.path.dirname(_FLAVOUR_STATE_PATH), exist_ok=True)
        tmp = _FLAVOUR_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_flavour_state or {}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FLAVOUR_STATE_PATH)
    except Exception:
        pass  # 写不进去就只影响「学」，不影响这一轮的剥除


def configure_flavour(mode: str = "strip", learn_min: int = 3,
                      deactivate_days: int = 7, short_window: float = 180.0) -> None:
    _flavour_cfg.update({
        "mode": (mode or "strip").strip().lower(),
        "learn_min": max(2, int(learn_min or 3)),
        "deactivate_days": max(1, int(deactivate_days or 7)),
        "short_window": max(30.0, float(short_window or 180.0)),
    })


def _flavour_active() -> set[str]:
    st = _flavour_load()
    return {r for r, e in st.items() if isinstance(e, dict) and e.get("active")}


def _flavour_observe(root: str, ts: float) -> str:
    st = _flavour_load()
    e = st.get(root)
    if not isinstance(e, dict):
        e = {"count": 0, "last_ts": 0.0, "active": False}
        st[root] = e
    e["count"] = int(e.get("count", 0)) + 1
    e["last_ts"] = ts
    if e.get("active"):
        return "hot"
    if e["count"] >= _flavour_cfg["learn_min"]:
        e["active"] = True
        _flavour_save()
        return "upgraded"
    return ""


def _flavour_deactivate_stale(now: float) -> list[str]:
    st = _flavour_load()
    gone = []
    for root, e in list(st.items()):
        if isinstance(e, dict) and e.get("active") and \
                now - float(e.get("last_ts") or 0.0) > _flavour_cfg["deactivate_days"] * 86400.0:
            e["active"] = False
            e["count"] = 0
            gone.append(root)
    if gone:
        _flavour_save()
    return gone


def flavour_inspect(text: str, gid: str = "", ts: float | None = None) -> dict:
    """查一条回复里的 AI 味。不修改文本，只更新学习状态。"""
    import time as _time

    ts = float(ts if ts is not None else _time.time())
    res = {"strong_hits": [], "shadow_hits": [], "dyn_hits": [], "tic_hits": [],
           "dyn_upgraded": None, "brake": False}
    if not text:
        return res
    norm = re.sub(r"\s+", "", text)
    for p in sorted(FLAVOUR_STRONG, key=len, reverse=True):
        if re.sub(r"\s+", "", p) in norm:
            res["strong_hits"].append(p)
    for w in sorted(FLAVOUR_WEAK, key=len, reverse=True):
        if re.sub(r"\s+", "", w) in norm:
            res["shadow_hits"].append(w)
    for root in FLAVOUR_ROOTS:
        if root in norm:
            st = _flavour_observe(root, ts)
            if st == "upgraded":
                res["dyn_upgraded"] = root
            if st in ("upgraded", "hot"):
                res["dyn_hits"].append(root)
    for tic in FLAVOUR_TICS:
        if tic in norm:
            st = _flavour_observe(tic, ts)
            if st == "upgraded":
                res["dyn_upgraded"] = tic
            if st in ("upgraded", "hot"):
                res["tic_hits"].append(tic)
    _flavour_deactivate_stale(ts)
    if res["dyn_hits"]:
        last = _flavour_last_block.get(gid, 0.0)
        if ts - last <= _flavour_cfg["short_window"]:
            res["brake"] = True
        else:
            _flavour_last_block[gid] = ts
    return res


def _flavour_strip_all(cur: str, pat: str) -> tuple[str, list[str]]:
    """剥掉所有命中，并收干净命中点上悬空的分隔符。

    只吃右邻标点会在句尾剥出「妈什么妈，」这种尾逗号；右邻空了就改吃左邻。
    """
    rx = re.compile(pat)
    got: list[str] = []
    for _ in range(8):
        m = rx.search(cur)
        if not m:
            break
        if m.group(0):
            got.append(m.group(0))
        left, right = cur[:m.start()], cur[m.end():]
        right = re.sub(r"^[ \t]*[,，。；!！?？][ \t]*", "", right)
        if not right.strip():
            left = re.sub(r"[,，。；、][ \t]*$", "", left).rstrip()
        nxt = left + right
        if nxt == cur:
            break
        cur = nxt
    return cur, got


def flavour_strip(text: str) -> tuple[str, list[str]]:
    """剥静态强词 + 当前剥除名单里的词根/口癖。返回 (新文本, 命中原词)。"""
    hits: list[str] = []
    cur = text or ""
    pats = [re.escape(p) for p in FLAVOUR_STRONG]
    pats += [re.escape(r) for r in _flavour_active()]
    for pat in sorted(set(pats), key=len, reverse=True):
        cur, got = _flavour_strip_all(cur, pat)
        if got:
            hits.extend(got)
    return cur, hits


def flavour_status() -> str:
    """给面板/日志看的状态：当前剥除名单 + 计数。"""
    st = _flavour_load()
    rows = ["%s×%d" % (r, int(e.get("count", 0))) for r, e in sorted(st.items())
            if isinstance(e, dict) and e.get("active")]
    return "、".join(rows) if rows else "（空）"
