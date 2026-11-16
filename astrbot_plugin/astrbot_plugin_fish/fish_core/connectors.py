# -*- coding: utf-8 -*-
"""联网扩展能力：trace.moe 截图搜番 + 免 Key 网页检索 + B 站热门推荐。

三条铁律（都是从旧仓库踩坑换来的）：
1. 所有请求都走调用方传进来的 aiohttp session —— 本模块自己不建连接池，
   超时/代理/连接数由插件统一管；测试时也能塞假 session 做离线跑。
2. 失败一律静默降级：返回空列表或 {"error": ...}，绝不抛异常。
   这些能力是「群里聊天时顺手加的料」，一次超时不该把整条消息处理链搞崩。
3. 用户可控的 URL（群友发的截图地址）必须先过公网校验，且建连后复核对端 IP，
   否则公网域名 302/rebinding 到 127.0.0.1 就能打内网，还会把内网响应转手外带。

本模块不依赖 astrbot，只用标准库 + aiohttp。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("DeepSeekBot")

# media_resolver 由同目录的兄弟模块提供。它还在移植中，没落地时不能让整个插件
# import 失败，所以这里兜底一份「宁可拒绝」的最小实现：功能一样，只是没有做
# DNS 结果缓存的优化。
try:  # pragma: no cover - 正常路径（media_resolver 已就位）
    from media_resolver import (  # type: ignore
        _is_public_url,
        _read_body,
        connected_peer_is_public,
    )
except ImportError:  # pragma: no cover - 兜底分支，自测里单独覆盖
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlparse

    def _is_public_ip(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        )

    async def _is_public_url(url: str) -> bool:
        """主机名解析成真实 IP 再判断：只看字符串挡不住 DNS 指向内网。"""
        try:
            parsed = urlparse(url)
            scheme, host = parsed.scheme, (parsed.hostname or "").lower()
        except ValueError:
            return False
        if scheme not in ("http", "https") or not host or host == "localhost":
            return False
        try:
            ipaddress.ip_address(host)
            return _is_public_ip(host)
        except ValueError:
            pass
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
            return bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)
        except Exception:
            return False

    def connected_peer_is_public(resp: Any) -> bool:
        """看 socket 对端地址，防 DNS rebinding；拿不到信息就按原校验放行。"""
        try:
            conn = getattr(resp, "connection", None)
            transport = getattr(conn, "transport", None)
            if transport is None:
                return True
            peer = transport.get_extra_info("peername")
            if not peer:
                return True
            ip = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
            return _is_public_ip(ip)
        except Exception:
            return True

    async def _read_body(resp: Any, limit: int = 2 * 1024 * 1024) -> bytes:
        """分块读到 EOF。不能用 content.read(limit) 一把梭：它可能半截就返回。"""
        declared = getattr(resp, "content_length", None)
        if declared and declared > limit:
            return await resp.content.read(limit)
        chunks: List[bytes] = []
        total = 0
        while total < limit:
            chunk = await resp.content.read(min(65536, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)


# ==================== 公共常量 ====================

# 搜番图片和网页正文都限死：群友贴个几百兆的链接就能把内存和事件循环一起拖死
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 512 * 1024
MAX_JSON_BYTES = 1024 * 1024

TRACE_MOE_API = "https://api.trace.moe/search?cutBorders"
# 检索结果和问题对不上的比例高得离谱：搜「50系显卡多少钱」必应会返回
# 「50（自然数之一）_百度百科」「现在的中国时间」。这种结果喂给模型，它只能
# 编一句「我连不上网」。所以先算字面重合度，不够就换引擎，再不够就当没搜到
_REL_THRESHOLD = 0.5
_QUERY_STOP_CHARS = set("的了是吗么呢吧啊呀多少钱现在最新一下帮我查搜请问最近")


def _relevance(query: str, items: list) -> float:
    """问题里有多少个字真的出现在返回结果里（0~1）。

    按字算而不是按词算：中文切词要 jieba，插件运行时不想为这点事挂依赖，
    而字面重合已经足够把百科/时间/新闻噪音筛掉。
    """
    chars = {c for c in str(query or "") if c.strip() and c not in _QUERY_STOP_CHARS
             and not c.isascii() or (c.isascii() and c.isalnum())}
    chars = {c for c in chars if c not in _QUERY_STOP_CHARS}
    # 问题里有效字太少（「x」「aa」这种）就没法判断相关性，直接放行，
    # 硬套阈值只会把所有短查询都判成不相关
    if len(chars) < 2:
        return 1.0
    if not items:
        return 0.0
    best = 0.0
    for it in items:
        text = str(it.get("title") or "") + str(it.get("snippet") or "")
        hit = sum(1 for c in chars if c in text)
        best = max(best, hit / len(chars))
    return best


_BING_SEARCH = "https://cn.bing.com/search"
_SOGOU_SEARCH = "https://www.sogou.com/web"
_SO360_SEARCH = "https://www.so.com/s"
_BILI_POPULAR = "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _timeout(total: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=total, connect=min(5.0, total))


def _declared_charset(resp: Any, raw: bytes) -> str:
    """响应头 → meta charset → 猜。老中文站还是 GBK，一律 utf-8 会得到乱码标题。"""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        return m.group(1)
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?([\w-]+)', head)
    if m:
        return m.group(1)
    return "utf-8"


def _decode_body(resp: Any, raw: bytes) -> str:
    charset = _declared_charset(resp, raw)
    for candidate in (charset, "utf-8", "gb18030"):
        try:
            return raw.decode(candidate, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


async def _fetch_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
    limit: int = MAX_PAGE_BYTES,
) -> Optional[Any]:
    """GET 一个公网地址，返回 (resp 头信息所需的上下文, body bytes)。

    返回值特意是 (resp, raw) 二元组而不是直接给解码后的文本：
    调用方要按 Content-Type 判图片、按 charset 解码 HTML、或者直接 json.loads。
    任何一步不对都返回 None —— 调用方据此静默降级，不抛异常。
    """
    if not await _is_public_url(url):
        logger.warning("拒绝请求非公网地址: %s", url[:120])
        return None
    try:
        async with session.get(
            url, headers=headers or {"User-Agent": _UA}, timeout=_timeout(timeout)
        ) as resp:
            # 首跳校验不够：公网域名 302 到内网时，aiohttp 默认跟随重定向，
            # 只能靠对端 IP 复核兜住。allow_redirects=False 会更严，但 bing/搜狗
            # 正常也会跳一次，这里保持跟随 + 复核。
            if not connected_peer_is_public(resp):
                logger.warning("拒绝请求：实际连到的不是公网地址（疑似重定向到内网）")
                return None
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > limit:
                logger.warning("响应体过大，放弃: %.1fMB %s", declared / 1024 / 1024, url[:80])
                return None
            raw = await _read_body(resp, limit + 1)
            if len(raw) > limit:
                logger.warning("响应体超限，放弃: %s", url[:80])
                return None
            return resp, raw
    except Exception as e:
        logger.debug("请求失败 %s: %s", url[:80], e)
        return None


# ==================== Trace.moe 搜番 ====================

async def _download_image(session: aiohttp.ClientSession, image_url: str) -> Optional[bytes]:
    """把群友发的图下载成字节。

    不走 trace.moe 的 url= 参数：那是让 trace.moe 自己去抓图，QQ/微信的图片 CDN
    基本不让它抓，群友截图十有八九返回 400/404。所以先自己扒下来再 multipart 直传。
    """
    fetched = await _fetch_bytes(session, image_url, timeout=10.0, limit=MAX_IMAGE_BYTES)
    if not fetched:
        return None
    resp, raw = fetched
    ctype = (resp.headers.get("Content-Type") or "").lower()
    # 状态码不是 200 或者不是图片：可能是登录页 / 防盗链 HTML
    if getattr(resp, "status", 200) != 200 or not ctype.startswith("image/"):
        logger.warning("搜番图片不可用: HTTP %s %s", getattr(resp, "status", "?"), ctype or "?")
        return None
    return raw or None


async def search_anime_by_image(
    session: aiohttp.ClientSession, image_url: str
) -> Optional[Dict[str, Any]]:
    """检索动画截图出处。

    失败时返回 {"error": ...} 而不是 None：调用方据此给群友一句交代
    （「这张图没搜到出处」比已读不回体面）。置信度低时返回 uncertain 标记。
    """
    try:
        image_bytes = await _download_image(session, image_url)
        if not image_bytes:
            return {"error": "download"}

        form = aiohttp.FormData()
        form.add_field("image", image_bytes, filename="anime.jpg", content_type="image/jpeg")

        # trace.moe 是固定公网端点，同样过一遍公网校验，保证「所有出站都受控」
        if not await _is_public_url(TRACE_MOE_API):
            logger.warning("Trace.moe 端点解析到非公网地址，跳过")
            return {"error": "unsafe_endpoint"}

        async with session.post(
            TRACE_MOE_API, data=form, timeout=_timeout(25.0)
        ) as resp:
            if not connected_peer_is_public(resp):
                logger.warning("Trace.moe 连接到的不是公网地址，跳过")
                return {"error": "unsafe_endpoint"}
            if resp.status == 402:
                logger.warning("Trace.moe 免费额度已用完")
                return {"error": "quota"}
            if resp.status != 200:
                logger.warning("Trace.moe 请求失败: HTTP %d", resp.status)
                return {"error": "http", "status": resp.status}

            # 不用 resp.json()：它会把整个响应体一次性读进内存，且半截读走时抛得很难看。
            # 走 _read_body 有上限、能拿全，再自己 json.loads。
            raw = await _read_body(resp, MAX_JSON_BYTES)
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, AttributeError):
                logger.warning("Trace.moe 返回非 JSON len=%d", len(raw))
                return {"error": "badjson"}
            if not isinstance(data, dict):
                return {"error": "badjson"}

            quota, used = data.get("quota"), data.get("quotaUsed")
            if isinstance(quota, int) and isinstance(used, int) and used >= quota:
                logger.warning("Trace.moe 额度告警: %d/%d", used, quota)

            results = data.get("result") or []
            if not isinstance(results, list) or not results:
                return {"error": "noresult"}

            best = results[0] if isinstance(results[0], dict) else {}
            try:
                similarity = float(best.get("similarity") or 0)
            except (TypeError, ValueError):
                similarity = 0.0

            # 低于 80% 相似度多半是「长得像」，硬报名字会误导群友
            if similarity < 0.80:
                return {
                    "uncertain": True,
                    "similarity": similarity,
                    "title": "未找到高确信度动画",
                    "data": best,
                }

            anilist = best.get("anilist") or {}
            titles = anilist.get("title") or {}
            if not isinstance(titles, dict):
                titles = {}
            title_native = titles.get("native") or titles.get("romaji") or "未知动画"
            title_chinese = titles.get("chinese") or title_native

            episode = best.get("episode") or "?"
            try:
                from_sec = int(best.get("from") or 0)
            except (TypeError, ValueError):
                from_sec = 0
            m, s = divmod(from_sec, 60)

            return {
                "title": title_chinese,
                "native_title": title_native,
                "episode": episode,
                "time": f"{m:02d}:{s:02d}",
                "similarity": round(similarity * 100, 1),
                "is_adult": bool(anilist.get("isAdult", False)),
            }
    except Exception as e:
        # 兜底不是多余：FormData、JSON 结构、连接异常都可能在上面的分支之外炸
        logger.warning("Trace.moe 搜番异常: %s", e)
        return {"error": "exception"}


# ==================== 免 Key 网页轻量检索 ====================

def _clean_html(raw: str) -> str:
    txt = html_lib.unescape(raw or "")
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = txt.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt).strip()


def _clamp_limit(limit: Any, default: int = 3, upper: int = 10) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, upper))


async def search_web_brief(
    session: aiohttp.ClientSession, query: str, limit: int = 3
) -> List[Dict[str, str]]:
    """免 Key 网页检索：必应 → 360 → 搜狗，谁先给出沾边的结果用谁，都不行就返回空列表。

    为什么不用 DuckDuckGo HTML：国内直连超时，搜索静默失败，表现是「问什么都答不上」。
    返回值固定是 [{"title": str, "snippet": str}, ...]，调用方不用判 None。
    """
    results: List[Dict[str, str]] = []
    limit = max(1, int(limit or 1))   # 0 或负数当 1 条，老调用方有传 0 的
    query = (query or "").strip()
    if not query:
        return results
    limit = _clamp_limit(limit)
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}

    async def _try_bing() -> list:
        out = []
        try:
            fetched = await _fetch_bytes(
                session, _BING_SEARCH + "?q=" + quote(query),
                headers=headers, timeout=10.0, limit=MAX_PAGE_BYTES)
            if not fetched:
                return out
            resp, raw = fetched
            if getattr(resp, "status", 200) != 200:
                logger.debug("bing 返回 HTTP %s", getattr(resp, "status", "?"))
                return out
            page = _decode_body(resp, raw)
            for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
                tm = re.search(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
                if not tm:
                    continue
                sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
                title = _clean_html(tm.group(1))
                if title:
                    out.append({"title": title,
                                "snippet": _clean_html(sm.group(1)) if sm else ""})
                if len(out) >= limit * 2:
                    break
        except Exception as e:
            logger.debug("bing 检索异常: %s", e)
        return out

    async def _try_360() -> list:
        """360 搜索。中文长尾（价格、行情、赛事）比必应准得多。

        必应中文对「50系显卡价格」这种词会返回「50（自然数之一）_百度百科」，
        360 直接给「RTX 50 系官方定价一览」，这条链路就是为它加的。
        """
        out = []
        try:
            fetched = await _fetch_bytes(
                session, _SO360_SEARCH + "?q=" + quote(query),
                headers=headers, timeout=10.0, limit=MAX_PAGE_BYTES * 4)
            if not fetched:
                return out
            resp, raw = fetched
            if getattr(resp, "status", 200) != 200:
                logger.debug("360 返回 HTTP %s", getattr(resp, "status", "?"))
                return out
            page = _decode_body(resp, raw)
            blocks = re.findall(r"<h3[^>]*>(.*?)</h3>(.*?)(?=<h3|$)", page, re.S)
            for head, rest in blocks:
                tm = re.search(r"<a[^>]*>(.*?)</a>", head, re.S)
                if not tm:
                    continue
                title = _clean_html(tm.group(1))
                if not title:
                    continue
                sm = re.search(r'<(?:p|div)[^>]*class="[^"]*(?:res-desc|summary)[^"]*"[^>]*>(.*?)</', rest, re.S)
                out.append({"title": title,
                            "snippet": _clean_html(sm.group(1)) if sm else _clean_html(rest)[:120]})
                if len(out) >= limit * 2:
                    break
        except Exception as e:
            logger.debug("360 检索异常: %s", e)
        return out

    async def _try_sogou() -> list:
        out = []
        try:
            fetched = await _fetch_bytes(
                session, f"{_SOGOU_SEARCH}?query={quote(query)}",
                headers=headers, timeout=10.0,
                # 搜狗结果页普遍 0.5~0.8MB，套通用上限会被整页丢掉
                limit=MAX_PAGE_BYTES * 6)
            if not fetched:
                return out
            resp, raw = fetched
            if getattr(resp, "status", 200) != 200:
                return out
            page = _decode_body(resp, raw)
            heads = re.findall(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", page, re.S)
            snips = re.findall(r'<div class="fz-mid space-txt">(.*?)</div>', page, re.S)
            for i, h in enumerate(heads):
                title = _clean_html(h)
                if not title:
                    continue
                out.append({"title": title,
                            "snippet": _clean_html(snips[i]) if i < len(snips) else ""})
                if len(out) >= limit * 2:
                    break
        except Exception as e:
            logger.debug("搜狗检索异常: %s", e)
        return out

    def _rank(items: list) -> list:
        qchars = {c for c in str(query) if c not in _QUERY_STOP_CHARS}
        wants_price = any(k in query for k in ("价格", "多少钱", "报价", "售价"))

        def score(it):
            title = str(it.get("title") or "")
            text = title + str(it.get("snippet") or "")
            s = sum(1 for c in qchars if c in title)
            # 问价钱时，摘要里带数字加金额单位的才是干货，纯导航页沉后面
            if wants_price and re.search(r"\d[\d,\.]*\s*(?:元|块|￥|¥|美元|万)", text):
                s += 4
            return -s

        return sorted(items, key=score)

    # 三家挨个试，谁先给出「跟问题沾边」的结果就用谁。
    # 全都不沾边就返回空：宁可说没查到，也不拿百科条目硬答
    for engine in (_try_bing, _try_360, _try_sogou):
        items = await engine()
        if items and _relevance(query, items) >= _REL_THRESHOLD:
            return _rank(items)[:limit]

    return []


# ==================== B 站热门/新番动态免登录抓取 ====================

# 负面过滤词：热门榜里混着大量直播回放、带货，群聊场景下纯噪音
_BILI_NEGATIVE_WORDS = ("直播回放", "录播", "拼多多", "抽奖", "领券", "教程", "带货")


async def fetch_bilibili_hot_videos(
    session: aiohttp.ClientSession, keyword: str = "", limit: int = 5
) -> List[Dict[str, Any]]:
    """抓 B 站免登录官方热门排行榜，可按 keyword 过滤标题/分区。

    返回 [{"bvid","title","author","tag","desc"}, ...]；接口风控或网络失败返回空列表。
    """
    filtered: List[Dict[str, Any]] = []
    limit = _clamp_limit(limit, default=5, upper=20)
    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    keyword = (keyword or "").strip().lower()

    try:
        fetched = await _fetch_bytes(
            session, _BILI_POPULAR, headers=headers, timeout=8.0, limit=MAX_JSON_BYTES
        )
        if not fetched:
            return filtered
        resp, raw = fetched
        if getattr(resp, "status", 200) != 200:
            logger.debug("B 站热门返回 HTTP %s", getattr(resp, "status", "?"))
            return filtered
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            logger.debug("B 站热门返回非 JSON len=%d", len(raw))
            return filtered

        # 风控时 B 站返回 {"code":-352,"data":null}：默认值只对缺失键生效，
        # 显式 null 会让 None.get() 抛 AttributeError —— 必须逐层判类型
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            logger.debug("B 站热门无数据（可能被风控）")
            return filtered
        video_list = payload.get("list") or []
        if not isinstance(video_list, list):
            return filtered

        for v in video_list:
            if not isinstance(v, dict):
                continue
            title = str(v.get("title") or "")
            if not title:
                continue
            if any(nw in title for nw in _BILI_NEGATIVE_WORDS):
                continue
            tname = str(v.get("tname") or "")
            if keyword and keyword not in title.lower() and keyword not in tname.lower():
                continue
            owner = v.get("owner")
            owner_name = str((owner or {}).get("name") or "") if isinstance(owner, dict) else ""
            filtered.append(
                {
                    "bvid": str(v.get("bvid") or ""),
                    "title": title,
                    "author": owner_name,
                    "tag": tname,
                    "desc": str(v.get("desc") or "")[:80],
                }
            )
            if len(filtered) >= limit:
                break
    except Exception as e:
        logger.debug("抓取 B 站热门异常: %s", e)
    return filtered
