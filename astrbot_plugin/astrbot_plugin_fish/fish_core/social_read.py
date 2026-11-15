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
    "在说与你无关的私事、消息只是「哈哈/草/？」这类无内容碎片、话题已经结束。\n"
    "join=true 的情形：有人提问没人答、在讨论一个具体的东西、在吐槽/玩梗且有空位、"
    "点名或明确在对你说话。"
)


def build_prompt(timeline: list[str], latest: str, nick: str, bot_nick: str = "小鲸鱼",
                 is_at: bool = False) -> str:
    lines = timeline[-8:] if timeline else ["（群里暂时没别人说话）"]
    head = f"你是「{bot_nick}」，群里一个爱插话的普通群友。\n最近记录（从旧到新）："
    tail = f"\n刚发生的这句：{nick}：{latest or '（只发了图/只@了人）'}"
    if is_at:
        tail += "\n（这句@了你，是你被直接叫到。除非完全没有内容，否则 join 必须为 true）"
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


async def decide(llm_call, *, timeline, latest, nick, bot_nick="小鲸鱼", is_at=False) -> dict:
    """llm_call(prompt, system) -> str，由调用方注入（AstrBot 的 provider 或测试桩）。"""
    prompt = build_prompt(timeline, latest, nick, bot_nick, is_at)
    text = await llm_call(prompt, SYSTEM)
    verdict = parse(text)
    verdict["prompt"] = prompt
    return verdict


def hint_line(verdict: dict) -> str:
    """把结论翻成一句给主模型的提示（不贴 JSON，免得模型学成结构化输出）。"""
    if not verdict:
        return ""
    topic = verdict.get("topic") or ""
    how = verdict.get("how") or ""
    bits = []
    if topic:
        bits.append(f"【这轮在聊】：{topic}")
    if how:
        bits.append(f"【怎么接】：{how}")
    if not bits:
        return ""
    return "\n" + "\n".join(bits) + "\n（顺着上面这个角度说一句就够，别把「该怎么接」复述出来）"
