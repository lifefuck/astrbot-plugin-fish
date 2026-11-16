"""谐音梗识别：图里的字认得出来，但「夺笋」是「多损」模型常看不出。

两条路：先查本地词表（字面），再按读音找（图上换了个写法也认）。
不调模型、不联网，一张图毫秒级。
"""

import re

try:
    from pypinyin import lazy_pinyin
except Exception:  # 没装就只按字面认，功能降级但不出错
    lazy_pinyin = None

_CJK = re.compile(r"[\u4e00-\u9fff]+")

# 谐音写法 → 本来的词。只收网上公开流传的写法，不收脏话：
# 收脏话等于把这个 bot 变成一个骂人词典，群里风气会跟着歪
TABLE = {
    "夺笋": "多损", "蚌埠住了": "绷不住了", "蚌埠": "绷不",
    "栓Q": "thank you", "我太南了": "我太难了", "南上加南": "难上加难",
    "蓝瘦香菇": "难受想哭", "雨女无瓜": "与你无关", "尊嘟假嘟": "真的假的",
    "泰裤辣": "太酷啦", "鸭梨山大": "压力山大", "鸭梨": "压力",
    "鸡不可失": "机不可失", "年年有鱼": "年年有余", "万柿如意": "万事如意",
    "柿柿如意": "事事如意", "苹安喜乐": "平安喜乐", "大桔大利": "大吉大利",
    "猪事顺利": "诸事顺利", "步步糕升": "步步高升", "耗子尾汁": "好自为之",
    "针不戳": "真不错", "铁汁": "铁子", "集美": "姐妹", "冲鸭": "冲呀",
    "达咩": "だめ（不行）", "芭比Q": "完蛋了", "爱豆": "idol",
    "520": "我爱你", "1314": "一生一世", "886": "拜拜了", "9494": "就是就是",
}

# 近音：拼音里这几种人在口语里根本分不出来，硬区分只会漏
_INITIAL = {"zh": "z", "ch": "c", "sh": "s", "n": "l", "f": "h", "r": "l"}
_FINAL = {"ang": "an", "eng": "en", "ing": "in", "iang": "ian", "uang": "uan"}


def _norm_py(term: str):
    """把一个词变成「不分声调、不分平翘舌、不分前后鼻音」的音节元组。"""
    if not lazy_pinyin:
        return None
    out = []
    for syl in lazy_pinyin(term):
        syl = syl.lower()
        m = re.match(r"^([a-z]+?)([aeiou].*)?$", syl)
        if not m:
            out.append(syl)
            continue
        ini, rest = m.group(1), (m.group(2) or "")
        ini = _INITIAL.get(ini, ini)
        rest = _FINAL.get(rest, rest)
        out.append(ini + rest)
    return tuple(out)


def _build_index():
    idx = {}
    for key, real in TABLE.items():
        # 两个字的词按读音找太容易撞（「鸭梨」和「压力」本来就是一个词）；
        # 三个字以上才走读音，学的是鱼力max那边的阈值
        if len(key) < 3:
            continue
        py = _norm_py(key)
        if py and py not in idx:
            idx[py] = (key, real)
    return idx


_INDEX = None


def note(text: str, limit: int = 2):
    """给看图结果补一条谐音线索；没线索返回空串。"""
    global _INDEX
    if not text:
        return ""
    hits = []
    matched = []
    # 1. 字面：图上直接写了谐音写法。长的优先，否则「蚌埠住了」会连带吐出「蚌埠」
    for key, real in sorted(TABLE.items(), key=lambda kv: -len(kv[0])):
        if key not in text or real in text:
            continue
        if any(key in m for m in matched):
            continue
        matched.append(key)
        hits.append(f"「{key}」是「{real}」的谐音")
    # 2. 读音：图上换了个写法，但读出来是那个词
    if _INDEX is None:
        _INDEX = _build_index()
    if _INDEX:
        for run in _CJK.findall(text):
            if len(run) < 3:
                continue
            for size in range(3, min(5, len(run)) + 1):
                for i in range(len(run) - size + 1):
                    seg = run[i:i + size]
                    if seg in TABLE:
                        continue
                    py = _norm_py(seg)
                    if not py:
                        continue
                    got = _INDEX.get(py)
                    if not got:
                        continue
                    key, real = got
                    if real in text or seg == key:
                        continue
                    hits.append(f"「{seg}」读作 {' '.join(py)}，可能是「{real}」的谐音")
                    if len(hits) >= limit:
                        return "；".join(hits[:limit])
    return "；".join(hits[:limit])
