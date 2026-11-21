"""/tocken 的文字兜底版（画图失败、或用户在群里要纯文字时才走这条路）。

排版按最初定稿的那份：标题 → 当前上下文 → 最近10条 → 总用量/单条聊天/单条联网/
速度/命中率，每组都按 7日 / 昨日 / 今日 三行展开。
数字全部来自 fish_token_logs 实记值，没数据就写 —。
"""
from __future__ import annotations

from typing import Dict


def _n(v) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except Exception:
        return "—"


def _pct(hit, inp) -> str:
    return f"{float(hit) / float(inp) * 100:.1f}%" if inp else "—"


def _speed(outp, ms) -> str:
    return f"{float(outp) / (float(ms) / 1000.0):.1f} tok/s" if (outp and ms) else "—"


def _per(w: dict, kind: str) -> str:
    n = float(w.get(f"n_{kind}") or 0)
    if not n:
        return "—"
    return _n(((w.get(f"{kind}_inp") or 0) + (w.get(f"{kind}_outp") or 0)) / n)


def render(st: Dict[str, Dict[str, float]]) -> str:
    l10, cur = st.get("最近10") or {}, st.get("当前") or {}
    wins = [("7日", st.get("7日") or {}), ("昨日", st.get("昨日") or {}),
            ("今日", st.get("今日") or {})]
    ln = float(l10.get("n") or 0)

    out = ["Token用量统计",
           f"当前上下文总大小 {_n(cur.get('inp')) if cur.get('inp') else '—'} token",
           "",
           # 平均用量 = (输入+输出)/条数，别把输入当成总量
           f"最近10条平均用量 {_n((float(l10.get('inp') or 0) + float(l10.get('outp') or 0)) / ln) if ln else '—'}"
           f"（输入 {_n(float(l10.get('inp') or 0) / ln) if ln else '—'}"
           f" / 输出 {_n(float(l10.get('outp') or 0) / ln) if ln else '—'}）",
           f"最近10条平均缓存命中率 {_pct(l10.get('hit'), l10.get('inp'))}",
           "",
           "Token用量"]
    for name, w in wins:
        total = (w.get("inp") or 0) + (w.get("outp") or 0)
        out.append(f"  {name}  {_n(total) if w.get('n') else '—'}"
                   f"（输入 {_n(w.get('inp'))} / 输出 {_n(w.get('outp'))}）")

    for title, fn in (("平均聊天单条Token用量", lambda w: _per(w, "chat")),
                      ("平均联网搜索单条Token用量", lambda w: _per(w, "search")),
                      ("平均Token输出速度", lambda w: _speed(w.get("outp"), w.get("ms"))),
                      ("平均缓存命中率", lambda w: _pct(w.get("hit"), w.get("inp"))),
                      ("调用次数", lambda w: _n(w.get("n")) if w.get("n") else "—")):
        out.append("")
        out.append(title)
        for name, w in wins:
            out.append(f"  {name}  {fn(w)}")
    return "\n".join(out)
