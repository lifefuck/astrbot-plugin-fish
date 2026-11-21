"""/tocken 的图片卡：PIL 画一张竖图，彻底绕开 QQ 代码块折行。

口径和文字版完全一致，数字全部来自 fish_token_logs 实记值，
没数据的格子画「—」，不拿 0 冒充。
"""
from __future__ import annotations

import os
import time
from typing import Dict

# 字体：本机只有 Noto CJK 带全中文，emoji 字体缺失，所以卡片里不放 emoji
_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-{}.ttc"

BG = (22, 24, 29)
CARD = (33, 36, 43)
FG = (236, 238, 243)
DIM = (148, 154, 168)
ACC = (116, 198, 255)
LINE = (52, 56, 66)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    return ImageFont.truetype(_FONT.format("Bold" if bold else "Regular"), size, index=0)


def _num(v) -> str:
    """≥10000 用 k，卡片窄一点更精神；没数据给 —。"""
    try:
        v = float(v)
    except Exception:
        return "—"
    return f"{v / 1000:.1f}k" if v >= 10000 else f"{int(round(v)):,}"


def _pct(hit, inp) -> str:
    return f"{float(hit) / float(inp) * 100:.1f}%" if inp else "—"


def _speed(outp, ms) -> str:
    return f"{float(outp) / (float(ms) / 1000.0):.1f}" if (outp and ms) else "—"


def _per(w: dict, kind: str) -> str:
    n = float(w.get(f"n_{kind}") or 0)
    if not n:
        return "—"
    return _num(((w.get(f"{kind}_inp") or 0) + (w.get(f"{kind}_outp") or 0)) / n)


def _total(w: dict) -> str:
    return _num((w.get("inp") or 0) + (w.get("outp") or 0)) if w.get("n") else "—"


def render(st: Dict[str, Dict[str, float]], out_path: str) -> str:
    from PIL import Image, ImageDraw

    cols = [st.get("7日") or {}, st.get("昨日") or {}, st.get("今日") or {}]
    l10, cur = st.get("最近10") or {}, st.get("当前") or {}

    # 行序照用户给的那份清单：总量 → 聊天单条 → 联网单条 → 速度 → 命中率 → 调用次数
    rows = [
        ("Token用量", [_total(w) for w in cols], True),
        ("输入", [_num(w.get("inp")) if w.get("n") else "—" for w in cols], False),
        ("输出", [_num(w.get("outp")) if w.get("n") else "—" for w in cols], False),
        ("平均聊天单条", [_per(w, "chat") for w in cols], False),
        ("平均联网单条", [_per(w, "search") for w in cols], False),
        ("平均输出速度", [_speed(w.get("outp"), w.get("ms")) for w in cols], False),
        ("平均缓存命中率", [_pct(w.get("hit"), w.get("inp")) for w in cols], False),
        ("调用次数", [_num(w.get("n")) if w.get("n") else "—" for w in cols], False),
    ]

    W, pad, rowh, headh = 760, 36, 50, 214
    H = headh + rowh * len(rows) + 58
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 12, W - 12, H - 12], 20, fill=CARD)

    d.text((pad, 36), "Token 用量统计", font=_font(32, True), fill=FG)
    ln = float(l10.get("n") or 0)
    avg10 = ((l10.get("inp") or 0) + (l10.get("outp") or 0)) / ln if ln else None
    # 三个单值指标放标题下面，和清单前三条一一对应
    d.text((pad, 86), f"当前上下文总大小 {_num(cur.get('inp')) if cur.get('inp') else '—'} tok", font=_font(21), fill=DIM)
    d.text((pad, 118), f"最近10条平均用量 {_num(avg10) if avg10 else '—'}"
                       f" · 平均缓存命中率 {_pct(l10.get('hit'), l10.get('inp'))}", font=_font(21), fill=DIM)

    # 三列右边界：7日 / 昨日 / 今日 从左到右，最后一列贴右边距，不会溢出
    right = [W - pad - 320, W - pad - 160, W - pad]
    y = headh
    for i, h in enumerate(("7日", "昨日", "今日")):
        ft = _font(19)
        d.text((right[i] - d.textlength(h, font=ft), y - 36), h, font=ft, fill=DIM)
    d.line([pad, y - 4, W - pad, y - 4], fill=LINE)

    for name, vals, strong in rows:
        d.text((pad, y), name, font=_font(23, strong), fill=FG if strong else DIM)
        for i, v in enumerate(vals):
            ft = _font(24, strong)
            d.text((right[i] - d.textlength(v, font=ft), y - 3), v, font=ft,
                   fill=ACC if i == 2 else FG)
        y += rowh

    d.text((pad, H - 44), time.strftime("更新 %Y-%m-%d %H:%M"), font=_font(16), fill=(104, 110, 124))
    img.save(out_path, "PNG", optimize=True)
    return out_path


def make(st, cache_dir: str) -> str:
    """画卡并顺手清掉陈旧的卡，免得 data/cache 越堆越多。"""
    os.makedirs(cache_dir, exist_ok=True)
    for fn in os.listdir(cache_dir):
        if fn.startswith("tocken_") and fn.endswith(".png"):
            p = os.path.join(cache_dir, fn)
            try:
                if time.time() - os.path.getmtime(p) > 600:
                    os.remove(p)
            except OSError:
                pass
    out = os.path.join(cache_dir, f"tocken_{int(time.time())}.png")
    try:
        return render(st, out)
    except Exception:
        # 字体缺失/磁盘满之类：清掉半成品，让调用方退回文字版
        try:
            os.remove(out)
        except OSError:
            pass
        raise
