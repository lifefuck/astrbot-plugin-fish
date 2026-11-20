"""群友自然语言点歌的下载与发送。

为什么不直接调 listen-music 插件的接口：它内部的 MediaStore 只有一个并发闸门，
租约一旦泄漏（发送被取消、事件被打断都可能），后面所有请求会永久卡在 acquire 上，
实测直接卡满 180 秒然后空错误返回。这里复用它的 B 站客户端与音频封装代码，
但搜索、下载、发送跑在自己的生命周期里，坏了只坏这一首。
"""

import asyncio
import base64
import importlib
import os
import time

from astrbot.api import logger

_PKG = "data.plugins.astrbot_plugin_listen_music"

# 语音点歌只认「歌」：有声书、相声、吉他教程、超长合集都能搜出结果，
# 15 分钟的音频转成语音在群里要等一分多钟，用户只看到「点了没反应」
_MAX_SEC = 600      # 超过 10 分钟不当歌
_MIN_SEC = 25       # 太短的是切片/试听
_MAX_SEND_MB = 12.0  # 原文件超过这个体积才压（10 分钟的歌基本到不了）


class SongService:
    """一首歌一次完整的搜索 → 下载 → 发语音流程。"""

    def __init__(self, media_dir):
        self._media_dir = media_dir
        self._session = None
        self._media = None
        self._search = None
        self._delivery = None
        self._limits = None

    async def _ensure(self):
        # 两个群同时第一次点歌时，原来会各建一个 MediaStore、各跑一次
        # reclaim_stale()，而 reclaim 会把整个 media 目录 rmtree 掉——
        # 正在发送的那首歌的文件会被另一条并发路径删掉
        if self._delivery is not None:
            return
        lock = getattr(self, "_init_lock", None)
        if lock is None:
            lock = self._init_lock = asyncio.Lock()
        async with lock:
            if self._delivery is not None:
                return
            await self._init_once()

    async def close(self):
        """插件卸载/重载时收尾，否则每 reload 一次漏一个 aiohttp 会话。"""
        try:
            if self._session is not None and not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        self._session = None
        self._delivery = None
        self._search = None
        self._media = None

    async def _init_once(self):
        import aiohttp

        services = importlib.import_module(_PKG + ".core.services")
        bilibili_mod = importlib.import_module(_PKG + ".core.bilibili")
        media_mod = importlib.import_module(_PKG + ".core.media")

        self._session = aiohttp.ClientSession()
        bili = bilibili_mod.BilibiliClient(self._session)
        self._media = media_mod.MediaStore(self._session, self._media_dir)
        # reclaim_stale 会清空整个目录，只在初始化这一次调
        await self._media.reclaim_stale()
        self._limits = media_mod.VOICE_MEDIA_LIMITS
        self._search = services.SearchService(bili)
        self._delivery = services.DeliveryService(bilibili=bili, media=self._media)

    async def _shrink(self, src: str) -> str:
        """转成单声道 24k 采样、96kbps 的 mp3。

        采样率跟 QQ 语音本身的 silk 保持一致（24k），不做无用重采样；
        码率给到 96k 是因为实测发送耗时几乎不变（48k 23.1s / 96k 25.2s，
        瓶颈在 NapCat 转 silk 而不是上传体积），但喂给 silk 的输入干净得多。
        """
        dst = os.path.splitext(src)[0] + ".mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, "-vn", "-ac", "1", "-ar", "24000", "-b:a", "96k", dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # 长音频转码可能几十秒，卡住就掐掉，别让整条链路一起等
            await asyncio.wait_for(proc.wait(), 90)
        except asyncio.TimeoutError:
            logger.info("[fish] 点歌压缩超时，改发原始文件")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return src
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        logger.info("[fish] 点歌压缩失败，改发原始文件")
        return src

    async def _send_voice(self, event, gid: str, path: str) -> None:
        """直接走 OneBot 接口发语音。

        在事件处理器里 await event.send(...) 会一直挂着：AstrBot 的发送要过
        respond 阶段，那一步在等当前处理器收尾，等于自己等自己。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            raise RuntimeError("拿不到 OneBot 客户端")
        with open(path, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode()
        await bot.call_action(
            "send_group_msg",
            group_id=int(gid),
            message=[{"type": "record", "data": {"file": "base64://" + payload}}],
        )

    async def say(self, event, gid: str, text: str) -> None:
        """点歌慢了先说一声。和语音一样直调 OneBot，不走 respond 阶段。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        await bot.call_action(
            "send_group_msg",
            group_id=int(gid),
            message=[{"type": "text", "data": {"text": text}}],
        )

    async def play(self, event, query: str, session_id: str, gid: str, on_stage=None):
        """点歌入口：先看缓存，再按几种写法依次搜。

        为什么要有缓存：同一首歌一晚上被点三次很正常，每次重新搜+下载要三十秒，
        群里就是三次「点了没反应」。缓存按清洗后的歌名存，跟站上标题无关。
        """
        await self._ensure()
        hit = self._cache_get(query)
        if hit:
            logger.info("[fish] 点歌「%s」命中缓存，直接发", query)
            if on_stage is not None:
                try:
                    await on_stage(query)
                except Exception as e:
                    logger.debug("[fish] 点歌进度提示没发出去：%s", e)
            return await self._send_path(event, gid, hit)
        for q in self._variants(query):
            ok = await self._play_once(event, q, session_id, gid, on_stage)
            if ok == "nocand":
                # 只有「一首都没搜到」才值得换个写法再搜一次；
                # 搜到了但不符合条件（太长/太短）说明名字是对的，重试没意义
                continue
            return ok
        logger.info("[fish] 点歌「%s」试过 %s，都没搜到", query, "、".join(self._variants(query)))
        return False

    def _variants(self, query: str) -> list:
        """把歌名换几种常见写法。B 站搜索对多出来的修饰词很敏感。"""
        import re as _re
        base = _re.sub(r"[《》「」\"\'“”]", "", str(query or "")).strip()
        out = [base] if base else []
        # 去掉括号里的补充说明：「晴天（现场版）」「起风了 feat.xxx」
        no_paren = _re.sub(r"[（(【\[].*?[)）】\]]", "", base).strip()
        if no_paren and no_paren not in out:
            out.append(no_paren)
        # 去掉版本后缀：无损/高音质/完整版/翻唱 这些是搜索词，不是歌名的一部分
        no_tag = _re.sub(r"\s*(无损|高音质|完整版|mv|MV|现场版|翻唱|remix|Remix)$", "", no_paren or base).strip()
        if no_tag and no_tag not in out:
            out.append(no_tag)
        return out[:3]

    def _cache_dir(self) -> str:
        # 不能放进 _media_dir：它每次初始化都会被 reclaim_stale 整目录 rmtree，
        # 缓存等于每次启动归零，7 天 TTL 从来没生效过
        d = os.path.join(os.path.dirname(self._media_dir), "song_cache")
        os.makedirs(d, exist_ok=True)
        return d

    def _cache_get(self, query: str):
        """命中返回文件路径，过期或不存在返回 None。7 天没被点到就让它自然淘汰。"""
        import hashlib
        p = os.path.join(self._cache_dir(), hashlib.sha1(
            str(query).strip().lower().encode()).hexdigest() + ".mp3")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0 \
                    and time.time() - os.path.getmtime(p) < 7 * 86400:
                return p
        except OSError:
            pass
        return None

    def _cache_put(self, query: str, src: str) -> None:
        """把下好的文件拷进缓存目录。

        必须拷贝而不是记路径：media 目录每次初始化都会 reclaim_stale 整个清空，
        记下来的路径第二天就指向一个不存在的文件。
        """
        import hashlib, shutil
        try:
            dst = os.path.join(self._cache_dir(), hashlib.sha1(
                str(query).strip().lower().encode()).hexdigest() + ".mp3")
            shutil.copyfile(src, dst)
            logger.info("[fish] 点歌「%s」已缓存 %.1fMB", query, os.path.getsize(dst) / 1048576)
        except Exception as e:
            logger.debug("[fish] 点歌缓存写入失败：%s", e)

    async def _send_path(self, event, gid: str, path: str):
        """发一个已经在磁盘上的音频文件，返回 True / "uncertain" / False。"""
        small = ""
        try:
            small = path
            size_mb = os.path.getsize(small) / 1048576
            if size_mb > _MAX_SEND_MB:
                logger.info("[fish] 点歌文件 %.1fMB 偏大，先压一版", size_mb)
                small = await self._shrink(small)
            try:
                await asyncio.wait_for(self._send_voice(event, gid, small), 90)
            except asyncio.TimeoutError:
                logger.warning("[fish] 点歌发送等回包超时，按已发出处理")
                return "uncertain"
            return True
        except Exception as e:
            logger.warning("[fish] 点歌发送失败：%s %r", type(e).__name__, e)
            return False
        finally:
            try:
                if small and small != path and os.path.exists(small):
                    os.remove(small)
            except OSError:
                pass

    async def _play_once(self, event, query: str, session_id: str, gid: str, on_stage=None):
        """搜到就发语音；搜不到返回 \"nocand\"，别的失败返回 False，由调用方决定怎么圆场。

        on_stage(title)：挑中曲目之后回调一次，让上层能马上在群里吭一声，
        别让人干等几十秒还以为没点上。
        """
        await self._ensure()
        snapshot = await asyncio.wait_for(
            self._search.search(
                session_id=session_id,
                query=query,
                song_title=query,
                max_duration_ms=self._limits.max_duration_ms,
            ),
            30,
        )
        candidates = list(getattr(snapshot, "candidates", ()) or ())
        if not candidates:
            logger.info("[fish] 点歌搜不到「%s」", query)
            return "nocand"
        # 第一首经常是翻唱/剪辑，标题里带原词的那首更像原曲
        flat = query.replace(" ", "")

        def _ok(cand):
            dur = int(getattr(cand, "duration_ms", 0) or 0) / 1000
            return _MIN_SEC <= dur <= _MAX_SEC

        usable = [c for c in candidates if _ok(c)]
        if not usable:
            logger.info("[fish] 点歌「%s」只搜到超长/超短音频，不当歌发", query)
            return "nocand"
        # 标题带原词、时长又像首歌的那首最可能是原曲；光看标题会挑到「吉他教学 day4」
        chosen = None
        for cand in usable:
            if flat and flat in str(cand.display_title).replace(" ", ""):
                chosen = cand
                break
        chosen = chosen or usable[0]
        logger.info("[fish] 点歌「%s」命中 %s", query, chosen.display_title)
        if on_stage is not None:
            try:
                await on_stage(chosen.display_title)
            except Exception as e:
                logger.debug("[fish] 点歌进度提示没发出去：%s", e)

        t0 = time.monotonic()
        result = await asyncio.wait_for(
            self._delivery.deliver(chosen, limits=self._limits), 90
        )
        logger.info(
            "[fish] 点歌下载完成 %.1fs %.2fMB",
            time.monotonic() - t0,
            result.media.size_bytes / 1048576,
        )
        # 下完先存一份：这一首下次被点时直接发，省掉三十秒搜索+下载
        try:
            self._cache_put(query, str(result.media.path))
        except Exception:
            pass
        small = ""
        try:
            # 原文件直发：少一次有损转换，实测听感明显好，代价是多等几秒
            small = str(result.media.path)
            size_mb = os.path.getsize(small) / 1048576
            if size_mb > _MAX_SEND_MB:
                logger.info("[fish] 点歌原文件 %.1fMB 偏大，先压一版", size_mb)
                small = await self._shrink(small)
                size_mb = os.path.getsize(small) / 1048576
                if size_mb > _MAX_SEND_MB:
                    logger.info("[fish] 点歌压缩后仍 %.1fMB，放弃发送", size_mb)
                    return False
            t1 = time.monotonic()
            try:
                await asyncio.wait_for(self._send_voice(event, gid, small), 90)
            except asyncio.TimeoutError:
                # 超时只是没等到 NapCat 的回包，语音大概率已经进群了。
                # 这里必须区分「没发出去」和「不确定」：说「没弄下来」会让群里
                # 出现「歌 + 一句说没弄下来」的自相矛盾
                logger.warning("[fish] 点歌发送等回包超时（%.1fMB），按已发出处理", size_mb)
                return "uncertain"
            logger.info(
                "[fish] 点歌语音已发出 %.1fs %.2fMB",
                time.monotonic() - t1,
                os.path.getsize(small) / 1048576,
            )
        except Exception as e:
            logger.warning("[fish] 点歌发送失败「%s」：%s %r", query, type(e).__name__, e)
            return False
        finally:
            # 临时文件无论成败都要删：原来只在成功后删，失败一次留一个 mp3
            try:
                if small and os.path.exists(small) and small != str(result.media.path):
                    os.remove(small)
            except OSError:
                pass
            # 归还租约，否则下一首歌会卡在并发闸门上
            await self._media.release(result.media)
        return True
