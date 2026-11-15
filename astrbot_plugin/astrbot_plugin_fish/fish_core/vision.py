# -*- coding: utf-8 -*-
"""群里有人发图：拉下来交给 Gemini 白描一句，拼进 prompt。

设计取舍：
- 群聊是实时场景，等 40 秒等于没回。所以单模型超时压到 8~10 秒，失败立刻换下一个模型。
  实测 gemini-3.8-flash 高峰期 503 / 卡死很常见，降级链是必要的，不是防御性编程。
- 只取前 max_images 张，图片先缩到 640px 再传：原图几 MB 会让上传+推理都变慢，还要多花 token。
- 任何失败都返回空字符串，调用方当成「没图」继续走，绝不因为识图挂掉把回复整条吞掉。
- 密钥只从环境变量 / env 文件读，日志里只打 model 名和错误类型，绝不打 key。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import time

import aiohttp

PROMPT = (
    "这是一张 QQ 群聊里发的图。用两三句话白描，60 字以内：图里是什么、谁在做什么；"
    "图上的文字必须逐字抄下来（日期、时间、数字、人名一个都别漏），"
    "不许只写「有几行文字」。不要评论，不要以「这张图」开头。"
)

_env_cache = {"mtime": 0.0, "data": {}}
# 上次成功的模型排到队首：3.8-flash 高峰期经常 503，第一次失败后不该每次都先等它超时
_preferred = {"model": ""}


def _read_env_file(path: str) -> dict:
    """读 KEY=VALUE 形式的 env 文件，带 mtime 缓存。"""
    if not path:
        return {}
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


def _shrink(raw: bytes, max_side: int = 640, quality: int = 78) -> tuple[bytes, str]:
    """缩图。PIL 不在时装不了就直接原样发，别为了缩图把功能弄挂。"""
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


async def _one_model(session: aiohttp.ClientSession, key: str, model: str, parts: list[dict],
                     timeout: float, proxy: str | None) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0.2},
    }
    async with session.post(url, json=payload, proxy=proxy) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} {body[:120]}")
        data = await resp.json(content_type=None)
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("无 candidates（可能被安全策略拦了）")
    texts = [p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if p.get("text")]
    return "".join(texts).strip()


async def describe(images_b64: list[str], cfg: dict, logger=None) -> str:
    """images_b64：纯 base64（不带 data: 前缀）。全部失败返回 ""。"""
    if not images_b64:
        return ""
    max_images = int(cfg.get("vision_max_images", 2) or 2)
    imgs = [b for b in images_b64 if b][:max_images]
    if not imgs:
        return ""
    key, src = resolve_key(cfg)
    if not key:
        if logger:
            logger.warning("[fish] 视觉跳过：没找到 GEMINI_API_KEY / GOOGLE_API_KEY")
        return ""
    timeout = float(cfg.get("vision_timeout_sec", 10) or 10)
    proxy = str(cfg.get("vision_proxy") or "").strip() or None

    parts: list[dict] = [{"text": PROMPT}]
    for b64 in imgs:
        try:
            raw = base64.b64decode(b64)
        except Exception:
            continue
        small, mime = await asyncio.to_thread(_shrink, raw)
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(small).decode()}})
    if len(parts) == 1:
        return ""

    models = [str(cfg.get("vision_model") or "gemini-3.8-flash")]
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
    for model in ordered:
        # 总预算用完了就放弃：一张图等 20 秒，这条回复也没意义了
        if time.time() - started > budget:
            if logger:
                logger.warning("[fish] 视觉放弃：已用 %.1fs 超过总预算 %.0fs", time.time() - started, budget)
            break
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                             trust_env=False) as session:
                text = await _one_model(session, key, model, parts, timeout, proxy)
            if text:
                _preferred["model"] = model
                if logger:
                    logger.info("[fish] 视觉 %s 出描述（%.1fs，key 来自 %s）：%s",
                                model, time.time() - t0, src, text[:60])
                return text
        except Exception as e:
            if logger:
                logger.warning("[fish] 视觉 %s 失败（%.1fs）：%s: %s",
                               model, time.time() - t0, type(e).__name__, str(e)[:100])
            continue
    return ""


async def from_components(components: list, cfg: dict, logger=None) -> str:
    """把 AstrBot 的 Image 组件转成 base64 再描述。转换失败的单张跳过。"""
    out: list[str] = []
    limit = int(cfg.get("vision_max_images", 2) or 2)
    for comp in components:
        if len(out) >= limit:
            break
        try:
            out.append(await comp.convert_to_base64())
        except Exception as e:
            if logger:
                logger.warning("[fish] 拉图失败（跳过这张）：%s: %s", type(e).__name__, str(e)[:80])
    if not out:
        return ""
    return await describe(out, cfg, logger=logger)
