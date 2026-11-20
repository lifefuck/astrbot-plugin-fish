#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 _conf_schema.json 生成配置参考文档。

为什么生成而不是手写：配置项已经 90 个，手写的表格一定会跟不上——
上一版 README 里 split_max_chars 还写着 60（实际 26）、视觉模型还写着
gemini-3.8-flash（实际 3.5-flash-lite），读者照着调就是错的。

用法：
    python3 scripts/gen_config_doc.py           # 重写 docs/config.md
    python3 scripts/gen_config_doc.py --check   # 只比对，CI 用；不一致退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "astrbot_plugin" / "astrbot_plugin_fish" / "_conf_schema.json"
OUT = ROOT / "docs" / "config.md"

HEADER = """# 配置参考

本文件由 `scripts/gen_config_doc.py` 从 `_conf_schema.json` 生成，**不要手改**。
改配置项后跑一次生成脚本；CI 会检查它和 schema 是否一致。

面板里每一项都带 `hint`，这里只做速查。默认值列里的 `-` 表示默认空字符串。
"""


def fmt_default(value) -> str:
    """把默认值渲染成终端里能直接抄的样子。"""
    if value == "":
        return "-"
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    if isinstance(value, list):
        return "`[]`" if not value else "`" + json.dumps(value, ensure_ascii=False) + "`"
    text = str(value).replace("|", "\\|")
    return f"`{text}`"


def render(data: dict) -> str:
    lines = [HEADER, "| 配置项 | 默认 | 说明 |", "| :--- | :--- | :--- |"]
    for key, meta in data.items():
        desc = str(meta.get("description") or "").strip()
        hint = str(meta.get("hint") or "").strip()
        # hint 是面板里的补充说明，比 description 信息量大；两段都留，用句号连
        text = desc + ("。" + hint if hint and hint != desc else "")
        text = text.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{key}` | {fmt_default(meta.get('default'))} | {text} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写盘")
    args = ap.parse_args()

    data = json.loads(SCHEMA.read_text(encoding="utf-8"))
    text = render(data)

    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/config.md 与 _conf_schema.json 不一致，跑一次脚本重新生成", file=sys.stderr)
            return 1
        print(f"配置文档已同步（{len(data)} 项）")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}（{len(data)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
