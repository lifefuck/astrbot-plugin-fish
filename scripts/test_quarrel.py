#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骂战分级单测：重点是「没有指向的阴阳词不许升级」。

跑法：python3 scripts/test_quarrel.py
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"

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


Q = _load("quarrel", CORE / "quarrel.py")

# 1 群里在笑：同一个人连发两条，第二条只是「笑死」，没提鱼一个字
ok("1 连发「笑死」不算阴阳",
   Q.classify("笑死", consecutive=True) == 0,
   Q.classify("笑死", consecutive=True))

# 2 带了「你」就是冲着人来的
ok("2 笑死+你还算二档",
   Q.classify("笑死 你这不行", consecutive=True) == 2,
   Q.classify("笑死 你这不行", consecutive=True))

# 3 不带 @ 但点着鱼的名
ok("3 臭鱼一条算二档",
   Q.classify("臭鱼一条", consecutive=False) == 2,
   Q.classify("臭鱼一条", consecutive=False))

# 4 @鱼 骂脏话才是三档
ok("4 @鱼脏话三档",
   Q.classify("傻逼", at_me=True) == 3,
   Q.classify("傻逼", at_me=True))

# 5 邀架必须接
ok("5 邀架三档",
   Q.classify("来骂一架敢不敢", at_me=True) == 3,
   Q.classify("来骂一架敢不敢", at_me=True))

# 6 别人互骂不关鱼的事
ok("6 旁人互骂不接",
   Q.classify("你就是个傻逼", at_me=False, reply_to_me=False) == 0,
   Q.classify("你就是个傻逼", at_me=False, reply_to_me=False))

print("骂战分级 %d 例：%d 通过 / %d 失败" % (6, 6 - len(FAIL), len(FAIL)))
sys.exit(1 if FAIL else 0)
