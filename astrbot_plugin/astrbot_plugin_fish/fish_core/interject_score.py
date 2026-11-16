# -*- coding: utf-8 -*-
"""接话质量分：判断「这句话值不值得接」，纯本地规则、零 token。

移植自 qq-deepseek-bot 的 bot.score_message_quality / interject_threshold，
公式与词表保持一比一，改动只在去掉对 bot 全局状态的依赖——时间类的输入
（上一条消息、上次自己说话的时间）改成参数传进来，方便测试和单群隔离。
"""

import re
import time

INTERJECT_SLANG_WORDS = (
    "笑死", "离谱", "绝了", "卧槽", "绷不住", "破防", "太抽象", "什么鬼",
    "真的服", "逆天", "抽象", "答辩", "急急急",
)
INTERJECT_QUESTION_HINTS = (
    "吗", "呢", "怎么", "为什么", "为啥", "谁", "哪", "是不是", "有没有",
    "如何", "求", "知不知道", "干嘛", "干啥", "咋", "？", "?",
)
INTERJECT_CODE_HINTS = ("报错", "traceback", "npm", "pip", "git", "docker", "编译", "配置")
# 点名要看全名：群里单独一个「你」十有八九指别人
_ADDRESS_WORDS = ("小鲸鱼", "大肥鱼", "肥鱼")


def interject_threshold(desire: float) -> float:
    """接话意愿 → 质量门槛。desire 越高门槛越低，0.30 对应 0.62。"""
    return max(0.40, min(0.95, 0.86 - 0.8 * float(desire)))


def score_message_quality(
    text: str,
    *,
    has_image: bool = False,
    has_media: bool = False,
    recent_msgs: int = 0,
    now: float | None = None,
    last_msg_ts: float = 0.0,
    last_bot_reply_ts: float = 0.0,
) -> tuple[float, str]:
    """给一条群消息打 0~1 的「值得接话」分，返回 (分数, 命中原因串)。"""
    t = (text or "").strip()
    if not t and not has_image and not has_media:
        return 0.0, "空消息"

    # 净化后只剩 [表情:xx] 这类占位符时，长度判断会把它当成有内容的长句
    meaningful = re.sub(r"\[(?:表情|大表情|图片|卡片)[^\]]*\]", "", t).strip()
    if not meaningful and not has_image and not has_media:
        return 0.0, "纯表情"

    score = 0.30
    hits = []

    length = len(meaningful)
    if length >= 6:
        score += 0.10
    if length >= 15:
        score += 0.10
        hits.append("有内容")
    if length > 60:
        score += 0.05
        hits.append("长文")

    if any(h in t for h in INTERJECT_QUESTION_HINTS):
        score += 0.20
        hits.append("提问")
    if any(w in t for w in _ADDRESS_WORDS):
        score += 0.25
        hits.append("点名")
    if any(w in t for w in INTERJECT_SLANG_WORDS) or "！" in t or "!" in t:
        score += 0.20
        hits.append("有情绪")
    if any(w in t.lower() for w in INTERJECT_CODE_HINTS):
        score += 0.10
        hits.append("技术")
    if has_image:
        score += 0.15
        hits.append("带图")
    if has_media:
        score += 0.20
        hits.append("带卡片")

    if recent_msgs >= 3:
        score += 0.06
        hits.append("话题延续")
    if recent_msgs >= 6:
        score += 0.04

    cleaned = re.sub(r"[\s\d\W_]+", "", t)
    # 带图带卡片时「字少」不是缺点——图本身就是内容。群里的真实形态就是
    # 甩一张图配两个字，这时候再按「过短」扣分，等于永远不接图
    if len(cleaned) <= 1 and not has_image and not has_media:
        score -= 0.20
        hits.append("过短")
    if re.fullmatch(r"(哈+|6+|草+|啊+|嗯+|哦+|好+)", cleaned or ""):
        score -= 0.15
        hits.append("纯水")

    now = time.time() if now is None else now
    if last_msg_ts > 0 and (now - last_msg_ts) >= 600.0:
        # 冷场是真人最容易开口的时刻（没人说话才轮得到你起话头），
        # 权重得压得住「过短」的扣分，否则半夜群里永远是不理人
        score += 0.15
        hits.append("冷场")
    elif last_bot_reply_ts > 0 and (now - last_bot_reply_ts) < 600.0:
        score += 0.05
        hits.append("热场")

    return max(0.0, min(1.0, score)), "+".join(hits) or "普通闲聊"
