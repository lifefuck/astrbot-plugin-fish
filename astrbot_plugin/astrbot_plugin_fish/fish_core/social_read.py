# -*- coding: utf-8 -*-
"""两步生成的第一步：先判「这帮人在聊什么、我该不该接、怎么接」，再写回复。

为什么值得多一次调用：主模型看到群聊记录只会顺着最后一句写，经常答非所问地插进
两个人的私聊，或者把别人的吐槽当成问题一本正经地回答。先花一次小调用（只出 JSON，
几十个 token）判清楚，比在主 prompt 里堆十条约束管用。

延迟控制：单次超时 8 秒、max_tokens 很小；判「不接」时直接 stop，省掉一次十几秒的主模型。
"""

from __future__ import annotations

import json
import re

SYSTEM = (
    "你在旁观一个 QQ 群的聊天。只判断两件事，然后输出一行 JSON，不要解释、不要 Markdown：\n"
    '{"join": true/false, "how": "如果该接，用一句话说我该怎么接（角度，不是原文）", "topic": "这轮他们在聊什么，12 字以内"}\n'
    "join=false 的情形：两个人在私聊式对话、在吵架、在发图斗图不需要文字、"
    "在说与你无关的私事、消息只是「哈哈/草/？」这类无内容碎片、话题已经结束；"
    "两个人互相骂、跟你没关系的也别接；"
    "两个人已经在一来一回地解决同一个问题（第三个人插进去只会重复他们已经说过的步骤）；"
    "有人正在被帮、进展顺利，你插不上新东西；"
    "这一轮已经有人（也包括你自己之前）把同一个问题答过了，后面的话只是再补一句提醒、注意事项，"
    "没有新信息——这种也不接，同一件事说两遍比不说话更像机器人。\n"
    "join=true 的情形：有人开口骂你、阴阳你（这类要顶回去，不是装没看见，但也别写长）、"
    "有人提问没人答、在讨论一个具体的东西、在吐槽/玩梗且有空位、"
    "点名或明确在对你说话。\n"
    "记录里你自己说过的话也在其中。别人接着你的话往下说——哪怕只有「懂事」「行」「哦」"
    "这么两个字，哪怕没点你的名——那就是在跟你说话，join 必须为 true。"
    "把接着你说的话当成别人的私聊而不接，是这类群里最招人烦的行为。\n"
    "有人喊「给我搞死他」「给我配死这个发图的」「弄他」这种，是在起哄、解气、开玩笑，"
    "不是在给你派活。这时候 how 别用「问一句细节」那个角度，要直接站他那边损那个人一句。"
    "写成「问他想怎么配」这种，等于没听懂人话。\n"
    "有人回你上一句时还带了个表情、一个问号、一句「？」这种近乎空的内容，"
    "只要它是对你那句的回应，也算在跟你说话。\n"
    "how 只能落在这三种角度：问一句细节、就着对方说的表个态、把话递回去让对方讲。"
    "起哄骂人的场景走「表个态」——顺着骂或者笑他，不要反问。\n"
    "how 开头必须点明你在接谁：写成「接A的：角度」或「对B：角度」，"
    "看不出该接谁就 join=false——把两个人之间的话算成自己参与，是这类群里最假的行为。\n"
    "how 是「角度」，不是台词：最多 15 字，短语形态（如「吐槽他这操作」「问他值不值」），"
    "不许写成一句能直接发出去的话，不许我字开头、不许「问一句……吧」这种把台词写全的样子。\n"
    "how 里不许出现上下文没提到的具体细节：不许替他编原因（夜宵、熬夜、吃多了、喝酒、奶茶），"
    "不许编时间地点数字。这些一旦写进 how，主模型会原样念出去，看起来就是机器人凭空乱猜。\n"
    "绝对不许让接话的人声称自己玩过、用过、去过、吃过、买过——它没有任何亲身经历。"
    "碰到只有同好才能接的话题（约人开黑、晒装备、晒使用感受），how 必须落成「问对方」："
    "问这游戏什么手感、问对方玩得怎么样、让对方讲讲。写成「报名凑热闹」「一起玩」「我也在玩」都算错。"
)


def build_prompt(timeline: list[str], latest: str, nick: str, bot_nick: str = "小鲸鱼",
                 is_at: bool = False, is_follow: bool = False) -> str:
    lines = timeline[-8:] if timeline else ["（群里暂时没别人说话）"]
    head = f"你是「{bot_nick}」，群里一个爱插话的普通群友。\n最近记录（从旧到新）："
    tail = f"\n刚发生的这句：{nick}：{latest or '（只发了图/只@了人）'}"
    if is_at:
        tail += "\n（这句@了你，是你被直接叫到。除非完全没有内容，否则 join 必须为 true）"
    elif is_follow:
        # 追问窗口：他在你上一句之后接着说的。判官以前看不到这个信号，
        # 只当「两个群友在私聊」→ 判不接，用户当场就发现「我说明事它怎么不吭声」
        tail += "\n（这句接在你刚说的话后面，是在跟你说，不是你插进别人的私聊。join 必须为 true）"
    return head + "\n" + "\n".join(lines) + tail


def parse(text: str) -> dict:
    """解析模型输出。模型爱加 ```json 包裹、爱在后面补解释，都要能兜住。"""
    raw = (text or "").strip()
    body = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", body, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            join = data.get("join")
            if isinstance(join, str):
                join = join.strip().lower() in ("true", "1", "yes", "接")
            return {
                "join": bool(join),
                "how": str(data.get("how") or "").strip()[:80],
                "topic": str(data.get("topic") or "").strip()[:24],
                "raw": raw[:200],
            }
        except Exception:
            pass
    # 兜底：JSON 坏了就看关键字。宁可判「接」（少一次误杀）也别把该回的话吞掉。
    return {
        "join": not bool(re.search(r"不接|不该接|skip|false", body, re.I)),
        "how": "",
        "topic": "",
        "raw": raw[:200],
    }


async def decide(llm_call, *, timeline, latest, nick, bot_nick="小鲸鱼", is_at=False,
                 is_follow=False) -> dict:
    """llm_call(prompt, system) -> str，由调用方注入（AstrBot 的 provider 或测试桩）。"""
    prompt = build_prompt(timeline, latest, nick, bot_nick, is_at, is_follow)
    text = await llm_call(prompt, SYSTEM)
    verdict = parse(text)
    verdict["prompt"] = prompt
    return verdict


# 判官偶尔还是会给出「报名凑热闹」这类需要亲身经历的接法。它一旦漏进来，
# 主模型就只能靠编「我也玩过」来执行。这里兜一道：命中就换成问对方的写法，
# 宁可接得平一点，也不让它凭空长出一段经历。
_EXPERIENCE_CLAIM = re.compile(
    r"报名|凑一?局|凑热闹|一起玩|一起打|开黑|我也在?玩|我也(?:用|去|吃|买)|带我一个"
)


_INVENT_REASON = re.compile(r"夜宵|宵夜|吃多|熬夜|奶茶|啤酒|喝酒|减肥|健身|外卖|胖|瘦")
_LEAD_IN = re.compile(r"^(?:问一?句|问问他|问问|说一?句|接一?句|可以|就着|顺着|针对|吐槽|表态)[：:，,、\s]*")


def _sanitize_how(how: str, context: str = "") -> str:
    how = (how or "").strip()
    if not how:
        return ""
    if _EXPERIENCE_CLAIM.search(how):
        return "就着对方说的问一句细节（手感/值不值/什么时候开），让对方接着讲，别说自己玩过"
    # 判官把 how 写成一句现成台词，主模型就原样念出去。更糟的是台词里
    # 常带着上下文根本没出现过的原因（「他最近是不是夜宵吃多了」）——
    # 那等于替群里人编身世。逐词对上下文：只要有一个词上下文没提过，就退回成不带细节的态度
    if context:
        invented = [m.group(0) for m in _INVENT_REASON.finditer(how) if m.group(0) not in context]
        if invented:
            return "就着对方这句表个态，别替他猜原因"
    # 「问一句：XXX？」剥掉引导词，只留角度短语
    trimmed = _LEAD_IN.sub("", how).strip("：:，,。？?！! ")
    if trimmed and len(trimmed) <= 30:
        return trimmed
    return how


def how_of(verdict: dict, context: str = "") -> str:
    """取干净后的「怎么接」。on_group 用它把角度带进出口做照抄检测。"""
    if not verdict:
        return ""
    return _sanitize_how(verdict.get("how") or "", context)


def hint_line(verdict: dict, context: str = "") -> str:
    """把结论翻成一句给主模型的提示（不贴 JSON，免得模型学成结构化输出）。"""
    if not verdict:
        return ""
    topic = verdict.get("topic") or ""
    how = how_of(verdict, context)
    bits = []
    if topic:
        bits.append(f"【这轮在聊】：{topic}")
    if how:
        bits.append(f"【怎么接】：{how}")
    if not bits:
        return ""
    # 「角度」两个字写进括号里不管用，这句得说得更死：判官写的是方向，念出来就是机器人味
    return ("\n" + "\n".join(bits)
            + "\n（上面那个是方向不是台词：别把它的字面写进回复，也别添它没提过的原因和细节，"
              "用你自己的话顺一句就够）")
