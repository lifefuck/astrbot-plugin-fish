#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 回归：体检报告 16 条里能脱离框架断言的 10 条。

跑法：python3 scripts/test_p2_fixes.py

不在本文件覆盖的（靠闸门/线上观察）：
- 线上 .bak-* 与退役模块：部署流程改动，无代码可测
- 数据文件权限：写入路径已改 0600，靠部署后 ls -l 核
- _seen_mid 空 id 指纹：要造 AstrMessageEvent，成本大于收益
"""
import importlib.util
import os
import sys
import tempfile
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


# ---------- astrbot 最小桩（kernel 要 import 框架） ----------
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

_TMP = tempfile.mkdtemp(prefix="fish-p2-")
os.environ["FISH_MEMORY_DB"] = os.path.join(_TMP, "mem.db")

_install_stub()
sys.path.insert(0, str(HERE / "astrbot_plugin"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PKG / "fish_core" / filename)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


PUNS = _load("t_puns", "puns.py")
GATE = _load("t_gate", "meme_gate.py")
SPLIT = _load("t_split", "splitter.py")
OW = _load("t_ow", "order_watch.py")
GAMES = _load("t_games", "games.py")
PLAN = _load("t_plan", "interject_planner.py")
KERNEL = _load("astrbot_plugin_fish_kernel", "kernel.py")
BK = _load("t_backup", "backup.py")

# ---------- ① 数字谐音必须有边界 ----------
ok("独立 886 仍判拜拜", "拜拜" in str(PUNS.note("886")), PUNS.note("886"))
ok("8861 不判拜拜", "拜拜" not in str(PUNS.note("订单号8861")), PUNS.note("订单号8861"))
ok("第520条消息不判我爱你", "我爱你" not in str(PUNS.note("第520条消息")), PUNS.note("第520条消息"))
ok("520 独立仍判我爱你", "我爱你" in str(PUNS.note("520")), PUNS.note("520"))

# ---------- ② 从没说过话不算冷场 ----------
_s1, w1 = PLAN.adjust(0.5, now=1000.0, last_bot_reply=0.0, msgs_since_bot=1)
ok("从没说过话不被当冷场罚", "冷场" not in w1, w1)
_s2, w2 = PLAN.adjust(0.5, now=5000.0, last_bot_reply=1000.0, msgs_since_bot=1)
ok("真冷场仍然降权", "冷场" in w2 and abs(_s2 - 0.5 * 0.85) < 1e-9, (w2, _s2))

# ---------- ③ 时钟回拨不再锁死表情包 ----------
okf, whyf = GATE.decide("摸鱼", 1000.0, last_meme_ts=5000.0, min_gap_sec=300.0)
ok("未来时间戳不锁表情包", okf is True, whyf)
okf2, whyf2 = GATE.decide("摸鱼", 1000.0, last_meme_ts=900.0, min_gap_sec=300.0)
ok("正常间隔仍按门限拦", okf2 is False, whyf2)

# ---------- ④ 拆条：每条不超字数；短尾巴不回吞 ----------
long_text = "今天上班好累啊，老板又临时加需求。" * 12  # 约 200 字
parts = SPLIT.split_reply(long_text, max_chars=45, max_parts=3)
ok("拆条每条不超过 45 字", all(len(p) <= 45 for p in parts), [len(p) for p in parts])
ok("拆条不为均分造 80+ 字长条", max(len(p) for p in parts) <= 45, [len(p) for p in parts])

edge = "啊" * 45 + "嗯嗯"   # 47 字
pe = SPLIT.split_reply(edge, max_chars=45, max_parts=3)
ok("46/47 字不再被回吞顶破上限", all(len(p) <= 45 for p in pe), [len(p) for p in pe])

# ---------- ⑤ cfg_int 保住 0 ----------
ok("cfg_int 认 0", KERNEL.cfg_int({"split_max_chars": 0}, "split_max_chars", 45) == 0)
ok("cfg_int 空串回落默认", KERNEL.cfg_int({"split_max_chars": ""}, "split_max_chars", 45) == 45)
ok("cfg_int 缺键回落默认", KERNEL.cfg_int({}, "split_max_chars", 45) == 45)
ok("cfg_int 字符串数字能转", KERNEL.cfg_int({"x": "30"}, "x", 45) == 30)

# ---------- ⑥ 广告/辱骂粗筛不再误报 ----------
ok("「你妈喊你吃饭」不判辱骂", OW.suspicious("你妈喊你吃饭了") == "", OW.suspicious("你妈喊你吃饭了"))
ok("「微信发你截图」不判广告", OW.suspicious("微信发你截图") == "", OW.suspicious("微信发你截图"))
ok("「加微信领红包」仍判广告", OW.suspicious("加微信领红包") == "广告拉人", OW.suspicious("加微信领红包"))
ok("「你妈的」仍判辱骂", OW.suspicious("你妈的") == "辱骂", OW.suspicious("你妈的"))

# ---------- ⑦ 文字游戏：炸弹已整体删除，只剩单次玩法 ----------
ok("数字炸弹的接口已删除", not hasattr(GAMES, "process_number_bomb"), "")
ok("「数字炸弹」当闲聊", GAMES.handle_quick_game_commands("数字炸弹") is None, "")
ok("裸数字当闲聊", GAMES.handle_quick_game_commands("42") is None, "")
ok("掷骰子仍可玩", "点" in (GAMES.handle_quick_game_commands("roll") or ""), "")
ok("抛硬币仍可玩", "硬币" in (GAMES.handle_quick_game_commands("抛硬币") or ""), "")
ok("抽签仍可玩", "抽到" in (GAMES.handle_quick_game_commands("抽签") or ""), "")

# ---------- ⑧ 备份快照 0600 ----------
_j = Path(_TMP) / "state.json"
_j.write_text('{"a": 1}', encoding="utf-8")
os.chmod(_j, 0o664)
BK.snapshot_json(str(_j))
baks = sorted(Path(_TMP).glob("state.json.bak-*"))
mode = oct(baks[-1].stat().st_mode & 0o777) if baks else "none"
ok("备份快照权限 600", mode == "0o600", mode)

print()
if FAIL:
    print("失败 %d 个：%s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("全部通过")
