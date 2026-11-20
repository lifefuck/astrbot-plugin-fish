#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站视频下载 —— 群里贴链接说「发我」「下下来」时把片子本身发过去

只解析文字卡片是敷衍：人家要的是视频。这里用 yt-dlp 下载后用 OneBot 的视频段发出去。

为什么要有临时 HTTP：
NapCat 常跑在另一台机器（盒子/小主机）上，读不到本机的绝对路径，file:// 传不过去。
base64:// 能发（实测 13MB 的片要 21 秒），HTTP 只要 4.5 秒，所以优先走 HTTP。

格式为什么钉死 avc1（H.264）：
B 站默认给 av1 轨，体积只有 H.264 的六分之一，但很多安卓机没法硬解 av1，
QQ 播出来就是黑屏一片。群里发片子兼容性优先，宁可大一点。

作者: life (lifefuck)
"""

import asyncio
import base64
import glob
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("DeepSeekBot")

# BV 号 / av 号 / b23.tv、bili2233.cn 短链 / 番剧 ep·ss / 直播间
# 也认光秃秃的 BV 号：群里多数人就是复制一串 BV1xxxxxxxxx 甩过来，不带 https
_BILI_RE = re.compile(
    r"(?:"
    r"https?://(?:"
    r"(?:www\.|m\.)?bilibili\.com/(?:video/(?:BV[0-9A-Za-z]{10}|av\d+)|bangumi/play/(?:ep|ss)\d+|live/\d+)"
    r"|b23\.tv/[0-9A-Za-z]+"
    r"|bili2233\.cn/[0-9A-Za-z]+"
    r")"
    r"|(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10}|av\d{5,})(?![0-9A-Za-z])"
    r")",
    re.I,
)

LAST_REASON = ""

# 「发视频过来」的意图词。只贴链接不算——那还是要卡片，别把每个链接都下一遍
_WANT_RE = re.compile(
    r"(发(?:我|过来|给我|一下|来)|下(?:载|下来|一下|来)|传(?:过来|给我)|"
    r"发到群里|转(?:发|过来)|整个(?:视频|原片)|原片|完整视频|要视频)"
)


def find_url(text: str) -> str:
    m = _BILI_RE.search(text or "")
    if not m:
        return ""
    raw = m.group(0)
    if raw.lower().startswith(("http", "b23", "bili2233")):
        return raw
    # 群里甩过来的往往是光秃秃的 BV 号，yt-dlp 只认完整地址
    return "https://www.bilibili.com/video/" + raw


def wants_video(text: str) -> bool:
    """贴了 B 站链接，还明说要片子本身"""
    if not find_url(text):
        return False
    return bool(_WANT_RE.search(text or ""))


def _ytdlp() -> str:
    # 装在 astrbot 的 venv 里，直接调同目录的二进制，不走 PATH
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        cand = os.path.join(here, ".venv", "bin", "yt-dlp")
        if os.path.exists(cand):
            return cand
        here = os.path.dirname(here)
    return shutil.which("yt-dlp") or "yt-dlp"


def _run(args, timeout: float) -> Tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "超时"
    except Exception as e:  # noqa: BLE001
        return -2, repr(e)


# 画质档位。默认不锁编码：B 站自己会给 AV1，同样 720p 体积只有 H.264 的六分之一，
# 群里发得动也发得快。播放黑屏的话群友喊一句就按 force_h264 重下（见最后那档）。
_FORMATS = (
    ("bv*[height<=720]+ba[ext=m4a]/b[height<=720]", "720p"),
    ("bv*[height<=480]+ba[ext=m4a]/b[height<=480]", "480p"),
    ("bv*[height<=360]+ba[ext=m4a]/b[height<=360]", "360p"),
)
_H264_FORMATS = (
    ("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][vcodec^=avc1]", "720p"),
    ("bv*[height<=480][vcodec^=avc1]+ba[ext=m4a]/b[height<=480][vcodec^=avc1]", "480p"),
)

_CODEC_CN = {"av1": "AV1", "hevc": "H.265", "h264": "H.264", "vp9": "VP9", "vp8": "VP8"}


def probe_codec(path: str) -> str:
    """看成品到底是什么编码。手机上 AV1 常常放不出来，得知道发的是哪种才敢给提示"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return (p.stdout or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def probe_height(path: str) -> int:
    """成品实际高度。yt-dlp 报的是「请求到的档位」，源最高只有 480p 时会自动降档，
    照 label 写「720p」等于骗人"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return int((p.stdout or "0").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def download(
    url: str,
    dest_dir: str,
    max_bytes: int = 80 * 1024 * 1024,
    max_duration: int = 900,
    timeout: float = 180,
    force_h264: bool = False,
) -> Optional[Dict[str, Any]]:
    """下载一条 B 站视频，返回 {path, title, size, quality, codec}；失败返回 None

    失败原因写进模块级 LAST_REASON，调用方拿它拼提示语（时长超限 / 体积超限 / 网络）。
    force_h264=True 时不问编码直接要 H.264，用于「AV1 播不了，换个格式」。
    """
    global LAST_REASON
    LAST_REASON = ""
    os.makedirs(dest_dir, exist_ok=True)
    exe = _ytdlp()
    if not (os.path.exists(exe) or shutil.which(exe)):
        logger.warning("[bili] 找不到 yt-dlp，视频下载不可用")
        LAST_REASON = "nofile"
        return None

    # 先摸一遍元信息：时长超限的直接放弃，别下完 200MB 才发现是两小时直播回放
    code, out = _run([exe, "-J", "--no-warnings", "--no-playlist", "--flat-playlist", url], 60)
    title = ""
    if code == 0:
        try:
            import json as _json
            info = _json.loads(out[out.index("{"):out.rindex("}") + 1])
            title = str(info.get("title") or "")
            dur = float(info.get("duration") or 0)
            if dur and dur > max_duration:
                logger.info("[bili] 时长 %d 秒超过上限 %d，改成只发卡片", dur, max_duration)
                LAST_REASON = "duration"
                return None
        except Exception:
            pass

    picked = _H264_FORMATS if force_h264 else _FORMATS
    for fmt, label in picked:
        # 前缀带毫秒时间戳：yt-dlp 的 %(id)s 是它自己替换的，不能拿去 glob（会当成字面量）
        prefix = os.path.join(dest_dir, f"bili_{int(time.time() * 1000)}_")
        code, out = _run(
            [exe, "-f", fmt, "--merge-output-format", "mp4", "--no-playlist",
             "--no-warnings", "--no-progress", "--max-filesize", str(max_bytes),
             "-o", prefix + "%(id)s.%(ext)s", url],
            timeout,
        )
        # 必须排掉 yt-dlp 的中间分片：合并前它会先落 <id>.f137.mp4（纯视频轨），
        # 名字同样匹配 *.mp4。ffmpeg 合并失败时把它当成品发出去，群里收到的是无声视频
        made = sorted(
            (p for p in glob.glob(prefix + "*.mp4") if not re.search(r"\.f\d+\.mp4$", p)),
            key=os.path.getmtime, reverse=True,
        )
        if not made:
            logger.info("[bili] %s 下载失败（%s）：%s", label, code, out.strip()[-200:])
            # 文件超限和网络失败要分开：前者能提示用户，后者只能安静算了
            if "max-filesize" in out or "File is larger than max-filesize" in out:
                LAST_REASON = "size"
            continue
        path = made[0]
        size = os.path.getsize(path)
        if size > max_bytes:
            logger.info("[bili] %s 成品 %dMB 超过上限，换低清", label, size // 1024 // 1024)
            os.remove(path)
            LAST_REASON = "size"
            continue
        # 元信息那一趟拿不到标题时，用文件名兜底
        if not title:
            code2, out2 = _run([exe, "--get-title", "--no-warnings", "--no-playlist", url], 30)
            title = out2.strip().splitlines()[0] if code2 == 0 and out2.strip() else ""
        height = probe_height(path)
        return {"path": path, "title": title or "B站视频", "size": size,
                "quality": (f"{height}p" if height else label),
                "codec": probe_codec(path)}
    if not LAST_REASON:
        LAST_REASON = "nofile"
    return None


def to_data_url(path: str) -> str:
    """HTTP 服务起不来时的兜底：base64 直塞（慢，但一定能发出去）"""
    with open(path, "rb") as f:
        return "base64://" + base64.b64encode(f.read()).decode()


def cleanup(dest_dir: str, keep_sec: int = 3600) -> None:
    """超过保留期的成品一律删掉，别让磁盘被视频撑爆"""
    now = time.time()
    # .part / 独立音轨是超时或中断的正规产物，只清 *.mp4 会让目录只增不减
    for pat in ("*.mp4", "*.part", "*.m4a", "*.temp.*", "*.ytdl"):
        for p in glob.glob(os.path.join(dest_dir, pat)):
            try:
                if now - os.path.getmtime(p) > keep_sec:
                    os.remove(p)
            except OSError:
                continue


# ---------------------------------------------------------------- 临时 HTTP

_runner = None
_port = 0
_server_lock: Optional[asyncio.Lock] = None


async def ensure_server(dest_dir: str) -> int:
    """惰性起一个只读静态服务，端口自动挑空闲的；起不来返回 0"""
    global _runner, _port, _server_lock
    if _port:
        return _port
    # 两个群同时贴链接会一起进到这里，各自起一个 runner 各占一个端口，
    # 先建的那个没人回收，端口池（8891-8900）被吃光后退化成 base64 直塞
    if _server_lock is None:
        _server_lock = asyncio.Lock()
    async with _server_lock:
        if _port:
            return _port
        return await _ensure_server_locked(dest_dir)


async def _ensure_server_locked(dest_dir: str) -> int:
    global _runner, _port
    try:
        from aiohttp import web

        async def _health(_req):
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/_health", _health)
        app.router.add_static("/v/", dest_dir, show_index=False)
        _runner = web.AppRunner(app, access_log=None)
        await _runner.setup()
        for port in range(8891, 8901):
            try:
                site = web.TCPSite(_runner, "0.0.0.0", port)
                await site.start()
                _port = port
                logger.info("[bili] 临时视频服务监听 :%d", port)
                return port
            except OSError:
                continue
        await _runner.cleanup()
        _runner = None
    except Exception as e:  # noqa: BLE001
        logger.warning("[bili] 临时视频服务起不来：%r", e)
    return 0


async def video_ref(path: str, dest_dir: str, host: str) -> str:
    """返回能给 NapCat 用的引用：优先 http，失败退 base64"""
    port = await ensure_server(dest_dir)
    if port:
        return f"http://{host}:{port}/v/{os.path.basename(path)}"
    return to_data_url(path)


async def shutdown() -> None:
    global _runner, _port
    if _runner is not None:
        try:
            await _runner.cleanup()
        except Exception:
            pass
    _runner = None
    _port = 0
