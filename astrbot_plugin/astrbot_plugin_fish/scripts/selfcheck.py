# -*- coding: utf-8 -*-
"""出口加工链自检：不需要 AstrBot、不需要连 QQ，直接跑一遍各模块。

用途：模型偶尔才吐一次 [MEME:xx] / 撞模板 / 手滑，靠等真实流量验证太慢；
这里用固定输入把「剥标签 → 清 markdown → 反模板 → 摘口癖 → 去复述开场 →
打错字 → 配图」逐段跑出来，确认接线没断。

  ./.venv/bin/python data/plugins/astrbot_plugin_fish/scripts/selfcheck.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "fish_core")
sys.path.insert(0, CORE)

import anti_template  # noqa: E402
import composer  # noqa: E402
import cross_memory  # noqa: E402
import interject_planner  # noqa: E402
import interject_score  # noqa: E402
import meme_catalog  # noqa: E402
import meme_gate  # noqa: E402
import peer_bots  # noqa: E402
import social_read  # noqa: E402
import splitter  # noqa: E402


def line(title):
    print(f"\n--- {title} ---")


def main():
    line("1. 表情标签剥离 + 出口清洗")
    raw = "**[MEME:得意]** 那必须的（笑）`真的`"
    clean, tag = meme_catalog.extract_tag(raw)
    print("模型原文 :", raw)
    print("抽出标签 :", tag)
    print("清洗后   :", composer.sanitize_reply(clean))
    print("配图命中 :", bool(meme_catalog.pick_b64(tag, clean)) if tag else "无标签")

    line("2. 反模板：起手撞车")
    recent = ["那必须的，我早就会了", "那必须的，这还用问"]
    text = "那必须的，我昨天就说过，这事早定了"
    reason = anti_template.match(text, recent)
    print("最近说过 :", recent)
    print("待发     :", text)
    print("命中原因 :", reason)
    print("削形后   :", anti_template.reshape(text, reason))

    line("3. 口癖/复述开场")
    t = "是这样吧，我昨天就说过这个事情啊"
    print("原文     :", t)
    print("摘语气词 :", anti_template.trim_particle(t, ["就这啊", "行吧啊"]))
    print("去复述   :", anti_template.strip_echo_opener("面试？面试确实得准备一下", "我明天要去面试"))

    # 4. 手滑打错字：旧仓 services/typo.py 迁移时没搬过来，插件里也没有任何调用点。
    # 这里原本 import typo 直接 ModuleNotFoundError，让整个自检跑不起来，
    # 干脆去掉这一节；哪天真把打错字做回来，再连同 import 一起补

    line("5. 接话质量分（门槛 0.62，desire=0.30）")
    thr = interject_score.interject_threshold(0.30)
    print(f"门槛 = {thr:.2f}")
    cases = [
        ("今天好热啊", 0, 0),
        ("哈哈", 0, 0),
        ("草", 0, 0),
        ("有人知道这个报错咋整吗？pip 装到一半就挂了", 0, 0),
        ("小鲸鱼你这画的什么玩意，离谱", 0, 1),
    ]
    for text, recent_msgs, has_img in cases:
        s, why = interject_score.score_message_quality(
            text, has_image=bool(has_img), recent_msgs=recent_msgs,
            now=1000.0, last_msg_ts=0.0, last_bot_reply_ts=0.0)
        s2, timing = interject_planner.adjust(s, now=1000.0, last_bot_reply=0.0,
                                             msgs_since_bot=recent_msgs)
        print(f"  {s2:.2f} {'接' if s2 >= thr else '略过'}  ({why} | {timing or '无修正'})  <- {text}")

    line("6. 长回复拆条（60 字起拆，最多 3 条，括号内不切）")
    longs = [
        "面试啊，明天下午那场，你紧张得跟什么似的。先把作品集挑三四个拿得出手的，"
        "别全塞进去；线上二面记得先试试摄像头和网，别到点掉链子。行，就这样。",
        "行吧（我上次也这么说的，结果第二天就忘了）。你还是写下来吧。",
        "嗯。",
    ]
    for t in longs:
        parts = splitter.split_reply(t, max_chars=60, max_parts=3)
        print(f"  {len(t):3d} 字 -> {len(parts)} 条  {parts}")

    line("7. 斗图闸（一回合一张 / 不跟图 / 同标签不重复）")
    now = 10000.0
    gate_cases = [
        ("刚发过一张（120s 前）", dict(last_meme_ts=now - 120, recent_tags=["得意"])),
        ("群友 30s 前发了图", dict(incoming_image_ts=now - 30, recent_tags=[])),
        ("上一次同标签", dict(last_meme_ts=now - 900, recent_tags=["得意"])),
        ("干净的一轮", dict(last_meme_ts=now - 900, recent_tags=["无语"])),
    ]
    for name, kw in gate_cases:
        ok, why = meme_gate.decide("得意", now, min_gap_sec=300, follow_gap_sec=90, **kw)
        print(f"  {'放行' if ok else '拦住'}  {why or '-'}   <- {name}")

    line("8. 视觉配置解析（不联网，只看降级链与预算）")
    print("  主模型 / 降级链 :", "gemini-3.5-flash-lite", "/", ["gemini-3.6-flash", "gemini-3.8-flash"])
    print("  单模型超时/总预算:", "6s / 12s；任一张图失败 → 空字符串 → 按没图继续")

    line("9. 两步生成：judge 提示词与结论解析")
    print("  判不接的解析 :", social_read.parse('{"join": false, "topic": "两人私事", "how": ""}'))
    print("  判接的解析   :", social_read.parse('好的\n{"join": true, "topic": "问报错", "how": "先问报错原文"}'))
    print("  坏 JSON 兜底 :", social_read.parse("我不太确定"))
    print("  hint 行      :", social_read.hint_line({"join": True, "topic": "问报错", "how": "先问报错原文"}))

    line("10. 跨群记忆：摘要清洗与引用")
    # 用临时文件而不是 ":memory:"：CrossGroupMemory 是 JSON 落盘实现，传 ":memory:"
    # 会在当前目录生成一个名字叫 :memory: 的文件（踩过）
    import tempfile
    mem = cross_memory.CrossGroupMemory(os.path.join(tempfile.mkdtemp(), "topics.json"), max_topics=5)
    mem.record_topic("111", "张三", "今晚八点开黑 https://example.com/x @李四 我的号 123456789")
    mem.record_topic("222", "王五", "在讨论要不要换工作")
    print("  别的群(999)能看到的 :", mem.hint("999", 2).strip() or "（无）")
    print("  自己群(111)不该看到 :", mem.hint("111", 2).strip() or "（无，符合预期）")

    line("11. 别的机器人识别（名单 + 昵称特征）")
    peers = {"10088"}
    for uid, nick in [("10088", "随便啥名"), ("10099", "小助手"), ("10001", "张三")]:
        print(f"  {nick:8s}({uid}) -> {'机器人' if peer_bots.is_peer_bot(uid, nick, peers) else '真人'}")

    line("12. 时间/身份锚点")
    print(composer.time_hint().strip())
    sysp = composer.build_system_prompt("（人设正文略）", "小鲸鱼", "10000001", "999999", False)
    print(sysp.splitlines()[0])


if __name__ == "__main__":
    main()
