#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆出处单测：来源判定、话题提取、改述、可引用性矩阵。

跑法：python3 scripts/test_memory_source.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"

# 必须在 import 之前指到临时库，否则模块顶层的单例会就地建库
_TMP = tempfile.mkdtemp(prefix="fishmem-src-")
os.environ["FISH_MEMORY_DB"] = os.path.join(_TMP, "m.db")

FAIL = []


def ok(name, cond, got=""):
    if cond:
        print("ok", name)
    else:
        print("FAIL", name, "|", got)
        FAIL.append(name)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


FM = _load("fish_memory", CORE / "fish_memory.py")

# ---- 来源判定 ----
ok("1 自称算本人说的", FM.classify_source("我养了两只猫") == "said",
   FM.classify_source("我养了两只猫"))
ok("2 听说算转述", FM.classify_source("听说他最近换工作了") == "reported",
   FM.classify_source("听说他最近换工作了"))
ok("3 提第三人算听来的",
   FM.classify_source("他妈说他最近老加班") == "heard",
   FM.classify_source("他妈说他最近老加班"))
ok("4 我听说优先判转述（不是自述）",
   FM.classify_source("我听说这家店关门了") == "reported",
   FM.classify_source("我听说这家店关门了"))

# ---- 话题 ----
tp = FM.topics_of("我养了两只猫，一只叫豆豆一只叫团子")
ok("5 话题最多三个词", len(tp.split(",")) <= 3 and tp != "", tp)
ok("6 空串没有话题", FM.topics_of("") == "", FM.topics_of(""))

# ---- 改述 ----
ok("7 改述翻转人称", "我" not in FM.restate("我养了两只猫") and "你养了两只猫" in FM.restate("我养了两只猫"),
   FM.restate("我养了两只猫"))
ok("8 我们→你们", FM.restate("我们明天去爬山").startswith("你们"),
   FM.restate("我们明天去爬山"))

# ---- 可引用性 ----
_Q_HIT = "我猫几只来着"     # 跟「养猫」沾边
_Q_MISS = "今天股票怎么样"  # 一点不沾
_cases = [
    ("9 亲口+有锚点可主动提", {"source_type": "said", "confidence": 0.9, "anchor_text": "我养了两只猫"}, "active", _Q_MISS, True),
    ("10 无锚点不可主动提", {"source_type": "said", "confidence": 0.9, "anchor_text": ""}, "active", _Q_MISS, False),
    ("11 低置信不可主动提", {"source_type": "said", "confidence": 0.4, "anchor_text": "x"}, "active", _Q_MISS, False),
    ("12 旧记录不可主动提", {"source_type": "said_legacy", "confidence": 0.9, "anchor_text": "x"}, "active", _Q_HIT, False),
    ("13 鱼自己的印象不可主动提", {"source_type": "inferred", "confidence": 0.9, "anchor_text": "x"}, "active", _Q_HIT, False),
    ("14 转述有权主动提", {"source_type": "reported", "confidence": 0.5, "anchor_text": "x"}, "active", _Q_MISS, True),
    ("15 旧记录话题重合可被动引用",
     {"source_type": "said_legacy", "confidence": 0.6, "anchor_text": "x", "content": "我养了两只猫",
      "topic": "养猫,两只"}, "passive", _Q_HIT, True),
    ("16 旧记录话题不沾不得被动引用",
     {"source_type": "said_legacy", "confidence": 0.6, "anchor_text": "x", "content": "我养了两只猫",
      "topic": "养猫,两只"}, "passive", _Q_MISS, False),
    ("17 印象原子话题碰上也只被动",
     {"source_type": "inferred", "confidence": 0.9, "anchor_text": "x", "content": "他喜欢喝冰美式",
      "topic": "冰美,美式"}, "active", _Q_HIT, False),
]
for name, item, mode, q, want in _cases:
    ok(name, FM.is_quotable(item, q, mode) == want, FM.is_quotable(item, q, mode))

# ---- 记忆行渲染 ----
_rec = [{"id": "a", "content": "我养了两只猫，一只叫豆豆", "type": "fact",
         "source_type": "said", "confidence": 0.9, "anchor_text": "我养了两只猫",
         "topic": "养猫"}]
_line = FM.shape_recalled(_rec, nick="甲", text="我今天喂猫", fmt_when=lambda t: "昨天", limit=2)
ok("18 行里带来源", bool(_line) and "他自己说的" in _line[0], _line)
ok("19 行里带动作提示", bool(_line) and "——" in _line[0], _line)

_low = [dict(_rec[0], confidence=0.45)]
_line2 = FM.shape_recalled(_low, nick="甲", text="我今天喂猫", fmt_when=lambda t: "昨天",
                           limit=2, mode="passive")
ok("20 低置信度必须说自己不确定", bool(_line2) and "我记得好像" in _line2[0], _line2)

_legacy = [dict(_rec[0], source_type="said_legacy", confidence=0.6)]
_line3 = FM.shape_recalled(_legacy, nick="甲", text="我今天喂猫", fmt_when=lambda t: "昨天",
                           limit=2, mode="active")
ok("21 旧记录在主动模式下不出行", _line3 == [], _line3)

print("记忆出处 %d 例：%d 通过 / %d 失败" % (21, 21 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
