#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查未定义名字：拆分后最容易死在哪一行，这个脚本能提前看见。

背景：main.py 拆成 Mixin 后，同一个坑踩了五次 —— GroupState、@dataclass
装饰器被吞、flatten_conf、NO_TOOLS、attach_file_log。共同点是 **import 不报错、
compileall 不报错、线上跑到那一行才 NameError**，而 pyflakes 遇到
`from .kernel import *` 会直接放弃（"unable to detect undefined names"），等于没防线。

第一版是手写扫描，报了一堆狼来了（函数参数、闭包里的外层变量、lambda 参数、
`_MIN_PREV, _MAX_PREV = 4, 60` 这种多目标赋值全被当成未定义），最后没人看。
现在改用标准库 symtable 做真正的作用域分析：
  只报「在函数作用域里被当全局名读、模块级又确实没有」的名字，
  参数/闭包局部/推导式目标由 symtable 自己认；star import 的名字从被导入
  模块的 __all__（没有就数它的顶层定义）补进来。

用法：python3 scripts/check_names.py       退出码 1 = 有未定义名字。
"""

from __future__ import annotations

import ast
import builtins
import symtable
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PLUGIN = ROOT_DIR / "astrbot_plugin" / "astrbot_plugin_fish"
BUILTIN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__builtins__"}

_exports_cache: dict[Path, set] = {}


def _all_literal(path: Path) -> set:
    """__all__ = [...] 里的名字（只认字面量，动态拼的跳过）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return {e.value for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return set()


def _top_level(path: Path) -> set:
    """模块顶层绑定的名字（含 try/if 块里的，NO_TOOLS 当初就藏在 try 里）。"""
    try:
        st = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    except Exception:
        return set()
    return {s.get_name() for s in st.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace()}


def _star_targets(path: Path) -> list:
    """文件里所有 `from X import *` 对应的本地文件。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)):
            continue
        mod = (node.module or "").split(".")
        if node.level:                                  # from .x import *
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            target = base.joinpath(*mod).with_suffix(".py") if mod else None
        elif mod:                                       # from pkg.x import *
            target = (ROOT_DIR / Path(*mod)).with_suffix(".py")
            if not target.exists():
                alt = PLUGIN.joinpath(*mod).with_suffix(".py")
                target = alt if alt.exists() else target
        else:
            target = None
        if target and target.exists():
            out.append(target)
    return out


def exports(path: Path, depth: int = 0) -> set:
    """模块对外提供的名字：__all__ 优先，再并上顶层定义和自己 star 进来的。"""
    if depth > 6:
        return set()
    if path in _exports_cache:
        return _exports_cache[path]
    names = _all_literal(path) | _top_level(path)
    for sub in _star_targets(path):
        names |= exports(sub, depth + 1)
    _exports_cache[path] = names
    return names


def _scan(table, defined: set, out: list):
    for sym in table.get_symbols():
        # 只有「在子作用域里以全局身份被引用、模块级又没有」才算可疑
        if table.get_type() != "module" and sym.is_global() and sym.is_referenced():
            if sym.get_name() not in defined:
                out.append((table.get_lineno() or 0, sym.get_name()))   # symtable 的 symbol 没有行号，用所属作用域的行号
    for child in table.get_children():
        _scan(child, defined, out)


def check_file(path: Path) -> list:
    defined = set(BUILTIN) | exports(path)
    for sub in _star_targets(path):
        defined |= exports(sub)
    st = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    out: list = []
    _scan(st, defined, out)
    return sorted(set(out))


def main() -> int:
    files = [PLUGIN / "main.py"] + sorted((PLUGIN / "fish_core").glob("*.py"))
    total = 0
    for path in files:
        if not path.exists() or "__pycache__" in str(path):
            continue
        try:
            hits = check_file(path)
        except SyntaxError as e:
            print(f"{path.relative_to(PLUGIN)}: 语法错误 {e}")
            total += 1
            continue
        for lineno, name in hits:
            print(f"{path.relative_to(PLUGIN)}:{lineno}: 未定义名字 {name}")
        total += len(hits)
    if total:
        print(f"未定义名字 {total} 处")
        return 1
    print(f"未定义名字检查通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
