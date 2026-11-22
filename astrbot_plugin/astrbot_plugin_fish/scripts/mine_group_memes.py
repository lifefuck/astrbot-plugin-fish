#!/usr/bin/env python3
"""从 4 个真实群的聊天记录里挖「本群自己的梗」，落成 data/group_memes.json。

两样东西，全部来自群里真实出现过的话，一个字都不编：
  1. words   —— 群黑话（jieba 词库外词，至少 2 个人说过、出现 3 次以上）
  2. phrases —— 群里反复出现的那几句（同一句被不同人刷过 ≥3 次），
                也就是「之前出过圈的话」，接话时顺着这个口径说才对味

为什么不靠 LLM 生成释义：编出来的梗一接就穿帮，群里会立刻听出不对。
这里只做频次统计，梗怎么用交给模型自己判断。

用法：
    python3 scripts/mine_group_memes.py                # 只看
    python3 scripts/mine_group_memes.py --apply        # 写盘
    python3 scripts/mine_group_memes.py --db xxx.db    # 指定库（默认用线上库）
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
DEFAULT_DB = Path.home() / "projects/astrbot/data/plugins/astrbot_plugin_fish/data/fish_memory.db"
OUT = PLUGIN_DIR / "data" / "group_memes.json"

MIN_LEN, MAX_LEN = 2, 6
MIN_USERS, MIN_COUNT = 2, 3
PHRASE_MIN, PHRASE_MAX = 3, 12
PHRASE_MIN_COUNT, PHRASE_MIN_USERS = 3, 2

_BAD_HEAD = set("我你他她它这那有是全就要先哪找换戴看给不会能想说做去来上下里个把被在和也还又很最太更都只才再让对跟从向为")

STOP = {
    "什么", "这个", "那个", "就是", "不是", "可以", "知道", "怎么", "现在", "已经",
    "没有", "一个", "我也", "你这", "他那", "真的", "好像", "应该", "感觉", "有点",
    "哈哈", "哈哈哈", "呵呵", "笑死", "确实", "然后", "所以", "但是", "因为", "如果",
    "我们", "你们", "他们", "自己", "东西", "时候", "地方", "问题", "事情", "一会儿",
    "群里", "群名", "哔哩", "微信", "推特", "图中", "面露", "整了", "打个",
    "发个", "中是", "锅开", "楼里", "上个", "下个",
}

# 梗句里不要的：纯表情、指令、链接、以及「戳了戳小鲸鱼」这类系统提示
_JUNK_LINE = re.compile(r"^[\[（(>]|^[/#*%]|https?://|\.(?:com|cn|net)\b|小鲸鱼")
# 命令回显/求助句：真实出现过，但是指令不是梗，收进去只会让鱼背命令
_JUNK_WORDS = ("帮我", "搜索", "联网", "关闭", "开启", "欢迎", "签到", "签到", "指令", "命令",
               "今日老婆", "今日老公", "自我介绍", "面板", "帮助", "绑定", "怎么", "为什么", "是不是")
# 泛应答：人人都会说，不算本群的梗
_GENERIC = {"不知道", "哈哈哈", "笑死我了", "你好啊", "我也是", "真的假的", "确实", "好的", "在吗"}


def load_rows(db: Path, limit_days: int):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    since = time.time() - limit_days * 86400
    rows = conn.execute(
        "SELECT group_id, person_id, user_text FROM fish_turn_logs "
        "WHERE ts >= ? AND user_text <> '' ORDER BY ts DESC LIMIT 200000", (since,)
    ).fetchall()
    conn.close()
    return rows


def _clean(text: str) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", text or "")   # [图片]/[CQ:...]
    t = re.sub(r"@\S+", " ", t)                   # @某人
    t = re.sub(r"\s+", " ", t).strip()
    return t


def mine_words(rows):
    """群黑话：jieba 词库外的词，按「出现次数 + 说过的人数」筛。"""
    try:
        import jieba
    except ImportError:
        print("需要 jieba（AstrBot 的 venv 里有）：用 ~/projects/astrbot/.venv/bin/python 跑", file=sys.stderr)
        raise
    jieba.setLogLevel(60)

    freq = defaultdict(Counter)
    users = defaultdict(lambda: defaultdict(set))
    for gid, uid, text in rows:
        clean = _clean(text)
        if not clean or len(clean) > 60:
            continue
        gk = str(gid)
        for w in jieba.cut(clean):
            w = w.strip()
            if not (MIN_LEN <= len(w) <= MAX_LEN) or w in STOP or w.isdigit():
                continue
            if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]+", w):
                continue
            if re.fullmatch(r"[A-Za-z0-9]+", w):      # 纯英文数字是工具词
                continue
            if w[0] in _BAD_HEAD:                     # 切词碎片
                continue
            if jieba.dt.FREQ.get(w, 0) > 0:           # 词典里有的不算黑话
                continue
            freq[gk][w] += 1
            users[gk][w].add(uid)

    out = {}
    for gid, counter in freq.items():
        picked = []
        for w, n in counter.most_common(200):
            if n < MIN_COUNT or len(users[gid][w]) < MIN_USERS:
                continue
            if any(w != o and w in o and counter[o] >= n for o in counter if len(o) == len(w) + 1):
                continue
            picked.append({"w": w, "n": n, "u": len(users[gid][w])})
            if len(picked) >= 40:
                break
        if picked:
            out[gid] = picked
    return out


def mine_phrases(rows):
    """出圈话：同一句短话被不同人反复刷过。只统计，不加工。"""
    count = defaultdict(Counter)
    users = defaultdict(lambda: defaultdict(set))
    for gid, uid, text in rows:
        clean = _clean(text)
        if not (PHRASE_MIN <= len(clean) <= PHRASE_MAX):
            continue
        if _JUNK_LINE.search(clean):
            continue
        if any(j in clean for j in _JUNK_WORDS) or clean in _GENERIC:
            continue
        if clean.startswith("戳了戳") or "小鲸鱼" in clean:   # 戳一戳是系统提示，不是群友的梗
            continue
        if not re.search(r"[\u4e00-\u9fa5]", clean):   # 全是标点的不要
            continue
        gk = str(gid)
        count[gk][clean] += 1
        users[gk][clean].add(uid)

    out = {}
    for gid, counter in count.items():
        picked = []
        for t, n in counter.most_common(100):
            if n < PHRASE_MIN_COUNT or len(users[gid][t]) < PHRASE_MIN_USERS:
                continue
            picked.append({"t": t, "n": n, "u": len(users[gid][t])})
            if len(picked) >= 20:
                break
        if picked:
            out[gid] = picked
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        sys.exit(f"库不存在：{db}")
    rows = load_rows(db, args.days)
    print(f"近 {args.days} 天 {len(rows)} 条消息，库 {db}")

    words = mine_words(rows)
    phrases = mine_phrases(rows)

    groups = {}
    for gid in sorted(set(words) | set(phrases)):
        groups[gid] = {"words": words.get(gid, []), "phrases": phrases.get(gid, [])}
        print(f"  群 {gid}: {len(groups[gid]['words'])} 词 / {len(groups[gid]['phrases'])} 句")
        print("    词：" + "、".join(i["w"] for i in groups[gid]["words"][:14]))
        print("    句：" + " | ".join(f"{i['t']}({i['n']})" for i in groups[gid]["phrases"][:8]))

    if args.apply:
        payload = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_messages": len(rows),
            "groups": groups,
        }
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已写盘 {OUT}")
    else:
        print("（dry-run，加 --apply 才写盘）")


if __name__ == "__main__":
    main()
