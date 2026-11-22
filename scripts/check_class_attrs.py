# -*- coding: utf-8 -*-
"""类常量悬空检查：self._UPPER_CASE 引用了，但全仓没有任何一处定义。

为什么要有这一条：把 main.py 拆成 Mixin 的时候，原来挂在 Star 类体上的常量
（_QUOTE_PLACEHOLDERS、_OPENER_RE）没有跟着搬过去，代码照常 import、照常启动，
只有真正走到那条分支才 AttributeError，而外层大多有 except 兜底 → 静默失效。
引用原文整段没进过 prompt、开场白复读削不掉，都是这么埋了一个月的。

跑法：python3 scripts/check_class_attrs.py
有悬空引用就以非 0 退出。
"""
import pathlib
import re
import sys

PKG = pathlib.Path(__file__).resolve().parent.parent / "astrbot_plugin" / "astrbot_plugin_fish"

# 真需要放行时往这里加，并在注释里写清为什么；默认必须为空，
# 否则这条闸门会把自己要防的那个 bug 一起放过去
WHITELIST: set = set()


def main() -> int:
    files = sorted(list((PKG / "fish_core").glob("*.py")) + [PKG / "main.py"])
    used: dict[str, list[str]] = {}
    defined: set[str] = set()

    for f in files:
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"self\.(_[A-Z][A-Z0-9_]*)\b", line):
                used.setdefault(m.group(1), []).append(f"{f.name}:{i}")
        # 类体（4 空格）或模块级的常量定义都算
        for m in re.finditer(r"^\s*(_[A-Z][A-Z0-9_]*)\s*=", text, re.M):
            defined.add(m.group(1))

    dangling = {k: v for k, v in used.items() if k not in defined and k not in WHITELIST}
    if dangling:
        print("悬空类常量（引用了但全仓没定义）：")
        for k, where in sorted(dangling.items()):
            print(f"  {k:26s} {', '.join(where)}")
        return 1
    print(f"类常量检查通过（引用 {len(used)} 个，均有定义）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
