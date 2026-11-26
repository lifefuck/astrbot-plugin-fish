"""听觉：把群里别人发的语音转成文字，让鱼真的听到内容再搭话。

为什么要自己写而不是装现成插件：市面上的 STT 插件都走框架的 request_llm 二次喂回，
等于再插一条会自己回话的链路，跟鱼的接话闸抢答。这里只做「转文字」一件事，
拿到的文本当普通消息内容用，回不回、怎么回还是鱼自己那套判定。

链路：silk（QQ 语音）→ pysilk 解成 pcm(24k) → ffmpeg 转 mp3 → Gemini inline audio → 文本
"""

import asyncio
import base64
import io
import re
import subprocess

# 语音本身很小，但有人会发超长录音。超过这个体积直接放弃，不为一条语音卡住整条回复
MAX_AUDIO_BYTES = 6 * 1024 * 1024
# 转写结果拿来当「这条消息说了什么」，太长的截断，别把上下文挤爆
MAX_TEXT_CHARS = 200

DEFAULT_PROMPT = (
    "这是一段群聊语音。把说话内容原样转成中文文字：只输出说的话，"
    "不要加引号、不要解释、不要描述语气。听不清就写「听不清」。"
    "有方言口音照原样写，别改成人话。"
)

_vision = None

# Gemini 转中文会逐字带空格，汉字/CJK 标点之间的一律压掉
_CJK_GAP_RE = re.compile(r"(?<=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])\s+(?=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])")


def configure(vision_module) -> None:
    """复用 vision 的 key 解析、连接池和请求封装，不重复造一遍降级链。"""
    global _vision
    _vision = vision_module


def _is_silk(raw: bytes) -> bool:
    return b"#!SILK" in raw[:16]


def _silk_to_pcm(raw: bytes) -> bytes:
    """QQ 语音是 silk，Gemini 不认，先解成裸 pcm。

    采样率固定 24k，跟 QQ 语音本身的 silk 一致，省一次重采样。
    """
    import pysilk  # 延迟导入：没装依赖时整个插件不该起不来

    out = io.BytesIO()
    pysilk.decode(io.BytesIO(raw), out, 24000)
    return out.getvalue()


def _ffmpeg_mp3(data: bytes, *, pcm: bool = False) -> bytes:
    """ffmpeg 统一转成 mp3。

    pcm=True 表示输入是裸 s16le 单声道流（刚从 silk 解的），否则让 ffmpeg
    自己嗅探容器（amr / wav / m4a / ogg 都认）。
    """
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if pcm:
        args += ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    args += ["-i", "pipe:0", "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1"]
    proc = subprocess.run(args, input=data, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b"ffmpeg failed")[:160].decode("utf-8", "ignore"))
    return proc.stdout


def _to_mp3(raw: bytes) -> bytes:
    if _is_silk(raw):
        return _ffmpeg_mp3(_silk_to_pcm(raw), pcm=True)
    return _ffmpeg_mp3(raw)


# 静音阈值：真静音 mean_volume 约 -91 dB，正常说话在 -20 上下。取 -50 只挡
# 「录了半天没出声」，不碰清辅音多的小声说话
_SILENT_DB = -50.0


def _is_silent(audio: bytes) -> bool:
    """mp3 字节是否基本没有声音。

    Gemini 听到静音会凭空编一句人话（实测 4 秒 anullsrc 转出「那就是有点忙呢」，
    440Hz 正弦转出「喂」），这句假的还会被当成用户原话喂给模型。
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", "pipe:0",
             "-af", "volumedetect", "-f", "null", "-"],
            input=audio, capture_output=True, timeout=30,
        )
    except Exception:
        return False  # 测不出来就别拦，宁可多花一次转写
    m = re.search(rb"mean_volume:\s*(-?[\d.]+) dB", proc.stderr or b"")
    if not m:
        return False
    return float(m.group(1)) < _SILENT_DB


async def transcribe(raw: bytes, cfg: dict, logger=None) -> str:
    """字节 → 说话内容。任何一步失败都返回空串，由调用方决定怎么退化。"""
    if not raw or len(raw) > MAX_AUDIO_BYTES:
        if raw and logger:
            logger.info("[fish] 语音 %.1fMB 超上限，不转写", len(raw) / 1048576)
        return ""
    if _vision is None:
        return ""

    try:
        audio = await asyncio.to_thread(_to_mp3, raw)
    except Exception as e:
        if logger:
            logger.warning("[fish] 语音解码失败（silk/ffmpeg）：%s: %s", type(e).__name__, str(e)[:100])
        return ""
    if not audio:
        return ""
    if _is_silent(audio):
        if logger:
            logger.info("[fish] 语音基本是静音（mean_volume < %.0f dB），不送转写", _SILENT_DB)
        return ""

    key, key_src = _vision.resolve_key(cfg)
    if not key:
        if logger:
            logger.warning("[fish] 语音转文字没有可用的 Gemini key")
        return ""
    model = str(cfg.get("hearing_model") or "gemini-3.5-flash-lite")
    parts = [
        {"text": str(cfg.get("hearing_prompt") or DEFAULT_PROMPT)},
        {"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(audio).decode()}},
    ]
    timeout = float(cfg.get("hearing_timeout_sec", 30) or 30)
    try:
        text = await _vision._one_model(
            _vision._get_session(), key, model, parts,
            timeout=timeout, proxy=str(cfg.get("vision_proxy") or "") or None,
        )
    except Exception as e:
        if logger:
            logger.warning("[fish] 语音转写失败（%s，key 来自 %s）：%s: %s",
                           model, key_src, type(e).__name__, str(e)[:100])
        return ""

    text = (text or "").strip().strip("「」\"'")
    # Gemini 转中文时常逐字带空格（「生 贷 转 两 百」），汉字之间的一律压掉：
    # 这些空格既占 token，也会被模型当成生硬断句
    if text:
        text = _CJK_GAP_RE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if text in ("听不清", "无法识别", "空"):
        return ""
    return text[:MAX_TEXT_CHARS]


async def from_components(comps: list, cfg: dict, logger=None) -> str:
    """从消息里的 Record 组件取第一段语音转写。取不到返回空串。"""
    if not comps or not _vision:
        return ""
    comp = comps[0]
    try:
        raw = await _vision.component_bytes(comp, logger=logger)
    except Exception as e:
        if logger:
            logger.warning("[fish] 语音下载失败：%s: %s", type(e).__name__, str(e)[:100])
        return ""
    return await transcribe(raw or b"", cfg, logger=logger)
