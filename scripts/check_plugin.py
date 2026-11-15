#!/usr/bin/env python3
"""插件结构自检：不进 AstrBot 也能查出「装上去就报错」的那类问题。

跑在 CI 里。只做静态检查，不连 QQ、不加载 AstrBot——真跑起来的效果只能上线看，
但格式错、字段漏、配置项没 description 这类低级问题，没必要等到装上去才发现。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "astrbot_plugin"

# 配置项这几种类型 AstrBot 面板才认，写错面板会渲染不出来
VALID_TYPES = {"string", "text", "int", "float", "bool", "list", "dict", "file", "object"}

errors: list[str] = []
ok: list[str] = []


def check_metadata(d: Path) -> None:
    f = d / "metadata.yaml"
    if not f.is_file():
        errors.append(f"{d.name}: 缺 metadata.yaml")
        return
    try:
        meta = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        errors.append(f"{d.name}: metadata.yaml 不是合法 YAML（{e}）")
        return
    for key in ("name", "display_name", "desc", "version", "author"):
        if not str(meta.get(key, "")).strip():
            errors.append(f"{d.name}: metadata.yaml 缺 {key}")
    if meta.get("name") and meta["name"] != d.name:
        errors.append(f"{d.name}: metadata.name 是 {meta['name']}，和目录名不一致，AstrBot 会用目录名")


def check_schema(d: Path) -> None:
    f = d / "_conf_schema.json"
    if not f.is_file():
        return  # 没配置项也是合法的
    try:
        schema = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"{d.name}: _conf_schema.json 解析失败（{e}）")
        return
    if not isinstance(schema, dict):
        errors.append(f"{d.name}: _conf_schema.json 顶层得是对象")
        return
    for key, item in schema.items():
        if not isinstance(item, dict):
            errors.append(f"{d.name}: 配置项 {key} 不是对象")
            continue
        t = item.get("type")
        if t not in VALID_TYPES:
            errors.append(f"{d.name}: 配置项 {key} 的 type={t!r} 不认识")
        if not str(item.get("description", "")).strip():
            errors.append(f"{d.name}: 配置项 {key} 没有 description，面板上会显示成空白")


def check_entry(d: Path) -> None:
    f = d / "main.py"
    if not f.is_file():
        errors.append(f"{d.name}: 缺 main.py")
        return
    src = f.read_text(encoding="utf-8")
    if "astrbot.api" not in src:
        errors.append(f"{d.name}: main.py 没从 astrbot.api 引入东西，多半不是插件入口")
    if not re.search(r"@(filter\.|register|command)", src):
        errors.append(f"{d.name}: main.py 里没有 handler 装饰器")
    # 曾经想把「类里首参叫 event 又没装饰器的方法」判成隐患，实测走不通：
    # AstrBot 只在装饰器里注册 handler，类里的普通异步辅助方法不会被自动接走，
    # 而且插件里这种辅助方法很常见（拿群名片、拼转发节点），一律拦掉全是误报。


def check_pages(d: Path) -> None:
    """插件自带的页面：目录名就是 view 名，缺 index.html 面板里点进去就是 404。"""
    for root_name in ("pages", "views"):
        root = d / root_name
        if not root.is_dir():
            continue
        for page_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if not (page_dir / "index.html").is_file():
                errors.append(f"{d.name}: {root_name}/{page_dir.name} 缺 index.html")


def main() -> int:
    dirs = sorted(p for p in PLUGIN_DIR.iterdir() if p.is_dir()) if PLUGIN_DIR.is_dir() else []
    if not dirs:
        errors.append("astrbot_plugin/ 下没有插件目录")
    for d in dirs:
        check_metadata(d)
        check_schema(d)
        check_entry(d)
        check_pages(d)
        ok.append(d.name)

    for name in ok:
        print(f"  检查 {name}")
    for e in errors:
        print(f"  问题 {e}")
    if errors:
        print(f"\n{len(errors)} 处问题")
        return 1
    print(f"\n{len(ok)} 个插件结构正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
