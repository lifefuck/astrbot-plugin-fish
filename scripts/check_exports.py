#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""静态核对 main 的顶层导出：拆分后谁也别再把定义弄丢。

为什么要有这个：main.py 按 Mixin 拆开时，顶层定义（常量、数据类、帮助函数）
如果没进 kernel 的 __all__，import 照样成功、CI 的 compileall 照样过，
只有线上真正跑到那一行才 NameError。已经踩过三次——
GroupState、@dataclass 装饰器被吞、NO_TOOLS（它定义在 try 块里，
生成 __all__ 的脚本只扫了顶层赋值）。

做法：scripts/main_exports.txt 是从拆分前的 main.py 导出的顶层名清单，
这里静态确认每个名字都能从 kernel 的 __all__ 或 main.py 的显式 import 拿到。
不用 import astrbot，所以在 CI 里能跑。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent / "astrbot_plugin" / "astrbot_plugin_fish"
KERNEL = PLUGIN / "fish_core" / "kernel.py"
MAIN = PLUGIN / "main.py"
MANIFEST = HERE / "main_exports.txt"


def kernel_all() -> set[str]:
    tree = ast.parse(KERNEL.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    return set()


def explicit_imports(path: Path) -> set[str]:
    """main.py 自己提供的名字：显式 import 的 + 文件里自己定义的（如 FishPlugin）。

    星号导入的名字不在这里，由 kernel.__all__ 负责。
    """
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {(a.asname or a.name) for a in node.names if a.name != "*"}
        elif isinstance(node, ast.Import):
            names |= {(a.asname or a.name.split(".")[0]) for a in node.names}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):  # try 块里的定义也算（NO_TOOLS 就这么丢的）
            for sub in node.body + node.handlers + node.orelse + node.finalbody:
                if isinstance(sub, ast.Assign):
                    names |= {t.id for t in sub.targets if isinstance(t, ast.Name)}
    return names


def main() -> int:
    wanted = [ln.strip() for ln in MANIFEST.read_text(encoding="utf-8").splitlines() if ln.strip()]
    available = kernel_all() | explicit_imports(MAIN)
    missing = [n for n in wanted if n not in available]
    if missing:
        print(
            "main.py 顶层名丢失（拆分时没搬进 kernel 或没进 __all__）：" + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    print(f"顶层导出完整（{len(wanted)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
