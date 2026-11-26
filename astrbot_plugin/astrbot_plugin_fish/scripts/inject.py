# -*- coding: utf-8 -*-
"""注入法实测：冒充 NapCat 连 AstrBot 的反向 WS，灌假群消息，收它发出来的动作。

不碰真群：用独立的 X-Self-ID（10000001），AstrBot 按事件里的 self_id 回发，
只会落回这条连接。也可以用 QQ_FAKE_SELF_ID 覆盖。

用法（在 AstrBot 根目录下）：
  ./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/inject.py
  ./.venv/bin/python .../inject.py --only at
"""

import argparse
import asyncio
import base64
import json
import os
import time

import websockets

WS_URL = os.environ.get("QQ_WS", "ws://127.0.0.1:6199/ws")
SELF_ID = int(os.environ.get("QQ_FAKE_SELF_ID", "10000001"))
GROUP = 999999
OTHER_GROUP = 888888
_counter = {"seq": 0, "mid": int(time.time()) % 100000 * 10}
# message_id 从时间派生：固定起始值会让上一轮注入的 id 重复，
# AstrBot 按 id 去重，第二轮事件会被静默丢弃，看起来像「插件没反应」
HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_IMG = os.path.join(HERE, "assets", "probe_card.png")


def _next_mid():
    _counter["mid"] += 1
    return _counter["mid"]


def text_msg(text, user_id, nick, at=False, group_id=GROUP):
    message = []
    if at:
        message.append({"type": "at", "data": {"qq": str(SELF_ID)}})
        message.append({"type": "text", "data": {"text": " " + text}})
    else:
        message.append({"type": "text", "data": {"text": text}})
    ev = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "group_id": group_id,
        "message_id": _next_mid(),
        "user_id": user_id,
        "message": message,
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nick, "card": nick, "role": "member"},
    }
    if at:
        # At 段的 qq 必须和事件 self_id 逐字一致，否则适配器认为 @ 的是别人，
        # 插件判不到「被@」，事件也会被当成发给第三方的消息吞掉
        ev["message"][0]["data"]["qq"] = str(ev["self_id"])
    return ev


def private_msg(text, user_id, nick):
    """私聊事件：AstrBot 里私聊默认直接唤醒，不需要 @ 也不需要前缀。"""
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nick},
    }


def image_msg(path, user_id, nick, caption="", at=False):
    """发图事件。带一张真图（不是占位），识图链路才有东西可看。

    坑：图片段必须用 base64:// 前缀，NapCat/aiocqhttp 都是这么传的；
    传本地路径 AstrBot 会当成相对路径去找，找不到就变成空图。
    """
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    message = []
    # @ 必须是真正的 At 组件，光在文本里写「@小鲸鱼」不算被 @：
    # 那样只能靠质量分过线，冷却期还会被拦住，测出来的东西不是想测的
    if at:
        message.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    message.append({"type": "image", "data": {"file": f"base64://{b64}", "sub_type": 0}})
    if caption:
        message.append({"type": "text", "data": {"text": " " + caption}})
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": int(time.time()),
        "self_id": SELF_ID,
        "group_id": GROUP,
        "message_id": _next_mid(),
        "user_id": user_id,
        "message": message,
        "raw_message": f"[图片]{caption}",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": nick, "card": nick, "role": "member"},
    }


class Injector:
    def __init__(self):
        self.actions = []  # [(ts, payload)]
        self.ws = None

    async def reader(self):
        """AstrBot 会往这条 WS 发 API 调用（send_group_msg 等），必须回 ok，否则它会卡住等超时。"""
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if "post_type" in data:
                continue
            self.actions.append((time.time(), data))
            echo = data.get("echo") or {}
            await self.ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": _next_mid()},
                "echo": echo,
            }, ensure_ascii=False))

    async def run(self, steps, settle=6.0):
        async with websockets.connect(
            WS_URL,
            additional_headers={
                "X-Self-ID": str(SELF_ID),
                "X-Client-Role": "universal",
                # AstrBot 的 aiocqhttp 反向 WS 要 Bearer token，跟 NapCat 一样的头；
                # 不带就是 HTTP 401，看起来像「端口没开」
                **({"Authorization": "Bearer " + os.environ["QQ_TOKEN"]} if os.environ.get("QQ_TOKEN") else {}),
            },
            max_size=None,
        ) as ws:
            self.ws = ws
            task = asyncio.create_task(self.reader())
            reports = []
            for step in steps:
                mark = time.time()
                if step.get("event") is not None:
                    await ws.send(json.dumps(step["event"], ensure_ascii=False))
                await asyncio.sleep(step.get("wait", settle))
                got = [(ts, p) for (ts, p) in self.actions if ts >= mark]
                reports.append((step, got))
                print(f"\n=== {step['title']} ===")
                print(f"  -> 注入：{step.get('desc', '')}")
                if not got:
                    print("  <- 动作：无（插件未触发回复）")
                for p_ts, p in got:
                    act = p.get("action")
                    params = p.get("params", {})
                    msg = params.get("message")
                    if isinstance(msg, list):
                        msg = "".join(
                            (m.get("data", {}).get("text") or f"[{m.get('type')}]")
                            for m in msg
                        )
                    # 带上「注入 → 收到动作」的秒数，延迟就得靠这行量
                    print(f"  <- 动作 +{p_ts - mark:.2f}s：{act} group={params.get('group_id')} msg={str(msg)[:200]!r}")
            task.cancel()
        return reports


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", default=[], help="只跑这些 key，可写多个")
    args = ap.parse_args()

    steps = [
        {
            "title": "场景0 预热：给张三种一条记忆（被动消息，正常略过）",
            "key": "seed",
            "desc": "我明天下午要去面试，紧张死了",
            "event": text_msg("我明天下午要去面试，紧张死了", 10001, "张三"),
            "wait": 4,
        },
        {
            "title": "场景1 被@必回（同时验证记忆注入）",
            "key": "at",
            "desc": "@小鲸鱼 你还记得我明天要干嘛吗",
            "event": text_msg("@小鲸鱼 你还记得我明天要干嘛吗", 10001, "张三", at=True),
            "wait": 25,
        },
        {
            "title": "场景2 普通闲聊不接",
            "key": "chitchat",
            "desc": "今天好热啊",
            "event": text_msg("今天好热啊", 10002, "李四"),
            "wait": 6,
        },
        {
            "title": "场景2b 连发夸奖，把心境推上去（都不该回）",
            "key": "fun",
            "desc": "太强了哈哈哈 ×6",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景2b 连发夸奖（第 2 条）",
            "key": "fun",
            "desc": "太强了哈哈哈",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景2b 连发夸奖（第 3 条）",
            "key": "fun",
            "desc": "太强了哈哈哈",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景2b 连发夸奖（第 4 条）",
            "key": "fun",
            "desc": "太强了哈哈哈",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景2b 连发夸奖（第 5 条）",
            "key": "fun",
            "desc": "太强了哈哈哈",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景2b 连发夸奖（第 6 条）",
            "key": "fun",
            "desc": "太强了哈哈哈",
            "event": text_msg("太强了哈哈哈", 10002, "李四"),
            "wait": 3,
        },
        {
            "title": "场景2c 心境已推高，@ 一次看 prompt 里有没有「此刻的感觉」",
            "key": "mood_at",
            "desc": "@小鲸鱼 你今天怎么这么有精神",
            "event": text_msg("@小鲸鱼 你今天怎么这么有精神", 10002, "李四", at=True),
            "wait": 35,
        },
        {
            "title": "场景3 连续刷屏不接（连发 4 条）",
            "key": "spam",
            "desc": "哈哈 / 哈哈 / 哈哈哈哈 / 草",
            "event": text_msg("哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景3 连续刷屏（第 2 条）",
            "key": "spam",
            "desc": "哈哈",
            "event": text_msg("哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景3 连续刷屏（第 3 条）",
            "key": "spam",
            "desc": "哈哈哈哈",
            "event": text_msg("哈哈哈哈", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景3 连续刷屏（第 4 条）",
            "key": "spam",
            "desc": "草",
            "event": text_msg("草", 10002, "李四"),
            "wait": 4,
        },
        {
            "title": "场景4 ab! 前缀不插手（交给 AstrBot 自己的 LLM）",
            "key": "prefix",
            "desc": "ab! 用一句话解释什么是快速排序",
            "event": text_msg("ab! 用一句话解释什么是快速排序", 10003, "王五"),
            "wait": 25,
        },
        {
            "title": "场景5 有价值的闲聊偶尔接（提问+技术词，分数过线）",
            "key": "interject",
            "desc": "有人知道这个报错咋整吗？pip 装到一半就挂了",
            "event": text_msg("有人知道这个报错咋整吗？pip 装到一半就挂了", 10004, "赵六"),
            "wait": 25,
        },
        {
            "title": "场景6 刚说完话，同类消息应被冷却挡住",
            "key": "cooldown",
            "desc": "我也遇到了，谁知道怎么解决？",
            "event": text_msg("我也遇到了，谁知道怎么解决？", 10005, "钱七"),
            "wait": 6,
        },
        {
            "title": "场景7 面板指令前缀不接（#帮助）",
            "key": "cmd",
            "desc": "#帮助",
            "event": text_msg("#帮助", 10006, "孙八"),
            "wait": 5,
        },
        {
            "title": "场景8 私聊（不需要 @，同一套人格）",
            "key": "private",
            "desc": "在吗，问你个事",
            "event": private_msg("在吗，问你个事", 10001, "张三"),
            "wait": 25,
        },
        # ---------------- 新增能力实测 ----------------
        {
            "title": "场景9 发图 + @：Gemini 白描要进 prompt（回复内容得提到面试/周三）",
            "key": "image",
            "desc": "[图片: 面试安排 周三 14:00 线上二面] @小鲸鱼 你看下这个，我该准备啥",
            "event": image_msg(PROBE_IMG, 10009, "图图", "你看下这个，我该准备啥", at=True),
            "wait": 40,
        },
        {
            "title": "场景10 要一段长答案：应拆成 2~3 条隔 0.6~1.5s 补发",
            "key": "long",
            "desc": "@小鲸鱼 你到底为什么叫小鲸鱼？讲详细一点，多说几句",
            "event": text_msg("@小鲸鱼 你到底为什么叫小鲸鱼？讲详细一点，多说几句", 10010, "话痨", at=True),
            "wait": 45,
        },
        {
            "title": "场景12 别的机器人发的高分提问：不接、不进时间线",
            "key": "peerbot",
            "desc": "有人知道这个报错咋整吗？pip 装到一半就挂了（来自 peer_bots 名单里的号）",
            "event": text_msg("有人知道这个报错咋整吗？pip 装到一半就挂了", 10088, "小助手"),
            "wait": 8,
        },
        {
            "title": "场景14 斗图闸：连着两次想让它配图，第二张必须被拦",
            "key": "meme",
            "desc": "@小鲸鱼 笑死我了，你这也太离谱了吧",
            "event": text_msg("@小鲸鱼 笑死我了，你这也太离谱了吧", 10014, "乐子人", at=True),
            "wait": 30,
        },
        {
            "title": "场景14b 同一话题再要一张图（表情包间隔/同标签该拦）",
            "key": "meme",
            "desc": "@小鲸鱼 再来一张，刚才那个太逗了",
            "event": text_msg("@小鲸鱼 再来一张，刚才那个太逗了", 10014, "乐子人", at=True),
            "wait": 30,
        },
        # 跨群记忆：另一群里有人聊一句（该群要临时加进 enabled_groups），
        # 然后回主群 @ 一次，prompt 里应该出现「别的群此刻在聊」
        {
            "title": "场景15 另一个群（888888）聊了一句，记进跨群话题",
            "key": "cross",
            "desc": "我们组周五团建去爬山，谁去谁报名",
            "event": text_msg("我们组周五团建去爬山，谁去谁报名", 10015, "爬山的", group_id=888888),
            "wait": 6,
        },
        {
            "title": "场景15b 主群 @ 一次：prompt 里允许顺口带一句别的群在聊什么",
            "key": "cross",
            "desc": "@小鲸鱼 周末有啥安排",
            "event": text_msg("@小鲸鱼 周末有啥安排", 10001, "张三", at=True),
            "wait": 30,
        },
        # 两个人的对话：先垫两句，再让第三句去撞 judge。
        # 垫的两句故意是低质量碎片（不插话），只有最后那句值得判
        {
            "title": "场景13a 铺垫：李四和周九在私下对答（碎片，不该被接）",
            "key": "social_skip",
            "desc": "嗯嗯",
            "event": text_msg("嗯嗯", 10002, "李四"),
            "wait": 1.0,
        },
        {
            "title": "场景13b 铺垫：周九回一句两人的私事",
            "key": "social_skip",
            "desc": "那事我晚点跟你说",
            "event": text_msg("那事我晚点跟你说", 10012, "周九"),
            "wait": 1.0,
        },
        {
            "title": "场景13 两步生成判「不接」：两个人的对话，judge 该拦（主模型一次都不调）",
            "key": "social_skip",
            "desc": "@李四 你那个项目最后上线了没（需要临时把 interject_desire 调高，否则质量分先把它拦了）",
            "event": text_msg("@李四 你那个项目最后上线了没", 10012, "周九"),
            "wait": 25,
        },
    ]

    if args.only:
        wanted = list(args.only)
        steps = [s for s in steps if s["key"] in wanted]

    await Injector().run(steps)


if __name__ == "__main__":
    asyncio.run(main())
