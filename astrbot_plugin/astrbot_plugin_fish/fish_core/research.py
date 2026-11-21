"""被 @ 的求助问题：带工具的多轮检索。

为什么要这个文件：提示词里写「你要联网查」是没用的——模型手上没有工具，
它只会拿训练记忆编一个听起来很像的答案。实测同一个模型，不给工具时问
「REDMI 15R 解 BL 锁」，它连代号都答「不知道，听说叫 ruby」；给了 web_search
它立刻自己连调两次。所以能力差距不在模型，在「有没有把工具递给它」。

三个坑，都是实测踩出来的：

1. **只给搜索摘要不够。** 搜出来的是标题 + 一句 snippet，答案里的关键细节
   （代号、芯片型号、状态表的日期）只存在于正文。所以必须配 fetch_page。
2. **正文必须截断。** 一个页面动辄几百 KB，原样塞进上下文，两三轮就把窗口
   撑爆、还把钱烧光。这里按字符数硬截。
3. **循环必须有上限。** 模型会一直搜下去，尤其是搜不到的时候。轮数和总时长
   都要卡死，超了就拿手上的东西收尾，宁可答得不全也不能把群挂住。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger

try:
    from astrbot.core.agent.tool import FunctionTool, ToolSet
except Exception:  # 版本差异：没工具就让上层降级成普通回答
    FunctionTool = None
    ToolSet = None

# 系统提示词只在「干活模式」用，跟闲聊的人设分开：
# 人设负责语气，这里负责「只写查得到的东西」
RESEARCH_SYSTEM = (
    "你在群里被问了正经问题，可以用 web_search、fetch_page、github_search 查了再答。\n"
    "做法：先搜这型号的代号和芯片，再搜「代号 + 解锁/bootloader」；问项目/ROM/解锁进度的，"
    "用 github_search 找仓库再看正文；细节要打开页面读正文，别只看摘要。\n"
    # 以前这里写的是「一条一条写要点，内容多就多写几条，讲清为止」，
    # 实测直接产出 1396 字清单，群里没人看。群里要的是懂行的人随口接一句
    "回答：就一段话，两三句说完，总共别超过 120 字，像一个懂行的人在群里随口答；"
    "不分条、不列清单、不写小标题、不加粗、不放表格；最多附一个最关键的链接，"
    "没有就不放。别复述我的问题，别解释你查了哪些站。\n"
    "只能写查得到的东西，查不到就说没查到，不许猜。付费维修站、文库、营销页说的「支持」"
    "不算证据，别拿它当结论。"
)

_PAGE_MAX_CHARS = 1600      # 单页正文上限：够看清型号和结论，又不至于撑爆窗口
_ROUND_MAX = 2              # 最多几轮工具调用（实测 4 轮 = 123 秒，2 轮够用）
_TOTAL_SEC = 45.0           # 整个检索过程的上限，超了就拿现有材料收尾
# 单次工具调用上限，按类型分：搜索本身就要 6~9 秒（实测 duckduckgo 8.4s），
# 一个统一 9 秒会把正常搜索砍成超时；读页卡住则没必要等那么久
_TOOL_SEC_SEARCH = 14.0
_TOOL_SEC_FETCH = 8.0
_LLM_SEC = 32.0             # 单轮模型调用上限
_ANSWER_MAX = 220           # 成稿硬上限，超了按句号截断
_WIDGET_PARALLEL = True     # 同一轮的多个工具调用并发跑，别串行等


_WORK_HINT = re.compile(
    r"吗|怎么|如何|为什么|有没有|能不能|可不可以|可以吗|哪[个里家些]|什么|多少|几点|"
    r"求|帮[我忙]|找[一下个]|查[一下找]|搜|地址|链接|项目|方案|教程|文档|下载|安装|配置|"
    r"支持|兼容|推荐|对比|区别|区别是|是不是|真的假的|可靠|靠谱|报错|失败|闪退|进不去|"
    r"解锁|刷机|固件|驱动|版本|型号|代号|芯片"
)
_CHITCHAT_ONLY = re.compile(r"^(哈+|呵+|嘿+|在吗|在不在|早|晚安|草|卧槽|牛|6+|[？?]+)[\s。！!~～]*$")


def detect(question: str) -> bool:
    """这句是不是「求事实/求方法」，而不是闲扯。

    不用模型判：这一步要快，而且误判的代价不对称——多跑一次检索只是慢几秒，
    漏跑一次就是硬编一个假答案。所以宁可宽进，只把明显是打招呼/情绪的话挡掉。
    """
    t = (question or "").strip()
    if len(t) < 6:
        return False
    if not _WORK_HINT.search(t):
        return False
    return not _CHITCHAT_ONLY.search(t)


def _github_repo(url: str) -> str:
    """github.com/owner/repo[/...] → owner/repo，其他返回空串。"""
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else ""


async def _read_page(http, url: str, limit_chars: int = _PAGE_MAX_CHARS) -> str:
    """读一个公开网页的正文纯文本。fetch_page 工具和「自动补正文」都走这里。"""
    from . import connectors

    url = (url or "").strip()
    gh = _github_repo(url)
    if gh:
        # GitHub 的网页是 JS 壳，直接抓 HTML 只有框架（实测模型只能看到标题）。
        # 走 API 取 README 原文，才是这个仓库真正的说明。
        import os as _os

        tok = _os.environ.get("GITHUB_TOKEN", "")
        headers = {"Accept": "application/vnd.github.raw", "User-Agent": "fish-bot"}
        if not tok:
            try:
                import subprocess as _sp
                tok = _sp.run(["gh", "auth", "token"], capture_output=True, text=True,
                              timeout=6).stdout.strip()
            except Exception:
                tok = ""
        if tok:
            headers["Authorization"] = "Bearer " + tok
        try:
            async with http.get(f"https://api.github.com/repos/{gh}/readme",
                                headers=headers) as resp:
                if resp.status == 200:
                    return (await resp.text())[:limit_chars]
        except Exception as e:
            logger.info("[fish] 读 GitHub README 失败 %s：%s", gh, e)
        # 读不到 README 也继续走下面的 HTML 兜底
    try:
        ok = await connectors._is_public_url(url)
    except Exception:
        ok = False
    if not ok:
        return ""      # 内网地址一律不读：这是给公开网页用的工具
    try:
        async with http.get(url) as resp:
            if resp.status != 200:
                return ""
            raw = await connectors._read_body(resp, limit=1024 * 1024)
        text = connectors._clean_html(connectors._decode_body(resp, raw))
    except Exception as e:
        logger.info("[fish] 读页失败 %s：%s", url[:60], e)
        return ""
    return " ".join(text.split())[:limit_chars]


def _tidy_answer(text: str) -> str:
    """把成稿压成一段话：去列表符号/小标题，超长按句末截断。

    提示词管不住模型写清单（实测 1396 字、十几条要点）。这里做最后一道：
    群里要的是「随口接一句」，不是报告。
    """
    parts: list[str] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        # 先去掉加粗/标题符号再剥列表符号：反过来的话「**结论」会被剥成一个「*结论」
        s = s.replace("**", "").replace("__", "")
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^[-*•·]\s*|^\d+[.、)]\s*|^[一二三四五六七八九十]+[、.)]\s*", "", s)
        parts.append(s)
    out = " ".join(parts).strip()
    if len(out) > _ANSWER_MAX:
        cut = out[:_ANSWER_MAX]
        idx = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("."))
        out = cut[: idx + 1] if idx >= 60 else cut.rstrip("，,、；;：: ") + "…"
    return out


def _toolset(http, proxy: str | None = None, sources: list | None = None) -> "tuple[ToolSet, dict] | None":
    """把两个工具包成框架的 ToolSet，同时返回 {名字: 协程函数}。

    为什么要另外返回 handler：这个版本（4.x）的 FunctionTool.call() 是直接
    raise NotImplementedError 的，构造函数里给的 handler 根本不会被调用，
    实测每次都返回「must be implemented by subclasses」。与其去继承它的类，
    不如自己留一份名字到函数的映射，谁都不依赖。

    http 用插件已有的 aiohttp 会话，别另开连接池。
    """
    if ToolSet is None:
        return None

    async def web_search(query: str = "") -> str:
        """联网搜索。返回若干条结果的标题、链接和摘要。"""
        from . import websearch

        try:
            hits = await websearch.search(http, str(query or "").strip(), limit=6,
                                           proxy=proxy)
        except Exception as e:
            return f"搜索失败：{e}"
        if not hits:
            return "没有结果"
        # 只留模型真正用得上的字段，多余的键会白烧 token
        rows = [{"title": h.get("title", ""), "url": h.get("url", ""),
                 "snippet": (h.get("snippet") or "")[:200]} for h in hits]
        # 自动读前两条的正文。实测模型虽然握着 fetch_page，但它经常只看摘要就
        # 下结论——型号代号、状态表这类关键细节只在正文里，不补就等于没查。
        # 这一步放在代码里做，不赌模型的自觉。
        for r in rows[:2]:
            body = await _read_page(http, r["url"], 900)
            if body:
                r["text"] = body
                if sources is not None:
                    sources.append(r["url"])
        return json.dumps(rows, ensure_ascii=False)

    async def github_search(query: str = "") -> str:
        """搜 GitHub 仓库。问「有没有项目/做到哪一步了」时用，比通用搜索准。"""
        from . import websearch

        try:
            rows = await websearch.github_repos(http, str(query or "").strip(), limit=5)
        except Exception as e:
            return f"搜索失败：{e}"
        return json.dumps(rows, ensure_ascii=False) if rows else "没有结果"

    async def fetch_page(url: str = "") -> str:
        """打开一个网页，返回正文纯文本。查细节（型号、状态、发布日期）时用。"""
        from . import connectors

        try:
            ok = await connectors._is_public_url(str(url or "").strip())
        except Exception:
            ok = False
        if not ok:
            # 内网地址一律不许读：这是给公开网页用的工具，不是内网探针
            return "这个地址不能访问"
        text = await _read_page(http, url)
        return text or "页面没有可读正文"

    handlers = {"web_search": web_search, "fetch_page": fetch_page,
                "github_search": github_search}
    return ToolSet(tools=[
        FunctionTool(name="web_search", description="联网搜索，返回标题、链接和摘要",
                     parameters={"type": "object", "properties": {
                         "query": {"type": "string", "description": "搜索词，越具体越好"}},
                         "required": ["query"]}, handler=web_search),
        FunctionTool(name="github_search", description="搜 GitHub 仓库，问项目/工具/解锁进度时用",
                     parameters={"type": "object", "properties": {
                         "query": {"type": "string", "description": "仓库搜索词，如 redmi 15r bootloader"}},
                         "required": ["query"]}, handler=github_search),
        FunctionTool(name="fetch_page", description="打开网页读正文，查细节时用",
                     parameters={"type": "object", "properties": {
                         "url": {"type": "string", "description": "要打开的 http/https 地址"}},
                         "required": ["url"]}, handler=fetch_page),
    ]), handlers


def _usage_of(resp) -> tuple:
    """(输入, 缓存命中, 输出)。网关的命中数在 raw_completion.usage 里，框架字段未必有。"""
    hit = inp = outp = None
    u = getattr(resp, "usage", None)
    if u is not None:
        hit = getattr(u, "input_cached", None)
        inp = getattr(u, "input", None)
        outp = getattr(u, "output", None)
    if inp in (None, 0):
        raw = getattr(resp, "raw_completion", None)
        if raw is not None and hasattr(raw, "get"):
            try:
                us = raw.get("usage") or {}
                hit = us.get("prompt_cache_hit_tokens", hit)
                inp = us.get("prompt_tokens", inp)
                outp = us.get("completion_tokens", outp)
            except Exception:
                pass
    return int(inp or 0), int(hit or 0), int(outp or 0)


async def run(question: str, *, provider: Any, http: Any, proxy: str | None = None,
              max_rounds: int = _ROUND_MAX, budget_sec: float = _TOTAL_SEC) -> dict:
    """跑一轮「查了再答」，返回 {text, rounds, sources, ms}。

    上层拿到 text 直接发，sources 用来在控制台显示「这条查了什么」。
    """
    sources: list[str] = []
    built = _toolset(http, proxy, sources)
    if built is None:
        return {"text": "", "rounds": 0, "sources": [], "ms": 0, "why": "框架没有 ToolSet"}
    tools, handlers = built

    started = time.time()
    # 联网这条路一轮要调 2~3 次模型，每次的用量都攒起来给 /tocken 用
    tu = {"input": 0, "hit": 0, "output": 0, "ms": 0}
    contexts: list[dict] = []   # 工具结果当 user 消息回灌，比拼进 prompt 更省
    rounds = 0
    text = ""

    for i in range(max(1, int(max_rounds))):
        if time.time() - started > budget_sec:
            logger.warning("[fish] 检索超时（%.0fs），用手上材料收尾", time.time() - started)
            break
        rounds = i + 1
        _t0 = time.time()
        try:
            # 加硬超时：网关抽风时一轮能挂 90 秒，整条消息就烂在手里
            resp = await asyncio.wait_for(provider.text_chat(
                # 第二轮起别再塞 prompt：WB 网关会因空 content 直接 400
                # （a message has empty content），而框架会把 None 当作没这条
                prompt=question if i == 0 else None,
                system_prompt=RESEARCH_SYSTEM,
                contexts=contexts,
                func_tool=tools,
            ), timeout=_LLM_SEC)
        except Exception as e:
            logger.warning("[fish] 检索第 %d 轮调用失败：%s", rounds, e)
            break

        _u = _usage_of(resp)
        tu["input"] += _u[0]; tu["hit"] += _u[1]; tu["output"] += _u[2]
        tu["ms"] += int((time.time() - _t0) * 1000)
        names = list(getattr(resp, "tools_call_name", []) or [])
        args = list(getattr(resp, "tools_call_args", []) or [])
        if not names:
            text = (getattr(resp, "completion_text", "") or "").strip()
            break

        ctxlen = sum(len(str(m.get("content", ""))) for m in contexts)
        # 同一轮的多个工具调用并发跑：串行时三个网页各超时 30 秒 = 90 秒，
        # 并发后最坏只吃一个 _TOOL_SEC。这是 123 秒里最大的一块
        _jobs: list[tuple] = []
        for name, kw in zip(names, args):
            if ctxlen > 24000 or len(_jobs) >= 3:
                # 材料已经够多，再读只会把上下文撑爆：让模型直接收尾
                break
            fn = handlers.get(name)
            if fn is None:
                continue
            _jobs.append((name, kw or {}, fn))
            ctxlen += _PAGE_MAX_CHARS + 60

        async def _call(name: str, kw: dict, fn) -> str:
            tmo = _TOOL_SEC_SEARCH if name in ("web_search", "github_search") else _TOOL_SEC_FETCH
            try:
                return str(await asyncio.wait_for(fn(**kw), timeout=tmo))
            except asyncio.TimeoutError:
                logger.info("[fish] 工具 %s 超时（%.0fs），跳过", name, tmo)
                return ""
            except Exception as e:  # noqa: BLE001
                return f"工具出错：{e}"

        _outs = await asyncio.gather(*[_call(*j) for j in _jobs]) if _jobs else []
        for (name, kw, _fn), out in zip(_jobs, _outs):
            if not out:
                continue
            if name == "fetch_page" and kw.get("url"):
                sources.append(str(kw["url"]))
            logger.info("[fish] 检索调用 %s(%s) → %d 字", name,
                        json.dumps(kw or {}, ensure_ascii=False)[:80], len(out))
            contexts.append({"role": "assistant",
                             "content": f"（调用 {name} {json.dumps(kw or {}, ensure_ascii=False)}）"})
            contexts.append({"role": "user", "content": f"工具 {name} 返回：\n{out}"})
            ctxlen += len(out) + 60
            # 每个 url 只读一次，避免模型把同一页反复打开
            if name == "fetch_page":
                try:
                    tools.get_tool("fetch_page").description = (
                        f"打开网页读正文。已读过：{'、'.join(sources[-4:])}，别重复读")
                except Exception:
                    pass

    ms = int((time.time() - started) * 1000)
    if not text:
        # 循环用完了还没落笔：让模型就着手上的材料直接答，别再要工具
        try:
            resp = await provider.text_chat(
                prompt="就用手上已经查到的内容直接回答，不要再查。查不到的部分明确说没查到。",
                system_prompt=RESEARCH_SYSTEM,
                contexts=contexts,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning("[fish] 检索收尾失败：%s", e)
    return {"text": _tidy_answer(text), "rounds": rounds, "sources": sources,
            "ms": ms, "usage": tu}
