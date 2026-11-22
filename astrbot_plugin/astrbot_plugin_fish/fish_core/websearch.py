"""免 Key 网页检索：给研究循环提供「真的能用」的搜索结果。

为什么不用旧的 `connectors.search_web_brief`：那三条链路（必应/360/搜狗）的
HTML 结构早就变了，实测搜「Redmi 15R tornado 解BL锁」返回的全是
GitHub 剪贴板工具、截图库——结构没解析出来，降级逻辑又把不相关的结果当成了
结果。更糟的是它返回的字段里没有 url，模型只有标题可看，想读正文都没地方读。

现在的顺序：
1. DuckDuckGo HTML（走本机代理，能给出原始 url，质量最稳）
2. 百度（免代理，中文长尾最全，代价是链接是跳转链，要跟着重定向）
3. 都没有 → 返回空，让模型明说「没查到」，绝不给它塞不相关的东西

排序：按查询词在标题/摘要里的覆盖率打分，覆盖太低的直接丢。宁可少给几条，
也不能把「搜到 6 条全是别的主题」喂给模型——它会照着编。
"""

from __future__ import annotations

import aiohttp
import asyncio
import html
import re
from typing import Dict, List
from urllib.parse import quote, unquote

from astrbot.api import logger

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_TIMEOUT = 12.0
# 本地代理：DDG 直连在国内基本没戏。只在本机地址上用，不算隐私面
_PROXY_HINT = "http://127.0.0.1:7890"

# 打分时不算数的字符：语气词、标点、泛用连接词
_GH_TOKEN = ""   # 首次用到时从 gh CLI 取，进程内缓存


def _gh_token() -> str:
    """拿 GitHub token：优先环境变量，其次 `gh auth token`。

    为什么要用真 token：匿名搜索是 10 次/分钟，群里连问几个问题就打满 403；
    登录后是 30 次/分钟，而且能看到私有仓库。
    """
    global _GH_TOKEN
    if _GH_TOKEN:
        return _GH_TOKEN
    import os as _os
    import subprocess as _sp

    tok = (_os.environ.get("GITHUB_TOKEN") or _os.environ.get("GH_TOKEN") or "").strip()
    if not tok:
        try:
            tok = _sp.run(["gh", "auth", "token"], capture_output=True, text=True,
                          timeout=6).stdout.strip()
        except Exception as e:
            logger.warning("[fish] 取 GitHub token 失败：%s", e)
    _GH_TOKEN = tok
    return tok


_STOP = set("的了是吗呢啊吧和与及或在有为个我要你他她它们这那么什么东西怎么怎么")


def _clean(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def _score(query: str, title: str, snippet: str) -> float:
    """查询词在两段文本里的覆盖率。0 表示完全不相干。"""
    qchars = {c for c in query if c.strip() and c not in _STOP}
    if not qchars:
        return 0.0
    pool = (title or "") + (snippet or "")
    hit = sum(1 for c in qchars if c in pool)
    return hit / len(qchars)


def _rank(query: str, rows: List[Dict[str, str]], limit: int) -> List[Dict[str, str]]:
    seen = set()
    scored = []
    for r in rows:
        url = (r.get("url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        s = _score(query, r.get("title", ""), r.get("snippet", ""))
        if s < 0.30:          # 三分之一都对不上，基本可以判定是别的主题
            continue
        scored.append((s + (0.2 if "http" in url and "baidu.com/link" not in url else 0), r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


async def _get(session, url: str, proxy: str | None):
    # 必须显式给 timeout。主 session 没设默认值 → aiohttp 默认 300s，
    # 代理一抖就是这个搜索线程挂 31 秒 0 结果，把 research 的 14s 工具预算吃光。
    async with session.get(url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                           proxy=proxy, timeout=aiohttp.ClientTimeout(total=6, connect=3)) as resp:
        if resp.status != 200:
            return ""
        raw = await resp.read()
        return raw.decode("utf-8", "ignore")


async def _duckduckgo(session, query: str, proxy: str | None) -> List[Dict[str, str]]:
    page = await _get(session, "https://html.duckduckgo.com/html/?q=" + quote(query), proxy)
    if not page:
        return []
    out = []
    titles = re.finditer(r'<a rel="nofollow" class="result__a" href="(.*?)"[^>]*>(.*?)</a>', page, re.S)
    snips = [_clean(x) for x in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)]
    for i, m in enumerate(titles):
        href = html.unescape(m.group(1))
        mm = re.search(r"uddg=([^&]+)", href)
        if mm:                       # DDG 的跳转链里带着真实地址
            href = unquote(mm.group(1))
        out.append({"title": _clean(m.group(2)), "url": href,
                    "snippet": (snips[i][:200] if i < len(snips) else "")})
    return out


async def _baidu(session, query: str, proxy: str | None) -> List[Dict[str, str]]:
    page = await _get(session, "https://www.baidu.com/s?wd=" + quote(query) + "&rn=10", proxy)
    if not page:
        return []
    out = []
    for m in re.finditer(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        title = _clean(m.group(2))
        if not title:
            continue
        # 百度摘要混在一堆嵌套标签里，拆不准；宁可不给，也别给半截错位文本
        out.append({"title": title, "url": html.unescape(m.group(1)), "snippet": ""})
    return out


async def github_repos(session, query: str, limit: int = 5) -> List[Dict[str, str]]:
    """GitHub 仓库搜索（免 token，未认证限 10 次/分钟，够用）。

    为什么要单独一路：「解 BL 锁有没有项目」这类问题，答案就躺在某个仓库的
    README 里（有没有人做出来、做到哪一步）。通用搜索引擎经常只给出卖服务的
    网页和文库，模型照着它们就会答成「官方支持常规解锁」——实测过，直接答错。
    """
    import json as _json

    q = (query or "").strip()
    if not q:
        return []
    url = ("https://api.github.com/search/repositories?q=" + quote(q)
           + "&sort=stars&order=desc&per_page=" + str(max(1, min(limit, 8))))
    try:
        headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
        tok = _gh_token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        async with session.get(url, headers=headers, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("[fish] GitHub 搜索 HTTP %s", resp.status)
                return []
            data = _json.loads(await resp.text())
    except Exception as e:
        logger.warning("[fish] GitHub 搜索失败：%s", e)
        return []
    out = []
    for it in (data.get("items") or [])[:limit]:
        desc = (it.get("description") or "").strip()
        out.append({
            "title": it.get("full_name", ""),
            "url": it.get("html_url", ""),
            "snippet": f"{desc}（★{it.get('stargazers_count', 0)}，更新 {str(it.get('pushed_at', ''))[:10]}）",
        })
    return out


# 三个引擎并发后的总上限。research 的工具预算是 14s，留出读正文的余量
_TOTAL_BUDGET = 8.0


async def search(session, query: str, limit: int = 6,
                 proxy: str | None = None) -> List[Dict[str, str]]:
    """返回 [{title,url,snippet}]，可能为空列表（空表示真没搜到）。"""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 6), 10))

    engines = (
        ("duckduckgo", _duckduckgo, proxy or _PROXY_HINT),
        ("baidu", _baidu, proxy),
        ("duckduckgo_direct", _duckduckgo, proxy),   # 代理挂了还能直连试试
    )
    # 串行跑最坏是 3×6s=18s，已经超过研究循环 14s 的工具预算——等于每次必然超时、
    # 每次都是空结果。改成并发：三个一起打，按原来的优先级顺序挑第一个有结果的，
    # 总时长压到单个引擎的水平；超预算的一律掐掉，不让它拖住整轮研究。
    tasks = [asyncio.create_task(fn(session, query, px)) for _n, fn, px in engines]
    try:
        done, _ = await asyncio.wait(tasks, timeout=_TOTAL_BUDGET)
        for (name, _fn, _px), task in zip(engines, tasks):
            if task not in done or task.cancelled():
                continue
            try:
                raw = task.result()
            except Exception as e:
                logger.warning("[fish] %s 检索失败：%s", name, e)
                continue
            rows = _rank(query, raw, limit)
            if rows:
                logger.info("[fish] 检索「%s」→ %s %d 条", query, name, len(rows))
                return rows
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    logger.warning("[fish] 检索「%s」无可用结果", query)
    return []
