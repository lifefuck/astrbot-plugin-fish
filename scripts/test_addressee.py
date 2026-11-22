# -*- coding: utf-8 -*-
"""收件人判定表的行为测试（纯逻辑，不联网、不落盘）。

跑法：python3 scripts/test_addressee.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "astrbot_plugin"))

from astrbot_plugin_fish.fish_core import addressee as A  # noqa: E402

A._READONLY = True        # 测试不许写 aliases.json
SELF = "111"              # 机器人自己
ME = "222"                # 主人
X, Y = "333", "444"       # 两个群友
CFG = {"addressee_hard_threshold": 70, "addressee_gray_threshold": 40,
       "addressee_thread_window_sec": 180, "addressee_topic_gap_sec": 60}

fail = 0


def check(name, got, want):
    global fail
    ok = got == want
    fail += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + f"  got={got!r} want={want!r}")


def sc(name, want_score, floor=None, ceil=None, **kw):
    """打分断言：want_score 精确比对，或者只比区间（用于只关心阈值过不过的用例）。"""
    s, why = A.judge(cfg=CFG, self_id=SELF, **kw)
    if floor is not None:
        ok = floor <= s <= (ceil if ceil is not None else floor + 100)
        print(("PASS " if ok else "FAIL ") + name + f"  score={s} want∈[{floor},{ceil}] ({why})")
        global fail
        fail += 0 if ok else 1
        return
    check(name + f" ({why})", s, want_score)


# ---------- L1 显式 ----------
sc("被@满分", 100, is_at=True, text="在吗")
sc("引用我", 95, is_quote_bot=True, text="你看这个")
sc("唤醒前缀", 100, is_wake=True, text="讲讲这个")

# ---------- L2 点名 ----------
sc("点名学到的小名", 70, text="鲸宝你觉得呢", bot_names=("鲸宝",))
sc("点名兜底名字", 70, text="小鲸鱼出来")
sc("没点名只是提到鱼", 0, text="今天吃鱼", bot_names=("鲸宝",))

# ---------- L2 代词 ----------
sc("代词·鱼刚说过", 60, text="你怎么看", last_bot_ts=1000.0, now=1030.0)
sc("代词·鱼一小时前说过", 0, text="你怎么看", last_bot_ts=1000.0, now=5000.0)

# ---------- L3 接续 ----------
sc("追问窗口", 60, text="那再讲讲", is_follow=True, last_bot_ts=1000.0, now=5000.0)

# ---------- L4 相邻 ----------
sc("鱼刚说过(30s)", 30, text="嗯？", last_bot_ts=1000.0, now=1030.0)
sc("鱼一分钟前说过", 0, text="嗯？", last_bot_ts=1000.0, now=1200.0)

# ---------- 反向扣分 ----------
sc("被@但@了别人", 40, is_at=True, text="你看这个", at_uids=("999",))
sc("被@且@别人+引用别人", 0, is_at=True, text="你看这个", at_uids=("999",), reply_uid="999")
sc("点名但@别人", 10, text="鲸宝你看", bot_names=("鲸宝",), at_uids=("999",))

# ---------- 活跃线程 ----------
sc("A↔B线程里鱼不抢", 0, text="哈哈哈哈", thread=(Y, 2), last_bot_ts=1000.0, now=1030.0)
sc("A↔B来回四轮", 0, text="笑死", thread=(Y, 4), last_bot_ts=1000.0, now=1030.0)
# 一次交替（A说完B应一声）还是群聊常态，不算线程，不压
sc("一次交替不压制", 30, text="嗯？", thread=(Y, 1), last_bot_ts=1000.0, now=1030.0)
# 被 @ 拉进线程仍然要说：显式指向优先级高于线程压制
sc("线程里被@照说", 100, is_at=True, text="鱼你看", thread=(Y, 4))

# ---------- record：别名学习 + 线程登记 + to_uid ----------
A.reset()
A.record(gid="g1", uid=ME, nick="lifefuck", text="@鲸宝 在吗", now=1000.0,
         self_id=SELF, at_uids=(SELF,), at_named=((SELF, "鲸宝"),), is_at=True, cfg=CFG)
check("别名学到鲸宝", A.names_of("g1", SELF), ("鲸宝",))
ref = A.table("g1")[-1]
check("被@的 to_uid 是自己", ref.to_uid, SELF)
check("被@的分数", ref.score, 100)

# 同一个人连说两句：不构成线程
A.record(gid="g1", uid=X, nick="甲", text="今天天气不错", now=1010.0, self_id=SELF, cfg=CFG)
A.record(gid="g1", uid=X, nick="甲", text="确实不错", now=1015.0, self_id=SELF, cfg=CFG)
# 自己连说不累加轮数：这次的 1 轮来自前面「主人说完甲应一声」那一次交替
check("自己连说不累加轮数", A.thread_of("g1", X, 1016.0, 180, SELF), (ME, 1))

# 甲乙交替一次：登记线程，但只算 1 轮，不压制
A.record(gid="g1", uid=Y, nick="乙", text="我觉得还行", now=1020.0, self_id=SELF, cfg=CFG)
check("甲乙成线程", A.thread_of("g1", X, 1021.0, 180, SELF), (Y, 1))
s1, _ = A.judge(cfg=CFG, self_id=SELF, text="聊啥呢", now=1021.0,
                last_bot_ts=1018.0, thread=A.thread_of("g1", X, 1021.0, 180, SELF))
check("一轮不压（只剩相邻分）", s1, 30)

# 再来回一轮：2 轮，鱼插话被压到 0
A.record(gid="g1", uid=X, nick="甲", text="那当然", now=1030.0, self_id=SELF, cfg=CFG)
A.record(gid="g1", uid=Y, nick="乙", text="确实", now=1040.0, self_id=SELF, cfg=CFG)
check("交替第三轮", A.thread_of("g1", X, 1041.0, 180, SELF), (Y, 3))
s2, why2 = A.judge(cfg=CFG, self_id=SELF, text="聊啥呢", now=1041.0,
                   last_bot_ts=1038.0, thread=A.thread_of("g1", X, 1041.0, 180, SELF))
check("闲聊里插话被压到0 → " + why2, s2, 0)

# 线程过期
check("线程 181 秒后过期", A.thread_of("g1", X, 1040.0 + 181, 180, SELF), ("", 0))

# 引用别人的话：to_uid 指向被引用的人
A.reset()
A.record(gid="g2", uid=ME, nick="lifefuck", text="他说得对", now=2000.0, self_id=SELF,
         reply_uid=X, cfg=CFG)
check("引用别人的 to_uid", A.table("g2")[-1].to_uid, X)

# 跨群隔离：g1 学到的别名不出现在 g2
check("别名不串群", A.names_of("g2", SELF), ())

print("\n失败 %d 个" % fail)
sys.exit(1 if fail else 0)
