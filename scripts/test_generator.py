#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成器单测：打字延迟、草稿覆盖、过时重判。

跑法：python3 scripts/test_generator.py
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]
CORE = HERE / "astrbot_plugin/astrbot_plugin_fish/fish_core"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


GEN = _load("generator", CORE / "generator.py")

NOW = 1_800_000_000.0
CFG = {"typing_delay_enabled": True, "typing_max_sec": 6.0, "draft_ttl_sec": 25}
CASES = []


def case(name, ok, detail=""):
    CASES.append((name, bool(ok), detail))


def dlg(*rows):
    return [{"uid": u, "who": u, "text": t, "ts": ts, "me": me} for u, ts, t, me in rows]


def main():
    # 1 延迟按字数走：短句落在 1.0~1.6 秒
    d = GEN.typing_delay("嗯", CFG)
    case("1 短句 1~1.6 秒", 1.0 <= d <= 1.6, str(d))

    # 2 长句封顶
    d = GEN.typing_delay("这句话特别特别长" * 20, CFG)
    case("2 长句封顶 6 秒", d == 6.0, str(d))

    # 3 关掉就没有延迟
    case("3 关掉延迟为 0", GEN.typing_delay("随便说点啥", {"typing_delay_enabled": False}) == 0.0)

    # 4 字数越多越久（单调）
    a, b = GEN.typing_delay("好啊", CFG), GEN.typing_delay("这个我觉得可以再想想", CFG)
    case("4 字数越多越慢", b > a, "%s < %s" % (a, b))

    # 5 期间没人说话 → 照发
    draft = GEN.make_draft(text="在呢", now=NOW, ver=3, intent_id=1, uid="222", cfg=CFG)
    act, why = GEN.revalidate(draft, now=NOW + 2, ver=3, dialogue=[], cfg=CFG)
    case("5 没人说话就发", act == GEN.SEND, act)

    # 6 同一个人接着说 → 还是发（别把人晾着）
    draft = GEN.make_draft(text="在呢", now=NOW, ver=3, intent_id=1, uid="222", cfg=CFG)
    rows = dlg(("222", NOW + 1, "那个报错还在", False))
    act, why = GEN.revalidate(draft, now=NOW + 2, ver=5, dialogue=rows, cfg=CFG)
    case("6 同一人接着说照发", act == GEN.SEND and "接着" in why, "%s %s" % (act, why))

    # 7 两个人聊开了 → 丢掉
    rows = dlg(("222", NOW + 1, "你昨天去哪了", False), ("333", NOW + 2, "在家啊", False))
    act, why = GEN.revalidate(draft, now=NOW + 3, ver=6, dialogue=rows, cfg=CFG)
    case("7 两人聊开了就丢", act == GEN.ABANDONED, "%s %s" % (act, why))

    # 8 有人在叫鱼 → 旧草稿作废，让新消息自己走一轮
    rows = dlg(("222", NOW + 1, "小鲸鱼你说呢", False))
    act, why = GEN.revalidate(draft, now=NOW + 2, ver=4, dialogue=rows,
                              names=("小鲸鱼",), cfg=CFG)
    case("8 有人在叫鱼就作废", act == GEN.SUPERSEDED, "%s %s" % (act, why))

    # 9 草稿超时 → 丢掉
    act, why = GEN.revalidate(draft, now=NOW + 100, ver=3, dialogue=[], cfg=CFG)
    case("9 草稿超时就丢", act == GEN.ABANDONED and "超时" in why, "%s %s" % (act, why))

    # 10 别人插一句但没聊开 → 照发（不至于别人一说话就哑）
    rows = dlg(("333", NOW + 1, "哈哈", False))
    act, why = GEN.revalidate(draft, now=NOW + 2, ver=4, dialogue=rows, cfg=CFG)
    case("10 别人插一句照发", act == GEN.SEND, "%s %s" % (act, why))

    # 11 草稿覆盖：后到的冲掉先到的，旧的不发
    class ST:
        draft = {}

    logs = []

    class L:
        def info(self, *a):
            logs.append(a[0] % a[1:])

    GEN.store(ST, GEN.make_draft(text="旧的", now=NOW, ver=1, intent_id=7, cfg=CFG), logger=L())
    GEN.store(ST, GEN.make_draft(text="新的", now=NOW + 1, ver=2, intent_id=8, cfg=CFG), logger=L())
    got = GEN.take(ST)
    case("11 后到的草稿冲掉先到的", got["text"] == "新的" and got["intent_id"] == 8
         and any("覆盖旧草稿" in s for s in logs), str(got))

    # 12 取完就清空，不会重复发
    case("12 取完即清空", GEN.take(ST) is None)

    bad = [c for c in CASES if not c[1]]
    for name, ok, detail in CASES:
        print(("  ok  " if ok else "  FAIL") + " " + name + ("  ← " + detail if not ok else ""))
    print("\n生成器 %d 例：%d 通过 / %d 失败" % (len(CASES), len(CASES) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
