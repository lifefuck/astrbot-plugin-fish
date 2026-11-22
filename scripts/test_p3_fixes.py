#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 回归：审计索引 §5 三批 22 条里能脱离框架断言的条目。

跑法：python3 scripts/test_p3_fixes.py

不在本文件覆盖的（只能靠线上/脚本自查）：
- A05 私聊记账键：靠 main.py 文本断言（私聊已不进记忆写入分支）
- A29 迁移幂等：靠源码断言（SELECT 里必须排除已迁移行）
- A55/A56/A57/A59/A60：运维脚本，靠文本断言 + 手工演练
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PKG = HERE / "astrbot_plugin" / "astrbot_plugin_fish"
FAIL = []


def ok(name, cond, got=""):
    if cond:
        print("ok", name)
    else:
        print("FAIL", name, "|", got)
        FAIL.append(name)


def _install_stub():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    api = mod("astrbot.api")
    api.__path__ = []
    api.logger = __import__("logging").getLogger("stub")
    web = mod("astrbot.api.web")
    web.request = types.SimpleNamespace()
    web.response = types.SimpleNamespace()
    api.web = web

    ev = mod("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class _Chain:
        pass

    ev.AstrMessageEvent = AstrMessageEvent
    ev.MessageChain = _Chain
    ev.MessageEventResult = _Chain

    class _Filter:
        @staticmethod
        def command(*a, **k):
            return lambda f: f

        @staticmethod
        def event_message_type(*a, **k):
            return lambda f: f

    ev.filter = _Filter
    api.event = ev

    flt = mod("astrbot.api.event.filter")

    class EventMessageType:
        GROUP_MESSAGE = 1
        ALL = 2

    flt.EventMessageType = EventMessageType

    star = mod("astrbot.api.star")
    star.Context = object
    star.Star = object
    star.register = lambda *a, **k: (lambda c: c)
    api.star = star

    agent = mod("astrbot.core.agent")
    tool = mod("astrbot.core.agent.tool")
    tool.ToolSet = type("ToolSet", (), {"__init__": lambda self, *a, **k: None})
    tool.FunctionTool = type("FunctionTool", (), {"__init__": lambda self, *a, **k: None})
    agent.tool = tool

    comps = mod("astrbot.core.message.components")
    for n in ("At", "AtAll", "Image", "Plain", "Poke", "Reply"):
        setattr(comps, n, type(n, (), {}))
    mod("astrbot.core")
    mod("astrbot.core.message").components = comps


class _AiohttpStub(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return type(item, (), {})


sys.modules.setdefault("aiohttp", _AiohttpStub("aiohttp"))

_TMP = tempfile.mkdtemp(prefix="fish-p3-")
# 必须在 import 之前设：fish_memory 顶层单例会在仓库里就地建库（已踩过两次）
os.environ["FISH_MEMORY_DB"] = os.path.join(_TMP, "mem.db")

_install_stub()
sys.path.insert(0, str(HERE / "astrbot_plugin"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PKG / "fish_core" / filename)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


GEN = _load("t3_gen", "generator.py")
MEM = _load("t3_mem", "fish_memory.py")
ADM = _load("t3_adm", "admin_actions.py")
MW = _load("t3_mw", "meme_wiki.py")
WS = _load("t3_ws", "websearch.py")

# runtime.py 用的是相对 import（from .kernel import *），扁平加载会炸，
# 这里按包名正常导入一份：插件目录已经进 sys.path
import importlib

RT = importlib.import_module("astrbot_plugin_fish.fish_core.runtime")


def _fmt_when(ts):
    return ""


# ---------- A03 异常回滚必须带 token ----------
_RT = next(v for v in vars(RT).values()
           if isinstance(v, type) and hasattr(v, "_claim") and hasattr(v, "GroupState") is False)
_st = RT.GroupState()
_t1 = _RT._claim(_st, 100.0)
_t2 = _RT._claim(_st, 200.0)
_RT._release_claim(_st, _t1)          # 上一轮的 token，不该动本轮
ok("A03 过期 token 不能回滚别人的占位",
   _st.claiming is True and _st.claim_owner == _t2, (_st.claiming, _st.claim_owner, _t2))
_RT._release_claim(_st, _t2)
ok("A03 持有者能正常回滚",
   _st.claiming is False and abs(_st.last_bot_reply_ts - 100.0) < 1e-9,
   (_st.claiming, _st.last_bot_reply_ts))

# ---------- A04 打字期间被聊开，回给人的草稿不能丢 ----------
_dlg = [{"uid": "u1", "ts": 0.5, "text": "哈哈"},
        {"uid": "u2", "ts": 0.6, "text": "笑死"},
        {"uid": "u3", "ts": 0.7, "text": "确实"}]
_d_hi = GEN.Draft(text="嗯嗯在呢", ts=0.0, ver=1, priority=2, uid="u9")
_act, _why = GEN.revalidate(_d_hi, now=10.0, ver=2, dialogue=_dlg, names=())
ok("A04 回给人的草稿照发", _act == GEN.SEND, (_act, _why))
_d_lo = GEN.Draft(text="随便说句", ts=0.0, ver=1, priority=0, uid="u9")
_act2, _why2 = GEN.revalidate(_d_lo, now=10.0, ver=2, dialogue=_dlg, names=())
ok("A04 低优先级草稿照旧作废", _act2 == GEN.ABANDONED, (_act2, _why2))

# ---------- A25 短句不再被当碎片 ----------
_short = [{"content": "我到了", "source_type": "said", "confidence": 0.9,
           "anchor_text": "x", "created_at": 1, "type": "said"}]
_lines = MEM.shape_recalled(_short, nick="A", text="在吗", fmt_when=_fmt_when, limit=3)
# 断言"条目被保留"而不是"原文照抄"：restate() 会做人称转述（我→你）
ok("A25 三字完整短句不再被砍", len(_lines) == 1 and "到了" in _lines[0], _lines)

# ---------- A27 子串不再算重复 ----------
_two = [{"content": "明天见吧兄弟", "source_type": "said", "confidence": 0.9,
         "anchor_text": "x", "created_at": 1, "type": "said"},
        {"content": "那我们明天见吧兄弟好不好", "source_type": "said", "confidence": 0.9,
         "anchor_text": "x", "created_at": 2, "type": "said"}]
_l2 = MEM.shape_recalled(_two, nick="A", text="在吗", fmt_when=_fmt_when, limit=3)
ok("A27 真子串（非前后缀）两条都留", len(_l2) == 2, _l2)

# ---------- A26 置信度门槛接上配置 ----------
_item = {"content": "他住在杭州", "source_type": "said", "confidence": 0.6,
         "anchor_text": "x", "created_at": 1, "type": "said"}
ok("A26 门槛 0.9 时拦住 0.6", MEM.is_quotable(_item, "", "active", min_conf=0.9) is False)
ok("A26 默认门槛 0.5 照旧放行", MEM.is_quotable(_item, "", "active") is True)

# ---------- A15 同名不静默挑人 ----------
_uid, _nick = ADM.resolve_target("小明太吵了", {"111": "小明", "222": "小明"}, [])
ok("A15 两个同名要求 @ 明确", _uid == ADM.AMBIGUOUS, (_uid, _nick))
_uid2, _ = ADM.resolve_target("小明太吵了", {"111": "小明"}, [])
ok("A15 唯一命中照常返回", _uid2 == "111", _uid2)

# ---------- A17 超额/冷却且无缓存不崩 ----------
MW._state["count"] = MW.DAILY_CAP
try:
    _r = asyncio.run(MW.lookup("从没查过的词xyz", use_network=False))
    ok("A17 超额且无缓存返回 None", _r is None, _r)
except AttributeError as e:
    ok("A17 超额且无缓存返回 None", False, "AttributeError: %s" % e)
MW._state["count"] = 0

# ---------- A35 三引擎并发，不再串行累加超时 ----------
async def _slow(*a, **k):
    await asyncio.sleep(5)
    return []


WS._duckduckgo = _slow
WS._baidu = _slow
WS._TOTAL_BUDGET = 0.4
_t0 = time.time()
_rows = asyncio.run(WS.search(None, "测试查询", 3))
_dt = time.time() - _t0
ok("A35 三个 6s 引擎并发后按总预算收口", _rows == [] and _dt < 1.5, round(_dt, 2))

# ---------- A05 / A18 / A57 已修项的回归钉子 ----------
_MAIN = (PKG / "main.py").read_text(encoding="utf-8")
_RESP = (PKG / "fish_core" / "respond.py").read_text(encoding="utf-8")
_CMD = (PKG / "fish_core" / "commands.py").read_text(encoding="utf-8")
ok("A05 私聊不进记忆写入分支", 'acct.get("gid_real") != "private"' in _RESP)
ok("A18 私聊 /off 有回应", "静默只能在群里发" in _CMD)
# A57 的脚本在 NapCat 那台机器上，CI 里拿不到：用 FISH_NAPCAT_LOOP 指过去才检查。
# 只验「非回环地址排在 127.0.0.1 之前」这个顺序，不写死具体内网 IP
_NL = Path(os.environ.get("FISH_NAPCAT_LOOP") or (Path.home() / "scripts" / "napcat-loop.sh"))
if _NL.exists():
    import re as _re

    _txt = _NL.read_text(encoding="utf-8", errors="ignore")
    _lan = _re.search(r"/dev/tcp/(?!127\.0\.0\.1)[\d.]+:3001", _txt)
    _loop = "/dev/tcp/127.0.0.1:3001"
    ok("A57 探活先打内网地址", bool(_lan) and _loop in _txt
       and _txt.index(_lan.group(0)) < _txt.index(_loop))

# ---------- A29 / A55 / A56 / A59 / A60 脚本层断言 ----------
_MIG = (HERE / "scripts" / "migrate_memory_source.py").read_text(encoding="utf-8")
ok("A29 迁移排除已标行（幂等）", "COALESCE(source_type,'') IN ('', 'said')" in _MIG)

_GHOST = (Path.home() / "scripts" / "ghost_check.py")
if _GHOST.exists():
    _g = _GHOST.read_text(encoding="utf-8")
    ok("A55 非文本发言不进对账", "def is_plain_text" in _g)
    ok("A56 中途失败也落盘状态", "finally:" in _g and "json.dump(seen" in _g)

_BK = (PKG / "scripts" / "backup_data.sh").read_text(encoding="utf-8")
_tail = _BK[_BK.rindex("trap - EXIT") - 200:]
ok("A59 正常收尾清理快照", 'rm -rf "$SNAP"' in _tail)
ok("A60 轮转只吃自动命名包", "fish-data-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-" in _BK)
ok("A60 不再用 mtime 排序", 'ls -1t "$OUT"' not in _BK)

print("\n%d 项失败" % len(FAIL) if FAIL else "\n全部通过")
sys.exit(1 if FAIL else 0)
