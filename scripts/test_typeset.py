#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排版归一化的对照测试：每一条都是一个真实的坏样子，清洗后必须达标。

跑法：python3 scripts/test_typeset.py
不用 pytest（仓里其它测试也是裸脚本），失败返回 1。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "astrbot_plugin"))
from astrbot_plugin_fish.fish_core import typeset  # noqa: E402

CASES = [
    # 汉字之间莫名加空格（模型最常见的毛病）
    ("今天 好 累 啊", "今天好累啊", "汉字间空格"),
    ("这 波 太 逆 天 了", "这波太逆天了", "汉字间空格"),
    # 汉字和英文/数字/表情之间的空格要留
    ("看 BLG 的比赛 3 比 0", "看 BLG 的比赛 3 比 0", "中英/数字间保留"),
    ("牛逼 😂 真的", "牛逼 😂 真的", "汉字与表情间保留"),
    # 行首行尾空格、连续换行
    ("  第一行  \n\n\n  第二行  ", "第一行\n第二行", "行首尾+连续换行"),
    ("开头有空格\n结尾有换行\n\n", "开头有空格\n结尾有换行", "首尾空白"),
    # 代码块整段不动（行首缩进就是语义，删了代码就废）
    ("看这个\n\n```python\n    print('今天 好')\n```\n\n然后再说", "看这个\n```python\n    print('今天 好')\n```\n然后再说", "代码块缩进保留、空行照样压"),
    # 中文标点前后的空格
    ("好的 。", "好的。", "标点前空格"),
    ("行吧 ， 那就这样", "行吧，那就这样", "标点两侧空格"),
]

FINISH_CASES = [
    # 10 字内不加句号
    ("好累。", "好累", "短句去句号"),
    # 超过 10 字保留句号
    ("今天真的很累啊我快撑不住了。", "今天真的很累啊我快撑不住了。", "长句留句号"),
    # 感叹号至多一个
    ("好耶!太棒了!!", "好耶!太棒了", "多余感叹号"),
    # 表情至多一个
    ("笑死😂😂😂", "笑死😂", "多余表情"),
]

MULTI_CASES = [
    # 三个要点 → 2~3 条，每条最多一个换行
    ("第一件事\n\n第二件事\n\n第三件事", 3, "三段拆条"),
    ("一段\n二段\n三段\n四段\n五段", 3, "超上限收进最后一条"),
]


def main() -> int:
    fail = 0
    for src, want, name in CASES:
        got = typeset.normalize(src)
        if got != want:
            print(f"FAIL {name}: {src!r} -> {got!r}，期望 {want!r}")
            fail += 1
        else:
            print(f"PASS {name}")
    for src, want, name in FINISH_CASES:
        got = typeset.finish_one(src)
        if got != want:
            print(f"FAIL {name}: {src!r} -> {got!r}，期望 {want!r}")
            fail += 1
        else:
            print(f"PASS {name}")
    for src, want_max, name in MULTI_CASES:
        msgs = typeset.to_messages(src, max_parts=3)
        ok = (1 <= len(msgs) <= want_max
              and all(m.count("\n") <= 1 for m in msgs)
              and all(m.strip() == m and m for m in msgs))
        if not ok:
            print(f"FAIL {name}: {msgs!r}")
            fail += 1
        else:
            print(f"PASS {name}（{len(msgs)} 条）")
    # 拆条后每条不许出现两个换行
    parts = typeset.finish_parts(["一二三\n\n四五六\n\n七八九", "十"], max_parts=3)
    if any(p.count("\n") > 1 for p in parts):
        print(f"FAIL finish_parts 有两条以上换行：{parts!r}")
        fail += 1
    else:
        print(f"PASS finish_parts 每条≤1 换行（{len(parts)} 条）")

    print()
    print("全部通过" if not fail else f"{fail} 个失败")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
