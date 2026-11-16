#!/usr/bin/env python3
"""从真实群聊里挖「群里自己的黑话」，落成 group_slang.json。

为什么不靠热搜榜：热搜榜给的是「BLG 夺冠」「APEC 为何重要」这种新闻标题，
群里根本没人当梗用。真正天天出现的是本群自己循环的短词——游戏角色名、
战术黑话、群友外号。这些词在 jieba 默认词典里查不到，正好是「词库外词」，
切出来按词频、按出现人数一筛就是黑话候选。

用法：
    python3 scripts/mine_group_slang.py            # 只看结果
    python3 scripts/mine_group_slang.py --apply    # 写盘
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PLUGIN_DIR / "data"
DB = DATA_DIR / "fish_memory.db"
OUT = DATA_DIR / "group_slang.json"

# 黑话长度：1 个字的不要（全是「啊」「草」），超过 8 个字基本是句子片段
MIN_LEN, MAX_LEN = 2, 6
# 至少几个人说过：一个人刷屏刷出来的词不是群黑话
MIN_USERS = 2
# 至少出现几次
MIN_COUNT = 3

# 纯功能性词，看着像黑话但接不住
# 以这些字开头的组合十有八九是句子碎片而不是词
_BAD_HEAD = set("我你他她它这那有是全就要先哪找换戴看给不会能想说做去来上下里个把被在和也还又很最太更都只才再让对跟从向为")

STOP = {
    "什么", "这个", "那个", "就是", "不是", "可以", "知道", "怎么", "现在", "已经",
    "没有", "一个", "我也", "你这", "他那", "真的", "好像", "应该", "感觉", "有点",
    "哈哈", "哈哈哈", "呵呵", "笑死", "确实", "然后", "所以", "但是", "因为", "如果",
    "我们", "你们", "他们", "自己", "东西", "时候", "地方", "问题", "事情", "一会儿",
    # 平台名/常见搭配：出现频率够但不是梗，注进去只会占位置
    "群里", "群名", "群里", "哔哩", "微信", "推特", "图中", "面露", "整了", "打个",
    "发个", "中是", "锅开", "楼里", "上个", "下个",
}


def load_rows(limit_days: int = 90):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    since = time.time() - limit_days * 86400
    rows = conn.execute(
        "SELECT group_id, person_id, user_text FROM fish_turn_logs "
        "WHERE ts >= ? AND user_text <> '' ORDER BY ts DESC LIMIT 200000", (since,)
    ).fetchall()
    conn.close()
    return rows


def mine(rows):
    try:
        import jieba
    except ImportError:
        print("需要 jieba：pip install jieba（AstrBot 的 venv 里已有）", file=sys.stderr)
        raise

    jieba.setLogLevel(60)
    freq = defaultdict(Counter)      # gid -> word -> count
    users = defaultdict(lambda: defaultdict(set))

    for gid, uid, text in rows:
        # 转发、命令、长段落、纯表情都别进来
        clean = re.sub(r"\[[^\]]*\]", " ", text or "")
        if not clean.strip() or clean.startswith("/") or len(clean) > 60:
            continue
        gk = str(gid)
        for w in jieba.cut(clean):
            w = w.strip()
            if not (MIN_LEN <= len(w) <= MAX_LEN):
                continue
            if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]+", w):
                continue
            if w in STOP or w.isdigit():
                continue
            # 纯英文/纯数字是 AI、QQ、token 这类工具词，不是群黑话
            if re.fullmatch(r"[A-Za-z0-9]+", w):
                continue
            # 以虚词/指示词/常用动词开头的双字组合，基本都是切词碎片：
            # 「我要」「有个」「这是」「换个」「戴着」——看着像词，其实是半句
            if w[0] in _BAD_HEAD:
                continue
            # 词典里有的词不是黑话；jieba 的 FREQ 里 freq==0 才是词库外词。
            # 英文/数字串 FREQ 里查不到，单独靠「非常见词」判断
            if jieba.dt.FREQ.get(w, 0) > 0:
                continue
            freq[gk][w] += 1
            users[gk][w].add(uid)

    result = {}
    for gid, counter in freq.items():
        picked = []
        for w, n in counter.most_common(200):
            if n < MIN_COUNT or len(users[gid][w]) < MIN_USERS:
                continue
            # 被更长的同前缀词以高得多的频次覆盖时，短的往往是它的碎片
            if any(w != o and w in o and counter[o] >= n for o in counter if len(o) == len(w) + 1):
                continue
            picked.append({"w": w, "n": n, "u": len(users[gid][w])})
            if len(picked) >= 40:
                break
        if picked:
            result[gid] = picked
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写盘 group_slang.json")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    rows = load_rows(args.days)
    print(f"近 {args.days} 天消息 {len(rows)} 条")
    slang = mine(rows)
    for gid, items in sorted(slang.items(), key=lambda kv: -sum(i["n"] for i in kv[1]))[:8]:
        print(f"  群 {gid}: " + "、".join(f"{i['w']}({i['n']})" for i in items[:18]))

    if args.apply:
        payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "groups": slang}
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        total = sum(len(v) for v in slang.values())
        print(f"已写盘 {OUT}：{len(slang)} 个群、{total} 个词")
    else:
        print("（dry-run，加 --apply 才写盘）")


if __name__ == "__main__":
    main()
