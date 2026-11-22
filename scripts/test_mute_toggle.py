#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静默开关单测：/off 之后 /on 必须真的能把鱼叫回来（P0 回归）。

跑法：python3 scripts/test_mute_toggle.py

为什么自带 astrbot 桩：commands.py 顶部要 import astrbot.api，
但这条链路的逻辑（放行判定 + cmd_on 副作用）跟框架无关，
CI 里没有 astrbot，只能在 sys.modules 里塞一个最小桩再按包路径加载。
"""
import asyncio
import importlib.util
import json
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


# ---------- astrbot 最小桩 ----------
def _install_stub():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    astrbot = mod("astrbot")
    api = mod("astrbot.api")
    api.__path__ = []          # 让 astrbot.api.web 这类子模块能挂上去
    api.logger = __import__("logging").getLogger("stub")
    astrbot.api = api
    # kernel 顶层会按路径加载 panel.py，panel 又要 astrbot.api.web.request
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

    # kernel 顶层 try-import ToolSet（失败会留 NO_TOOLS=None，但 __all__ 里
    # 仍然写着这个名字，星号导入会炸），所以这里必须给它留一个
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


# CI 里只装 pyyaml，没有 aiohttp；这条链路用不到真会话，给个空壳即可
class _AiohttpStub(types.ModuleType):
    def __getattr__(self, item):  # 任何子名字都返回一个空类/空函数
        if item.startswith("__"):
            raise AttributeError(item)
        return type(item, (), {})


sys.modules.setdefault("aiohttp", _AiohttpStub("aiohttp"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


_install_stub()
sys.path.insert(0, str(HERE / "astrbot_plugin"))
CMD = _load("astrbot_plugin_fish.fish_core.commands", PKG / "fish_core" / "commands.py")


# ---------- 假 event ----------
class FakeEvent:
    def __init__(self, text, gid="100000001", uid="100000002", role="admin"):
        self.message_str = text
        self._gid = gid
        self._uid = uid
        self._role = role
        self.stopped = False
        self.results = []

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return "测试群管"

    def get_self_id(self):
        return "999999999"

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    @property
    def message_obj(self):  # 权限判定会读 raw_message.sender.role
        rm = types.SimpleNamespace(sender=types.SimpleNamespace(role=self._role))
        return types.SimpleNamespace(message_id="m1", raw_message=rm)


# ---------- 假插件：只装这条链路要用的成员 ----------
class FakePlugin(CMD.CommandsMixin):
    def __init__(self, tmpdir):
        self.cfg = {"enabled": True, "owner_qq": "100000002", "yunzai_sync": False,
                    "command_prefixes": ["/"]}
        self._mute_file = os.path.join(tmpdir, "group_muted.json")
        self._muted = {}
        self._group_overrides = {}
        # cmd_on 恢复时要清掉的东西（真实实例在 __init__ 里建）
        self._ban_expire = {}
        self._ban_checked = {}
        self._silence_heal = {}
        self.dry_mute_path = self._mute_file

    # admin.py 里的实现要读写 json，这里按同样语义来实现，落盘格式一致
    def _is_muted(self, gid):
        return bool(self._muted.get(str(gid), {}).get("muted"))

    def _mute_restore(self, gid):
        return bool(self._muted.get(str(gid), {}).get("restore_yunzai"))

    def _set_muted(self, gid, muted, restore_yunzai=None):
        cur = dict(self._muted.get(str(gid)) or {})
        cur["muted"] = bool(muted)
        if restore_yunzai is not None:
            cur["restore_yunzai"] = bool(restore_yunzai)
        self._muted[str(gid)] = cur
        with open(self._mute_file, "w", encoding="utf-8") as f:
            json.dump(self._muted, f, ensure_ascii=False)

    def load_muted(self):
        if os.path.exists(self._mute_file):
            with open(self._mute_file, encoding="utf-8") as f:
                self._muted = json.load(f) or {}

    def _gid_of(self, event):
        return str(event.get_group_id() or "")

    def _is_boss(self, event):
        return str(event.get_sender_id()) == str(self.cfg.get("owner_qq") or "")

    def save_group_overrides(self, ov):
        pass

    def log_event(self, *a, **k):
        pass


def run(gen):
    async def _collect():
        out = []
        async for r in gen:
            out.append(r)
        return out
    return asyncio.run(_collect())


def main():
    tmp = tempfile.mkdtemp(prefix="fishmute-")
    P = FakePlugin(tmp)
    G = "100000001"

    # ---- 1. /off 真的静默 ----
    ev_off = FakeEvent("/off")
    out = run(P.cmd_off(ev_off))
    ok("1 /off 落静默", P._is_muted(G) and ev_off.stopped, (P._is_muted(G), ev_off.stopped))
    ok("2 /off 有回执且提到 /on", any("/on" in str(r[1]) for r in out), out)

    # ---- 3. 静默期间普通消息不放行（on_group 的静默分支判定）----
    ok("3a 静默时普通消息不算解除", P._is_unmute_cmd(FakeEvent("今天吃啥")) is False)
    ok("3b 静默时「on my way」不算解除（不能被英文聊天叫醒）",
       P._is_unmute_cmd(FakeEvent("on my way 马上到")) is False)
    ok("3c 静默时「在线吗」不算解除", P._is_unmute_cmd(FakeEvent("在线吗")) is False)

    # ---- 4. 静默分支里 /on 走的就是 cmd_on：真能恢复 ----
    for text in ("/on", "on", "恢复", "开启"):
        P._set_muted(G, True)
        ok(f"4_{text} 认作解除指令", P._is_unmute_cmd(FakeEvent(text)) is True)
        ev = FakeEvent(text)
        out = run(P.muted_branch(ev, G))
        ok(f"4_{text} 静默分支能恢复", (not P._is_muted(G)) and bool(out), (P._is_muted(G), out))

    # 静默期间发 /on 但说话的是普通群员：不允许
    P._set_muted(G, True)
    ev_low = FakeEvent("/on", uid="100000003", role="member")
    ev_low.cfg = None
    P.cfg["owner_qq"] = "100000002"
    run(P.muted_branch(ev_low, G))
    ok("5 非群管发 /on 不解除", P._is_muted(G), P._is_muted(G))

    # ---- 6. 重启后静默仍在，且 /on 依然能恢复（状态落盘 + 重新加载）----
    P2 = FakePlugin(tmp)
    P2.load_muted()
    ok("6a 重启后仍静默", P2._is_muted(G) is True, P2._muted)
    ev_on2 = FakeEvent("/on")
    out = run(P2.muted_branch(ev_on2, G))
    ok("6b 重启后 /on 仍能恢复", (not P2._is_muted(G)) and bool(out), (P2._is_muted(G), out))

    print()
    if FAIL:
        print(f"{len(FAIL)} 项失败：{FAIL}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
