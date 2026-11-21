"""Pixiv 按标签取一张「全年龄」原图。

R18 闸的做法：**只信 pixiv 自己的分级字段**，不信 tag。
- `xRestrict`：0=全年龄，1=R18，2=R18G
- `sl`：2=一般向（1 温和、4 成人）
这两个字段站点判的，作者改不了；tag 是作者自己填的，漏标错标太常见。
另外搜索时直接用 `mode=safe`（服务端已经筛过一轮），本地再拦一遍。
账号本身开了 R18 显示，接口会把成人作品一起返回，所以这里的过滤一次都不能省。
"""

import aiohttp
import json
import logging
import re
import random
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger("DeepSeekBot")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_REFERER = "https://www.pixiv.net/"

# tag 兜底黑名单：万一分级字段漏标，这些词也不许发
_R18_WORDS = ("r-18", "r18", "r-18g", "r18g", "グロ", "エロ", "guro", "nsfw", "adult", "18禁")

MAX_BYTES = 4 * 1024 * 1024  # 超过 4MB 就退到 regular（1200px），原图动辄十几 MB 发不出去


def _headers(cookie: str) -> Dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": _REFERER,
        "Accept": "application/json,image/*,*/*",
        "Cookie": cookie or "",
    }


def _is_safe(item: Dict[str, Any]) -> bool:
    """三道闸：官方分级 + 类型 + tag 黑名单。任一不过就丢。"""
    try:
        if int(item.get("xRestrict") or 0) != 0:
            return False
        if int(item.get("sl") or 0) != 2:
            return False
    except (TypeError, ValueError):
        return False
    # illustType: 0=插图 1=漫画 2=动图。动图要转码，这里不接
    if int(item.get("illustType") or 0) == 2:
        return False
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    if any(any(w in t for w in _R18_WORDS) for t in tags):
        return False
    return True


async def _get_json(session, url: str, cookie: str,
                    proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
    # 必须给 timeout。原来写 timeout=None → aiohttp 默认 300s，一次黑洞请求
    # 就能把整条群回复卡在拼消息链前（main 里是同步 await）。
    async with session.get(url, headers=_headers(cookie), proxy=proxy,
                           timeout=aiohttp.ClientTimeout(total=8, connect=5)) as r:
        if r.status != 200:
            logger.info("[fish] pixiv HTTP %s：%s", r.status, url[:80])
            return None
        raw = await r.text()
    try:
        return json.loads(raw)
    except Exception:
        logger.info("[fish] pixiv 返回不是 JSON：%s", raw[:80])
        return None


async def _search_once(session, word: str, cookie: str, proxy: Optional[str],
                       order: str = "popular_d", mode: str = "safe") -> List[str]:
    """一次搜索，返回过闸的 id 列表。搜不到返回空列表（不是异常）。

    线上真实踩过：同一个词「芙宁娜」手工 curl 有 31672 条，机器人这边
    偶尔返回 0 条——`order=popular_d` 配中文 tag 时服务端会给空页。
    所以这里把「空结果」当成可重试状态，换排序再要一次。
    """
    w = quote(word)
    url = (f"https://www.pixiv.net/ajax/search/artworks/{w}"
           f"?word={w}&order={order}&mode={mode}&p=1&s_mode=s_tag&type=all&lang=zh")
    data = await _get_json(session, url, cookie, proxy)
    if not data or data.get("error"):
        logger.info("[fish] pixiv 搜索失败（%s/%s）：%s", order, mode,
                    (data or {}).get("message", "无返回"))
        return []
    try:
        items = data["body"]["illustManga"]["data"]
    except Exception:
        logger.info("[fish] pixiv 返回结构异常：%s", str(data)[:120])
        return []
    ids = [str(it.get("id")) for it in items if it.get("id") and _is_safe(it)]
    logger.info("[fish] pixiv 搜「%s」(%s/%s)：%d 条结果，全年龄 %d 条",
                word, order, mode, len(items), len(ids))
    return ids


async def translate_hints(session, word: str, cookie: str,
                          proxy: Optional[str] = None) -> List[str]:
    """中文角色名换成 pixiv 认的写法：官方给的 romaji / 英文名。

    「芙宁娜」这种官方中文名，pixiv 有时按中文能搜、有时只能按
    `フリーナ`/`Furina` 搜到，联想接口里就带着这些写法，白拿不用。
    """
    w = quote(word)
    data = await _get_json(session, f"https://www.pixiv.net/ajax/search/tags/{w}"
                                    f"?word={w}&lang=zh&type=all", cookie, proxy)
    if not data or data.get("error"):
        return []
    tr = (data.get("body") or {}).get("tagTranslation") or {}
    out: List[str] = []
    for _tag, langs in tr.items():
        if not isinstance(langs, dict):
            continue
        for k in ("en", "romaji"):
            v = str(langs.get(k) or "").strip()
            # romaji 有时是拼接的乱码（furi-rixennmatahafuri-renn），太长就别用
            if v and v.lower() != word.lower() and len(v) <= 24 and v not in out:
                out.append(v)
    return out[:2]


def word_variants(word: str) -> List[str]:
    """把「崩坏星穹铁道 流萤 站姿」这类长串拆成几个能搜的候选。

    pixiv 多词条是 **AND**：每多一个不存在的 tag 就直接 0 条。
    实测「流萤」15433 条 →「崩坏星穹铁道 流萤」12352 条 →「流萤 站姿」0 条，
    因为「站姿」根本不是 pixiv 的 tag（日文写作「立ち姿」也搜不到）。
    所以从右往左逐个砍修饰词，最后再单拎每个词单独搜。

    中文不加空格也会连成串（「流萤的站姿」），所以「的」也当分隔符。
    """
    raw = (word or "").strip()
    if not raw:
        return []
    # `|` `、` `，` 是「多候选」分隔：各候选互不相干，别把整串拿去 AND
    groups = [g.strip() for g in re.split(r"[|｜、,，]+", raw) if g.strip()] or [raw]
    out: List[str] = []
    for g in groups:
        parts = [p for p in re.split(r"[\s　/]+", g) if p]
        if len(parts) == 1 and "的" in g:
            # 只按「的」拆一次，避免「喜欢的」这种把词拆碎
            head, _, tail = g.partition("的")
            parts = [p for p in (head, tail) if len(p) >= 2] or [g]
        out.append(g)
        for i in range(len(parts) - 1, 0, -1):
            out.append(" ".join(parts[:i]))
        out.extend(parts)
    seen, uniq = set(), []
    for v in out:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:5]


async def search_ids(session, word: str, cookie: str,
                     proxy: Optional[str] = None) -> List[str]:
    """按标签搜一般向作品。任一段拿到就收工：

    0. 先把长串拆成候选（见 word_variants），逐个用常用排序 + mode=safe 试
    1. 换时间排序（date_d）——popular_d 对中文 tag 偶尔返回空页
    2. 用联想接口给的英文名 / romaji 再搜一遍
    3. mode=all + 本地严格过闸（服务端不筛，我们自己筛，闸一步没少）
    """
    for cand in word_variants(word):
        ids = await _search_once(session, cand, cookie, proxy)
        if ids:
            if cand != word:
                logger.info("[fish] pixiv「%s」拆词搜到（用的「%s」）", word, cand)
            return ids
    ids = await _search_once(session, word, cookie, proxy, order="date_d")
    if ids:
        return ids
    for hint in await translate_hints(session, word, cookie, proxy):
        ids = await _search_once(session, hint, cookie, proxy)
        if ids:
            logger.info("[fish] pixiv「%s」改用译名「%s」搜到了", word, hint)
            return ids
    ids = await _search_once(session, word, cookie, proxy, mode="all")
    if ids:
        logger.info("[fish] pixiv「%s」safe 模式空页，改用 all + 本地过闸", word)
    return ids


async def fetch_one(session, word: str, cookie: str,
                    proxy: Optional[str] = None) -> Optional[Tuple[bytes, str, str]]:
    """搜一个标签 → 拿一张图。返回 (图片字节, 作品 id, 标题)；全被闸掉就 None。

    最多试 3 个候选：详情接口失败、图片 404、超过体积上限都能换下一张，
    比一次失败就放弃稳。
    """
    ids = await search_ids(session, word, cookie, proxy)
    if not ids:
        return None
    random.shuffle(ids)
    for pid in ids[:3]:
        # 详情接口偶发 ConnectionReset/超时，必须局部兜住：以前一个异常直接冒到
        # main._grab_pixiv 的 except，剩下两个候选根本没试，整次取图变成「没图」，
        # 而 docstring 写的是「能换下一张」。
        try:
            detail = await _get_json(session, f"https://www.pixiv.net/ajax/illust/{pid}",
                                     cookie, proxy)
        except Exception as e:
            logger.info("[fish] pixiv %s 详情请求异常，换下一个候选：%s: %s",
                        pid, type(e).__name__, e)
            continue
        if not detail or detail.get("error"):
            continue
        body = detail.get("body") or {}
        # 详情页的 tag 是全的，列表接口那个常常被截断，所以这里再查一遍黑名单
        _dtags = [str(t.get("tag") or "").lower()
                  for t in (body.get("tags") or {}).get("tags") or []]
        if any(any(w in t for w in _R18_WORDS) for t in _dtags):
            logger.info("[fish] pixiv %s 详情 tag 命中 R18 黑名单，跳过", pid)
            continue
        # 详情页同样要过一遍闸：列表接口和详情接口偶有不一致
        if int(body.get("xRestrict") or 0) != 0 or int(body.get("sl") or 0) != 2:
            logger.info("[fish] pixiv %s 详情页分级不是全年龄（xRestrict=%s sl=%s），跳过",
                        pid, body.get("xRestrict"), body.get("sl"))
            continue
        urls = body.get("urls") or {}
        for key in ("original", "regular"):
            u = urls.get(key)
            if not u:
                continue
            try:
                async with session.get(u, headers=_headers(cookie), proxy=proxy,
                                       timeout=aiohttp.ClientTimeout(total=25, connect=5)) as r:
                    if r.status != 200:
                        continue
                    data = await r.read()
            except Exception as e:
                logger.info("[fish] pixiv 图片下载失败（%s）：%s", key, e)
                continue
            if not data:
                continue
            if len(data) > MAX_BYTES:
                if key == "original":
                    continue  # 换 regular 再试
                logger.info("[fish] pixiv %s 图太大（%.1fMB），跳过", pid, len(data) / 1048576)
                continue
            return data, pid, str(body.get("title") or "")
    return None


class Cooldown:
    """同一个标签在窗口内不重复搜：pixiv 有频率限制，被 429 一次整段时间都别想用。

    两个修正：
      1) 记账放在取图成功之后（mark），失败不再白占 10 分钟 —— 以前搜不到/被 429
         也会把标签锁死，用户看到的就是「刚说一次，后面十次都不给图」。
      2) key 由调用方带上群号，冷却按群隔离，A 群刚问过不影响 B 群。
    """

    def __init__(self, window: float = 600.0) -> None:
        self.window = window
        self._last: Dict[str, float] = {}

    def allow(self, key: str, now: float) -> bool:
        """只判断，不记账。"""
        return now - self._last.get(key, 0.0) >= self.window

    def mark(self, key: str, now: float) -> None:
        self._last[key] = now
