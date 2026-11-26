#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""毛边层 + 心流状态机（对照 dsh-huoren 的逻辑，按 fish 的出口重写，不是照抄代码）。

两块东西：

1. 毛边：错别字 + 自我更正。一次回复最多一个错字，而且必须补一句「打错了，是X」，
   否则就是单纯的错别字，不是手滑。技术名词、数字、英文、括号内容一律不碰。

2. 心流：energy / warmth 两个数按会话落盘，被催、被骂、深夜会掉，被谢会涨，
   每轮向基线（60/55）回落 6%，最后压成提示词里一句话。
   这跟 fish 原有的 affect（心境/好感）不是一回事：affect 记「关系」，这里记
   「这一轮的状态」——昨晚被骂过，今天早上还带点冷淡，是这个模块要的效果。

dsh-huoren 的 fillers（说白了/说实话）和 particles（吧/呗/啊）在 fish 里默认关闭：
fish 的 GUARD 明确禁「说白了/先说结论」这类起手式，句尾挂「～哦啦呀」也是被点名
的 AI 腔。留着开关但默认 0，别一边禁一边加。
"""
import json
import os
import random
import re

# ---------------- 错别字 ----------------

# 只收「拼音输入法真会打错、且错得不影响理解」的常用词。
# 技术名词、专有名词一律不收——错在代码或配置里就不是人味了，是事故。
TYPO_PAIRS = (
    ("应该", "因该"), ("已经", "以经"), ("现在", "现再"), ("知道", "之道"),
    ("觉得", "决得"), ("因为", "因位"), ("所以", "所已"), ("怎么", "怎末"),
    ("真的", "真得"), ("时候", "时侯"), ("事情", "事晴"), ("问题", "问提"),
    ("可以", "可一"), ("需要", "须要"), ("一下", "以下"), ("然后", "然厚"),
    ("我们", "我门"), ("他们", "他门"), ("你们", "你门"), ("还有", "孩有"),
    ("没有", "没由"), ("开始", "开使"), ("直接", "直结"), ("确认", "确人"),
    ("修改", "修该"), ("文件", "文见"), ("结果", "结过"), ("发现", "发先"),
    ("处理", "处里"), ("建议", "建意"), ("感觉", "感决"), ("好像", "好想"),
    ("那边", "哪边"), ("这里", "这李"), ("等会", "等回"), ("稍等", "少等"),
    ("完全", "完权"), ("一直", "一值"), ("其实", "其是"), ("这样", "这养"),
)

# 自我更正的句式，随机挑一个；{X} 是本来要打的词
CORRECTION_TPL = (
    "打错了，是{X}",
    "啊不对，{X}",
    "{X}，我刚打错了",
    "手滑，{X}",
)

# 不碰的区域：数字/英文/代码/引号里的原话。错字落进去就是事故
_PROTECT_RE = re.compile(r"(?:https?://\S+|`[^`]*`|[A-Za-z0-9_]{2,}|「[^」]*」|\"[^\"]*\"|\[[^\]]*\])")


def _plain_spans(text):
    """返回 (是否可改写, 片段) 列表，把受保护的片段标出来。"""
    out, pos = [], 0
    for m in _PROTECT_RE.finditer(text):
        if m.start() > pos:
            out.append((True, text[pos:m.start()]))
        out.append((False, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((True, text[pos:]))
    return out


def inject_typo(text, rng=None):
    """在最后一条上造一个错别字。

    返回 (新文本, typo) —— typo 是 (打错的词, 本来的词) 或 None。
    太短的句子不动：一句话里塞错字，读起来像坏了不像手滑。
    """
    rng = rng or random
    t = str(text or "")
    if len(t) < 12:
        return t, None
    spans = _plain_spans(t)
    cands = []
    for i, (ok, seg) in enumerate(spans):
        if not ok:
            continue
        for right, wrong in TYPO_PAIRS:
            start = 0
            while True:
                at = seg.find(right, start)
                if at < 0:
                    break
                cands.append((i, at, right, wrong))
                start = at + len(right)
    if not cands:
        return t, None
    i, at, right, wrong = rng.choice(cands)
    _, seg = spans[i]
    spans[i] = (True, seg[:at] + wrong + seg[at + len(right):])
    return "".join(s for _, s in spans), (wrong, right)


def make_correction(typo, rng=None):
    """自我更正那种「补一句」的短消息；没有 typo 就没有。"""
    if not typo:
        return None
    rng = rng or random
    return rng.choice(CORRECTION_TPL).format(X=typo[1])


def humanize_parts(parts, typo_rate=0.06, correction_rate=0.5, rng=None):
    """对已分好条的回复做毛边。

    条数被 fish 的出口卡在 2 条以内，所以只在「单条」时造错字：
    拆成两条了还补第三条自我更正，就碎成三截了，比不错字更假。
    返回 (parts, correction)。
    """
    rng = rng or random
    if not parts:
        return parts, None
    if len(parts) != 1:
        return parts, None
    if typo_rate <= 0 or rng.random() >= typo_rate:
        return parts, None
    text, typo = inject_typo(parts[0], rng)
    if not typo:
        return parts, None
    corr = make_correction(typo, rng) if rng.random() < correction_rate else None
    return [text], corr


# ---------------- 心流 ----------------

BASE_ENERGY = 60
BASE_WARMTH = 55
DECAY = 0.06

# 一句话信号：被催、被谢、被骂，各扣各的
_RE_URGE = re.compile(r"(?:快点|怎么还没|在吗|急)")
_RE_THANKS = re.compile(r"(?:谢谢|辛苦|麻烦你|拜托|感谢)")
# 骂人信号必须指向「你/鱼」才算，裸的「傻」「垃圾」会误伤游戏吐槽和「垃圾佬」；
# 「傻鱼」「大肥鱼」是调戏不是骂，所以只认「你…傻/蠢/笨/废物」这类明确指向
_RE_ABUSE = re.compile(
    r"(?:傻逼|煞笔|SB|sb|滚|闭嘴"
    r"|(?:你|鱼|它)(?:真|就是|是个|这个)?(?:傻|蠢|笨|废物|垃圾))"
)


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def read_signal(text):
    """对方这句话让状态怎么变（数值照 dsh-huoren 的 readSignal）。"""
    d = {"energy": 0, "warmth": 0}
    t = str(text or "")
    if not t.strip():
        return d
    n = len(t)
    bangs = t.count("!") + t.count("！")
    questions = t.count("?") + t.count("？")
    if n > 300:
        d["warmth"] += 3
    if bangs >= 2:
        d["energy"] -= 2
        d["warmth"] -= 1
    if questions >= 2:
        d["energy"] += 1
    if _RE_URGE.search(t):
        d["energy"] -= 3
        d["warmth"] -= 2
    if _RE_THANKS.search(t):
        d["warmth"] += 3
    if _RE_ABUSE.search(t):
        d["warmth"] -= 8
        d["energy"] -= 2
    if not re.search(r"[。！？!?…]", t) and n < 8:
        d["energy"] += 1
    return d


def advance(state, text="", hour=None, now=None):
    """推进一轮：先吃这句话的信号，再深夜扣精力，最后向基线回落。"""
    import time as _t
    now = now or _t.time()
    if hour is None:
        hour = int(_t.strftime("%H", _t.localtime(now)))
    st = dict(state or {})
    energy = float(st.get("energy", BASE_ENERGY))
    warmth = float(st.get("warmth", BASE_WARMTH))

    d = read_signal(text)
    energy += d["energy"]
    warmth += d["warmth"]

    # 深夜和清早都掉精力，凌晨最狠
    if hour >= 23 or hour < 6:
        energy -= 4
    elif 6 <= hour < 8:
        energy -= 2

    # 向基线回落：状态不会永久卡住，但也散得很慢
    energy += (BASE_ENERGY - energy) * DECAY
    warmth += (BASE_WARMTH - warmth) * DECAY

    return {
        "energy": round(_clamp(energy), 1),
        "warmth": round(_clamp(warmth), 1),
        "turns": int(st.get("turns", 0)) + 1,
        "last_at": int(now),
    }


def describe(state, hour=None):
    """状态 → 提示词里的一句话。没有明显状态就返回空串，别白占 token。"""
    import time as _t
    if hour is None:
        hour = int(_t.strftime("%H"))
    energy = float((state or {}).get("energy", BASE_ENERGY))
    warmth = float((state or {}).get("warmth", BASE_WARMTH))
    bits = []
    if energy < 32:
        bits.append("你现在有点累，回话短，别铺垫，能一句说完就一句")
    elif energy > 74:
        bits.append("你现在来劲，愿意多说两句，也会主动补一句自己的看法")
    if warmth < 40:
        bits.append("你现在有点冷淡，不用刻意热情，也别解释太多")
    elif warmth > 72:
        bits.append("你现在跟对方还算熟，语气可以再随意一点")
    if 0 <= hour < 6:
        bits.append("现在是凌晨，你困，说话更短更懒")
    elif hour == 23:
        bits.append("已经很晚了，你有点撑不住，句子会变短")
    return "；".join(bits)


# ---------------- 落盘 ----------------

def _path(data_dir, key):
    safe = re.sub(r"[^\w.-]", "_", str(key or "default"))[:80]
    return os.path.join(data_dir, "huoren_mood-%s.json" % safe)


def load(data_dir, key):
    try:
        with open(_path(data_dir, key), encoding="utf-8") as f:
            d = json.load(f)
        return {
            "energy": _clamp(float(d.get("energy", BASE_ENERGY))),
            "warmth": _clamp(float(d.get("warmth", BASE_WARMTH))),
            "turns": max(0, int(d.get("turns", 0))),
            "last_at": int(d.get("last_at", 0)),
        }
    except Exception:
        return {"energy": BASE_ENERGY, "warmth": BASE_WARMTH, "turns": 0, "last_at": 0}


def save(data_dir, key, state):
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(_path(data_dir, key), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass  # 状态存不下不该让聊天挂掉
