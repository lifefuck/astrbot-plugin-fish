#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 九条的回归（只跑能脱离框架断言的几条）。

跑法：python3 scripts/test_p1_fixes.py

覆盖：
- 视觉：透明 PNG/贴纸不再整张变黑；超大图拒识（不解码，防 OOM）
- 告状：每人 5 分钟一次、每群每小时 3 次
- 纠错：日常否定句不再被当成改口（共享实词阈值 2 → 3）
- 印象原子：apply_decay 返回新列表，调用方不能再拿 == 判「没变化」而不落盘

@+链接、research 收尾超时、工具超时告知这三条是流程分支，靠闸门 + 线上日志看。
"""
import importlib.util
import io
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


# ---------- astrbot 最小桩（CI 里没有框架，逻辑本身跟框架无关） ----------
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

    class AstrMessageEvent:  # noqa: D101
        pass

    class _Chain:  # noqa: D101
        pass

    ev.AstrMessageEvent = AstrMessageEvent
    ev.MessageChain = _Chain
    ev.MessageEventResult = _Chain

    class _Filter:  # noqa: D101
        @staticmethod
        def command(*a, **k):  # noqa: D102
            return lambda f: f

        @staticmethod
        def event_message_type(*a, **k):  # noqa: D102
            return lambda f: f

    ev.filter = _Filter
    api.event = ev

    flt = mod("astrbot.api.event.filter")

    class EventMessageType:  # noqa: D101
        GROUP_MESSAGE = 1
        ALL = 2

    flt.EventMessageType = EventMessageType

    star = mod("astrbot.api.star")
    star.Context = object
    star.Star = object
    star.register = lambda *a, **k: (lambda c: c)
    api.star = star

    # kernel 顶层试导入 ToolSet，失败后 __all__ 里还留着这个名字
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

# 记忆层模块 import 时就会按环境变量定位库，必须先设再 import
_TMP = tempfile.mkdtemp(prefix="fish-p1-")
os.environ["FISH_MEMORY_DB"] = os.path.join(_TMP, "mem.db")

_install_stub()
sys.path.insert(0, str(HERE / "astrbot_plugin"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


V = _load("astrbot_plugin_fish.fish_core.vision", PKG / "fish_core" / "vision.py")
FM = _load("astrbot_plugin_fish.fish_core.fish_memory", PKG / "fish_core" / "fish_memory.py")
CMD = _load("astrbot_plugin_fish.fish_core.commands", PKG / "fish_core" / "commands.py")
MA = _load("astrbot_plugin_fish.fish_core.memory_atoms", PKG / "fish_core" / "memory_atoms.py")

from PIL import Image  # noqa: E402


def _png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- 视觉：透明底 ----------
full_transparent = Image.new("RGBA", (48, 48), (255, 0, 0, 0))
small, mime = V._shrink(_png(full_transparent))
back = Image.open(io.BytesIO(small)).convert("RGB")
px = back.getpixel((24, 24))
ok("透明贴纸不再变黑（白底）", min(px) > 235, px)
ok("透明图输出的 MIME 是 jpeg", mime == "image/jpeg", mime)

half = Image.new("RGBA", (48, 48), (255, 0, 0, 128))
small, _ = V._shrink(_png(half))
px = Image.open(io.BytesIO(small)).convert("RGB").getpixel((24, 24))
ok("半透明红合成成粉（不是纯红/纯黑）", 100 < px[1] < 160 and px[0] > 235, px)

pal = Image.new("P", (48, 48), 0)
pal.info["transparency"] = 0
small, _ = V._shrink(_png(pal))
px = Image.open(io.BytesIO(small)).convert("RGB").getpixel((24, 24))
ok("调色板图透明索引不炸", isinstance(px, tuple) and len(px) == 3, px)

# ---------- 视觉：超大图拒识 ----------
big = Image.new("1", (7000, 6000), 0)   # 42MP，1-bit 存出来只有几 MB，但 decode 后是 42MB+
try:
    V._shrink(_png(big))
    ok("42MP 巨图被拒识", False, "没有抛 _TooBig")
except V._TooBig as e:
    ok("42MP 巨图被拒识", str(e) == "7000x6000", e)

normal = Image.new("RGB", (3000, 2000), (30, 120, 200))
small, mime = V._shrink(_png(normal))
ok("正常大图仍能缩", len(small) > 0 and Image.open(io.BytesIO(small)).size[0] <= 1280,
   Image.open(io.BytesIO(small)).size)

# ---------- 告状冷却 ----------
class _Fake:
    pass


f = _Fake()
ok("首次告状放行", CMD.CommandsMixin._tattle_allowed(f, "1001", "2001", 1000.0) is True)
ok("同一人 3 分钟后仍拦", CMD.CommandsMixin._tattle_allowed(f, "1001", "2001", 1180.0) is False)
ok("同一人 5 分钟后放行", CMD.CommandsMixin._tattle_allowed(f, "1001", "2001", 1301.0) is True)
# 到这一步群里已经有 2 条（1000、1301），第三人正好用掉最后一个额度
ok("同群第二人放行", CMD.CommandsMixin._tattle_allowed(f, "1001", "2002", 1301.0) is True)
ok("同群第三人拦（一小时 3 次）", CMD.CommandsMixin._tattle_allowed(f, "1001", "2003", 1301.0) is False)
ok("换群不受影响", CMD.CommandsMixin._tattle_allowed(f, "1002", "2004", 1301.0) is True)
ok("一小时后额度恢复", CMD.CommandsMixin._tattle_allowed(f, "1001", "2005", 1301.0 + 3601) is True)

# ---------- 纠错：日常否定句不再被当改口 ----------
eng = FM.FishMemoryEngine.__new__(FM.FishMemoryEngine)
recalled = [{"memory_id": 1, "content": "他养了两只猫", "person_id": "222", "confidence": 0.9}]
ok("「不是两只，是三只」照旧命中",
   (eng.detect_correction("不是两只，是三只", recalled) or {}).get("memory_id") == 1)
# 共享字只有「不/是/我」这类虚词时，以前 2 个字就够，会把整句当改口
ok("日常否定句不命中（共享实词 < 3）",
   eng.detect_correction("我不是很喜欢猫", recalled) is None,
   eng.detect_correction("我不是很喜欢猫", recalled))
# 光靠「不」「是」凑出 2 个字的老误判：整句被当改口、0.9 假事实写进档案
ok("虚词凑数不算改口", eng.detect_correction("不是吧", recalled) is None,
   eng.detect_correction("不是吧", recalled))
ok("没有纠正话术一律不命中",
   eng.detect_correction("他养了两只猫吧", recalled) is None)
# 长句共享实词够 3 个，仍按改口处理
_work = [{"memory_id": 2, "content": "他今天去公司上班了", "person_id": "222", "confidence": 0.9}]
ok("长句改口照旧命中",
   (eng.detect_correction("我今天不是去上班，是去看病了", _work) or {}).get("memory_id") == 2)

# ---------- 印象原子：apply_decay 必须返回新对象 ----------
atoms = [{"text": "怕黑", "d": 0.9, "d_at": 0.0, "level": 3}]
out = MA.apply_decay(atoms, now=1000.0)
ok("apply_decay 返回新列表（调用方才能判出变化）", out is not atoms)
ok("apply_decay 原地刷新衰减时间", atoms[0]["d_at"] == 1000.0, atoms[0])
ok("apply_decay 幂等（同 now 再跑一次强度不变）",
   abs(MA.apply_decay(atoms, now=1000.0)[0]["d"] - out[0]["d"]) < 1e-9)

print()
if FAIL:
    print("失败 %d 个：%s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("全部通过")
