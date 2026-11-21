# -*- coding: utf-8 -*-
"""群里有人发图：拉下来交给视觉模型白描一句，拼进 prompt。

默认先走 WB 池（wb/glm-5v-turbo → wb/minimax-m3.1-flash），Gemini 的框架
provider 和直连链只当兜底——Gemini 常年限流，指望它群聊会经常「瞎」。

设计取舍：
- 群聊是实时场景，等 40 秒等于没回。所以单模型超时压到 4 秒左右，失败立刻换下一个模型。
  实测 gemini-3.8-flash 高峰期 503 / 卡死很常见，降级链是必要的，不是防御性编程。
- 只取前 max_images 张，图片先缩到 640px 再传：原图几 MB 会让上传+推理都变慢，还要多花 token。
- 任何失败都返回空字符串，调用方当成「没图」继续走，绝不因为识图挂掉把回复整条吞掉。
- 密钥只从环境变量 / env 文件读，日志里只打 model 名和错误类型，绝不打 key。
- 图片自己下载，不走 AstrBot 的 Image.convert_to_base64()：那个入口对 URL 不做任何
  校验，群友发个 http://127.0.0.1:6185 或 http://169.254.169.254 就会被读回来，
  等于把内网当图床。这里保持旧仓的闸门：只放行 http/https + 公网 IP，并且限 8MB。
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

PROMPT = (
    "这是一张 QQ 群聊里发的图。用两三句话白描，60 字以内：图里是什么、谁在做什么；"
    "图上的文字必须逐字抄下来（日期、时间、数字、人名一个都别漏），"
    "不许只写「有几行文字」。不要评论，不要以「这张图」开头。"
)

# 图片体积上限，和旧仓保持一致：群友贴个几十 MB 的图能把内存和事件循环一起拖死
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 跳转层数上限：图片 CDN 一般 1~2 跳，多跳多半是短链/跳板，别跟着乱跑
_MAX_REDIRECTS = 3
_DEFAULT_TIMEOUT = 6  # 实测单次 3.6~3.9s，压到 4 秒会把正常请求一起掐死
_UA = "Mozilla/5.0 (compatible; AstrBot-Fish/1.0)"

# 模型偶尔把 PROMPT 当模板往下续写（13:21 真出现过：描述是「），不许只写「有几行文字」"。
# This is a template」）。这种脏描述喂进主 prompt 比没有图更糟，直接丢、换下一个模型
_PROMPT_ECHO = re.compile(r"不许只写|60\s*字以内|逐字抄下来|不要评论|This is a template")
# 必须在 PROMPT 定义之后才能编译，所以放在函数里第一次调用时再建


_PUNS = None


def _puns():
    """按文件路径加载 puns。

    不能用 `from . import puns`：fish_core 里的模块是 _load() 用
    spec_from_file_location 直接加载的，没有包上下文，相对导入必炸，
    而外面还套着 except Exception: pass——炸了也一声不响，谐音功能静默失效。
    """
    global _PUNS
    if _PUNS is None:
        import importlib.util
        from pathlib import Path as _Path
        path = _Path(__file__).with_name("puns.py")
        spec = importlib.util.spec_from_file_location("fish_core_puns", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PUNS = mod
    return _PUNS


def _clean_desc(text: str) -> str:
    """过滤掉 prompt 泄漏和太短的残句；不合格返回空串让上层换模型。"""
    t = (text or "").strip().strip("\"'“”‘’「」 ")
    if len(t) < 4:
        return ""
    if _PROMPT_ECHO.search(t):
        return ""
    return t

_env_cache = {"mtime": 0.0, "data": {}}
# 上次成功的模型排到队首：3.8-flash 高峰期经常 503，第一次失败后不该每次都先等它超时
_preferred = {"model": ""}
# 复用一条连接。原来每次 describe 都新建 ClientSession，等于每张图重做一遍
# DNS + TLS + 代理握手，实测冷启动能吃掉 3~4 秒，直接顶满 4 秒超时
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(trust_env=False)
    return _session


async def close() -> None:
    """插件卸载时收掉连接，避免 Unclosed client session 告警。"""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _read_env_file(path: str) -> dict:
    """读 KEY=VALUE 形式的 env 文件，带 mtime 缓存。"""
    if not path:
        return {}
    # 配置里默认写的是 ~/.config/...，不展开就是「文件不存在」，key 直接读不到
    path = os.path.expanduser(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _env_cache["mtime"] == mtime and _env_cache["data"]:
        return _env_cache["data"]
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    _env_cache.update(mtime=mtime, data=data)
    return data


def resolve_key(cfg: dict) -> tuple[str, str]:
    """返回 (key, 来源)。来源只用于日志里说明「从哪读到的」，不含 key 本身。"""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VISION_API_KEY", "VISION_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip(), f"env:{name}"
    file_key = ""
    path = str(cfg.get("vision_key_env_file") or "")
    if path:
        env = _read_env_file(path)
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VISION_API_KEY", "VISION_KEY"):
            if env.get(name):
                file_key = env[name]
                break
    if file_key:
        return file_key.strip(), "envfile"
    cfg_key = str(cfg.get("vision_api_key") or "").strip()
    if cfg_key:
        return cfg_key, "config"
    return "", ""


# ---------------------------------------------------------------- SSRF 闸门


def _is_public_ip(ip_str: str) -> bool:
    """私有 / 环回 / 链路本地（含 169.254.169.254 元数据服务）一律不算公网。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


async def _url_is_public(url: str) -> tuple[bool, str]:
    """先看 scheme，再把主机名解析成真实 IP 判断，返回 (是否放行, 原因)。

    只看字符串挡不住「域名 A 记录指向 10.x」的写法，必须 getaddrinfo；
    解析出的所有地址都必须是公网，任何一个落在内网就整条拒绝。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "URL 解析失败"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"scheme={scheme or '空'} 不在 http/https 白名单"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL 没有主机名"
    if host == "localhost" or host.endswith(".localhost"):
        return False, "localhost 指向本机"
    try:
        ipaddress.ip_address(host)
        return (_is_public_ip(host), "公网 IP" if _is_public_ip(host) else "IP 落在私有/环回/链路本地段")
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except Exception as e:
        return False, f"DNS 解析失败 {type(e).__name__}"
    if not infos:
        return False, "DNS 无结果"
    for info in infos:
        addr = info[4][0]
        if not _is_public_ip(addr):
            return False, f"域名解析到非公网地址 {addr}"
    return True, "公网域名"


def _peer_ip(resp) -> str | None:
    """复核这条连接实际连到了哪个 IP。

    `_url_is_public` 先解析一次，aiohttp 建连时又解析一次；对方用自己控 DNS 的
    域名（TTL=0）就能第一次返回公网、第二次返回 127.0.0.1，校验形同虚设。
    直接看 socket 对端地址，rebinding 也躲不过。拿不到对端信息时不误伤。
    """
    try:
        transport = getattr(getattr(resp, "connection", None), "transport", None)
        if transport is None:
            return None
        peer = transport.get_extra_info("peername")
        if not peer:
            return None
        return peer[0] if isinstance(peer, (tuple, list)) else str(peer)
    except Exception:
        return None


async def _read_capped(resp, limit: int) -> bytes:
    """分块读响应体，超过 limit 立刻中止。

    不能用 `resp.content.read(limit)` 一把梭：它的语义是「最多给这么多、手头有多少先返多少」，
    慢网下会半截返回。这里按块循环，既拿全又守住内存上限。
    """
    declared = resp.content_length
    if declared and declared > limit:
        raise ValueError(f"Content-Length {declared} 超过上限 {limit} 字节")
    buf = bytearray()
    while True:
        chunk = await resp.content.read(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ValueError(f"下载超过 {limit} 字节上限，已中止（防大文件拖死内存）")
    return bytes(buf)


async def _download_image(session: aiohttp.ClientSession, url: str, *, logger=None,
                          timeout: float = 10.0, limit: int = MAX_IMAGE_BYTES) -> bytes | None:
    """自己下载图片：逐跳校验公网地址 + 限体积。任何异常都返回 None（当没图）。"""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        ok, why = await _url_is_public(current)
        if not ok:
            if logger:
                logger.warning("[fish] 拉图被 SSRF 闸拦下（%s）：%s", why, current[:120])
            return None
        try:
            async with session.get(
                current, allow_redirects=False, headers={"User-Agent": _UA},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    if not loc:
                        if logger:
                            logger.warning("[fish] 拉图跳转无 Location：%s", current[:120])
                        return None
                    current = urljoin(current, loc)
                    continue
                if resp.status != 200:
                    if logger:
                        logger.warning("[fish] 拉图 HTTP %s：%s", resp.status, current[:120])
                    return None
                ip = _peer_ip(resp)
                if ip and not _is_public_ip(ip):
                    if logger:
                        logger.warning("[fish] 拉图实际连到非公网 %s（疑似 DNS rebinding），放弃：%s",
                                       ip, current[:120])
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                # 只收图片：返回 HTML 说明撞到了登录页/错误页，缩图也会失败
                if ctype and not (ctype.startswith("image/") or "octet-stream" in ctype):
                    if logger:
                        logger.warning("[fish] 拉图 Content-Type 不是图片（%s）：%s",
                                       ctype.split(";")[0], current[:120])
                    return None
                return await _read_capped(resp, limit)
        except Exception as e:
            if logger:
                logger.warning("[fish] 拉图失败 %s: %s：%s", type(e).__name__, str(e)[:80], current[:120])
            return None
    if logger:
        logger.warning("[fish] 拉图跳转超过 %d 层，放弃：%s", _MAX_REDIRECTS, url[:120])
    return None


def _local_roots() -> tuple[str, ...]:
    """允许多读的本地目录：插件自己的目录、AstrBot 数据目录、系统临时目录。

    AstrBot 收到群图会先落盘到 data 下，拒绝所有本地路径会把正常图片一起挡掉；
    但放开整个文件系统又等于任意文件读取，所以按白名单目录收口。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here]
    # 插件住在 data/plugins/<名字>，往上两级就是 AstrBot 的 data/：
    # get_astrbot_data_path() 取不到时（导入失败/版本变化）靠它兜住，
    # 否则群图落盘在 data/temp 下会被白名单全挡掉，模型永远看不到图
    roots.append(os.path.abspath(os.path.join(here, "..", "..")))
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        roots.append(get_astrbot_data_path())
    except Exception:
        pass
    roots.append(tempfile.gettempdir())
    out = []
    for r in roots:
        if not r:
            continue
        try:
            out.append(os.path.realpath(str(r)))
        except OSError:
            continue
    return tuple(out)


def _read_local(path: str, limit: int = MAX_IMAGE_BYTES) -> bytes:
    """读本地图片，只允许白名单目录内且不超体积。不在白名单里直接拒绝。"""
    if not path:
        raise ValueError("本地路径为空")
    real = os.path.realpath(path)
    roots = _local_roots()
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise PermissionError(f"本地路径不在允许目录内：{real}")
    size = os.path.getsize(real)
    if size > limit:
        raise ValueError(f"本地文件 {size} 字节超过上限 {limit}")
    with open(real, "rb") as f:
        return f.read()


def _decode_b64(src: str, limit: int = MAX_IMAGE_BYTES) -> bytes:
    raw = base64.b64decode(src)
    if len(raw) > limit:
        raise ValueError(f"base64 解码后 {len(raw)} 字节超过上限 {limit}")
    return raw


async def _component_bytes(comp, session: aiohttp.ClientSession, logger=None) -> bytes | None:
    """从一个 AstrBot Image 组件拿到图片字节。取不到返回 None（调用方跳过）。"""
    url = str(getattr(comp, "url", "") or "").strip()
    file = str(getattr(comp, "file", "") or "").strip()
    path = str(getattr(comp, "path", "") or "").strip()
    src = url or file or path
    if not src:
        if logger:
            logger.warning("[fish] 图片组件没有任何 url/file/path，跳过")
        return None
    low = src.lower()
    # base64 直传的图不碰网络，直接解；但同样要限体积
    if low.startswith("base64://"):
        return await asyncio.to_thread(_decode_b64, src[len("base64://"):])
    if low.startswith("data:"):
        _, _, payload = src.partition(",")
        return await asyncio.to_thread(_decode_b64, payload)
    scheme = (urlparse(src).scheme or "").lower()
    if scheme in ("http", "https"):
        # 关键：不再交给 AstrBot 的 Image.convert_to_base64() 代下，否则任何 URL 都放行
        return await _download_image(session, src, logger=logger)
    if scheme == "file":
        return await asyncio.to_thread(_read_local, unquote(urlparse(src).path))
    if scheme:
        if logger:
            logger.warning("[fish] 图片 scheme=%s 不支持（只收 http/https/base64/file），跳过", scheme)
        return None
    # 无 scheme：AstrBot 有时给的是本地路径
    return await asyncio.to_thread(_read_local, path or src)


async def component_bytes(comp, logger=None) -> bytes | None:
    """对外入口：拿一个消息组件的原始字节（图片、语音都走这里）。

    复用同一套 SSRF 防护和连接池——语音的 url 同样是对方给的，不能绕过这层检查。
    """
    return await _component_bytes(comp, _get_session(), logger=logger)


# ---------------------------------------------------------------- 缩图 / 请求


def _shrink(raw: bytes, max_side: int = 640, quality: int = 78) -> tuple[bytes, str]:
    """缩图。PIL 不在时就原样发，别为了缩图把功能弄挂。"""
    try:
        from PIL import Image as PILImage
    except Exception:
        return raw, "image/jpeg"
    try:
        im = PILImage.open(io.BytesIO(raw))
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, "image/jpeg"


def _is_thinking_model(model: str) -> bool:
    """gemini-4-* 是思考型：默认思考能吃满 1500 token，正文一个字都不剩。

    实测 gemini-4-hydrogen 传 maxOutputTokens=400 时返回 200 但 candidates 文本为空
    （思考 397 token，正文 3 token），看着像「模型挂了」，其实是预算被推理吃光。
    """
    return str(model or "").lower().startswith("gemini-4")


def _gen_config(model: str) -> dict:
    if _is_thinking_model(model):
        # thinkingBudget=0 关不掉思考（仍固定烧 ~425 token），但能限制它不到处乱想；
        # 输出额度必须给到 1500，否则思考和正文抢预算
        return {"maxOutputTokens": 1500, "temperature": 0.2,
                "thinkingConfig": {"thinkingBudget": 0}}
    return {"maxOutputTokens": 400, "temperature": 0.2}


def _model_timeout(model: str, base: float) -> float:
    """思考型模型单个请求实测 7.5~11s，套 6 秒超时等于必然放弃。"""
    return max(base, 14.0) if _is_thinking_model(model) else base


async def _one_model(session: aiohttp.ClientSession, key: str, model: str, parts: list[dict],
                     timeout: float, proxy: str | None) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": _gen_config(model),
    }
    # 超时挂在单次请求上：会话是整条降级链共用的，不在这里关
    async with session.post(url, json=payload, proxy=proxy,
                            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
        data = await resp.json(content_type=None)
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("无 candidates（可能被安全策略拦了）")
    texts = [p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if p.get("text")]
    return "".join(texts).strip()


# ---------------------------------------------------------------- OpenAI 兼容通道（WB 池）
# 为什么加这一路：Gemini 那边老是限流，群里发张图鱼就瞎了。WB 池（本地 3425 网关）
# 上的 glm-5v-turbo / minimax-m3.1-flash 实测 2.6~4.3s 出描述、连打 6 发不限流，
# 所以默认先走它，Gemini 的框架通道和直连链降级到后面当兜底。
_WB_DEFAULT_BASE = "http://127.0.0.1:3425/v1"
_WB_DEFAULT_MODELS = ["wb/glm-5v-turbo", "wb/minimax-m3.1-flash"]

# 有的 WB 模型（实测 minimax-m3.1-flash 偶尔）会把「思考过程」当正文吐出来，
# 原样塞进 prompt 就是一句「We need inspect tiny image. Need exact text」——
# 这种描述用不了，当成空结果换下一个模型
_WB_SCRATCH_RE = re.compile(
    r"^\s*(we need|let me|i need|i'll|the user|okay|first,|sure|looking at|analyze)",
    re.IGNORECASE)
_WB_SCRATCH_ANY = re.compile(r"image \d{2,4}x\d{2,4}|need (exact|to inspect)", re.IGNORECASE)


def _looks_like_scratch(text: str) -> bool:
    """判断是不是模型没写完的思考碎片，不是给用户看的描述。"""
    t = (text or "").strip()
    if not t:
        return True
    if _WB_SCRATCH_RE.match(t) or _WB_SCRATCH_ANY.search(t):
        return True
    # 中文群里要的是中文描述；全英文且没一个汉字，基本是思考泄漏
    return not re.search(r"[\u4e00-\u9fff]", t)


async def _one_model_openai(session: aiohttp.ClientSession, base: str, key: str, model: str,
                            imgs: list[tuple[bytes, str]], timeout: float) -> str:
    """OpenAI 兼容的图片请求。imgs 是 (字节, mime) 列表。"""
    content: list[dict] = [{"type": "text", "text": PROMPT}]
    for raw, mime in imgs:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"},
        })
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 400,
        "temperature": 0.2,
    }
    url = base.rstrip("/") + "/chat/completions"
    async with session.post(url, json=payload, headers={"Authorization": f"Bearer {key or 'placeholder'}"},
                            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
    data = json.loads(body)
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:120])
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    text = (msg.get("content") or "").strip()
    if not text:
        # WB 上 deepseek-v4.1-flash 这类把正文全塞 reasoning_content，content 是空串，
        # 直接判「识别失败」会白白丢掉一个能用的模型
        text = (msg.get("reasoning_content") or "").strip()
    return text


async def _describe_wb(images: list[bytes], cfg: dict, logger=None) -> str:
    """先走 WB 池。全挂返回空串，交给后面的 Gemini 链路。"""
    base = str(cfg.get("vision_wb_base") or "").strip()
    if not base or not cfg.get("vision_wb_enable", True):
        return ""
    models = [str(m) for m in (cfg.get("vision_wb_models") or _WB_DEFAULT_MODELS) if m]
    imgs: list[tuple[bytes, str]] = []
    for raw in images[: int(cfg.get("vision_max_images", 2) or 2)]:
        if not raw:
            continue
        try:
            imgs.append(await asyncio.to_thread(_shrink, raw))
        except Exception:
            imgs.append((raw, "image/jpeg"))
    if not imgs:
        return ""

    key = str(cfg.get("vision_wb_key") or "")
    budget = float(cfg.get("vision_total_budget_sec", 12) or 12)
    started = time.time()
    session = _get_session()
    for model in models:
        used = time.time() - started
        if used > budget:
            if logger:
                logger.warning("[fish] WB 视觉放弃：已用 %.1fs 超过预算 %.0fs", used, budget)
            break
        # 冷启动那次 glm 实测要 8.6 秒，给到 10 秒，别把主力模型的第一发判死
        per_model = max(1.0, min(10.0, budget - used))
        t0 = time.time()
        try:
            text = await _one_model_openai(session, base, key, model, imgs, per_model)
            if text and _looks_like_scratch(text):
                if logger:
                    logger.warning("[fish] WB 视觉 %s 吐的是思考碎片，跳过：%s",
                                   model, text[:60])
                continue
            if text:
                if logger:
                    logger.info("[fish] WB 视觉 %s 出描述（%.1fs）：%s",
                                model, time.time() - t0, text[:60])
                try:
                    extra = _puns().note(text)
                    if extra:
                        text = f"{text}\n谐音（按图里的字查到的）：{extra}"
                except Exception:
                    pass
                return text
            if logger:
                logger.warning("[fish] WB 视觉 %s 返回空（%.1fs）", model, time.time() - t0)
        except Exception as e:
            if logger:
                logger.warning("[fish] WB 视觉 %s 失败（%.1fs）：%s: %s",
                               model, time.time() - t0, type(e).__name__, str(e)[:100])
    return ""


# 框架视觉通道：AstrBot 里配了带图片能力的 provider（gemini-vision）之后，
# 识别交给框架做，本文件自己的直连链路只当兜底。
# 为什么留兜底而不是删干净：框架 provider 被停用/额度打满时，鱼会突然「瞎」，
# 那时候宁可多花一次直连请求，也别让群里发图没人应。
_framework_caller = None


def set_framework_caller(fn) -> None:
    """由 main.py 在初始化时注入：async fn(list[bytes]) -> str"""
    global _framework_caller
    _framework_caller = fn


async def _describe_via_framework(images: list[bytes], cfg: dict, logger=None) -> str:
    if _framework_caller is None or not cfg.get("vision_use_framework_provider", True):
        return ""
    try:
        text = await _framework_caller(images)
    except Exception as e:
        # 拿不到就往下走直连，不在这里吞成「图中没有内容」
        if logger:
            logger.warning("[fish] 框架视觉失败（改用直连）：%s: %s", type(e).__name__, str(e)[:80])
        return ""
    return (text or "").strip()


async def _describe_raw(images: list[bytes], cfg: dict, logger=None) -> str:
    """images：原始图片字节。全部失败返回 \"\"。"""
    if not images:
        return ""
    # 顺序：WB 池（快、不限流）→ 框架 provider（Gemini）→ 直连 Gemini
    via_wb = await _describe_wb(images, cfg, logger=logger)
    if via_wb:
        return via_wb
    via_fw = await _describe_via_framework(images, cfg, logger=logger)
    if via_fw:
        return via_fw
    key, src = resolve_key(cfg)
    if not key:
        if logger:
            logger.warning("[fish] 视觉跳过：没找到 GEMINI_API_KEY / GOOGLE_API_KEY")
        return ""
    max_images = int(cfg.get("vision_max_images", 2) or 2)
    imgs = [b for b in images if b][:max_images]
    if not imgs:
        if logger:
            logger.warning("[fish] 视觉跳过：没有可用图片字节")
        return ""

    timeout = float(cfg.get("vision_timeout_sec", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    proxy = str(cfg.get("vision_proxy") or "").strip() or None

    parts: list[dict] = [{"text": PROMPT}]
    for i, raw in enumerate(imgs, 1):
        try:
            small, mime = await asyncio.to_thread(_shrink, raw)
        except Exception as e:
            # 缩图失败不该整条放弃：原样发，让模型自己扛
            if logger:
                logger.warning("[fish] 第 %d 张缩图失败（改用原图 %d 字节）：%s: %s",
                               i, len(raw), type(e).__name__, str(e)[:60])
            small, mime = raw, "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(small).decode()}})

    models = [str(cfg.get("vision_model") or "gemini-3.5-flash-lite")]
    models += [str(m) for m in (cfg.get("vision_fallback_models") or [])]
    seen: set[str] = set()
    ordered = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    if _preferred["model"] in ordered:
        ordered.remove(_preferred["model"])
        ordered.insert(0, _preferred["model"])

    budget = float(cfg.get("vision_total_budget_sec", 12) or 12)
    started = time.time()
    # 整条降级链共用一个会话：每次重开 ClientSession 既慢又容易在异常路径上漏关闭
    session = _get_session()
    for model in ordered:
        used = time.time() - started
        # 总预算用完了就放弃：一张图等 20 秒，这条回复也没意义了
        if used > budget:
            if logger:
                logger.warning("[fish] 视觉放弃：已用 %.1fs 超过总预算 %.0fs", used, budget)
            break
        # 单模型超时不能超过剩余预算，否则最后一个模型会把总时长拖穿
        per_model = max(1.0, min(_model_timeout(model, timeout), budget - used))
        t0 = time.time()
        try:
            text = await _one_model(session, key, model, parts, per_model, proxy)
            if text:
                _preferred["model"] = model
                if logger:
                    logger.info("[fish] 视觉 %s 出描述（%.1fs，key 来自 %s）：%s",
                                model, time.time() - t0, src, text[:60])
                # 谐音梗补一句：模型能把字抄对，但常看不出「夺笋」是「多损」
                try:
                    extra = _puns().note(text)
                    if extra:
                        text = f"{text}\n谐音（按图里的字查到的）：{extra}"
                except Exception:
                    pass
                return text
        except Exception as e:
            if logger:
                logger.warning("[fish] 视觉 %s 失败（%.1fs）：%s: %s",
                               model, time.time() - t0, type(e).__name__, str(e)[:100])
            continue
    return ""


async def describe(images_b64: list[str], cfg: dict, logger=None) -> str:
    """images_b64：纯 base64（不带 data: 前缀）。全部失败返回 \"\"。"""
    if not images_b64:
        return ""
    max_images = int(cfg.get("vision_max_images", 2) or 2)
    raws: list[bytes] = []
    failed = 0
    for i, b64 in enumerate(images_b64[:max_images], 1):
        if not b64:
            failed += 1
            continue
        try:
            raws.append(base64.b64decode(b64))
        except Exception as e:
            # 这里以前是静默 continue，图全坏掉时上层只看到「没图」，根本不知道为什么
            failed += 1
            if logger:
                logger.warning("[fish] 视觉跳过第 %d 张：base64 解码失败 %s: %s",
                               i, type(e).__name__, str(e)[:60])
    if not raws:
        if logger:
            logger.warning("[fish] 视觉放弃：%d 张图全部解码失败", failed or len(images_b64))
        return ""
    # 过一遍清洗：模型偶尔把「根据人设」这类提示词复述当成描述返回
    return _clean_desc(await _describe_raw(raws, cfg, logger=logger))


async def from_components(components: list, cfg: dict, logger=None) -> str:
    """把 AstrBot 的 Image 组件取成字节再描述。单张取不到就跳过并记一笔。"""
    if not components:
        return ""
    limit = int(cfg.get("vision_max_images", 2) or 2)
    raws: list[bytes] = []
    skipped = 0
    # 下载阶段共用一个会话：QQ 图床是公网 https，不走代理（trust_env=False）
    async with aiohttp.ClientSession(trust_env=False) as session:
        for i, comp in enumerate(components, 1):
            if len(raws) >= limit:
                break
            try:
                raw = await _component_bytes(comp, session, logger=logger)
            except Exception as e:
                skipped += 1
                if logger:
                    logger.warning("[fish] 第 %d 张图取不到（跳过）：%s: %s",
                                   i, type(e).__name__, str(e)[:80])
                continue
            if not raw:
                skipped += 1
                continue
            raws.append(raw)
    if not raws:
        if logger:
            logger.warning("[fish] 视觉放弃：%d 张图全部没拿到（已跳过 %d 张）",
                           len(components), skipped)
        return ""
    if skipped and logger:
        logger.info("[fish] 视觉：拿到 %d 张，跳过 %d 张", len(raws), skipped)
    return await _describe_raw(raws, cfg, logger=logger)
