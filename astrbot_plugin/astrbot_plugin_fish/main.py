# -*- coding: utf-8 -*-
"""小鲸鱼 · AstrBot 群聊拟真插件

把自研 QQ 机器人 qq-deepseek-bot 的对话能力整体搬到 AstrBot 上：
人设、心境、长期记忆、群表达学习、反模板、手滑错字、表情包、接话时机。

和原项目最大的结构差异：原项目是「一条消息进来 → 自己决定要不要回 → 自己调模型」，
AstrBot 的默认 LLM 只在被 @ 或唤醒前缀时才会触发。所以这里让 handler 的 filter
永远通过（拿到群里每一条消息），然后：
  - 该回的时候自己 yield event.request_llm(...)；
  - 不该回的时候 event.stop_event()。
ab! 前缀的消息不碰，原样留给 AstrBot 自己的 LLM 链路（见 README「为什么不 stop_event」）。
"""

from __future__ import annotations

import asyncio
import base64
import functools
from difflib import SequenceMatcher
import importlib.util

import aiohttp
import json
import os
import random
import re
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At, AtAll, Image, Plain, Poke, Reply

try:  # 空工具集 = 这次请求不给模型任何工具
    from astrbot.core.agent.tool import ToolSet

    NO_TOOLS = ToolSet()
except Exception:  # 版本差异时降级：至少不会因此起不来
    NO_TOOLS = None

def _guess_lan_ip() -> str:
    """视频中转地址没配时，自己报本机对外网卡的 IP。

    UDP connect 只是让内核按路由表选一张网卡，不真发包；拿不到就退回环，
    同机部署（NapCat 与 AstrBot 在一台）本来就连得上。硬编码内网 IP 会在
    换网段后静默发不出视频，而且地址本身也不该写进配置文件。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# 「这条是链接」的判定，只用来决定闭嘴，不做任何解析。
# 有人贴抖音分享会漏掉 https://（复制出来就是 v.douyin.com/xxx 这种裸域名），
# 所以短链域名单独列一遍，光靠 https?:// 会漏
_URL_RE = re.compile(
    r"https?://|www\.|v\.douyin\.com|xhslink\.com|b23\.tv|bili2233\.cn"
    r"|\bBV[a-zA-Z0-9]{10}\b|\bav\d{3,}")
# 贴了链接还要人评价：这时候才把大模型叫起来。光贴链接不聊天
_ASK_OPINION_RE = re.compile(
    r"(评价|点评|说说|讲讲|分析|怎么看|怎么评价|你觉得|你的看法|什么看法|如何评价|讲讲看|说说看|聊聊)", re.I)

# 触发联网的实时性关键词。只在「被叫到」时才看，一次检索 0.3 秒，成本可接受
_REALTIME_RE = re.compile(
    r"联网|网上|搜索|查一下|查查|查下|帮我查|搜一下|搜搜|搜个|百度|谷歌|必应|bing|google|"
    r"最新|新闻|比分|夺冠|结果|进展|开播|发布会|上市|多少钱|价格|天气|几点|什么时候|"
    r"真的假的|是不是真的|有没有发生")
# 技术讨论里也常出现「搜索」两个字（搜索算法/正则/接口），命中这些词就别外联。
# 只留「一听就是在讲课」的搭配：以前这里是裸的「代码|接口|函数|编译」，结果
# 「开源代码协议」直接命中「代码」——用户明说通过联网搜索，它一条请求都不发
_NO_SEARCH_RE = re.compile(r"搜索算法|复杂度|正则|排序算法|二叉树|哈希表|动态规划|"
                           r"sql 注入|prompt 注入|依赖注入")
# 明确要我联网的说法：这类句子不看上面那个「技术讨论」猜测，直接搜
_EXPLICIT_SEARCH_RE = re.compile(
    r"联网|网上搜|搜一下|搜一搜|搜搜|搜索一下|帮我查|查一下|查查|百度一下|谷歌|必应|google|bing",
    re.I)
# 洗完只剩这些词的，是「指令」不是「问题」。拿它去搜会搜出莫名其妙的东西：
# 实测「联网搜索回答」被洗成「回答」，搜回来三条北岛的诗。
_VAGUE_Q = {"回答", "一下", "查", "查下", "搜", "搜下", "看看", "说说", "这个", "那个",
            "问题", "内容", "东西", "帮我", "联网", "答案"}


def _usage_of(resp) -> tuple:
    """从模型响应里抠出 (输入, 缓存命中, 输出)。

    框架的 TokenUsage 只认 input_cached，WB 网关真正的命中数在
    raw_completion.usage.prompt_cache_hit_tokens 里，两条都读一遍。
    """
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


def _search_query(text: str) -> str:
    """把「帮我联网搜索一下BLG夺冠了吗」洗成「BLG夺冠」。

    只去口语外壳，不动实体词——检索质量全看实体有没有留下。
    """
    q = re.sub(r"\[[^\]]*\]|@\S+", " ", text or "")
    q = re.sub(r"帮我|麻烦|请|联网|网上|搜索一下|搜索|查一下|查查|查下|搜一下|搜搜|搜个|"
               r"百度|谷歌|必应|bing|google", " ", q, flags=re.I)
    q = re.sub(r"^(那|就|你|你们|能不能|可以|能不能帮我)+", " ", q)
    # 「给我介绍一下主流开源代码协议有哪些」→「主流开源代码协议」：
    # 这些是问法外壳，留在查询里会把实体稀释掉
    q = re.sub(r"通过|给我介绍一下|给我介绍|介绍一下|介绍下|告诉我|说一下|讲讲|"
               r"有哪些|有什么|都有啥|呢", " ", q)
    # 「主流的开源协议」搜出来是文库和短视频，搜「开源协议」反而干净
    q = re.sub(r"(主流的?|常见的?|常用的?|几个|几大|主要的?)", " ", q)
    # 「现在/今天/刚刚」这类时间副词对检索没用，反而把实体词冲散
    q = re.sub(r"(现在|今天|刚刚|目前|这会儿)", " ", q)
    # 口语问法换成检索词：搜「多少钱」出来的全是比价站和百科，搜「价格」才准
    q = re.sub(r"多少钱|什么价|卖多少|啥价格|贵不贵", " 价格 ", q)
    # 语气词可能连着一串（「了吗」「呢啊」），一次只能削一个，得循环
    q = re.sub(r"(?:吗|么|呢|吧|啊|呀|了|没有)+$", "", q.strip())
    q = re.sub(r"\s+", " ", q).strip(" ，,。？！?！")
    return q.strip()

PLUGIN_DIR = Path(__file__).resolve().parent
FISH_CORE = PLUGIN_DIR / "fish_core"
# 同目录模块之间要能互相 import（connectors / vision 这类成对的模块），
# 模块本身是按文件路径加载的、没有包结构，只能靠把目录塞进 sys.path 让扁平 import 生效
if str(FISH_CORE) not in sys.path:
    sys.path.insert(0, str(FISH_CORE))
DATA_DIR = PLUGIN_DIR / "data"


def _warm_png_b64() -> str:
    """现造一张 1x1 透明 PNG 的 base64（视觉预热用）。

    不写字面量：base64 长串会被日志/配置层的脱敏规则当成密钥吃掉，
    写进去等于没写。现造的字节是确定的，成本只有几毫秒。
    """
    import base64 as _b64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
           + chunk(b"IEND", b""))
    return _b64.b64encode(png).decode()


# 预热用图：1x1 透明 png，几乎没有 token 成本
_WARM_PNG = _warm_png_b64()

# 复用原项目的纯逻辑模块：用 path 直接加载，避免和别的插件同名串味
def _load(alias: str, filename: str):
    name = f"astrbot_plugin_fish_{alias}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FISH_CORE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


affect = _load("affect", "affect.py")
favor = _load("favor", "favor.py")
poke_context = _load("poke_context", "poke_context.py")
anti_template = _load("anti_template", "anti_template.py")
interject_planner = _load("interject_planner", "interject_planner.py")
style_learner = _load("style_learner", "style_learner.py")
meme_catalog = _load("meme_catalog", "meme_catalog.py")
meme_gate = _load("meme_gate", "meme_gate.py")
prompts = _load("prompts", "prompts.py")
composer = _load("composer", "composer.py")
# 挨骂还嘴的判定 + 出口谐音降级；骂人动作层（@ 到人头上那种）
quarrel = _load("quarrel", "quarrel.py")
insult_actions = _load("insult_actions", "insult_actions.py")
insult_actions.quarrel = quarrel   # 同一个实例：五分钟两次的冷却状态别分家
interject_score = _load("interject_score", "interject_score.py")
fish_memory = _load("fish_memory", "fish_memory.py")
splitter = _load("splitter", "splitter.py")
token_report = _load("token_report", "token_report.py")
token_card = _load("token_card", "token_card.py")
vision = _load("vision", "vision.py")
hearing = _load("hearing", "hearing.py")
pixiv = _load("pixiv", "pixiv.py")


def _media_parser_pixiv_cookie() -> str:
    """fish 自己没填 pixiv cookie 时，借 media_parser 那份。

    同一个账号在两处各维护一份，改了一边忘了另一边，表现出来的就是
    「昨天还能发原图，今天只剩缩略图」，排查起来极难。宁可读别人的配置。
    """
    # 部署后 main.py 在 data/plugins/<插件>/ 下，往上两级就是 data；
    # 仓库里开发时层级不一样，所以逐个往上找，找到为止
    for parent in Path(__file__).resolve().parents[:5]:
        p = parent / "config" / "astrbot_plugin_media_parser_config.json"
        try:
            if not p.exists():
                continue
            return (json.loads(p.read_text(encoding="utf-8-sig")).get("pixiv") or {}).get("cookie") or ""
        except Exception:
            continue
    return ""
meme_gen = _load("meme_gen", "meme_gen.py")
order_watch = _load("order_watch", "order_watch.py")
social_read = _load("social_read", "social_read.py")
peer_bots = _load("peer_bots", "peer_bots.py")
cross_memory = _load("cross_memory", "cross_memory.py")
panel_api = _load("panel", "panel.py")
admin_actions = _load("admin_actions", "admin_actions.py")
yunzai_link = _load("yunzai", "yunzai.py")
backup = _load("backup", "backup.py")
# 从旧仓库搬过来的能力：游戏/斗图/链接解析/联网检索/护栏/关系网/印象原子/热梗池
games = _load("games", "games.py")
fun_features = _load("fun_features", "fun_features.py")
connectors = _load("connectors", "connectors.py")
safety_guard = _load("safety_guard", "safety_guard.py")
cross_interaction = _load("cross_interaction", "cross_interaction.py")
memory_atoms = _load("memory_atoms", "memory_atoms.py")
meme_manager = _load("meme_manager", "meme_manager.py")

DEFAULTS = {
    "enabled": True,
    "enabled_groups": [],          # 群白名单，空 = 一个群都不生效
    "enabled_private_users": [],   # 私聊白名单，空 = 谁私聊都不理（主人始终放行）
    "order_guard_enabled": True,   # 主动巡检群秩序：骂人/广告/套提示词就上报主人
    "chat_min_gap_sec": 2,         # 对话延续时的最小间隔（防连珠炮，不是硬冷却）
    "judge_inline": True,
    "multi_turn_contexts": True,
    "context_turns_max": 48,
    "quarrel_enabled": True,       # 被点名骂 / 骂到主人头上时还嘴（判定在 fish_core/quarrel.py）
    "insult_actions_enabled": True,  # 群友让它去骂人（无差别/点名），会真 @ 到人
    "insult_cooldown_sec": 60,     # 同群两次执行骂人的最小间隔
    "insult_max_targets": 3,       # 无差别攻击一次最多点几个人
    "tattle_watch_groups": [],     # 哪些群开主动巡检，空 = 全部不开
    "order_guard_cooldown_sec": 600,  # 同一个群两次上报的最小间隔
    "bot_nickname": "小鲸鱼",
    "wake_prefix": "ab!",
    "interject_desire": 0.30,
    "interject_hourly_cap": 8,
    # 300 秒的冷却把主动接话几乎掐死了：24 小时里 1523 次「刚说过话，冷却中」
    # 只换来 32 次插话。120 秒既能防插话刷屏，又有小时上限 8 次兜底
    "interject_cooldown_sec": 120,
    "reply_when_at": True,
    "reply_private": True,
    "meme_enabled": True,
    # 发图：模型写 [看图:标签] 时去 pixiv 捞一张全年龄原图。cookie 留空就借
    # media_parser 那份（同一个号，两处各存一份迟早不同步）
    "pixiv_enabled": True,
    "pixiv_cookie": "",
    "pixiv_proxy": "http://127.0.0.1:7890",
    "pixiv_timeout_sec": 25,        # 一次取图的总上限；超时就当没图，别卡住整条回复
    # 时间窗（秒）内的发言都进 prompt，timeline_size 只是条数上限。
    # 只按条数截，热闹群里 24 条≈3 分钟，跨轮的话题必然断线
    "context_window_sec": 1800,
    "social_context_sec": 1800,
    "timeline_size": 40,
    "memory_recall_topk": 4,
    "style_learning": True,
    "debug_dump_prompt": False,
    # 云崽等面板指令前缀：这些消息归别的插件，绝不插嘴
    "command_prefixes": ["#", "*", "%", "~"],
    # --- 长回复拆条 ---
    "split_enabled": True,
    "split_max_chars": 60,
    "split_max_parts": 3,
    "split_gap_min": 0.6,
    "split_gap_max": 1.5,
    # --- Gemini 看图 ---
    "vision_enabled": True,
    "vision_model": "gemini-4-hydrogen",
    # 识图主通道改成 WB 池（Gemini 常年限流）；顺序 vision_wb_models → 框架 provider → Gemini 直连
    "vision_wb_enable": True,
    "vision_wb_base": "http://127.0.0.1:3425/v1",
    "vision_wb_key": "",
    "vision_wb_models": ["wb/glm-5v-turbo", "wb/minimax-m3.1-flash"],
    "vision_fallback_models": ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"],
    "vision_max_images": 2,
    "vision_timeout_sec": 12,
    "vision_total_budget_sec": 20,
    "vision_proxy": "http://127.0.0.1:7890",
    "vision_api_key": "",
    "vision_key_env_file": os.path.expanduser("~/.config/qq-deepseek-bot.env"),
    # --- 两步生成 ---
    "social_read_enabled": True,
    "social_read_provider": "",
    "social_read_timeout_sec": 8,
    # --- 斗图闸 ---
    "meme_gate_enabled": True,
    "meme_min_gap_sec": 180,
    "meme_follow_gap_sec": 60,
    # 被 @ 或被戳后，同一人在这个时间里继续说话可以不再 @（免@追问窗口）
    "follow_window_sec": 300,
    "follow_max_turns": 2,
    "reply_max_chars": 260,
    # --- 文字游戏 / 斗图 / 联网扩展 ---
    "enable_games": True,          # 数字炸弹、掷骰子、抽签、点名
    "meme_battle_enabled": True,   # @ 带图时斗图反杀
    "connectors_enabled": True,    # 被叫到且明确问「出处/搜一下/新番」时才外联
    "meme_pool_size": 30,          # 热梗池每轮注入多少条
    "meme_rotation_sec": 1800,     # 多久换一轮
    # --- 别的机器人 ---
    "peer_bots_path": "",
    "peer_bots_name_hints": [],
    # --- 刹车（老仓库的 /off、禁言短路、刷屏闸）---
    "owner_qq": "",                # 主人 QQ：/off /on /memes 这类指令的主人也放行
    "bot_ban_guard": True,         # 机器人在禁言期不跑模型，省 token
    "density_guard": True,         # 两分钟里主动说过两条就不再主动开口
    "density_window_sec": 120,
    "density_limit": 2,
    # --- 自然语言管理（老仓库 handlers/actions.py 的处置段）---
    "admin_actions_enabled": True,
    # --- 云崽白名单联动 ---
    "yunzai_sync": True,
    "yunzai_other_yaml": "",       # 留空用默认 ~/projects/trss-yunzai/config/config/other.yaml
    # --- 数据备份 ---
    "backup_enabled": True,
    "backup_dir": "",              # 留空落 ~/.astrbot/backups（放插件目录外，重装不丢）
    "backup_keep": 7,
    "backup_interval_hours": 24,
    # --- 跨群记忆 ---
    "cross_group_enabled": True,
    "cross_group_recall": 2,
    "cross_group_max_topics": 60,
}

MEME_PATH = FISH_CORE / "meme-pack" / "memes_catalog.json"


async def _noop() -> str:
    """并发 gather 里的占位：没图时也要有个 awaitable，省得写分支。"""
    return ""


def _write_json(path: str, data, pretty: bool = False) -> None:
    """原子写：心境/记忆状态都在进程里攒着，写一半崩了会留个空文件把状态吃没。

    覆盖前先留一份 .bak-时间戳 快照（老仓库同一套），状态文件写坏一次就是
    好感度/心境整段归零，事后没有任何地方能捞回来。
    """
    backup.snapshot_json(str(path))
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)
    os.replace(tmp, path)


# AstrBot 自带指令的白名单。插件必须对它们彻底让路：
# 否则「/help」会被当成一句闲聊回掉，框架的指令处理器永远轮不到

# 回戳的时间戳：同一个人 3 秒内只回戳一次，避免两边互戳成死循环
_POKE_BACK_TS: dict[str, float] = {}
# 戳一戳正常走模型（poke_context.build_prompt 负责「被戳了一下」的提示），
# 本地只剩 5 句兜底，给模型超时/失败时补一句——池子治不了复读，能自己写才行
# 挑选逻辑放 fish_core/poke_context 里，那边能离线测
_pick_poke = poke_context.pick_fallback

_BUILTIN_COMMANDS = frozenset({
    "help", "reset", "new", "stop", "sid", "stats",
    "provider", "set", "unset", "name", "ping", "dashboard_update",
    # 记忆查询由 astrbot_plugin_memory_cmd 接管，聊天插件必须让路
    "memary", "memory",
})


# ── 自然语言点歌 ────────────────────────────────────────────────
# 群友不会为了听首歌去背 /搜索歌曲。这里在聊天链路里直接识别「我想听晴天」
# 这类句子，转手交给 listen-music 插件的 LLM 接口干活（搜索→选候选→发语音），
# 自己的模型一句话都不用说。插件不在/搜不到/发失败时静默放手，回到正常聊天。
_SONG_INTENT_RE = re.compile(
    r"(?:我想听|我要听|想听|要听|听一下|听个|听首|听一首|来一首|来首|来一个|"
    r"放一首|放首|放个|点一首|点首|点歌|唱一首|唱首|给我放|给我来|能不能放|能放)"
    r"\s*[《「\"']?\s*([^《》「」\"'，,。！!？?\n]{1,24})"
)
# 泛指词：抓到的不是歌名，是被句子吃剩下的尾巴
_SONG_STOP_WORDS = {
    "歌", "首歌", "歌曲", "一首", "个", "点", "音乐", "一下", "听听", "什么",
    "啥", "这个", "那个", "点儿", "点儿歌", "歌儿", "吧", "呗",
    "视频", "片子", "电影", "番剧", "直播",
}


# ── 检索指针 ───────────────────────────────────────────────────
# 跟 [MEME:xx] 一个路数：要不要搜、搜什么，全交给模型自己判断，
# 它决定要查就吐一个 [搜索:关键词]。代码只负责认这个指针、跑检索、再让它答。
# 以前拿正则硬认「搜一下/查一下」是错的——什么时候该查不是词表能枚举的。
# 关键词放宽到 80 字、允许跨行：以前限 60 字且禁换行，模型写长一点或折行，
# 正则抓不到，`[搜索:...]` 就原样贴着答案进群了（实测 >60 字确实漏）
_SEARCH_MARK_RE = re.compile(r"[\[【]\s*(?:搜索|搜|查)\s*[:：]\s*([^\]】]{1,80})[\]】]")
# 未闭合 / 超长的残句也要抠：只要以 [搜索: 开头就不能让它出现在群里
_SEARCH_LEFTOVER_RE = re.compile(r"[\[【]\s*(?:搜索|搜|查)\s*[:：][^\]】]{0,80}[\]】]?")
# 发图指针：模型判断「该发张图」时吐 [看图:标签]，标签直接拿去 pixiv 搜。
# 不给模型看图、不让它描述内容——它只要会写标签，选中和过滤都是代码的事
_PIXIV_MARK_RE = re.compile(r"[\[【]\s*(?:看图|发图|来张图)\s*[:：]\s*([^\]】]{1,40})[\]】]")
_PIXIV_LEFTOVER_RE = re.compile(r"[\[【]\s*(?:看图|发图|来张图)\s*[:：][^\]】]{0,40}[\]】]?")


class _ImgRef:
    """给 vision.component_bytes 用的最小图片对象：引用里的图只有 url，没有 AstrBot 组件。

    vision 那边只读 url/file/path 三个属性，用不着整个 Image 组件，也不该为了
    一张引用图去伪造组件（Image 的构造会去碰文件、还可能触发下载）。
    """
    __slots__ = ("url", "file", "path")

    def __init__(self, url: str = "") -> None:
        self.url = url
        self.file = ""
        self.path = ""
# 只剥「内部标签」，不动正文：给兄弟插件的回复用。
# 以前对别人的回复也跑 composer.sanitize_reply，结果 stealer 的 `meme list: 3 memes`
# 被判成英文元信息整条吃掉，`@全体成员 注意` 里的 @ 也被削掉
# 括号里模型偶尔会多敲个空格（「[ MEME:嘲讽]」「【 表情：卖萌 】」），
# 原来的方括号后紧贴关键字漏掉这类，标签会原样发进群，所以允许开头留白
_TAG_ONLY_RE = re.compile(
    r"[\[【]\s*(?:表现|动作|神态|心理|记忆|回想|MEME|表情|画图|REPORT|PASS)[:：]?[^\]】\n]{0,40}[\]】]",
    re.I)


# 讨论「点歌」这个功能时，意图词后面跟着的是语法成分：点歌的话/点歌这件事/
# 点歌功能…。这些开头的都不是歌名
_SONG_META_HEAD = ("的", "归", "属", "由", "是", "要得", "得", "应该", "一般", "通常",
                   "不是", "都", "还", "也", "这个", "那个", "功能", "时候", "之后", "之前")
_SONG_META_CONTAINS = ("的话", "走的是", "指的是", "说的是", "就是", "算是", "等于",
                       "这件事", "这个问题", "这个功能", "插件")


def _parse_song_request(text: str) -> str:
    """从一句闲聊里抠出歌名，抠不到就返回空串（宁可不接，也别乱放歌）。"""
    # 一段话里带「点歌」两个字不等于在点歌：贴人设时那句「点歌归另一插件，
    # 音源只有 B 站」被当成点《归另一插件》，还命中缓存真发了语音。
    # 点歌从来都是短句，超长的一律不认
    if len((text or "").strip()) > 35:
        return ""
    # 带书名号/引号的时候最靠谱，直接拿引号里的东西，别让正则把「洛天依的」当歌名。
    # 但光有引号不算点歌——「《三体》好看吗」「他说"今天下雨了"」都是正常引用，
    # 之前只要带引号就去搜歌下载发语音，正常聊天都能点出一首歌
    quote_m = None
    for pat in (r"《([^《》\n]{1,24})》", r"「([^「」\n]{1,24})」", r"\"([^\"\n]{1,24})\""):
        quote_m = re.search(pat, text or "")
        if quote_m:
            break
    if quote_m:
        name = quote_m.group(1).strip()
        rest = (text or "").replace(quote_m.group(0), "").strip()
        has_intent = bool(_SONG_INTENT_RE.search(text or ""))
        # 「他说"今天下雨了"」「她回"好的呀"」是转述，不是点歌：
        # 引号前面挂着说话动词就别放歌，否则正常聊天都能点出一首
        quoted_by_speech = bool(re.search(r"(说|讲|问|答|写|提到|回复|喊|念|叫)", rest))
        # 整条消息几乎只剩歌名（「晴天」「好家伙《晴天》」）也算点歌
        if (name and name not in _SONG_STOP_WORDS and not quoted_by_speech
                and (has_intent or len(rest) <= 2)):
            return name
    for m in _SONG_INTENT_RE.finditer(text or ""):
        raw = m.group(1).strip().strip("　 ")
        # 「点歌：孤勇者」「点歌、孤勇者」：把紧跟「点歌」的标点也剥掉。
        # 不剥的话 B 站搜的是「：孤勇者」，报幕还会打出《：孤勇者》
        raw = raw.lstrip("：:，,、。.~～—－-\"'“”《》【】（）() \t").strip()
        # 「能不能放首晴天」这种，动词短语只吃掉「放」，前面的「首」会剩在歌名里
        for head in ("一首", "一个", "首", "个", "下", "点"):
            if raw.startswith(head) and len(raw) > len(head):
                raw = raw[len(head):].strip()
        # 尾巴上的语气词和客套话对搜索是噪音。要循环剥：
        # 「晴天吧谢谢」只跑一轮会剩「晴天吧」，拿去搜就是另一个结果
        _tails = ("行不行", "好不好", "可以不", "谢谢", "吧", "呗", "啊", "呢", "嘛", "呀")
        for _ in range(4):
            before = raw
            for tail in _tails:
                if raw.endswith(tail) and len(raw) > len(tail):
                    raw = raw[: -len(tail)].strip()
                    break
            if raw == before:
                break
        # 「我想听洛天依的」剩下的「洛天依的」不是歌名，尾巴的「的」一律剥掉
        if raw.endswith("的") and len(raw) > 2:
            raw = raw[:-1].strip()
        if not raw or raw in _SONG_STOP_WORDS:
            continue
        # 「我想听你说说」「你放不」这类不是歌名。
        # 只看开头的主语和结尾的语气词——歌名里带「不」很常见（不潮不用花钱、不得不爱），
        # 之前按「含有就否决」把这类歌名全杀了
        if re.match(r"^(你|我|他|她|它|咱)", raw) or raw.endswith(("吗", "么")):
            continue
        # 「点歌归另一插件」→「归另一插件」、「点歌的话走的是B站的通道」→
        # 「的话走的是B站的通道」：这类抠出来的是句子结构，不是作品名。
        # 群里只要在讨论点歌这件事，模糊正则就会咬一口，只能从语法上否决
        if raw.startswith(_SONG_META_HEAD) or any(k in raw for k in _SONG_META_CONTAINS):
            continue
        if len(raw) < 2:
            continue
        return raw
    return ""


# 有人在问「X 是什么梗」：这种问题本地猜不出来，得去梗百科查
_MEME_ASK_RE = re.compile(
    r"[「\"\'“]?([^「」\"\'“”，。,\s]{1,16})[」\"\'”]?\s*"
    r"(?:是什么梗|是啥梗|什么梗|啥梗|哪来的梗|出自哪|啥出处)"
)


def _meme_ask_term(text: str) -> str:
    """从问句里取出要查的那个词。带引号的最准，没有就靠句式猜。"""
    t0 = text or ""
    quoted = re.search(r"[「\"\'“]([^「」\"\'“”]{1,16})[」\"\'”]", t0)
    if quoted and _MEME_ASK_RE.search(t0):
        return quoted.group(1).strip()
    m = _MEME_ASK_RE.search(t0)
    if not m:
        return ""
    t = m.group(1).strip()
    t = re.sub(r"^(你知道|我想问|想问|问一下|请问|那个|这个|他说的|她说的)", "", t)
    t = re.sub(r"(是|的|这词|这个词|那个词|这句话)$", "", t)
    return t.strip("「」\"\'“” ")


# 违规上报标签：模型自己判断完，用 [REPORT:类别|理由] 挂一句，
# 群里那句话它自己写，代码只负责剥标签 + 私发给主人
_REPORT_RE = re.compile(r"\[\s*REPORT\s*[:：]\s*([^\]\n|｜]{1,16})(?:[|｜]\s*([^\]\n]{1,80}))?\s*\]", re.I)


def _extract_report(text: str):
    """剥出 [REPORT:...]，返回 (干净文本, (类别, 理由) 或 None)。"""
    found = None

    def _grab(m):
        nonlocal found
        found = ((m.group(1) or "").strip(), (m.group(2) or "").strip())
        return ""

    clean = _REPORT_RE.sub(_grab, text or "")
    return clean, found


# 告状：只认明确的告状/举报字样，别把「举报按钮在哪」这种也转过去
_TATTLE_RE = re.compile(r"(告状|举报|投诉|告发|打小报告)")


def _is_tattle_request(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 3:
        return False
    # 问怎么用的、问有没有这功能，都不该触发
    if any(w in t for w in ("怎么", "如何", "在哪", "有没有", "什么用")):
        return False
    return bool(_TATTLE_RE.search(t))


def _is_builtin_cmd(text: str) -> bool:
    # 注意：AstrBot 在唤醒阶段就把 wake_prefix（现在是「/」）剥掉了，
    # 到插件手里 message_str 已经是「memary」。所以斜杠可有可无，别只认带斜杠的
    t = (text or "").strip()
    if t.startswith("/"):
        t = t[1:]
    if not t:
        return False
    return t.split(" ", 1)[0].strip().lower() in _BUILTIN_COMMANDS


async def _poke_back(bot, gid: str, uid: str, self_id: str = "") -> None:
    """被戳就立刻戳回去。

    必须是模块级函数，不能写成类的 async 方法：AstrBot 会把类里第一个参数
    叫 event 的协程方法统统注册成消息处理器，`_poke_back` 会被当成 handler
    调用，报 missing positional arguments，而且每个事件都炸一次。
    """
    if bot is None:
        return
    key = f"{gid}:{uid}"
    now = time.time()
    if now - _POKE_BACK_TS.get(key, 0.0) < 3.0:
        return
    _POKE_BACK_TS[key] = now
    # 先带 self_id 精确路由，找不到 API 通道再退回「唯一连接」那条路。
    # 注入测试时 self_id 是假的，带 self_id 会查不到通道直接 ApiNotAvailable
    def _params():
        base = {"user_id": int(uid)}
        if gid != "private":
            base["group_id"] = int(gid)
        return base

    action = "friend_poke" if gid == "private" else "group_poke"
    try:
        try:
            await bot.call_action(action, **({"self_id": int(self_id)} if self_id else {}), **_params())
        except Exception as e:
            if type(e).__name__ != "ApiNotAvailable":
                raise
            await bot.call_action(action, **_params())
        logger.info("[fish] 已回戳 %s（群 %s）", uid, gid)
    except Exception as e:
        logger.warning("[fish] 回戳失败 %s(%s)：%s %s", uid, gid, type(e).__name__, e)


# 只在有人明确问「我多少分」时注入分数，普通聊天不提分
_ASK_FAVOR_RE = re.compile(r"好感|喜不喜欢我|讨厌我|多讨厌我|我(?:在你?这|这)多少分|给我打?几分")

# 技术复读闸只认英文/数字标识符（bypassnro、shift+f10、PE 这种），中文句子不进这套逻辑
# 只取纯字母数字串：oobe\bypassnro 要拆成 oobe / bypassnro，shift+F10 拆成 shift / f10，
# 否则整串当做一个 token，换个写法就匹配不上，看着像没复读
_TECH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


@dataclass
class GroupState:
    msgs: deque = field(default_factory=lambda: deque(maxlen=128))
    # style_learner.observe 要的是 [{sender, sender_id, content}] 形态的历史池
    pool: list = field(default_factory=list)
    recent_bot: deque = field(default_factory=lambda: deque(maxlen=8))
    # 真正的对话流：群友的话和我自己的话按时间混在一条线上。
    # msgs 只装群友发言（社交判断要用它数「几条没人理我」），
    # 所以我刚说过什么，模型以前完全看不见，只能靠 recent_bot 那 4 句负样本兜着
    dialogue: deque = field(default_factory=lambda: deque(maxlen=80))
    # 多轮上下文：跟 dialogue 不同，这条**只往后追加、不滑窗**。
    # 前缀缓存认的是「和上一轮一模一样的那段 token」，
    # 只要列表每轮整体位移，缓存就全 miss（这就是命中率 28% 的根）
    ctx: list = field(default_factory=list)
    # 上一句是回给谁的：冷却要能分清「他还在跟我说话」和「我插了句嘴」
    last_reply_uid: str = ""
    # 每个人最后一次被回话的时间：冷却按人算，不然群里换个人说话也被一起掐
    reply_uid_ts: dict = field(default_factory=dict)
    last_msg_ts: float = 0.0
    last_bot_reply_ts: float = 0.0
    msgs_since_bot: int = 0
    interject_times: deque = field(default_factory=lambda: deque(maxlen=40))
    # 免@追问窗口：被 @ 或被戳之后，这个人在几分钟内继续说话不用再 @。
    # 三个字段必须一起看：uid 决定「谁」还有特权，until 决定「到什么时候」，
    # left 防止一句 @ 换来无限连击。
    follow_uid: str = ""
    follow_until: float = 0.0
    follow_left: int = 0
    # 「这是在跟别人说」的延续：某人 @ 了别人，紧接着那句没 @ 的也还是冲对方说的。
    # uid -> 最近一次 @ 别人的时间戳
    other_dir: dict = field(default_factory=dict)
    # 冷场退避：主动插话之后没人搭理（没人 @、没叫名字、没顺着它说），
    # 连着两次就闭嘴一段时间——群里最烦的就是没人问它、它还一句一句往外倒
    pending_interject_ts: float = 0.0
    unanswered_interjects: int = 0
    silent_until: float = 0.0

    # 戳一戳连击：同一个人一分钟内反复戳，得知道这是第几下，
    # 否则每戳一次都当「第一次被戳」来写，十下就是十句同款抱怨
    poke_uid: str = ""
    poke_count: int = 0
    poke_first_ts: float = 0.0

    # 刷屏闸：机器人自己主动说了几次、各自什么时候说的
    reply_times: deque = field(default_factory=lambda: deque(maxlen=20))
    # 干 @ 不说话：上一次本地应声的时间，别连着回「？」「咋了」
    last_bare_at_ts: float = 0.0
    # 上一次因为戳一戳说话的时间：群里人多，六个人轮着戳就是六条回复
    last_poke_reply_ts: float = 0.0

    # 斗图闸：上一张表情包什么时候发的、什么标签、对方最近有没有发图
    last_meme_ts: float = 0.0
    recent_meme_tags: deque = field(default_factory=lambda: deque(maxlen=4))
    incoming_image_ts: float = 0.0

    # 冷却位两段式：先「占位」防止并发消息一起接，真正发出去才算数。
    # 生成失败 / 出口清洗成空 / social_read 改判不接时回滚到 claim_prev，
    # 否则一次失败要白静默五分钟
    claim_prev: float = 0.0
    claiming: bool = False
    # 占位是「谁的」。并发消息各占一次时，只有持有者能回滚——
    # 布尔位会被后来的消息顶掉，A 失败回落 last_bot_reply_ts=0 就把 B 的占位一起清了
    claim_token: int = 0
    claim_owner: int = 0


def _poke_aimed_at_me(seg, self_id: str, gid: str) -> bool:
    """这个 Poke 段是不是戳我。

    目标 id 得走 target_id()：NapCat 有时候把目标放在旧字段 qq 上，有时候
    直接给 id=0，只看 .id 会漏掉一整类戳——私聊戳基本全是这样，表现就是
    「戳了不回、也不回戳」。
    """
    getter = getattr(seg, "target_id", None)
    try:
        tgt = getter() if callable(getter) else str(getattr(seg, "id", "") or "")
    except Exception:
        tgt = str(getattr(seg, "id", "") or "")
    if tgt and str(tgt) == self_id:
        return True
    # 目标 id 缺失（NapCat 有时给 id=0 / 干脆不给）时按「戳我」处理：
    # 这类事件推给 bot 基本都是冲它来的，就算不是，回戳一下也远比
    # 让原生 LLM 对着一条空消息自由发挥强
    return not tgt


# _release_claim 的「没传 token」哨兵：不传=无条件回滚（老调用点），


# 传 None=必须匹配持有者（poke 这种没 claim 过的就不会误伤别人）
_UNSET = object()


def _handler_guard(fn):
    """事件入口兜底。

    没有这层的话，handler 里任何一个漏网异常都会逃到 AstrBot 的
    pipeline/context_utils，群里看到的是「在调用插件 … 出现异常」，
    而 event.stop_event() 没执行、冷却位却已经被占了。
    生成器函数不能直接套 try/except，所以包一层 async for。
    """

    @functools.wraps(fn)
    async def wrapper(self, event, *args, **kwargs):
        try:
            async for item in fn(self, event, *args, **kwargs):
                yield item
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单行也要能一眼看出原因：整段 traceback 里翻 Error 类型太慢，
            # UnboundLocalError 那次就是这么被当成「读不懂梗」的
            logger.error("[fish] handler %s 异常，本条放弃：%s: %s",
                         fn.__name__, type(sys.exc_info()[1]).__name__, sys.exc_info()[1])
            logger.exception("[fish] handler %s 异常，本条放弃", fn.__name__)
            # 异常发生时冷却位往往已经被 claim 走了，不还回去这群要白静默一个周期
            try:
                gid = event.get_group_id()
                if gid:
                    self._release_claim(self._state(str(gid)))
            except Exception:
                pass
            try:
                event.stop_event()
            except Exception:
                pass

    return wrapper


@register(
    "astrbot_plugin_fish",
    "life",
    "把「小鲸鱼」自研 QQ 机器人的对话能力搬进 AstrBot：人设/心境/记忆/群表达/反模板/表情包/接话时机",
    "1.0.0",
    "https://github.com/lifefuck/qq-deepseek-realistic-bot",
)
class FishPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = {**DEFAULTS, **(config or {})}
        # 同一个标签 10 分钟内只搜一次：pixiv 有频率限制，被 429 之后整段时间都发不出图
        self._pixiv_cd = pixiv.Cooldown(600)
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # 刹车状态落盘：/off 的静默和机器人自身禁言期都得记住。
        # 只放内存的话，重启一次「我刚说全关了」就全忘，群里照旧开麦
        self._mute_path = str(DATA_DIR / "group_muted.json")
        self._mute_state: dict = {}
        self._load_mute_state()
        self._ban_expire: dict = {}
        self._ban_checked: dict = {}
        # 消息去重：NapCat 断线重连会补投历史，同一条 message_id 走两遍就是两条回答
        self._mids: dict = {}
        # 群秩序巡检的限流：每个群上次上报的时间
        self._order_last: dict = {}
        # 点歌服务懒加载，第一次有人点歌才建 aiohttp 会话
        self._song_svc = None

        self.memory = fish_memory.FishMemoryEngine(str(DATA_DIR / "fish_memory.db"))
        # 启动时清一次：流水账留 45 天，软删记忆留 30 天，不然库只涨不落
        try:
            cleaned = self.memory.prune()
            if any(cleaned.values()):
                logger.info("[fish] 启动清理：流水 %d 行、软删记忆 %d 条、标签 %d 条",
                            cleaned["turn_logs"], cleaned["memories"], cleaned["tags"])
        except Exception as e:
            logger.warning("[fish] 启动清理失败：%s", e)
        # 心境状态落盘在同目录；原项目 attach 时故意不调 save，这里由 tick 触发
        affect.attach(writer=_write_json, path=str(DATA_DIR / "affect_state.json"))
        # 好感度：每群每人一个长期分，和心境分开存，别互相污染
        favor.attach(writer=_write_json, path=str(DATA_DIR / "favor_state.json"))
        # 主人在所有群锁满分：他骂模型、骂程序、骂鱼都是日常，好感分跟着掉
        # 只会让最熟的人被冷处理。配置 owner_favor_lock 可以改这个数
        _lock_owner = str(self.cfg.get("owner_qq") or "").strip()
        if _lock_owner:
            favor.set_locked(_lock_owner, float(self.cfg.get("owner_favor_lock", 100) or 100))
            favor.save(force=True)
        # style_learner 的池子路径写死在原项目的 data/ 下，指到插件自己的目录，别污染上游
        style_learner._POOL_PATH = str(DATA_DIR / "style_fewshot.json")
        # meme_catalog 用 _SCRIPT_DIR 拼相对路径，改指 fish_core，图库走 meme-pack 软链
        meme_catalog._SCRIPT_DIR = str(FISH_CORE)
        meme_catalog.CATALOG_FILE = str(MEME_PATH)

        # 热梗池：数据目录这时才确定，所以只 attach，落盘载入显式调一次
        self.memes = meme_manager.meme_manager
        try:
            self.memes.attach(data_dir=str(DATA_DIR),
                              pool_size=int(self.cfg.get("meme_pool_size", 30) or 30),
                              rotation_interval=int(self.cfg.get("meme_rotation_sec", 1800) or 1800))
            self.memes.load_pool()
            self.memes.load_group_slang()
        except Exception as e:
            logger.warning("[fish] 热梗池载入失败：%s", e)
        # 群友 @ 关系网：只记近期互动，落盘前剪掉 6 小时前的记录，不会无限涨
        self.relations_path = str(DATA_DIR / "relations.json")
        try:
            cross_interaction.load_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网载入失败：%s", e)
        # 印象原子：和 fish_memory 的句子分开存，自己的去重/衰减逻辑
        self.atoms_path = DATA_DIR / "atoms.json"
        # 最近一次给每个群发的 B 站视频，用于「播不了就换 H.264 重发」
        # 每群上次下载视频的时间，防止连贴链接时并发下好几份
        self._atoms_all = self._load_atoms()
        self._distill_ts: dict[str, float] = {}   # 印象提炼节流：uid -> 上次调用时间
        # 同一张图只识一次：时间线回填和「要不要回这条」会各要一次描述
        self._vision_jobs: dict[str, asyncio.Task] = {}
        # 联网类能力共用一个 session，别每句话新建连接
        self._sess = None

        self._groups: dict[str, GroupState] = {}
        # 最近对话落地一份：重启不该等于失忆
        self._ctx_path = DATA_DIR / "recent_context.json"
        self._ctx_saved_at = 0.0
        self._load_recent_context()
        # 预热视觉链路：第一次请求要付 DNS+TLS+代理握手，实测 5~6 秒，
        # 正好压着 6 秒超时线，群里第一张图必挂。开机先空跑一张，把连接焐热
        try:
            asyncio.create_task(self._warm_vision())
        except RuntimeError:
            logger.debug("[fish] 没有运行中的事件循环，跳过视觉预热")

        # 控制台用的最近事件环形缓冲：老面板那栏「实时交互日志」搬到面板里，
        # 不再自己维护一份文件，进程重启就重来，够看就行
        self._events: deque = deque(maxlen=int(self.cfg.get("panel_event_buffer", 200) or 200))
        # 群级 override：面板改的开关落在这个文件，跟 AstrBot 全局配置分开，
        # 面板改错也只是这一项，不会把整份插件配置写坏
        self.overrides_path = DATA_DIR / "group_overrides.json"
        self._group_overrides: dict = self._load_overrides()

        # 跨群话题库：只存短摘要，落盘在插件自己的 data/ 下
        self.cross = cross_memory.CrossGroupMemory(
            str(DATA_DIR / "cross_group_topics.json"),
            max_topics=int(self.cfg.get("cross_group_max_topics", 60) or 60),
            logger=logger,
        )
        # 别的机器人名单：文件不在就建空数组（日志里会打路径）
        self.peer_path = str(self.cfg.get("peer_bots_path") or (DATA_DIR / "peer_bots.json"))
        self.peers = peer_bots.load(self.peer_path, logger=logger)
        self._peer_mtime = self._mtime(self.peer_path)

        self._started = time.time()
        logger.info(
            "[fish] 插件已加载：记忆库 %s，心境 %s，表情包 %d 张，已知机器人 %d 个，跨群话题 %d 条",
            DATA_DIR / "fish_memory.db",
            DATA_DIR / "affect_state.json",
            sum(meme_catalog.stats().values()),
            len(self.peers),
            len(self.cross.topics),
        )
        try:
            panel_api.PanelApi(self).register()
        except Exception as e:
            # 面板挂了不该拖死聊天，这里只报警
            logger.warning("[fish] 控制台接口注册失败：%s: %s", type(e).__name__, e)

        # 看图走框架的视觉 provider（data/cmd_config.json 里的 gemini-vision），
        # 这样插件的识图逻辑只剩「取字节 + 兜底」，模型换代/换家都在面板上配，不用改代码
        try:
            vision.set_framework_caller(self._vision_via_framework)
            # 听觉复用视觉那套 key/连接池/降级链：语音和图片本来就是同一个模型在处理
            hearing.configure(vision)
        except Exception as e:
            logger.warning("[fish] 框架视觉通道挂不上（仍走直连）：%s: %s", type(e).__name__, e)

        logger.info("[fish] 拆条 %s（%d 字起拆，最多 %d 条）；两步生成 %s；看图 %s（%s）",
                    "开" if self.cfg.get("split_enabled", True) else "关",
                    int(self.cfg.get("split_max_chars", 60) or 60),
                    int(self.cfg.get("split_max_parts", 3) or 3),
                    "开" if self.cfg.get("social_read_enabled", True) else "关",
                    "开" if self.cfg.get("vision_enabled", True) else "关",
                    # 识图实际先走 WB 池，日志里打真实顺序，别让人以为还在用 Gemini
                    (",".join(self.cfg.get("vision_wb_models") or [])
                     if self.cfg.get("vision_wb_enable", True) and self.cfg.get("vision_wb_base")
                     else self.cfg.get("vision_model", "")))

        # 记忆库是这次迁移唯一「丢了就没了」的资产：启动先备一次，
        # 之后每小时醒来比一次心跳，真备份与否由 min_interval 决定
        # 备份默认落在插件目录之外：插件重装/删目录时备份不能被一起带走
        self._backup_dir = str(self.cfg.get("backup_dir") or (Path.home() / ".astrbot" / "backups"))
        self._backup_task = None
        # 后台任务（拆条补发、上报、预热）统一存引用，否则可能被 GC 掉，
        # 异常也只会打「Task exception was never retrieved」，不进日志
        self._bg_tasks = set()
        if self.cfg.get("backup_enabled", True):
            try:
                backup.backup_db(str(DATA_DIR / "fish_memory.db"), self._backup_dir,
                                 int(self.cfg.get("backup_keep", 7) or 7),
                                 min_interval=self._backup_interval())
            except Exception as e:
                logger.warning("[fish] 启动备份失败：%s", e)
            try:
                self._backup_task = asyncio.create_task(self._backup_loop())
            except Exception as e:
                logger.warning("[fish] 后台备份任务起不来：%s", e)

    async def _vision_via_framework(self, raws: list) -> str:
        """把图片交给 AstrBot 配好的视觉 provider 描述，失败返回空串（外层转直连）。"""
        pid = str(self.cfg.get("vision_provider_id") or "gemini-vision")
        prov = self.context.get_provider_by_id(pid)
        if prov is None:
            logger.warning("[fish] 框架里没有 provider %s，视觉转直连", pid)
            return ""
        urls = []
        for raw in raws:
            if not raw:
                continue
            mime = "image/png" if raw[:8].startswith(b"\x89PNG") else "image/jpeg"
            urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode()}")
        if not urls:
            return ""
        resp = await prov.text_chat(
            prompt="仔细看这些图，用中文说清画面内容：有没有人、在做什么、是什么表情或梗；判不准就说看不出。",
            image_urls=urls,
        )
        return str(getattr(resp, "completion_text", "") or "").strip()

    def _backup_interval(self) -> float:
        return float(self.cfg.get("backup_interval_hours", 24) or 24) * 3600

    async def _backup_loop(self):
        """每小时醒一次，够不够间隔交给 backup_db 自己判断。"""
        while True:
            await asyncio.sleep(3600)
            try:
                backup.backup_db(str(DATA_DIR / "fish_memory.db"), self._backup_dir,
                                 int(self.cfg.get("backup_keep", 7) or 7),
                                 min_interval=self._backup_interval())
                # 顺路清点歌缓存：7 天没被点到的 mp3 删掉，不然只进不出
                if self._song_svc is not None:
                    try:
                        _n = self._song_svc.prune_cache()
                        if _n:
                            logger.info("[fish] 清掉 %d 个过期点歌缓存", _n)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[fish] 清理点歌缓存失败：%s", e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[fish] 定时备份失败：%s", e)

    async def terminate(self) -> None:
        """插件卸载/重载时收尾：关连接、把关系网落盘。失败也不能抛，否则框架停不下来。"""
        try:
            if self._song_svc is not None:
                await self._song_svc.close()
        except Exception:
            pass
        # 备份任务必须收掉，否则重载后旧任务还握着上一份 self 在跑
        if getattr(self, "_backup_task", None):
            self._backup_task.cancel()
        try:
            if self._sess is not None and not self._sess.closed:
                await self._sess.close()
        except Exception:
            pass
        try:
            await vision.close()   # 视觉那条常驻连接也得收，否则退出时刷 Unclosed client session
        except Exception:
            pass
        try:
            cross_interaction.save_store(self.relations_path)
        except Exception as e:
            logger.debug("[fish] 关系网落盘失败：%s", e)

    # ---------------- 基础工具 ----------------
    def _load_atoms(self) -> dict:
        """印象原子按「群+人」各一份。

        读坏了先试 .bak，再试不出才算空——原来直接 return {}，然后任意一次
        保存就把 {}/空dict 覆盖回磁盘，全部印象静默清零、事后无从恢复。
        """
        try:
            return backup.load_json_recover(str(self.atoms_path)) or {}
        except Exception as e:
            logger.warning("[fish] 印象原子读取失败：%s", e)
            return {}

    def _save_atoms(self) -> None:
        # 走统一的原子写 + 快照：这里原来是裸 write_text，写一半断电就是空文件
        try:
            _write_json(str(self.atoms_path), self._atoms_all)
        except Exception as e:
            logger.debug("[fish] 印象原子落盘失败：%s", e)

    def _atoms_of(self, gid: str, uid: str) -> list:
        return list((self._atoms_all.get(f"{gid}:{uid}") or []))

    async def _http(self) -> aiohttp.ClientSession:
        if self._sess is None or self._sess.closed:
            self._sess = aiohttp.ClientSession()
        return self._sess

    def _gid_of(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id() or "")
        except Exception:
            return ""

    # ---------------- 上下文持久化 ----------------
    async def _warm_vision(self) -> None:
        """开机空跑一次视觉请求，把 DNS/TLS/代理连接先建起来。

        实测同一张图冷启动 5.6 秒、预热后 1.2~3.4 秒。不预热的话，重启后
        群里发来的第一张图会连吃三个超时，等于刚上线就不识图。
        """
        await asyncio.sleep(6)   # 错开启动期其他初始化，别抢依赖
        try:
            cfg = dict(self.cfg)
            # 用一张真实的群表情包当样本（缩到 96px）：1x1 的空白 png 会被
            # Gemini 直接判 400，只热身了 TCP 却没验到 key 和出图路径
            sample = None
            try:
                import glob as _glob
                files = sorted(_glob.glob(str(PLUGIN_DIR / "fish_core" / "meme-pack" / "**" / "*.png"), recursive=True))
                if files:
                    with open(files[0], "rb") as f:
                        shrunk, _mime = vision._shrink(f.read(), max_side=96)
                    sample = base64.b64encode(shrunk).decode()
            except Exception:
                sample = None
            t0 = time.time()
            out = await vision.describe([sample or _WARM_PNG], cfg, logger)
            logger.info("[fish] 视觉预热完成（%.1fs，%s）",
                        time.time() - t0, "可用" if out else "本次没出描述，不影响后续")
        except Exception as e:
            logger.debug("[fish] 视觉预热失败（不影响使用）：%s", e)

    def _load_recent_context(self) -> None:
        """启动时把各群最近的对话捞回来。

        这些状态全在内存里，重启一次就失忆：群里刚聊透「神话保底 2000」，
        重启后它又问一遍「神话是啥」，看着就像没在听人说话。
        落一小份盘很便宜，能省掉这种蠢问。
        """
        try:
            raw = json.loads(self._ctx_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("[fish] 最近对话读取失败（忽略）：%s", e)
            return
        cutoff = time.time() - 3600   # 一小时前的话接不上，别让模型当成刚发生的
        for gid, rows in (raw or {}).items():
            st = self._state(str(gid))
            for r in rows or []:
                ts = float(r.get("ts") or 0)
                if ts < cutoff:
                    continue
                me = bool(r.get("me"))
                st.dialogue.append({"who": r.get("who") or "", "text": r.get("text") or "",
                                    "ts": ts, "me": me, "uid": r.get("uid") or ""})
                if not me:
                    st.msgs.append({"uid": r.get("uid") or "", "nick": r.get("who") or "",
                                    "text": r.get("text") or "", "ts": ts})
            # 我上次说话的时间也得跟着捞回来。只捞消息不捞这个，重启后
            # last_bot_reply_ts 归零，戳一戳就把「已经回过的旧话」当成没回，
            # 硬补一句自我介绍——线上真发生过
            spoke = [float(r.get("ts") or 0) for r in (rows or []) if r.get("me")]
            if spoke:
                st.last_bot_reply_ts = max(st.last_bot_reply_ts, max(spoke))
        if self._groups:
            logger.info("[fish] 已恢复 %d 个群/会话的最近对话", len(self._groups))

    def _save_recent_context(self) -> None:
        """把每个会话最近 40 句落盘，重启后能接上话头。失败不影响主流程。"""
        try:
            out = {}
            for gid, st in self._groups.items():
                rows = [{"who": m.get("who") or "", "text": str(m.get("text") or "")[:200],
                         "ts": m.get("ts"), "me": bool(m.get("me")), "uid": m.get("uid") or ""}
                        for m in list(st.dialogue)[-40:]]
                if rows:
                    out[str(gid)] = rows
            tmp = self._ctx_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._ctx_path)
        except Exception as e:
            logger.debug("[fish] 最近对话落盘失败：%s", e)

    def _state(self, gid: str) -> GroupState:
        st = self._groups.get(gid)
        if st is None:
            st = self._groups[gid] = GroupState()
        return st

    @staticmethod
    def _echoes_hint(text: str, how: str) -> bool:
        """回复是不是把「怎么接」逐字念了一遍。

        判官那行是给模型的指令，不是给人看的话。真发生过：判官写
        「问一句他最近是不是夜宵吃多了」，模型就发「你最近夜宵吃多了吧」。
        字面重合到这个程度，说明它只是在复读指令，不是在接话——宁可不发。
        """
        def norm(s: str) -> str:
            return re.sub(r"[\s，。！？、；：\"'「」（）()~～…]+", "", str(s or ""))

        a, b = norm(text), norm(how)
        if not a or len(b) < 6:
            return False
        if b in a:
            return True
        # 短角度（「吐槽他这操作」）本来就该被用上，只钉长句照抄
        return len(b) >= 10 and SequenceMatcher(None, a, b).ratio() >= 0.7

    def _seen_mid(self, mid: str) -> bool:
        """同一条消息是否已经处理过。真的发生过：重连补投导致同一条被答两遍。"""
        if not mid:
            return False
        now = time.time()
        # 顺手剪掉 3 分钟前的，量级就几十个 key，不用 OrderedDict
        for k in [k for k, ts in self._mids.items() if now - ts > 180]:
            self._mids.pop(k, None)
        if mid in self._mids:
            return True
        self._mids[mid] = now
        return False

    @staticmethod
    def _claim(st: "GroupState", now: float) -> int:
        """占住冷却位，返回本次占位的 token。真正的确认在 on_decorate 里，失败要 _release_claim 回滚。"""
        st.claim_token += 1
        st.claim_prev = st.last_bot_reply_ts
        st.claiming = True
        st.claim_owner = st.claim_token
        st.last_bot_reply_ts = now
        return st.claim_owner

    @staticmethod
    def _release_claim(st: "GroupState", token=_UNSET) -> None:
        """没发出去就把冷却位还回去——不然一次生成失败要白静默五分钟。

        带 token 时只有持有者能回滚：同群两条消息并发时，先失败的那条
        不能把后一条的占位一起抹掉（实测会连着吐好几条，密度闸白设）。
        """
        if not st.claiming:
            return
        if token is not _UNSET and token != st.claim_owner:
            return
        st.last_bot_reply_ts = st.claim_prev
        st.claiming = False
        st.claim_owner = 0

    # ---------------- 控制台支撑 ----------------
    def _load_overrides(self) -> dict:
        """读群级 override。文件坏了就当空，别让插件起不来。"""
        try:
            raw = json.loads(self.overrides_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning("[fish] 群 override 读取失败（按空处理）：%s", e)
            return {}

    def save_group_overrides(self, groups: dict) -> None:
        """先写临时文件再改名：面板保存时正赶上进程被杀，也不会留下半截 JSON。"""
        tmp = self.overrides_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.overrides_path)
        self._group_overrides = groups

    def meme_stats(self) -> dict:
        """表情包按标签计数，面板要的是汇总数。"""
        try:
            return dict(meme_catalog.stats() or {})
        except Exception as e:
            logger.debug("[fish] 表情包统计失败：%s", e)
            return {}

    def log_event(self, kind: str, gid: str, text: str, **extra) -> None:
        """往环形缓冲里记一条，控制台「最近交互」读的就是它。

        只存长度受限的单行文本：这里的内容会被面板渲染，太长或带换行都会把界面撑乱。
        """
        try:
            self._events.append({
                "ts": time.time(),
                "kind": kind,
                "group_id": str(gid or ""),
                **{k: (str(v)[:80] if k != "score" else v) for k, v in extra.items()},
                "text": str(text or "").replace("\n", " ")[:120],
            })
        except Exception:
            pass

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _peer_set(self) -> set:
        """名单手改后不用重启：mtime 变了就重读。"""
        m = self._mtime(self.peer_path)
        if m != self._peer_mtime:
            self._peer_mtime = m
            self.peers = peer_bots.load(self.peer_path, logger=logger)
            logger.info("[fish] 机器人名单已重载：%d 个", len(self.peers))
        return self.peers

    async def _llm_once(self, prompt: str, system: str, timeout: float) -> str:
        """给 social_read 用的一次性小调用：不落会话历史、不挂工具。"""
        prov = None
        pid = str(self.cfg.get("social_read_provider") or "").strip()
        if pid:
            try:
                prov = await self.context.provider_manager.get_provider_by_id(pid)
            except Exception as e:
                logger.warning("[fish] social_read 指定的 provider %s 不可用：%s", pid, e)
        if prov is None:
            prov = await self.context.get_using_provider_async()
        if prov is None:
            raise RuntimeError("没有可用的对话模型")
        _t0 = time.time()
        resp = await asyncio.wait_for(
            prov.text_chat(prompt=prompt, system_prompt=system, contexts=[]), timeout
        )
        # 判官也要记账，否则「总用量」会少掉一块别人看不见的开销
        try:
            us = _usage_of(resp)
            self.memory.log_token("judge", us[0], us[1], us[2],
                                  int((time.time() - _t0) * 1000))
        except Exception:
            pass
        return getattr(resp, "completion_text", "") or ""

    @staticmethod
    def _tech_tokens(t: str) -> set:
        """抽出句子里像命令行/文件名/型号的英文数字串，用来判断「这套步骤是不是刚讲过」。"""
        return {x.lower().rstrip(".,;:") for x in _TECH_TOKEN_RE.findall(str(t or ""))}

    def _repeats_recent_tech(self, st: GroupState, text: str) -> bool:
        """自己 10 分钟内讲过同一套技术步骤就算复读。

        真实翻车：先说「输 bypassnro 就跳过去了」，下一句又把 shift+f10 全流程讲一遍。
        判据两条：重合两个以上标识符；或者重合一个「强标识符」（4 个字母以上的英文专名，
        像 bypassnro、oobe、cmd），后者是因为换个说法常常只剩一个专名还能对上。
        中文不参与比较，避免把正常接话误判成复读。
        """
        # 只比「像命令/文件/键位」的串：带数字、下划线、斜杠、点、加号，
        # 或者长到不像普通英文单词（驼峰专名）。
        # 以前只卡长度≥4，结果「GPLv2 / MIT / linux」这种技术名词一重合就判复读——
        # 用户接着问 GPLv2 和 MIT 的区别，那句正常回答被整条吞了
        def cmdish(t: str) -> bool:
            return bool(re.search(r"[0-9_\\/.+:]", t)) or (t.isalpha() and len(t) >= 7)

        mine = {t for t in self._tech_tokens(text) if cmdish(t)}
        if not mine:
            return False
        win = float(self.cfg.get("tech_repeat_window_sec", 600) or 600)
        now_t = time.time()
        for m in list(st.dialogue):
            if not m.get("me"):
                continue
            if now_t - float(m.get("ts") or 0) > win:
                continue
            common = mine & {t for t in self._tech_tokens(str(m.get("text") or "")) if cmdish(t)}
            # 两个以上对得上才算「同一套步骤」；一个长专名（bypassnro 这种）也够
            if len(common) >= 2 or any(len(t) >= 6 for t in common):
                return True
        return False

    async def _social_read(self, gid: str, st: GroupState, text: str, nick: str, is_at: bool,
                           is_follow: bool = False):
        """第一步：判该不该接。任何异常都当成「判不了」，直接放行给主模型，别把回复吞掉。"""
        if not self.cfg.get("social_read_enabled", True):
            return None
        if not text and not is_at:
            return None
        # 判官以前只看最近 5 条：群里刷得快的时候，这条窗口连半分钟都覆盖不到，
        # 「神话」这种 10 分钟前刚解释过的东西它根本没见过，于是让主模型再问一遍。
        # 改成时间窗 + 条数上限，判官至少能看见这轮话题是怎么起来的
        win = float(self.cfg.get("social_context_sec", 1800) or 1800)
        now_t = time.time()
        pool = [m for m in list(st.msgs)[:-1]
                if now_t - float(m.get("ts") or 0) <= win][-24:]
        timeline = [f"{m['nick']}：{m['text']}" for m in pool]
        timeout = float(self.cfg.get("social_read_timeout_sec", 8) or 8)

        async def call(prompt, system):
            return await self._llm_once(prompt, system, timeout)

        t0 = time.time()
        try:
            verdict = await social_read.decide(
                call, timeline=timeline, latest=text, nick=nick,
                bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), is_at=is_at,
                is_follow=is_follow,
            )
        except Exception as e:
            logger.warning("[fish] 群 %s social_read 失败（%s: %s），按能接处理",
                           gid, type(e).__name__, str(e)[:80])
            return None
        logger.info(
            "[fish] 群 %s social_read（%.1fs）判=%s 话题=%r 怎么接=%r | 原句：%s",
            gid, time.time() - t0, "接" if verdict.get("join") else "不接",
            verdict.get("topic", ""), verdict.get("how", ""), text[:30],
        )
        return verdict

    async def _grab_pixiv(self, tag: str, gid: str = "") -> str:
        """按标签取一张全年龄原图，返回 base64；任何一步不对都返回空串。

        R18 的判定全在 fish_core/pixiv.py 里（只信 xRestrict/sl 两个官方字段）。
        这里只负责：拿 cookie、控频、把字节转成能塞进消息链的 base64。
        失败一律静默——发不出图只是少一张，不能连累正文
        """
        try:
            cookie = str(self.cfg.get("pixiv_cookie") or "") or _media_parser_pixiv_cookie()
            if not cookie:
                logger.info("[fish] pixiv 没配 cookie，发图跳过")
                return ""
            now = time.time()
            # 冷却按群隔离：只用 tag 当 key 时，A 群问过一句，B 群两秒内同标签直接被拒
            cd_key = f"{gid}:{tag}"
            if not self._pixiv_cd.allow(cd_key, now):
                logger.info("[fish] pixiv 标签「%s」本群刚搜过，%d 秒内不重复搜",
                            tag, int(self._pixiv_cd.window))
                return ""
            proxy = str(self.cfg.get("pixiv_proxy") or "") or None
            # 整条取图给一个硬上限：3 个候选 × 详情 + 下载，最坏也能拖到几十秒，
            # 而这一步是在拼消息链之前同步 await 的，卡住整条回复就发不出去
            async with aiohttp.ClientSession() as sess:
                got = await asyncio.wait_for(
                    pixiv.fetch_one(sess, tag, cookie, proxy),
                    timeout=float(self.cfg.get("pixiv_timeout_sec", 25) or 25))
            if not got:
                logger.info("[fish] pixiv 搜「%s」没拿到可用图（全被分级闸掉或搜不到）", tag)
                return ""
            data, pid, title = got
            # 只有真取到图才记账，失败别白占窗口
            self._pixiv_cd.mark(cd_key, now)
            logger.info("[fish] pixiv 发图：标签「%s」→ %s 《%s》 %.0fKB",
                        tag, pid, title[:20], len(data) / 1024)
            return base64.b64encode(data).decode()
        except asyncio.TimeoutError:
            logger.info("[fish] pixiv 取图超时（%ss），本次不带图", self.cfg.get("pixiv_timeout_sec", 25))
            return ""
        except Exception as e:  # noqa: BLE001
            logger.warning("[fish] pixiv 取图异常：%s: %s", type(e).__name__, e)
            return ""

    def _meme_gate(self, gid: str, st: GroupState, tag: str, now: float | None = None) -> tuple[bool, str]:
        """斗图闸：同一话题一回合只发一张，被跟图/刚发过就不发。"""
        now = time.time() if now is None else now
        # 判定逻辑在 fish_core/meme_gate.py（纯函数），这边只负责把状态喂进去
        return meme_gate.decide(
            tag, now,
            last_meme_ts=st.last_meme_ts,
            recent_tags=list(st.recent_meme_tags),
            incoming_image_ts=st.incoming_image_ts,
            enabled=bool(self.cfg.get("meme_gate_enabled", True)),
            min_gap_sec=float(self.cfg.get("meme_min_gap_sec", 180) or 0),
            follow_gap_sec=float(self.cfg.get("meme_follow_gap_sec", 60) or 0),
        )

    @staticmethod
    def _first_plain_text(event: AstrMessageEvent) -> str:
        """原始第一个文本段。

        waking_check 已经把 wake_prefix 从 event.message_str 里剥掉了，
        想判断「这条是不是 ab! 唤醒的」只能看原始消息段。
        """
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                # 带 @ 的消息里文本段常以空格开头（"@机器人 你好"），
                # 不去掉的话 startswith("ab!") 永远判不到
                return (comp.text or "").lstrip()
            if isinstance(comp, (At, AtAll)):
                continue
        return ""

    def _wake_prefixes(self) -> tuple[str, ...]:
        """真正生效的唤醒前缀集合。

        插件自己配的那一条优先；没配就回落到 AstrBot 全局配置——
        因为 AstrBot 的唤醒词写在 provider_settings.wake_prefix（对话唤醒，
        如 ab!），顶层 wake_prefix 是指令前缀列表（/）。两个都得让路，否则
        同一句话框架 LLM 回一遍、插件再回一遍，群里像两个人在抢话。
        """
        items = []
        own = str(self.cfg.get("wake_prefix") or "").strip()
        if own:
            items.append(own)
        try:
            conf = self.context.get_config() or {}
            ps = conf.get("provider_settings") or {}
            wp = ps.get("wake_prefix") if hasattr(ps, "get") else None
            if isinstance(wp, str) and wp.strip():
                items.append(wp.strip())
            top = conf.get("wake_prefix")
            if isinstance(top, (list, tuple)):
                items.extend(str(x).strip() for x in top if str(x).strip())
            elif isinstance(top, str) and top.strip():
                items.append(top.strip())
        except Exception as e:
            logger.debug("[fish] 读取 AstrBot 唤醒前缀失败：%s", e)
        # dict.fromkeys 去重又保序
        return tuple(dict.fromkeys(items))

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        """@ 我、或者引用回复我。@全体 不算点名（群里一天能 @ 全体几十回）。"""
        self_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == self_id:
                return True
            if isinstance(comp, Reply) and str(getattr(comp, "sender_id", "")) == self_id:
                return True
        # 只有 @ 没有正文时，AstrBot 的唤醒检查可能把那个 At 从组件里摘掉，
        # 光看组件就漏判成「没被点名」→ 插件让路 → 框架用人格外的口气回一句
        # 「你@了我但没说话～」。所以再翻一遍原始事件里的 message 段
        raw = getattr(event.message_obj, "raw_message", None)
        segs = getattr(raw, "message", None)
        if segs is None and isinstance(raw, dict):
            segs = raw.get("message")
        for seg in (segs or []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                if str((seg.get("data") or {}).get("qq")) == self_id:
                    return True
        return False

    @staticmethod
    def _at_others(event: AstrMessageEvent) -> bool:
        """这条是不是 @ 了别人（不是 @全体、也不是 @我）。

        真实翻车：lifefuck 发「@没长大的御姐 我真求你了 你是女的吗」，
        机器人因为刚跟他说过话、处在追问窗口里，抢答成「没叫你吧，这是又求助御姐什么」——
        人家明明在问御姐。@ 了别人就说明这句不是冲它来的。
        """
        try:
            self_id = str(event.get_self_id())
            for comp in (event.message_obj.message or []):
                if isinstance(comp, At) and str(getattr(comp, "qq", "") or "") not in ("", "all", self_id):
                    return True
            raw = getattr(event.message_obj, "raw_message", None)
            segs = getattr(raw, "message", None)
            if segs is None and isinstance(raw, dict):
                segs = raw.get("message")
            for seg in (segs or []):
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str((seg.get("data") or {}).get("qq") or "")
                    if qq and qq not in ("all", self_id):
                        return True
        except Exception:
            return False
        return False

    def _has_media(self, event: AstrMessageEvent) -> tuple[bool, bool]:
        has_image = has_media = False
        for comp in event.message_obj.message:
            name = type(comp).__name__.lower()
            if name == "image":
                has_image = True
            elif name in ("face", "record", "json", "card", "file", "forward"):
                # 表情和语音不是图片：算进 has_image 的话发个表情就被当成「发了图」，
                # 表情包闸白压 90 秒，@ + 表情还会误触发斗图
                has_media = True
        return has_image, has_media

    def _group_allowed(self, gid: str) -> bool:
        """生效群白名单。

        默认空列表 = 谁都不生效：这套插件会主动插话，装完就闷声往真群里发消息
        是最坏的结果。要上线必须把群号写进 enabled_groups，或者填 ["*"] 全开。
        """
        ov = (self._group_overrides.get(str(gid)) or {}).get("enabled")
        # 面板上单独关掉的群优先：群里出事了第一反应是让它闭嘴，
        # 不该逼人回去翻整份配置找 group 白名单
        if ov is False:
            return False
        if ov is True:
            return True

        groups = self.cfg.get("enabled_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        groups = [str(g).strip() for g in groups]
        if "*" in groups:
            return True
        return str(gid) in groups

    def _private_allowed(self, uid: str) -> bool:
        """私聊白名单：默认谁私聊都不理，只有列进去的人和主人才生效。"""
        if not self.cfg.get("enabled", True) or not self.cfg.get("reply_private", True):
            return False
        uid = str(uid).strip()
        owner = str(self.cfg.get("owner_qq") or "").strip()
        if uid and uid == owner:
            return True
        users = self.cfg.get("enabled_private_users") or []
        if isinstance(users, str):
            users = [u for u in re.split(r"[,，\s]+", users) if u]
        return uid in [str(u).strip() for u in users]

    def _order_watch(self, gid: str) -> bool:
        """哪些群开了主动巡检。名单空 = 一个都不查，免得装完就在别人的群里告状。"""
        if not self.cfg.get("order_guard_enabled", True):
            return False
        groups = self.cfg.get("tattle_watch_groups") or []
        if isinstance(groups, str):
            groups = [g for g in re.split(r"[,，\s]+", groups) if g]
        return str(gid) in [str(g).strip() for g in groups]

    async def _report_order(self, event, gid, nick, uid, text, label) -> str:
        """把一条违规私聊上报主人，返回要在群里说的那句话（上报失败返回空串）。"""
        now = time.time()
        gap = float(self.cfg.get("order_guard_cooldown_sec", 600) or 600)
        last = self._order_last.get(str(gid), 0.0)
        if now - last < gap:
            # 同一个群十分钟内只惊动主人一次，不然刷屏的会把你手机打爆
            logger.info("[fish] 群 %s %d 秒前刚上报过，这条不再打扰主人", gid, int(now - last))
            return ""
        # 先占位再 await：下面最长等 30 秒，等回来才写的话，
        # 同一条违规被巡检和模型自报同时触发时，主人会收到两条一样的私聊
        self._order_last[str(gid)] = now
        inst = self._tattle_plugin()
        if inst is None:
            logger.warning("[fish] 巡检命中「%s」，但告状插件不在", label)
            return ""
        content = f"【{label}】{nick}（{uid}）在群 {gid} 说：\n{text}"
        try:
            result = await asyncio.wait_for(
                inst.complain_to_receivers(
                    event,
                    content=content,
                    accused=str(uid),
                    reason=label,
                    # 只上报，不触发插件的自动封禁：罚不罚、怎么罚由主人定
                    sender_at_fault=False,
                ),
                30,
            )
        except Exception as e:
            logger.warning("[fish] 巡检上报失败：%r", e)
            self._order_last.pop(str(gid), None)  # 没送出去，占位还回去
            return ""
        logger.info("[fish] 巡检上报群 %s（%s）：%s", gid, label, str(result)[:100])
        # 插件返回的是 "Complaint finished: 1 sent, 0 failed." 这种串，
        # 一个收件人（主人自己）就是成功；0 sent 才算没送出去，那就别在群里吹
        m = re.search(r"(\d+)\s*sent", str(result) or "", re.I)
        if m and int(m.group(1)) == 0:
            logger.warning("[fish] 巡检上报没送出去（0 sent），群里不吭声")
            self._order_last.pop(str(gid), None)
            return ""
        return random.choice((
            "这条我记下了",
            "行，记一笔",
            "这个我记住了，别当我没听见",
        ))

    # ---------------- 刹车：静默 / 禁言 / 刷屏 ----------------
    def _load_mute_state(self) -> None:
        try:
            if os.path.exists(self._mute_path):
                with open(self._mute_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._mute_state = data if isinstance(data, dict) else {}
        except Exception as e:
            # 状态文件坏了就当没静默过：宁可多嘴，也不能因为一个坏文件永久装死
            logger.warning("[fish] 静默状态读取失败，按未静默处理：%s", e)
            self._mute_state = {}

    def _is_muted(self, gid: str) -> bool:
        return bool(self._mute_state.get(str(gid)))

    def _set_muted(self, gid: str, muted: bool, restore_yunzai: bool | None = None) -> None:
        """记静默状态；restore_yunzai 记「关之前这个群在不在云崽白名单」。

        不记这一位的话，/on 会无条件 sync(True)，把本来就不在白名单的群
        塞进别人的 other.yaml —— 关一次开一次就永久多出一个群号。
        """
        if muted:
            self._mute_state[str(gid)] = (
                {"restore": bool(restore_yunzai)} if restore_yunzai is not None else True
            )
        else:
            self._mute_state.pop(str(gid), None)
        _write_json(self._mute_path, self._mute_state, pretty=True)

    def _mute_restore(self, gid: str) -> bool:
        """静默前它在云崽白名单里吗。老格式（直接 True）当作「不在」。"""
        v = self._mute_state.get(str(gid))
        if isinstance(v, dict):
            return bool(v.get("restore"))
        return False

    async def _bot_banned(self, event: AstrMessageEvent, gid: str) -> bool:
        """机器人在这个群是不是还在禁言期。

        被禁言照样收得到群消息，不拦住就是白烧 token，解禁后还要把一堆
        过期话接一遍。离解禁还远（>60 秒）就不复核，免得每条消息打一次 API。
        """
        if not self.cfg.get("bot_ban_guard", True) or not gid.isdigit():
            return False
        now = time.time()
        expire = self._ban_expire.get(gid, 0.0)
        if now < expire:
            if expire - now > 60:
                return True
            if now - self._ban_checked.get(gid, 0.0) < 30:
                return True
        elif now - self._ban_checked.get(gid, 0.0) < 600:
            # 没有禁言记录时十分钟顺手核一次：抓禁言那一刻可能一条消息都没走完
            return False
        self._ban_checked[gid] = now
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                return False
            info = await asyncio.wait_for(
                bot.call_action("get_group_member_info", group_id=int(gid),
                                user_id=int(event.get_self_id()), no_cache=True),
                timeout=2.0)
        except Exception as e:
            logger.debug("[fish] 群 %s 禁言状态核实失败（按未禁言跑）：%s", gid, e)
            return False
        shut = int((info or {}).get("shut_up_timestamp") or 0)
        if shut > now:
            self._ban_expire[gid] = float(shut)
            logger.info("[fish] 群 %s 机器人还在禁言到 %s，这段时间不回复",
                        gid, time.strftime("%H:%M:%S", time.localtime(shut)))
            return True
        self._ban_expire.pop(gid, None)
        return False

    def _density_blocked(self, st: "GroupState", now: float) -> bool:
        """两分钟里已经主动说了两条就闭嘴。连刷换禁言，比少接一句贵多了。"""
        if not self.cfg.get("density_guard", True):
            return False
        window = float(self.cfg.get("density_window_sec", 120) or 120)
        limit = int(self.cfg.get("density_limit", 2) or 2)
        # 只数「主动插话」：被 @ 和追问窗口是别人点名的，把它们算进来会让
        # 密度闸在正常问答后立刻锁死插话
        q = st.interject_times
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    def _sender_role(self, event: AstrMessageEvent) -> str:
        """尽最大努力取群角色（owner / admin / member）。

        官方 is_admin() 只认 event.role，而 aiocqhttp 适配器根本不给它赋值，
        于是 is_admin() 在 QQ 群永远 False —— 群管发 /off 会被回"这条得群管发"，
        管理动作的 is_admin 分支也形同虚设。唯一可靠来源是原始事件里的
        sender.role，这里把两条路都试一遍。
        """
        role = str(getattr(event, "role", "") or "").lower()
        # 注意：aiocqhttp 下 event.role 常常恒为 "member"（适配器没填），
        # 所以只有明确是 admin/owner 才能直接采信，member 必须继续往下查原始事件
        if role in ("owner", "admin"):
            return role
        # aiocqhttp 把原始事件整个挂在 raw_message 上（不是 dict，是 Event 对象，
        # 但同样支持 .get / 属性访问），所以两种取法都得试
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is not None:
            sender = None
            try:
                sender = raw.get("sender") if hasattr(raw, "get") else None
            except Exception:
                sender = None
            if sender is None:
                sender = getattr(raw, "sender", None)
            if sender is not None:
                try:
                    r = sender.get("role") if hasattr(sender, "get") else getattr(sender, "role", "")
                except Exception:
                    r = ""
                if r:
                    return str(r).lower()
                r2 = getattr(sender, "role", "")
                if r2:
                    return str(r2).lower()
        return role

    def _is_boss(self, event: AstrMessageEvent) -> bool:
        """群管或主人。这两个指令会真的把群关掉，不能谁发都算。"""
        try:
            owner = str(self.cfg.get("owner_qq") or "").strip()
            if owner and str(event.get_sender_id()) == owner:
                return True
            if self._sender_role(event) in ("owner", "admin"):
                return True
            return bool(event.is_admin())
        except Exception:
            return False

    @filter.command("tocken", alias={"token", "用量", "token用量"})
    async def cmd_tocken(self, event: AstrMessageEvent):
        """/tocken：token 用量账本。只给主人，免得群里谁都能查这只鱼的账单。"""
        # 别名里有 "token"，私聊里「token 超了怎么办」这种正常聊天会被当成指令吃掉，
        # 还会 stop_event 让 on_private 再也收不到。只认真·指令形态。
        _raw = (event.message_str or "").strip()
        if not re.match(r"^[/／]?(tocken|token|用量|token用量)(\s+文字)?$", _raw, re.I):
            return
        try:
            owner = str(self.cfg.get("owner_qq") or "").strip()
            if not owner or str(event.get_sender_id()) != owner:
                event.stop_event()
                yield event.plain_result("⚠️ 这条只有主人能看")
                return
        except Exception:
            event.stop_event()
            return
        try:
            st = self.memory.token_stats()
        except Exception as e:  # noqa: BLE001
            logger.warning("[fish] /tocken 统计失败：%s", e)
            event.stop_event()
            yield event.plain_result("⚠️ 账本读不出来，日志里有原因")
            return
        # 默认发图：文字表格在手机 QQ 里必然折行，图没有这个限制。
        # 想看文字版就 /tocken 文字。
        card = None
        if "文字" not in (event.message_str or ""):
            try:
                card = token_card.make(st, str(DATA_DIR / "cache"))
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] /tocken 画卡失败，退回文字版：%s", e)
        event.stop_event()
        if card:
            try:
                yield event.image_result(card)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] /tocken 发图失败，退回文字版：%s", e)
        yield event.plain_result(token_report.render(st))

    @filter.command("off", alias={"静默"})
    async def cmd_off(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            # 私聊里「on my way 马上到」这种正常聊天会被短别名当指令吃掉，
            # 以前还会 stop_event，owner 私聊从此不再回话。只有真写了 /off 才拦。
            if not (event.message_str or "").lstrip().startswith("/"):
                return
            event.stop_event()
            yield event.plain_result("⚠️ 静默只能在群里发")
            return
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        yaml_path = self.cfg.get("yunzai_other_yaml") or ""
        was_listed = yunzai_link.in_whitelist(yaml_path, int(gid))
        self._set_muted(gid, True, restore_yunzai=was_listed)
        if self.cfg.get("yunzai_sync", True):
            # 只关我自己没用：云崽那边的插件还在群里动，「全关」就是句空话
            yunzai_link.sync_whitelist(yaml_path, int(gid), False)
        logger.info("[fish] 群 %s 静默（%s 触发）", gid, event.get_sender_id())
        self.log_event("mute", gid, "静默", who=event.get_sender_name())
        event.stop_event()
        yield event.plain_result("全关了，从现在起群里我就当没听见，发 /on 才回来")

    @filter.command("on", alias={"恢复"})
    async def cmd_on(self, event: AstrMessageEvent):
        gid = self._gid_of(event)
        if not gid or not gid.isdigit():
            # 同上：私聊「on my way」不该被当指令，也不该把事件吞掉
            if not (event.message_str or "").lstrip().startswith("/"):
                return
            event.stop_event()
            yield event.plain_result("⚠️ 这条只能在群里发")
            return
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        restore_yunzai = self._mute_restore(gid)
        self._set_muted(gid, False)
        # 面板/旧数据可能在 override 里留了 enabled=false。只清静默不清它，
        # 就会出现「群里发了 /on、提示恢复了、实际还是不接话」的鬼打墙
        ov = dict(self._group_overrides.get(str(gid)) or {})
        if "enabled" in ov:
            ov.pop("enabled", None)
            try:
                if ov:
                    self._group_overrides[str(gid)] = ov
                else:
                    self._group_overrides.pop(str(gid), None)
                self.save_group_overrides(self._group_overrides)
                logger.info("[fish] 群 %s /on 顺带清掉 override.enabled", gid)
            except Exception as e:
                logger.warning("[fish] 群 %s 清理 override 失败：%s", gid, e)
        if self.cfg.get("yunzai_sync", True) and restore_yunzai:
            # 只有当 /off 之前它本来在白名单里才加回去，否则就是凭空塞群号
            yunzai_link.sync_whitelist(self.cfg.get("yunzai_other_yaml") or "", int(gid), True)
        # 禁言缓存也清掉：刚解禁的那一刻不该还拿旧记录装死
        self._ban_expire.pop(gid, None)
        self._ban_checked.pop(gid, None)
        logger.info("[fish] 群 %s 恢复（%s 触发）", gid, event.get_sender_id())
        # 不 stop 的话框架默认 LLM 会拿「/on」再回一句闲聊
        event.stop_event()
        yield event.plain_result("回来了，该说说该闹闹")

    @filter.command("memes", alias={"reload_memes", "梗库"})
    async def cmd_memes(self, event: AstrMessageEvent):
        """手动重载热梗池和本群黑话，改完 json 不用重启 AstrBot。"""
        if not self._is_boss(event):
            event.stop_event()
            yield event.plain_result("⚠️ 这条得群管发")
            return
        try:
            self.memes.load_pool(force=True)
            self.memes.load_group_slang()
        except Exception as e:
            logger.warning("[fish] 重载热梗失败：%s", e)
            yield event.plain_result(f"⚠️ 重载失败：{e}")
            return
        stats = {}
        try:
            stats = self.memes.stats() or {}
        except Exception:
            pass
        pool = stats.get("pool_size") or len(getattr(self.memes, "current_pool", []) or [])
        total = stats.get("total") or 0
        # 本群黑话单独存，按群号计数才看得出重载的是哪几个群
        slang = getattr(self.memes, "group_slang", None) or {}
        slang_desc = f"，本群黑话 {len(slang)} 个群" if slang else ""
        logger.info("[fish] 手动重载梗库：池 %s/%s 条，黑话 %s 群", pool, total, len(slang))
        event.stop_event()
        yield event.plain_result(f"热梗池 {pool}/{total} 条已重载{slang_desc}")

    # ---------------- 主流程：群消息 ----------------
    def _tattle_plugin(self):
        try:
            from astrbot.core.star.star import star_map
            for md in star_map.values():
                if "tattle" in str(getattr(md, "name", "")):
                    inst = getattr(md, "star_cls", None)
                    if inst is not None and hasattr(inst, "complain_to_receivers"):
                        return inst
        except Exception as e:
            logger.debug("[fish] 找不到告状插件：%s", e)
        return None

    async def _tattle_intent(self, text: str, nick: str):
        """这句话是「让我去告状」还是「在聊告状这功能」？判不了返回 None。"""
        try:
            ans = await self._llm_once(
                f"{nick}：{text}",
                order_watch.TATTLE_SYSTEM,
                float(self.cfg.get("tattle_intent_timeout_sec", 10) or 10),
            )
        except Exception as e:
            logger.warning("[fish] 告状意图判定失败：%s", e)
            return None
        up = str(ans or "").strip().upper()
        if up.startswith("YES"):
            return True
        if up.startswith("NO"):
            return False
        return None

    def _maybe_patrol(self, event, gid: str, text: str, nick: str, uid: str,
                      recent: list) -> None:
        """秩序巡检入口：命中粗筛就起一个后台判定任务。

        跟接话判定完全解耦——判「不接」的那些消息（@的是别人、质量分不够、冷却中）
        原来一次模型都不调，等于把对着骂和刷广告的全放过去了。
        """
        if not self._order_watch(gid):
            return
        if str(uid) == str(event.get_self_id()):
            return
        # 主人自己骂人不用上报：接收人就是他，等于把他的话再发回给他一遍，
        # 群里还会多一句「已经上报了」，纯属自导自演
        owner = str(self.cfg.get("owner_qq") or "").strip()
        if owner and str(uid) == owner:
            return
        hint = order_watch.suspicious(text, recent)
        if not hint:
            return
        now = time.time()
        gap = float(self.cfg.get("patrol_judge_gap_sec", 45) or 45)
        if now - self._order_last.get("judge:" + str(gid), 0.0) < gap:
            return
        self._order_last["judge:" + str(gid)] = now
        asyncio.create_task(
            self._patrol(event, gid, text, nick, uid, hint, self._at_bot(event)))

    # 框架把图 / 合并转发解析成这些占位字符串，光看 message_str 等于什么都没看见
    _QUOTE_PLACEHOLDERS = ("[图片]", "[聊天记录]", "[转发消息]", "[语音]", "[视频]",
                           "[文件]", "[动画表情]", "[表情]")

    async def _quote_audio_desc(self, event, seg_data: dict | None = None,
                                comp=None) -> str:
        """引用的那段语音听一下，返回转写文字（可能为空）。

        坑一：语音段里的 file 往往只是文件名，真正能取的是 url；NapCat 有时连 url
        都不给，那就走 get_record 让它按 mp3 导出——但导出的是容器上的本地路径，
        我们在另一台机器上读不到，只认它回的 url。
        坑二：silk/amr ffmpeg 不认 silk，所以优先要 mp3 导出。
        """
        if not self.cfg.get("hearing_enabled", True):
            return ""
        cands: list[str] = []
        for src in ((seg_data or {}).get("url"), (seg_data or {}).get("file"),
                    getattr(comp, "url", None), getattr(comp, "file", None),
                    getattr(comp, "path", None)):
            s = str(src or "").strip()
            if s and s not in cands:
                cands.append(s)
        deadline = time.time() + float(self.cfg.get("hearing_timeout_sec", 30) or 30)
        raw = b""
        for s in cands:
            if s.lower().startswith(("http://", "https://")):
                raw = await vision.download_audio(s, logger=logger)
            elif s.startswith("/") and os.path.isfile(s):
                # 框架自己下到 data/temp 的那种，直接读
                try:
                    with open(s, "rb") as f:
                        raw = f.read(16 * 1024 * 1024)
                except Exception:
                    raw = b""
            if raw:
                break
        if not raw:
            # 兜底：让 NapCat 转成 mp3 再给个能取的地址
            try:
                fid = str((seg_data or {}).get("file") or getattr(comp, "file", "") or "")
                if fid:
                    res = await asyncio.wait_for(
                        event.bot.call_action("get_record", file=fid, out_format="mp3"), 8)
                    got = ((res or {}).get("data") or {})
                    url2 = str(got.get("url") or got.get("file") or "")
                    if url2.lower().startswith(("http://", "https://")):
                        raw = await vision.download_audio(url2, logger=logger)
                    elif url2.startswith("/") and os.path.isfile(url2):
                        with open(url2, "rb") as f:
                            raw = f.read(16 * 1024 * 1024)
            except Exception as e:
                logger.info("[fish] get_record 兜底失败：%s: %s", type(e).__name__, str(e)[:60])
        if not raw:
            logger.info("[fish] 引用的语音取不到内容，当没听见")
            return ""
        # 转写复用 hearing.py：silk 解码 + ffmpeg 转码 + Gemini 直连都在那儿，
        # 别为「引用的语音」再养一条并行的转写链
        try:
            text = await asyncio.wait_for(
                hearing.transcribe(raw, self.cfg, logger=logger),
                max(3.0, deadline - time.time()))
        except Exception as e:
            logger.info("[fish] 引用语音转写失败：%s: %s", type(e).__name__, str(e)[:60])
            return ""
        return (text or "").strip()

    async def _quote_image_desc(self, url: str) -> str:
        """引用的那张图自己看一眼。缩图 + 走 WB 识图链，1~4 秒。"""
        if not url or not self.cfg.get("vision_enabled", True):
            return ""
        try:
            raw = await vision.component_bytes(_ImgRef(url), logger=logger)
            if not raw:
                return ""
            desc = await vision.describe([base64.b64encode(raw).decode()], self.cfg, logger=logger)
            desc = (desc or "").strip()
            if desc:
                logger.info("[fish] 引用图片识图完成（%d 字）：%s", len(desc), desc[:50])
            return desc
        except Exception as e:
            logger.warning("[fish] 引用图片识图失败：%s: %s", type(e).__name__, str(e)[:80])
            return ""

    async def _fetch_forward(self, event, fid: str):
        """拉合并转发内容。

        坑：转发卡片这一层的 id 不是 get_forward_msg 认的 id，直接拿消息 id 去查
        会回「消息已过期或者为内层消息」。得先 get_msg 拿到 json 卡片里的
        meta.detail.resid，再用它查；实测只有 resid 那条路能出节点。
        """
        try:
            return await asyncio.wait_for(
                event.bot.call_action("get_forward_msg", message_id=fid), 6)
        except Exception as e:
            logger.info("[fish] 转发直查失败（%s）：%s，改查 resid", fid, str(e)[:50])
        try:
            got = await asyncio.wait_for(event.bot.call_action("get_msg", message_id=fid), 6)
            for seg in (((got or {}).get("data") or {}).get("message") or []):
                if not isinstance(seg, dict) or seg.get("type") != "json":
                    continue
                raw = (seg.get("data") or {}).get("data") or ""
                card = json.loads(raw) if isinstance(raw, str) else (raw or {})
                resid = (((card.get("meta") or {}).get("detail") or {}).get("resid") or "")
                if resid:
                    return await asyncio.wait_for(
                        event.bot.call_action("get_forward_msg", message_id=resid), 6)
        except Exception as e:
            logger.info("[fish] 转发 resid 路也失败（%s）：%s", fid, str(e)[:60])
        return None

    async def _forward_text(self, event, fid: str) -> str:
        """合并转发 → 「昵称：内容 / …」。纯图转发会带上第一张图的描述。"""
        if not fid:
            return ""
        res = await self._fetch_forward(event, fid)
        txt = self._flatten_forward(res)
        # 纯图转发：只展开成「[图片]」的话模型完全不知道图里是什么
        if res and "[图片]" in txt:
            url = self._forward_first_image(res)
            if url:
                desc = await self._quote_image_desc(url)
                if desc:
                    txt += f"（转发的图：{desc}）"
        return txt

    async def _quote_detail(self, event, comp) -> str:
        """引用内容为空或只是占位符时，回查被引用的那条消息：文本 / 图片 / 语音 / 合并转发。"""
        mid = str(getattr(comp, "id", "") or "")
        texts: list[str] = []
        imgs: list[str] = []
        audios: list[tuple[dict, object]] = []
        for c in (getattr(comp, "chain", None) or []):
            n = type(c).__name__.lower()
            if n in ("plain", "text"):
                texts.append(str(getattr(c, "text", "") or ""))
            elif n == "image":
                u = str(getattr(c, "url", "") or getattr(c, "file", "") or "")
                if u:
                    imgs.append(u)
            elif n in ("record", "voice", "audio"):
                audios.append(({}, c))
            elif n in ("forward", "node", "nodes"):
                t = await self._forward_text(event, str(getattr(c, "id", "") or ""))
                if t:
                    texts.append(t)
        if not texts and not imgs and not audios and mid:
            try:
                res = await asyncio.wait_for(event.bot.call_action("get_msg", message_id=mid), 6)
                segs = ((res or {}).get("data") or {}).get("message") or []
                for seg in segs:
                    if not isinstance(seg, dict):
                        continue
                    st = seg.get("type")
                    d = seg.get("data") or {}
                    if st == "text":
                        texts.append(str(d.get("text") or ""))
                    elif st == "image":
                        u = str(d.get("url") or d.get("file") or "")
                        if u:
                            imgs.append(u)
                    elif st == "record":
                        audios.append((d, None))
                    elif st in ("forward", "node", "nodes"):
                        t = await self._forward_text(event, str(d.get("id") or ""))
                        if t:
                            texts.append(t)
            except Exception as e:
                logger.info("[fish] 引用回查失败（%s）：%s", mid, str(e)[:60])
        out = "".join(texts).strip()
        if imgs:
            desc = await self._quote_image_desc(imgs[0])
            out = (out + " " if out else "") + (
                f"（引用的图：{desc}）" if desc else "（引用了一张图，没看清内容）")
        if audios:
            seg_data, comp_obj = audios[0]
            heard = await self._quote_audio_desc(event, seg_data=seg_data, comp=comp_obj)
            # 「仅供参考」是硬要求：转写错字不少，模型别把它当原话复述
            out = (out + " " if out else "") + (
                f"（引用的语音，转写仅供参考、可能有错字：{heard}）" if heard
                else "（引用了一段语音，没听清）")
        return out

    async def _quote_context(self, event) -> str:
        """把「引用回复」和「合并转发」的内容取出来放进 prompt。

        框架其实已经调过 get_msg，Reply 段上直接带了被引用的原文（message_str），
        鱼以前只拿 sender_id 判「是不是回我」，原文全丢了；合并转发更惨，
        段里只有一个 id，内容从来没人拉过。所以群里「你看看这个」+ 引用发过来，
        鱼只看见三个字。

        引用图片 / 引用聊天记录时 message_str 只有「[图片]」「[聊天记录]」这种占位符，
        等于没引用——这时候回查原消息，图片直接走识图链看一眼。
        """
        parts = []
        for comp in (event.message_obj.message or []):
            kind = type(comp).__name__.lower()
            if kind == "reply":
                qt = str(getattr(comp, "message_str", "") or "").strip()
                who = str(getattr(comp, "sender_nickname", "") or "").strip()
                if not qt or qt in self._QUOTE_PLACEHOLDERS:
                    # 占位符 = 框架没给正文，回查原消息
                    logger.info("[fish] 引用只有占位符 %r，回查原消息", qt)
                    qt = await self._quote_detail(event, comp)
                    if not qt and str(getattr(comp, "id", "")):
                        logger.info("[fish] 引用回查空（消息 %s 可能已撤回/超时）",
                                    str(getattr(comp, "id", "")))
                if qt:
                    parts.append(f"【他引用了{who or '某人'}的消息】{qt[:400]}")
            elif kind in ("forward", "nodes", "node"):
                fid = str(getattr(comp, "id", "") or getattr(comp, "message_id", "") or "")
                txt = await self._forward_text(event, fid)
                if txt:
                    parts.append(f"【他发了一份聊天记录】{txt[:400]}")
                else:
                    # 内容取不到的转发别写成「（内容没取到）」：模型看见这句话会
                    # 顺着评一句「发的什么玩意」，等于对着空白瞎评价。直接当没看见
                    logger.info("[fish] 合并转发内容取不到（id=%s），这条不喂给模型", fid[:24])
        return "\n".join(parts)

    @staticmethod
    def _flatten_forward(res) -> str:
        """把 get_forward_msg 的节点列表拍成「昵称：内容 / 昵称：内容」。"""
        data = (res or {}).get("data") if isinstance(res, dict) else None
        data = data or res or {}
        if not isinstance(data, dict):
            return ""
        nodes = data.get("messages") or data.get("message") or []
        lines = []
        for node in nodes[:12]:
            if not isinstance(node, dict):
                continue
            sender = (node.get("sender") or {}).get("nickname") or node.get("nickname") or ""
            segs = node.get("message") or node.get("content") or []
            if isinstance(segs, str):
                segs = [{"type": "text", "data": {"text": segs}}]
            buf = []
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                st = seg.get("type")
                d = seg.get("data") or {}
                if st == "text":
                    buf.append(str(d.get("text") or ""))
                elif st == "image":
                    buf.append("[图片]")
                elif st in ("face", "mface"):
                    buf.append("[表情]")
                elif st == "at":
                    buf.append("@" + str(d.get("qq") or ""))
            t = "".join(buf).strip()
            if t:
                lines.append(f"{sender}：{t[:80]}")
        return " / ".join(lines)

    @staticmethod
    def _forward_first_image(res) -> str:
        """合并转发的节点里第一张图的 url。纯图转发全靠它。"""
        data = (res or {}).get("data") if isinstance(res, dict) else None
        data = data or res or {}
        if not isinstance(data, dict):
            return ""
        for node in (data.get("messages") or data.get("message") or [])[:12]:
            if not isinstance(node, dict):
                continue
            segs = node.get("message") or node.get("content") or []
            if isinstance(segs, str):
                continue
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "image":
                    d = seg.get("data") or {}
                    u = str(d.get("url") or d.get("file") or "")
                    if u:
                        return u
        return ""

    def _at_bot(self, event) -> bool:
        """这条消息有没有直接 @ 机器人。判定「骂的是不是它」时很关键：
        QQ 里「这傻逼 bot 好烦」@ 的是别人，其实是在跟群友吐槽。"""
        try:
            segs = getattr(event.message_obj, "message", None) or []
            self_id = str(event.get_self_id())
            for seg in segs:
                if getattr(seg, "type", "") == "at" and str(getattr(seg, "qq", "") or "") == self_id:
                    return True
        except Exception:
            pass
        return False

    async def _patrol(self, event, gid: str, text: str, nick: str, uid: str, hint: str,
                      at_bot: bool = False) -> None:
        """粗筛命中 → 模型判一次 → 真要报就私聊 + 群里说一句。"""
        try:
            ans = await self._llm_once(
                order_watch.judge_prompt(hint, nick, text, self.cfg.get("bot_nickname", "机器人"),
                                         at_bot),
                order_watch.JUDGE_SYSTEM,
                float(self.cfg.get("patrol_judge_timeout_sec", 12) or 12),
            )
        except Exception as e:
            logger.warning("[fish] 巡检判定失败：%s", e)
            return
        if "YES" not in str(ans or "").upper()[:8]:
            logger.info("[fish] 群 %s 粗筛命中「%s」但模型判不算（%s），不报：%s",
                        gid, hint, str(ans)[:20], text[:30])
            return
        line = await self._report_order(event, gid, nick, uid, text, hint)
        if not line:
            return
        try:
            await event.send(MessageChain(chain=[Plain(line)]))
            logger.info("[fish] 群 %s 巡检已在群里提示：%s", gid, line)
        except Exception as e:
            logger.warning("[fish] 巡检群里提示没发出去：%s", e)

    async def _try_tattle(self, event, text: str) -> bool:
        """群友说要告状，就把原话私发给配置里的接收人（群里不出声）。"""
        inst = self._tattle_plugin()
        if inst is None:
            return False
        try:
            # sender_at_fault 恒为 False：这里只是替人传话，
            # 不能因为谁喊了一句告状就把人拉进自动封禁的计数器
            result = await asyncio.wait_for(
                inst.complain_to_receivers(
                    event, content=text, accused="", reason="群友要求转达", sender_at_fault=False
                ),
                30,
            )
        except Exception as e:
            logger.warning("[fish] 告状转达失败：%r", e)
            return False
        # 别拿 "failed" 当关键字：正常返回串是 "1 sent, 0 failed."，一出现 failed 就判失败，
        # 结果每条都记成「告状转达失败」，实际早就送到了
        m = re.search(r"(\d+)\s*sent", str(result) or "", re.I)
        ok = bool(m and int(m.group(1)) > 0) if m else True
        logger.info("[fish] 告状转达%s：%s", "成功" if ok else "失败", str(result)[:120])
        return ok

    def _song_service(self):
        """点歌服务：复用 listen-music 插件的 B 站客户端，发语音由我们自己发。"""
        if self._song_svc is None:
            try:
                from .fish_core.song import SongService
                media_dir = str(DATA_DIR / "song_media")
                os.makedirs(media_dir, exist_ok=True)
                self._song_svc = SongService(media_dir)
            except Exception as e:
                logger.warning("[fish] 点歌服务装不起来：%s", e)
        return self._song_svc

    async def _try_song_request(self, event, gid: str, text: str) -> str:
        """自然语言点歌：识别意图 → 搜索 → 直接发语音。

        返回 ""（这压根不是点歌）/ "ok"（语音发出去了）/ "fail"（接了但没成）。
        三种结果分开才能给对反馈：成了别啰嗦，没成得说一声，别装作没听见。
        """
        title = _parse_song_request(text)
        if not title:
            return ""
        svc = self._song_service()
        if svc is None:
            return ""
        # 有没有真的挑中过曲目：决定失败后该说「没搜到」还是「下不动」
        hit = {"v": False}

        async def _stage(found_title: str) -> None:
            # 曲目一挑中就先报一声，剩下的下载上传要二三十秒。
            # 报用户点的那个名字就好——站上的标题带一堆【无损音质】【4K】装饰，念出来很蠢
            hit["v"] = True
            await svc.say(event, gid, f"找到了，《{title}》，拉着呢")

        try:
            ok = await svc.play(
                event, title, event.unified_msg_origin, gid, on_stage=_stage
            )
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            # CancelledError 不是 Exception，混在一起看才知道是超时还是被掐了
            logger.warning("[fish] 点歌失败「%s」：%s %r", title, type(e).__name__, e)
            return "fail" if hit["v"] else "notfound"
        if ok == "uncertain":
            # 语音多半已经进群，只是没等到回包。说什么都可能自相矛盾，就闭嘴
            logger.info("[fish] 群 %s 点歌「%s」发送结果不确定，不作声", gid, title)
            return "ok"
        if ok:
            logger.info("[fish] 群 %s 自然语言点歌「%s」已发送", gid, title)
            return "ok"
        return "fail" if hit["v"] else "notfound"

    @staticmethod
    def _fmt_mem_when(ts):
        """把记忆时间戳压成「今天/昨天/3 天前/9 月 12 日」。
        模型看绝对时间戳没感觉，看「上个月」才知道这是旧事、不能当现状说。"""
        try:
            t = float(ts or 0)
        except (TypeError, ValueError):
            return ""
        if t <= 0:
            return ""
        import time as _t
        now = _t.time()
        d = int((now - t) // 86400)
        if now - t < 3600:
            return "刚才"
        if now - t < 86400 and _t.localtime(t).tm_mday == _t.localtime(now).tm_mday:
            return "今天"
        if d <= 1:
            return "昨天"
        if d < 30:
            return f"{d} 天前"
        lt = _t.localtime(t)
        return f"{lt.tm_mon} 月 {lt.tm_mday} 日"

    @filter.event_message_type(EventMessageType.ALL)
    @_handler_guard
    async def on_at_only(self, event: AstrMessageEvent):
        """纯 @ 无文字的兜底入口。

        实测：群里发一条只有 @、没有文字的群消息，on_group 根本不触发
        （journal 里没有任何 [fish] 行，回话来自 AstrBot 原生流程）。
        后果不只是绕过开口判定：不记时间线、不记 turn log、不受密度闸和冷却约束。

        这里用 ALL 类型的独立入口接住它，再交给 on_group 走同一套流程：
        收消息 → 记时间线 → decide() 判定 → 该说才调模型。
        只要文本非空就不插手（避免和 on_group 抢同一条消息）。
        """
        if not self.cfg.get("enabled", True):
            return
        try:
            segs = list(getattr(event.message_obj, "message", []) or [])
            if not segs:
                return
            if any(type(c).__name__ in ("Poke", "Video", "File") for c in segs):
                return
            self_id = str(event.get_self_id())
            has_at_me = any(
                type(c).__name__ in ("At", "AtAll")
                and str(getattr(c, "qq", "")) in (self_id, "all")
                for c in segs
            )
            if not has_at_me:
                return
            if (event.message_str or "").strip():
                return
            logger.info("[fish] 纯@无文字消息进入兜底入口：%s <- %s",
                        event.get_group_id() or "private", event.get_sender_id())
            async for r in self.on_group(event):
                yield r
        except Exception as e:
            logger.error("[fish] on_at_only 异常：%s: %s", type(e).__name__, e)

    def _sd_signals(self, gid, st, now, **kw):
        """把群状态摊成 Signals。裸 @ 和主路径共用，避免两处各写一套字段。"""
        from .fish_core import speak_decision as _sd
        base = dict(
            group_id=str(gid), now=now,
            pending_interject_ts=float(getattr(st, "pending_interject_ts", 0.0) or 0.0),
            unanswered_interjects=int(getattr(st, "unanswered_interjects", 0) or 0),
            silent_until=float(getattr(st, "silent_until", 0.0) or 0.0),
            last_bot_reply_ts=float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0),
            last_interject_ts=float(getattr(st, "last_interject_ts", 0.0) or 0.0),
            interject_times=tuple(getattr(st, "interject_times", ()) or ()),
            is_follow=bool(getattr(st, "follow_left", 0)),
            followed_left_hint=0,
        )
        base.pop("followed_left_hint", None)
        base.update(kw)
        return _sd.Signals(**base)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    @_handler_guard
    async def on_group(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True):
            return

        gid = self._gid_of(event)
        # 心跳：先确认「消息到底进没进来」。群里没反应时，这一行是分界线——
        # 有它 = 进到插件了，问题在后面；没它 = NapCat/适配器/白名单，跟对话逻辑无关
        logger.info("[fish] 收到群消息 %s <- %s：%s", gid, event.get_sender_id(),
                    (event.message_str or "")[:30] or "<非文本>")
        if not self._group_allowed(gid):
            # 白名单外的群一个字都不说。但被 @ 时必须拦下来：
            # 只 return 不 stop，AstrBot 自己的默认 LLM 会顶替鱼回话，
            # 等于白名单形同虚设（而且口径完全不是鱼）
            if getattr(event, "is_at_or_wake_command", False):
                event.stop_event()
            return
        if self._is_muted(gid):
            # /off 之后群里说什么都当没听见：连时间线都不记，静默要真的静。
            # 这里必须 stop_event——不拦的话 AstrBot 自己的默认 LLM 还会接 @
            event.stop_event()
            return
        # 戳一戳自己接。不能只 return：on_group 排在 on_poke 前面，
        # return 之后如果 on_poke 也因为「目标 id 对不上」提前返回，
        # 这条空消息就没人拦，原生 LLM 会当成普通消息自由发挥
        if any(isinstance(c, Poke) for c in (event.message_obj.message or [])):
            async for r in self.on_poke(event):
                yield r
            event.stop_event()
            return
        uid = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        if uid == self_id:
            # 自己发的消息（含别的插件代发）：不入时间线，免得学自己说话
            return

        # 去重必须在动时间线之前：NapCat 断线重连会把最近的历史重投一遍，
        # 同一条走两遍就是两次模型调用、两条不同的回答（群里「答两遍」的来源）
        # 键里必须带群号：NapCat 重连后序号会重排，两个群撞同一个 id 时
        # 会把后到的真消息当重复静默丢掉（日志还写「重复投递」，排查会跑偏）
        _mid = getattr(event.message_obj, "message_id", "") or ""
        # 没有 message_id 时不能拼成「群号:」当作 key：那样 180 秒内该群所有消息
        # 都会被判成重复、直接 stop_event 丢掉
        if _mid and self._seen_mid("%s:%s" % (gid, _mid)):
            logger.info("[fish] 群 %s 收到重复投递的消息，忽略", gid)
            event.stop_event()
            return

        raw_first = self._first_plain_text(event)
        # 插件自己的唤醒词（ab!）由下面 forced 分支接管，不在此让路；
        # 只有框架自己的指令前缀（/ 之类）才需要让给 AstrBot
        own_prefix = str(self.cfg.get("wake_prefix") or "").strip()
        wake_prefixes = tuple(p for p in self._wake_prefixes() if p and p != own_prefix)
        if wake_prefixes and raw_first.startswith(wake_prefixes):
            # 带唤醒前缀 = 明确在跟 AstrBot 的主 LLM 说话，这里彻底让路。
            # 注意不能 stop_event：stop_event 会带上 STOP 结果，AstrBot 的默认
            # LLM 分支（process_stage）就整段跳过了，等于把用户的问题吃掉。
            logger.debug("[fish] 群 %s 收到唤醒前缀消息，交给 AstrBot 自身处理", gid)
            return

        text = (event.message_str or "").strip()

        # 视频：看图链路只吃 Image，视频到这儿就是「这行字是空的」，
        # 然后照样走接话评分（0.54）被判「接」，于是对着一个看不见的视频瞎点评。
        # 没画面就别装看见：只有视频 → 不进时间线、不接；视频配字 → 只聊字
        _unseen = ""
        try:
            for _c in (event.message_obj.message or []):
                _cn = type(_c).__name__
                # 只认视频和文件；语音有转文字通路，不能当「看不见」拦下来
                if _cn in ("Video", "File"):
                    _unseen = "视频" if _cn == "Video" else "文件"
                    break
        except Exception:
            _unseen = ""
        if _unseen and not text:
            logger.info("[fish] 群 %s 收到%s但没配字，看不见就不接", gid, _unseen)
            await self._remember(gid, uid, "[发了个%s]" % _unseen)
            event.stop_event()
            return

        if _is_builtin_cmd(text):
            logger.debug("[fish] 群 %s 让路给自带指令：%s", gid, text[:20])
            return
        prefixes = tuple(self.cfg.get("command_prefixes") or ())
        if text.startswith(prefixes):
            # 云崽/面板指令归别的插件，原项目铁律是静默。这里只「不接话」，
            # 不能 stop_event——那会把下游别的插件的同名指令一起掐死
            logger.debug("[fish] 群 %s 面板指令前缀，跳过：%s", gid, text[:20])
            return

        # 引用/合并转发的内容得单独捞：框架只给了 id 和（引用场景下的）原文，
        # 不捞就等于把「这条你听听」的上下文整段丢掉
        quote_ctx = ""
        if any(type(c).__name__.lower() in ("reply", "forward", "nodes", "node")
               for c in (event.message_obj.message or [])):
            try:
                quote_ctx = await self._quote_context(event)
            except Exception as e:
                logger.debug("[fish] 取引用内容失败：%s", e)
        # 合并转发喂进来的是 OCR 式转述：鱼读得懂一半、读不懂另一大半，
        # 读不懂就只能评一句「这记录我看了两遍还是没看出在讲啥」。没人叫就别接
        _has_forward = any(type(c).__name__.lower() in ("forward", "nodes", "node")
                           for c in (event.message_obj.message or []))

        nick = event.get_sender_name() or uid
        # 别的机器人：不接、不进时间线、不占插话机会。
        # 它的播报被当成人话去接、或者跟它对吵刷屏，都是事故；名单见 data/peer_bots.json
        if peer_bots.is_peer_bot(uid, nick, self._peer_set(),
                                 self.cfg.get("peer_bots_name_hints") or ()):
            logger.info("[fish] 群 %s 忽略机器人消息：%s(%s) %s", gid, nick, uid, text[:20])
            return

        st = self._state(gid)
        now = time.time()
        has_image, has_media = self._has_media(event)
        is_at = self._is_at_bot(event)
        # 语音单独拎出来：有它就能「听见」，纯语音消息也就不再是空消息
        recs = [c for c in event.message_obj.message
                if type(c).__name__.lower() == "record"] \
            if self.cfg.get("hearing_enabled", True) else []

        # 注入防护放在最前面：真要套人设的，不必等到后面调模型再说。
        # 只认「明确在改指令/要系统提示词」的说法，正常技术提问不会被拦
        if text:
            injected, snark = safety_guard.check_prompt_injection(text)
            if injected:
                logger.info("[fish] 群 %s 判定注入（%s）：%s", gid, nick, text[:40])
                self.log_event("inject", gid, text[:60], who=nick)
                yield event.plain_result(snark or "这句我不接。")
                event.stop_event()
                return

        # @ 关系：谁爱跟谁搭话，进 prompt 时给一句体感
        if gid and gid.isdigit() and text:
            self_id_now = str(event.get_self_id())
            for comp in event.message_obj.message:
                if isinstance(comp, At) and str(getattr(comp, "qq", "")) not in (self_id_now, "all", ""):
                    try:
                        # 第 5 个位置参数是 target_nick，不是时间戳；
                        # 传错的话文件里的名字会变成一串浮点数
                        cross_interaction.record_interaction(
                            int(gid), int(uid) if uid.isdigit() else 0, nick,
                            int(str(comp.qq)), getattr(comp, "name", "") or "",
                            self.relations_path)
                    except Exception as e:
                        logger.debug("[fish] 关系网记录失败：%s", e)

        # 时间线与在线学习都要「所有」群消息，包括没人叫它的那些
        # 纯语音也得进时间线：只认 text/图片的话，模型上下文里那条转写就成了
        # 没有前文的「凭空出现」，判官还会按「这人什么都没说」判
        if text or has_image or recs:
            # 文字配图是最常见的发法（「这啥」「看这个」+ 图）：只留文字等于把图吞了
            if text and has_image:
                _shown = f"{text} [图片]"
            else:
                _shown = text or ("[图片]" if has_image else "[语音]")
            _msg_entry = {"uid": uid, "nick": nick, "text": _shown, "ts": now}
            _dlg_entry = {"who": nick, "text": _shown, "ts": now,
                          "me": False, "uid": uid}
            st.msgs.append(_msg_entry)
            st.dialogue.append(_dlg_entry)
            # 同一句也压进「只增不改」的上文：下一轮它就是缓存前缀的一部分
            self._push_ctx(st, "user", f"{nick}：{_shown}")
            # 记下这条上文的确切长相：_ctx_of 靠它把「本轮这句」从上下文里弹掉。
            # 以前传的是出口那段被改写过的 text（加了视觉描述、媒体提示），
            # 跟压进去的 _shown 对不上就弹不掉，图和语音那一轮这句会被喂两遍
            event.set_extra("fish_ctxline", f"{nick}：{_shown}")
            # 图不能只留个 [图片] 占位：模型看得见「有人发了图」却拿不到内容，
            # 后面聊到就只能瞎编（实测「一堆截图我这也看不清」就是这么来的）。
            # 这里后台识图，识别完把描述写回时间线，代价是一条消息一次视觉调用
            if has_image and self.cfg.get("vision_enabled", True):
                asyncio.create_task(
                    self._backfill_image_desc(gid, event, _msg_entry, _dlg_entry))
            if now - getattr(self, "_ctx_saved_at", 0.0) > 20:
                self._ctx_saved_at = now
                self._save_recent_context()
            if gid:
                affect.on_group_message(gid, text, private=False, uid=uid, nick=nick)
                # 好感度走本地规则，不吃 token；同一人两分钟内只算一次
                try:
                    before = favor.score(gid, uid)
                    after = favor.on_message(gid, uid, text)
                    # 好感涨跌顺手喂心境：被夸语气会软、被骂会掉脸。
                    # affect.on_favor_delta 搬过来之后一直没人调，是条死链路
                    if abs(after - before) >= 1:
                        affect.on_favor_delta(gid, after - before, text)
                except Exception as e:
                    logger.debug("[fish] 好感/心境联动失败：%s", e)
            if self.cfg.get("style_learning", True) and text:
                st.pool.append({"sender": nick, "sender_id": uid, "content": text})
                if len(st.pool) > 200:
                    del st.pool[:-200]
                style_learner.observe(gid, st.pool)
            if has_image:
                st.incoming_image_ts = now
            # 跨群话题：只留削过隐私的短摘要，别的群能不能被引用看开关
            if self.cfg.get("cross_group_enabled", True) and text:
                try:
                    self.cross.record_topic(gid, nick, text)
                except Exception as e:
                    logger.debug("[fish] 跨群话题记录失败：%s", e)
        st.last_msg_ts = now
        st.msgs_since_bot += 1

        # 想听歌就直说，不用敲 /搜索歌曲：交给点歌插件发语音，自己不出声
        if self.cfg.get("natural_song", True) and text:
            song_res = await self._try_song_request(event, gid, text)
            if song_res in ("ok", "notfound", "fail"):
                # 三种结果分开说：以前搜不到和下载失败共用一句
                # 「搜到了但弄不下来」，搜不到时这句是假话
                if song_res == "ok":
                    # 歌已经发出去了，回一句就够，别学客服播报
                    line = random.choice(("喏", "放这了", "来了", "听着"))
                    self._record_spoke(gid, uid, line, reason="点歌", nick=nick)
                elif song_res == "notfound":
                    line = random.choice(("没搜到这首，歌名再说准点",
                                          "站上没有这首，换个名字试试"))
                    self._record_spoke(gid, uid, line, reason="点歌没搜到", nick=nick)
                else:
                    line = random.choice(("找到了但弄不下来，换一首吧",
                                          "这首拉不下来，换一个"))
                    self._record_spoke(gid, uid, line, reason="点歌失败", nick=nick)
                yield event.plain_result(line)
                event.stop_event()
                return

        # 说要告状就直接私发给接收人，自己不接话。
        # 只认关键词太蠢：群里聊「它会自动告状」也被当成请求转过去（实测就是这样）。
        # 现在三层：关键词初筛 → 必须是对着它说 → 模型确认这是「让我去告状」而不是在讨论功能
        if (self.cfg.get("natural_tattle", True) and text and _is_tattle_request(text)
                and (is_at
                     # 这里不能引用下面才赋值的 is_follow（会 UnboundLocalError，
                     # 非 @ 的告状请求全崩在入口），就地算一遍追问窗口
                     or (uid == st.follow_uid and now < st.follow_until and st.follow_left > 0)
                     or str(uid) == str(self.cfg.get("owner_qq") or ""))):
            asked = await self._tattle_intent(text, nick)
            if asked is None:
                # 判官挂了就按原样转，宁可多转一次，也别让人喊了没人管
                asked = True
            if asked and await self._try_tattle(event, text):
                event.stop_event()
                return
            elif not asked:
                logger.info("[fish] 群 %s 提到了告状，但看着是在聊功能不是求我办事，不转", gid)

        if not text and not has_image and not recs and not quote_ctx:
            if is_at:
                # 只是 @ 一下没说话。以前这里直接本地应一声就 return，
                # 判定对象永远看不到这条路——「任何路径都要过开口判定」的第一个漏洞就是它。
                # 现在先判：说不说归 decide()，说什么降级成本地应声（为一句「？」烧模型不值，
                # 模型还会顺着空白编出客服腔）。
                from .fish_core import speak_decision as _sd
                _replied = (now - float(st.last_bare_at_ts or 0)) <= 45
                _bare_sig = self._sd_signals(
                    gid, st, now, is_at=True, empty=True, uid=str(uid), nick=nick,
                    text="<裸@>", replied_recently=_replied,
                )
                _bare_dec = _sd.decide(_bare_sig, self.cfg)
                logger.info("[fish][sd] 群 %s 裸@ %s", gid, _bare_dec.log_line())
                if not _bare_dec.speak:
                    logger.info("[fish] 群 %s 裸@不接（%s）", gid, _bare_dec.reason)
                    self.log_event("skip", gid, "<裸@>", who=nick, why=_bare_dec.reason)
                    await self._remember(gid, uid, "")
                    event.stop_event()
                    return
                st.last_bare_at_ts = now
                yield event.plain_result(random.choice(["？", "咋了", "在呢", "你说"]))
                event.stop_event()
                return
            # 纯语音/表情/卡片：不接，但别 stop——语音转文字之类的插件还要处理这条
            if not is_at:
                return

        # 框架层的 ab! 唤醒词已关掉（不然那条路走原生流程、没有人设）。
        # 但用户的手感还在：手打前缀就是明着叫它，按「被叫」处理，前缀本身不喂给模型
        forced = False
        prefix = str(self.cfg.get("wake_prefix") or "ab!")
        if text and text.lstrip().lower().startswith(prefix.lower()):
            text = text.lstrip()[len(prefix):].lstrip()
            forced = True

        # 文字游戏：纯本地状态机，跟 @ 和插话概率都无关，谁发都能玩
        if self.cfg.get("enable_games", True) and text and gid.isdigit():
            quick = games.handle_quick_game_commands(text)
            if quick:
                yield event.plain_result(quick)
                event.stop_event()
                return
            handled, bomb_reply = games.process_number_bomb(int(gid), text, uid, nick)
            if handled:
                if bomb_reply:
                    yield event.plain_result(bomb_reply)
                event.stop_event()
                return

        # 斗图：有人 @ 它还甩图，就当是来斗图的。发图比说话慢半拍，两条分开发
        if is_at and has_image and self.cfg.get("meme_battle_enabled", True):
            img_url = ""
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    img_url = getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                    break
            action = fun_features.meme_battle(
                text=text, image_url=img_url or None, meme_catalog=meme_catalog,
                last_battle_ts=st.last_meme_ts, group_id=gid, user_id=uid, now=now)
            if action:
                st.last_meme_ts = float(action.get("battle_ts") or now)
                try:
                    await event.send(MessageChain(chain=[Plain(action["text"])]))
                    await asyncio.sleep(float(action.get("delay_sec") or 0.4))
                    await event.send(MessageChain(chain=[Image.fromBase64(action["payload"])]))
                    logger.info("[fish] 群 %s 斗图：%s", gid, action["text"][:24])
                    self._record_spoke(gid, uid, f"{action['text']} [斗图]",
                                       reason="斗图", nick=nick)
                except Exception as e:
                    logger.warning("[fish] 斗图发送失败：%s", e)
                event.stop_event()
                return

        # ---------------- 免@追问窗口 ----------------
        # 窗口的开启推迟到「这条真的要回」之后（见下面的 _claim 处）。
        # 早开的话 reply_when_at=false 也会开出窗口，后续不 @ 的发言照样被接
        # ---------------- 问好感度：本地报真实分 ----------------
        # 线上实测把这条交给模型，它会编成「系统提示：当前好感度 233，
        # 已解锁「炖鱼」隐藏剧情，你想刷到多少」——数字对不上，人设也塌了
        if is_at and "好感" in text and any(k in text for k in ("多少", "几", "查", "看", "显示", "值", "分")):
            _v = favor.score(gid, uid)
            line = random.choice([
                f"你现在 {_v:.0f} 分，{favor.tier(_v)}",
                f"查这个干嘛，{_v:.0f} 分，{favor.tier(_v)}",
                f"{_v:.0f} 分，{favor.tier(_v)}——问这个是想刷还是想掉",
            ])
            logger.info("[fish] 好感度查询 <- %s(%s) 群 %s：%.0f", nick, uid, gid, _v)
            self.log_event("reply", gid, line, who=nick, why="好感度查询", meme=0, parts=1)
            st.recent_bot.append(line)
            st.last_bot_reply_ts = now
            st.reply_times.append(now)
            yield event.plain_result(line)
            return

        # @ 了别人就不算「接着跟我说」：追问窗口是按「我刚说过话」开的，
        # 不是按「他冲我说」开的，中间他一转头跟别人聊，窗口就该让路
        _talk_to_other = self._at_others(event)
        if _talk_to_other:
            # 记一笔：这人这会儿在跟别人说话。紧跟着的那句没 @，也还是冲对方说的
            # （实测：@我是神人「感觉哈基米没有GPT有人味」→ 下一句「感觉Muse是最
            #  有人味的」没带 @，鱼当是在夸自己，回了「主人自己做的当然偏心它」）
            st.other_dir[uid] = now
        elif not is_at:
            _other_ts = st.other_dir.get(uid, 0.0)
            # 90 秒内且这中间我没插过话 = 还在跟对方聊，不该轮到我
            if _other_ts and now - _other_ts < 90 and st.last_bot_reply_ts < _other_ts:
                _talk_to_other = True

        # 结算上一次「主动插话有没有人搭理」：有人 @ 它、叫它名字，就是接上了；
        # 90 秒过完还是没人理，计一次冷场，连着两次就撤退一段时间
        if bool(is_at) or self._addressed_to_me(text):
            st.pending_interject_ts = 0.0
            st.unanswered_interjects = 0
            st.silent_until = 0.0
        elif st.pending_interject_ts and now - st.pending_interject_ts > 90:
            st.pending_interject_ts = 0.0
            st.unanswered_interjects += 1
            if st.unanswered_interjects >= int(self.cfg.get("cold_shoulder_strikes", 2) or 2):
                st.silent_until = now + float(self.cfg.get("cold_shoulder_sec", 600) or 600)
                logger.info("[fish] 群 %s 连续 %d 次插话没人接，安静 %d 秒",
                            gid, st.unanswered_interjects, int(st.silent_until - now))
        if (not (is_at or forced) and not _talk_to_other and self.cfg.get("reply_when_at", True)
                and uid == st.follow_uid and now < st.follow_until):
            if st.follow_left > 0:
                reason = "追问窗口"
                is_follow = True
            else:
                # 配额用完就当窗口关闭，别让他继续白嫖
                st.follow_uid = ""
                is_follow = False
        else:
            is_follow = False

        # ---------------- 禁言期直接收工 ----------------
        # 被禁言也收得到消息，不拦住就是白烧 token，解禁后还得回头接一堆过期话
        if await self._bot_banned(event, gid):
            event.stop_event()
            return

        # ---------------- 被指挥处置人：禁言 / 踢人 ----------------
        # 放闲聊之前：群管说「把某人禁言十分钟」，不该先被模型当玩笑回掉
        if self.cfg.get("admin_actions_enabled", True) and gid and gid.isdigit():
            nick_map = {str(m["uid"]): (m.get("nick") or str(m["uid"]))
                        for m in list(st.msgs)[-40:]}
            try:
                acted = await admin_actions.maybe_run(
                    event, event.bot, gid, str(uid), str(event.get_self_id()),
                    str(self.cfg.get("owner_qq") or ""), nick_map, text,
                    addressed=bool(is_at or forced), is_admin=self._sender_role(event) in ("admin", "owner"))
            except Exception as e:
                logger.warning("[fish] 处置动作执行异常：%s", e)
                acted = None
            if acted:
                logger.info("[fish] 群 %s 处置动作：%s", gid, acted)
                event.stop_event()
                yield event.plain_result(acted)
                return

        # ---------------- 解析外包给 media_parser 时的闭嘴闸 ----------------
        # 光贴链接是在「分享」，不是找它说话；以前它会接一句「我看不了视频」，
        # 等于主动自曝短板。带评价词（「你觉得呢」）的照旧交给模型。
        if text and gid and _URL_RE.search(text) and not _ASK_OPINION_RE.search(text):
            logger.info("[fish] 群 %s 链接消息，交给解析插件，不接话", gid)
            event.stop_event()
            return

        # ---------------- 秩序巡检 ----------------
        # 必须在接话判定之前：判「不接」的那些消息（@的是别人 / 质量分不够 / 冷却中）
        # 原来一次模型都不调，等于把对着骂和刷广告的全放过去了
        if text.strip():
            self._maybe_patrol(event, gid, text, nick, uid,
                               [m["text"] for m in list(st.msgs)[-6:]])

        # ---------------- 有人让我去骂人（动作层，搬自旧 bot 的 actions.py） ----------------
        # 「群友借它的嘴」和「它被骂了回嘴」是两件事，通道就这么分开：
        # 这条自己生成几句、真 @ 到人头上，不走接话概率、也不进人设
        if text and gid and self.cfg.get("insult_actions_enabled", True):
            _act = insult_actions.parse(text)
            if _act:
                if await self._do_insult(event, gid, text, uid, st, _act):
                    await self._remember(gid, uid, text)
                    event.stop_event()
                    return

        # ---------------- 挨骂了：要不要还嘴、凶到什么程度 ----------------
        # 判定全在代码里：别人互骂、吐槽第三方一律不动，只有点名骂它、
        # 或者骂到 life 头上才加火。提示词里没有常驻的「被骂就喷」——
        # 常驻一条会让它把调侃也当挑衅，好不容易追上的闲聊立刻退回去
        quarrel_hint, quarrel_force = "", False
        if text and self.cfg.get("quarrel_enabled", True):
            _same = [m for m in list(st.msgs)[-3:] if str(m.get("uid")) == str(uid)]
            _owner = str(self.cfg.get("owner_qq") or "").strip()
            _owner_hit = bool(_owner) and str(uid) == _owner
            # 25 秒内它刚说过话，就当这条是在对它说：群里骂人一大半不带 @，
            # 少这条判断，「你他妈傻逼」这种明摆着冲它来的会被当空气
            _to_me = ((now - getattr(self, "_last_bot_ts", {}).get(gid, 0.0)) < 25
                      and (is_at or self._addressed_to_me(text)
                           or bool(re.search(r"你(?:他妈|妈|丫|个|们|这|那|是|说|在)", text))))
            # 注意：这里要的是「这句在骂主人」，不是「这句话是主人说的」。
            # 以前传 _owner_hit，结果主人自己吐槽一句「这 sb 又崩了」，
            # 鱼收到的提示是「life 刚指名骂了你主人 life」，直接进三档开喷
            _owner_cursed = (not _owner_hit) and bool(_owner) and any(
                u in text for u in (f"@{_owner}", str(_owner)))
            _lvl = quarrel.classify(text, at_me=is_at, mentions_owner=_owner_cursed,
                                    consecutive=len(_same) >= 2, reply_to_me=_to_me)
            _hot = self._anger_unlimited(gid)
            if _lvl >= 3 and not _hot:
                # 同一个人五分钟两次封顶，超过自动降成二档：不然对方阴阳十句它喷十句
                _lvl = quarrel.gate(f"{gid}:{uid}", _lvl)
            if _lvl >= 2:
                quarrel_hint = quarrel.hint(_lvl, nick, owner=_owner_hit, anger=_hot)
                # 被骂了必须当场回：等冷却、等判官点头，等它想好骂战早结束了。
                # forced 一起置位，后面「该不该开口」和判官那两处都按被叫处理
                quarrel_force = forced = True
                logger.info("[fish] 群 %s %r 骂到头上（%d 档%s），加火：%s", gid, nick, _lvl,
                            "·红温" if _hot else "", text[:30])

        # 同一个人连发十条它回十条，真人不会这样。90 秒内已经被回过两次的人，
        # 第三条起只在被 @ 或带问号时才接——嘴上说「我在听」，手别抢着答
        # 被点名、被骂到头上、在追问窗口里，都不受这条限制：
        # 以前没豁免 forced，于是「有人刚骂了它，但同一人 90 秒内已被回 2 次」
        # 会被静默丢掉——anger 加了、force 置了，群里一个字没有，像装死
        if not is_at and not forced and gid and text:
            _hist = [x for x in getattr(self, "_replied_uids", {}).get(gid, []) if now - x[0] < 90]
            hit = sum(1 for x in _hist if x[1] == uid)
            if hit >= 2 and not re.search(r"[?？]", text):
                logger.info("[fish] 群 %s %s 90 秒内已被回 %d 次，这条先不接：%s",
                            gid, nick, hit, text[:30])
                self.log_event("skip", gid, text[:60], who=nick, why="同一人连发")
                await self._remember(gid, uid, text)
                return

        # ---------------- 开口判定（唯一入口） ----------------
        # 这里以前是一串 if：被 @ 一条、手打前缀一条、追问窗口一条、主动插话一条，
        # 各自判各自的密度和冷却，改一处漏三处。现在只剩下一个 decide()：
        # @ / 唤醒 / 被骂 / 追问 / 主动插话都只是输入信号（优先级），
        # 密度、冷却、配额、判官、冷场退避全部由对象统一算，任何路径都不许绕过。
        from .fish_core import speak_decision as _sd

        _link = bool(_URL_RE.search(text or ""))
        # 旁观态打分器只做「质量分 + 按人冷却 + 小时配额 + 复读」这几个信号，
        # 结论塞进 decide() 由它统一裁决（不再由它自己决定说不说）
        _gate = ""
        _ambient = not (is_at or forced or is_follow)
        if _ambient:
            _q_ok, _q_why = self._should_interject(gid, st, text, now, has_image, has_media, uid)
            if not _q_ok:
                _gate = _q_why
        _cap_win = float(self.cfg.get("follow_reply_window_sec", 180) or 180)
        _recent_replies = len([t for t in st.reply_times if now - t <= _cap_win])
        _sig = _sd.Signals(
            group_id=str(gid), uid=str(uid), nick=nick, text=text or "", now=now,
            is_at=bool(is_at), is_wake=bool(forced),
            # 被骂必须有指向证据才算数，无证据会被模块降级成普通消息
            abused=bool(quarrel_force), abuse_evidence="被点名骂" if quarrel_force else "",
            is_follow=bool(is_follow), follow_left=int(getattr(st, "follow_left", 0) or 0),
            pending_interject_ts=float(getattr(st, "pending_interject_ts", 0.0) or 0.0),
            unanswered_interjects=int(getattr(st, "unanswered_interjects", 0) or 0),
            silent_until=float(getattr(st, "silent_until", 0.0) or 0.0),
            last_bot_reply_ts=float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0),
            last_interject_ts=float(getattr(st, "last_interject_ts", 0.0) or 0.0),
            interject_times=tuple(getattr(st, "interject_times", ()) or ()),
            recent_replies=_recent_replies,
            has_image=bool(has_image), has_media=bool(has_media),
            has_forward=bool(_has_forward), is_link=_link,
            empty=not (text or "").strip(),
            # 分数只作记录与门槛参考：门槛由打分器按群意愿算过，这里不再重判一次
            judge_score=(getattr(self, "_last_quality", None) if _ambient else None),
            judge_threshold=0.0,
            ambient_gate_blocked=_gate,
            # 指向：旧代码用 _talk_to_other 一句话了事，现在作为信号进对象
            target_uid=("someone_else" if _talk_to_other else ""),
            address_evidence=("@别人" if _talk_to_other else ""),
            muted=bool((is_at or forced) and not self.cfg.get("reply_when_at", True)),
            banned=bool(getattr(st, "banned", False)),
        )
        _dec = _sd.decide(_sig, self.cfg)
        logger.info("[fish][sd] 群 %s %s", gid, _dec.log_line())
        if not _dec.speak:
            logger.info("[fish] 群 %s 不接（%s）：%s", gid, _dec.reason, (text or "")[:30])
            self.log_event("skip", gid, (text or "")[:60], who=nick, why=_dec.reason)
            await self._remember(gid, uid, text)
            # /off 之后被 @ 也不该让原生流程接手，这里照旧收口
            if _dec.reason.startswith("已 /off"):
                event.stop_event()
            return
        reason = _dec.reason

        # 到这儿这条已经确定要回：先开追问窗口，再占住冷却位。
        # 窗口在这里开（而不是在收到 @ 时开）——没回出去的 @ 不该换来后续免@ 特权。
        # 同一人在窗口内再 @，把截止时间往后推，次数只补到 2，不补满：
        # 补满就等于「@ 一下换 5 次回」，一个人能把机器人拖着吵一整天
        if is_at or forced:
            # 同一个人在窗口里再 @，至少补到 2 次：他明确点了名，接着说话还接不上太蠢。
            # 但不补满（补满就是「@ 一下换 N 次回」，一个人能把鱼拖着吵一整天）
            _fresh = uid != st.follow_uid or now >= st.follow_until
            _max = int(self.cfg.get("follow_max_turns", 2) or 0)
            st.follow_left = _max if _fresh else max(st.follow_left, min(2, _max))
            st.follow_uid = uid
            st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)

        # 占位而不是落定：social_read 和识图还要几秒，这期间同群陆续进来的消息
        # 若不占位，每条都以为自己「还没说过话」，一波刷屏会被连着接好几次。
        # 真发出去才确认，失败要 _release_claim 回滚（带上 token，只回滚自己这次）
        _claim_tok = self._claim(st, time.time())

        # 链接的卡片和下载在进「接不接话」判定之前就丢出去了（见 on_group 开头的
        # 媒体链接分支），这里只记一下，免得下面又解析一遍卡片、又下一次视频

        # ---------------- 第一步：这帮人在聊什么、我该不该接；同时看图 ----------------
        # 两件事互不依赖，并发跑。串行的话识图那十几秒会整个叠在 judge 后面
        social_hint = ""
        how_used = ""
        imgs = [c for c in event.message_obj.message if isinstance(c, Image)] \
            if (has_image and self.cfg.get("vision_enabled", True)) else []
        # 判官合并：没人点名时不再单独发一次小请求（那一次要 3~5 秒，而且
        # prompt 每轮都不同、缓存永远命不中），改成把「要不要开口」交给主模型，
        # 不想接就吐 [PASS]（出口已有 PASS 闸）。被 @ / 追问窗口里不许 PASS，照旧必回
        _inline_judge = bool(self.cfg.get("judge_inline", True)) and not (is_at or forced or is_follow)
        if _inline_judge:
            social_hint = ("【这条没人叫你】先掂量一下值不值得接：插进去只是废话、或者接不住，"
                           "就只输出 [PASS]，一个字都别多说；要接就一句话接住话头。")
        verdict, vision_desc, voice_text = await asyncio.gather(
            _noop() if _inline_judge else self._social_read(gid, st, text, nick, is_at or forced, is_follow),
            self._image_desc(gid, event) if imgs else _noop(),
            hearing.from_components(recs, self.cfg, logger=logger) if recs else _noop(),
        )
        if voice_text:
            logger.info("[fish] 群 %s 语音转写：%s", gid, voice_text[:60])
        if vision_desc:
            logger.info("[fish] 群 %s 图片描述：%s", gid, vision_desc[:80])
        if verdict and not verdict.get("join"):
            if not (is_at or forced):
                # 判「不接」就到此为止，主模型一次都不调（这是这一步省下的主要延迟）
                logger.info("[fish] 群 %s social_read 判不接（话题=%r），不调主模型：%s",
                            gid, verdict.get("topic", ""), text[:30])
                self.log_event("skip", gid, text[:60], who=nick,
                               why=f"social_read 判不接（{verdict.get('topic', '')}）")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)  # 模型都没调，冷却位不该被这条占掉
                # 能走到这里必然不是 @ 也不是追问窗口，不 stop，让兄弟插件照常处理
                return
            # 被直接点名的没法装死不回，记一笔继续走
            logger.info("[fish] 群 %s social_read 想判不接，但这句是 @ 你，仍然回", gid)
        elif verdict:
            # 把上下文一起交给清洗：判官编的细节只有对上上下文才判得出来
            _ctx = " ".join(str(m.get("text", "")) for m in list(st.msgs)[-24:])
            # 判官只决定「接不接」，不再往主模型塞「怎么接」的角度提示：
            # 表达阶段被塞了提纲，写出来就带汇报腔（照抄闸的样本全是这么来的）。
            # how 仍然取出来，只用于出口照抄闸和日志
            social_hint = ""
            how_used = social_read.how_of(verdict, _ctx)
            # 判官读出来的「这句在说什么」只当一行释义塞给主模型。
            # 梗和多义词（cpdd = 处CP / 组队上分）光靠模型自己猜会挑错意思，
            # 而判官那边早就读对了，卡在它的判断没往表达层传。
            # 只给释义、不给「怎么接」：表达阶段吃到提纲就会写成汇报腔
            _topic = str((verdict or {}).get("topic") or "").strip()
            # 开关留在配置里，万一发现它把闲聊带成解说腔，关一行就回到原样
            if (_topic and self.cfg.get("topic_hint_enabled", True)
                    and _topic not in ("无", "none", "None", "—")):
                social_hint = f"【这句在说：{_topic[:24]}】"
                logger.info("[fish] 群 %s 释义注入：%s", gid, social_hint)

        # 顺序要紧：先召回记忆再落这一条。反过来的话当前这句会被自己的召回捡回来，
        # prompt 里就出现「你还记得我明天要干嘛吗」这种自己引用自己的鬼话
        media_desc = ""
        if _unseen:
            # 「看不见但可以说话」：明确告诉模型没有画面，别猜内容
            media_desc = f"【他刚发了个{_unseen}，你没有画面也听不到，看不到里面是什么】"
        # 检索只走模型自己吐的 [搜索:关键词] 指针（见 _decorate_impl 0 段）。
        # 以前这里还有一条「被 @ + detect() 命中就抢先查」的近路，等于绕过模型判断：
        # 「帮我查查我昨天说的话」这种不该外联的也会被拖去搜。已删。

        # 联网扩展只在「被叫到 + 明确问了」时才发请求：普通闲聊一句都不外联。
        # 这里是体验和能力的平衡点——宁可少搜，也不让每句话多挂一秒外网延迟
        _searched = False
        if self.cfg.get("connectors_enabled", True) and (is_at or forced) and text:
            try:
                img_url = next((getattr(c, "url", "") or getattr(c, "file", "")
                                for c in event.message_obj.message if isinstance(c, Image)), "")
                if has_image and img_url and re.search(r"出处|哪部|什么番|动漫|动画|叫什么", text):
                    anime = await connectors.search_anime_by_image(await self._http(), img_url)
                    if anime and anime.get("title"):
                        media_desc += f"\n【以图搜番】{anime['title']} 相似度{anime.get('similarity', '?')}"
                elif _REALTIME_RE.search(text) and (
                        _EXPLICIT_SEARCH_RE.search(text) or not _NO_SEARCH_RE.search(text)):
                    # 「搜索」和「搜一下」是两回事，只写后者会漏掉「帮我联网搜索一下」这种最自然的说法
                    q = _search_query(text)
                    # 「联网搜索回答」这种只剩指令碎片的，改用上一句真人提问来搜
                    if len(q) < 4 or q.strip(" ，。！？") in _VAGUE_Q:
                        _prev = self._last_human_line(st, exclude=text)
                        if _prev:
                            _q2 = _search_query(_prev) or _prev
                            logger.info("[fish] 群 %s 检索词是空壳（%r），改用上一句提问：「%s」",
                                        gid, q, _q2[:30])
                            q = _q2
                        else:
                            q = ""
                    if q and (len(q) >= 4 or q not in _VAGUE_Q):
                        _searched = True
                        briefs = await connectors.search_web_brief(await self._http(), q, limit=3)
                        if briefs:
                            media_desc += ("\n【刚查到的东西（这就是实时结果，直接拿来回答，"
                                           "别再说自己查不了网）】：" + "；".join(
                                f"{b['title']}：{b['snippet'][:60]}" for b in briefs))
                            logger.info("[fish] 群 %s 联网检索「%s」命中 %d 条", gid, q, len(briefs))
                        else:
                            # 检索挂了也要说话，但别让模型编「我没有联网能力」这种谎
                            media_desc += "\n（这次没查到结果，照实说没搜到，可以说回头再帮着看一眼）"
                            logger.warning("[fish] 群 %s 联网检索「%s」无结果", gid, q)
                elif re.search(r"新番|有什么番|番剧推荐", text):
                    vids = await connectors.fetch_bilibili_hot_videos(await self._http(), limit=3)
                    if vids:
                        media_desc += "\n【B站热门】" + "；".join(v.get("title", "") for v in vids)
            except Exception as e:
                logger.warning("[fish] 联网扩展失败：%s", e)

        # 判官（social_read）每句本来就在跑，顺手让它吐检索词：碰到不认识的梗、
        # 或者要具体数字才答得上，它比主模型有余力停下来想一句。
        # 只在被@/被追问时才有判官，所以闲聊不会因此多挂一秒外网
        _jq = str((verdict or {}).get("search") or "").strip()
        if (_jq and not _searched and text and self.cfg.get("connectors_enabled", True)
                and not _SEARCH_MARK_RE.search(text)):
            try:
                _searched = True
                briefs = await connectors.search_web_brief(await self._http(), _jq, limit=3)
                if briefs:
                    media_desc += ("\n【刚查到的东西（这就是实时结果，直接拿来回答，"
                                   "别再说自己查不了网）】：" + "；".join(
                        f"{b['title']}：{b['snippet'][:60]}" for b in briefs))
                    logger.info("[fish] 群 %s 判官要查「%s」命中 %d 条", gid, _jq, len(briefs))
                else:
                    logger.info("[fish] 群 %s 判官要查「%s」，没查到", gid, _jq)
            except Exception as e:  # noqa: BLE001
                logger.debug("[fish] 判官检索失败：%s", e)


        cross_hint = self.cross.hint(gid, int(self.cfg.get("cross_group_recall", 2) or 2)) \
            if self.cfg.get("cross_group_enabled", True) else ""
        # 纯图片消息的 text 是空串。把一个「用户什么都没说」的空位喂给模型，
        # 它会自己脑补一句（真实翻车：回了「？光戳不说话」）。用图片描述顶上位
        prompt_text = text
        if text and voice_text:
            # 同一条消息既打字又发语音：两样都给它，别丢掉一半
            prompt_text = f"{text}（同一句还发了语音，说的是：{voice_text[:100]}）"
        if not prompt_text.strip():
            if voice_text:
                prompt_text = f"（发了段语音，说的是：{voice_text[:120]}）"
            else:
                prompt_text = f"（发了张图，图里是：{vision_desc[:80]}）" if vision_desc else ""
            if quote_ctx:
                # 只有转发/引用、没打字的：拿内容顶上位，别当空消息丢掉
                prompt_text = quote_ctx
            if not prompt_text:
                logger.info("[fish] 群 %s 这条媒体读不出内容（图/语音都没转出来），不调主模型", gid)
                self.log_event("skip", gid, "[媒体]", who=nick, why="媒体读不出内容")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)
                if is_at:
                    # 被 @ 却不回，必须拦下来，否则框架默认 LLM 会用另一副口气回一句
                    event.stop_event()
                return
        # 有正文也有引用：两样都给模型，不然「你看看这个」接不上引用的是啥
        if quote_ctx and prompt_text and quote_ctx not in prompt_text:
            prompt_text = f"{prompt_text}\n{quote_ctx}"
        # 空上下文：没上文、或只有一条且跟这句接不上时，模型只能靠编——
        # 常见的是把「你们刚才说的那个」当真事接着圆。被叫就先问一句短的让人补，
        # 没人叫就直接不开口。这一条在 decide() 之后，只决定「调不调模型」。
        if not self._ctx_of(st, event.get_extra("fish_ctxline") or text):
            _thin = self._thin_context(st, prompt_text or text)
            if _thin:
                _ask = bool(self.cfg.get("empty_context_ask_short", True)) and is_at
                logger.info("[fish] 群 %s 上下文太薄（%s），不调主模型%s", gid, _thin,
                            "，先问一句" if _ask else "")
                self.log_event("skip", gid, (text or "")[:40], who=nick,
                               why=f"上下文太薄·{_thin}")
                await self._remember(gid, uid, text)
                self._release_claim(st, _claim_tok)
                if _ask:
                    yield event.plain_result(random.choice(prompts.THIN_CONTEXT_ASKS))
                elif is_at:
                    event.stop_event()
                return
        try:
            system_prompt, user_prompt, _ctx_llm = await self._compose(
                gid, uid, nick, prompt_text, is_at, st, self_id=self_id,
                ctx_line=event.get_extra("fish_ctxline") or "",
                vision_desc=vision_desc,
                media_hint=media_desc,
                social_hint=social_hint, cross_hint=cross_hint,
                quarrel_hint=quarrel_hint,
            )
        except Exception:
            # 组 prompt 就炸了：这条发不出去，冷却位必须还回去。
            # 必须带 token：并发时另一条已经拿到新的占位，无 token 释放会把人家的抹掉
            self._release_claim(st, _claim_tok)
            raise
        await self._remember(gid, uid, text)
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick, "text": text, "at": is_at,
            # 决定回的原因一并带走：控制台要显示「这句为什么接」
            "reason": reason,
            # 追问窗口的次数和状态键在真正发出去时才结算（见 on_decorate）
            "sk": gid, "follow": is_follow,
            # 判官给的角度：出口要比一比是不是被逐字念出去了
            "how": how_used,
            # 图里的内容不在 msgs 里，离题闸要靠它跟上下文对上（见 _decorate_impl 2.5 段）
            "vision": vision_desc,
            "claim_tok": _claim_tok,
        })
        _ru = getattr(self, "_replied_uids", {}).setdefault(gid, [])
        _ru.append((now, uid))
        del _ru[:-20]
        self._replied_uids = {**getattr(self, "_replied_uids", {}), gid: _ru}
        # 主动接话要记账：90 秒后没人理它就算一次冷场（见下面的退避闸）
        if str(reason).startswith("主动接话"):
            st.pending_interject_ts = now
        logger.info("[fish] 群 %s %s(%s) 决定回（%s）：%s", gid, nick, uid, reason, text[:40])
        # 这里不能 stop_event：它有 STOP 结果，连我们自己刚 yield 的 request_llm 一起拦掉，
        # 实测 ab! 消息一条回复都发不出去。防双回靠「框架只在无 request_llm 时才走默认 LLM」
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt,
            system_prompt=system_prompt,
            contexts=_ctx_llm,
            image_urls=[],
        )

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    @_handler_guard
    async def on_private(self, event: AstrMessageEvent):
        if not self.cfg.get("enabled", True) or not self.cfg.get("reply_private", True):
            # 关私聊要真关：只 return 不 stop，AstrBot 自己的 LLM 链路照样会回
            event.stop_event()
            return
        text = (event.message_str or "").strip()
        if _is_builtin_cmd(text):
            return
        uid = str(event.get_sender_id())
        if uid == str(event.get_self_id()):
            return
        if not self._private_allowed(uid):
            # 私聊白名单：陌生人私聊一律不理，也不吭声
            logger.info("[fish] 私聊来自白名单外 %s，忽略", uid)
            event.stop_event()
            return
        # 戳一戳消息通常没有 message_id，走「p:」去重会互相顶掉——
        # 前一次空 id 把这次记成重复，戳就永远不回。这里对 Poke 直接跳过去重
        _is_poke_msg = any(isinstance(c, Poke) for c in (getattr(event.message_obj, "message", []) or []))
        if not _is_poke_msg and self._seen_mid("p:%s" % (getattr(event.message_obj, "message_id", "") or "")):
            logger.info("[fish] 私聊收到重复投递，忽略")
            event.stop_event()
            return
        nick = event.get_sender_name() or uid
        self_id = str(event.get_self_id())
        # 状态必须按人分：所有人共用一份 GroupState 的话，A 的对话会进 B 的 prompt
        # （隐私），B 在 A 说完 3 秒内发消息还会被冷却闸整条吞掉
        sk = f"private:{uid}"
        st = self._state(sk)
        now = time.time()
        # 群聊里有注入防护和机器人识别，私聊原来一条都没有——谁私聊都能套人设
        if text:
            injected, snark = safety_guard.check_prompt_injection(text)
            if injected:
                logger.info("[fish] 私聊判定注入 <- %s：%s", uid, text[:40])
                self.log_event("inject", "private", text[:60], who=uid)
                yield event.plain_result(snark or "这句我不接。")
                event.stop_event()
                return
        if peer_bots.is_peer_bot(uid, nick, self._peer_set(),
                                 self.cfg.get("peer_bots_name_hints") or ()):
            logger.info("[fish] 私聊忽略机器人消息：%s(%s)", nick, uid)
            return
        # 私聊里的戳一戳会被 AstrBot 当成「没有正文的私聊消息」派到这里，
        # on_poke 根本轮不上（实测 4 下戳全落在空消息分支）。所以在这里自己认 Poke：
        # 回戳必须照给，人话 25 秒内只说一句
        # 两个来源都要看：get_messages() 有时把 Poke 段吞了（转成空文本），
        # message_obj.message 还留着原始组件。只看一个会漏判，表现就是「戳了不回」
        _seg_lists = [list(event.get_messages() or []),
                      list(getattr(event.message_obj, "message", []) or [])]
        poked = any(
            isinstance(c, Poke) or "poke" in str(getattr(c, "type", "")).lower()
            for segs in _seg_lists for c in segs
        )
        if poked:
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), "private", uid, self_id)
            if now - st.last_bot_reply_ts < 25:
                logger.info("[fish] 私聊被戳 <- %s，%.0fs 内刚说过话，只回戳",
                            nick, now - st.last_bot_reply_ts)
                event.stop_event()
                return
            st.last_bot_reply_ts = now
            # 私聊的戳同样走模型：本地短句只有 5 句，戳几次就开始复读
            _pi = poke_context.decide(st.msgs, uid, st.last_bot_reply_ts, now)
            _sysp = composer.build_system_prompt(
                prompts.SYSTEM_PROMPT,
                bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"),
                bot_qq=self_id, group_id="private", is_private=True,
            )
            _userp = poke_context.build_prompt(
                _pi, f"戳你的是「{nick}」", repeat=1, last_said=list(st.recent_bot)[-2:])
            logger.info("[fish] 私聊被戳 <- %s，走模型", nick)
            self._spawn_poke_watch(event, "private", uid, nick, st, now)
            yield event.request_llm(prompt=_userp, system_prompt=_sysp,
                                    contexts=self._ctx_of(st, ""), tool_set=NO_TOOLS)
            return

        # 私聊里空消息/纯 @/纯表情：插件不接，框架自己会回「？」「咋了」，一句一句跟着刷。
        # 12 分钟内回过就彻底闭嘴，否则只给一句「说事」，然后把事件停掉
        if not text:
            if now - st.last_bot_reply_ts < 720:
                logger.info("[fish] 私聊 <- %s 空消息，%.0fs 内已应过，静默", nick, now - st.last_bot_reply_ts)
                event.stop_event()
                return
            st.last_bot_reply_ts = now
            event.stop_event()
            yield event.plain_result(random.choice(["说事", "在，说", "嗯？"]))
            return
        # 私聊是一对一，但也不是一句一回：连着发的先等他说完
        if now - st.last_bot_reply_ts < 3:
            logger.info("[fish] 私聊 <- %s %.1fs 前刚回，这轮先不说", nick, now - st.last_bot_reply_ts)
            event.stop_event()
            return
        st.last_bot_reply_ts = now
        st.dialogue.append({"who": nick, "text": text, "ts": now, "me": False, "uid": uid})
        # 私聊同样压进「只增不改」的上文：不然它每轮都要把整段对话重看一遍
        self._push_ctx(st, "user", f"{nick}：{text}")
        affect.on_group_message("private", text, private=True)
        try:
            system_prompt, user_prompt, _ctx_llm = await self._compose(
                "private", uid, nick, text, True, st,
                is_private=True, self_id=self_id,
                ctx_line=event.get_extra("fish_ctxline") or "")
        except Exception:
            # 组 prompt 失败：这条发不出去，别把冷却位留在现在（否则白静默三秒起）
            st.last_bot_reply_ts = 0.0
            raise
        event.set_extra("fish_pending", {"gid": "private", "uid": uid, "nick": nick,
                                         "text": text, "at": True, "sk": sk})
        yield event.request_llm(
            tool_set=NO_TOOLS,
            prompt=user_prompt, system_prompt=system_prompt, contexts=_ctx_llm, image_urls=[]
        )

    async def _distill_atom(self, text: str) -> str:
        """让模型判一句：这是不是这个人的长期信息，是就压成一条印象。

        规则抽取只能看「句子里有没有『我喜欢』」，分不出玩笑、转述和半句，
        档案里于是躺着「我叫XX过去帮你弄干净吧」。一次小调用换干净。
        判不出来就当没有——宁可少记一条，也不往档案里塞垃圾。
        """
        try:
            raw = await self._llm_once(memory_atoms.distill_prompt(text),
                                       memory_atoms.DISTILL_SYSTEM,
                                       float(self.cfg.get("atoms_distill_timeout_sec", 6) or 6))
        except Exception as e:
            logger.debug("[fish] 印象提炼失败（%s），这条不记", e)
            return ""
        return memory_atoms.clean_distilled(raw)

    def _msg_key(self, gid: str, event) -> str:
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        return f"{gid}:{mid}" if mid else ""

    async def _image_desc(self, gid: str, event) -> str:
        """这条消息里的图，识别一次就够了。

        时间线回填和「要不要回这条」会各要一次：不共享任务的话每次多烧一次
        视觉调用，而视觉本来就慢（12 秒超时），重复调更容易撞 429。
        """
        key = self._msg_key(gid, event)
        task = self._vision_jobs.get(key) if key else None
        if task is None:
            try:
                imgs = [c for c in event.message_obj.message if isinstance(c, Image)]
            except Exception:  # noqa: BLE001
                return ""
            if not imgs:
                return ""
            task = asyncio.create_task(vision.from_components(imgs, self.cfg, logger=logger))
            if key:
                self._vision_jobs[key] = task
                # 别让字典无界长：消息 id 只增不减
                if len(self._vision_jobs) > 64:
                    for _k in list(self._vision_jobs)[:32]:
                        self._vision_jobs.pop(_k, None)
        try:
            return await task or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("[fish] 识图失败：%s", e)
            return ""

    async def _backfill_image_desc(self, gid: str, event, msg_entry: dict, dlg_entry: dict) -> None:
        """识图完把描述写回时间线那两条记录，后续消息就能看见图里是什么。"""
        try:
            desc = (await self._image_desc(gid, event) or "").strip()
            if not desc:
                return
            shown = f"[图片：{desc[:80]}]"
            msg_entry["text"] = shown
            dlg_entry["text"] = shown
            logger.info("[fish] 群 %s 图片回填时间线：%s", gid, desc[:60])
        except Exception as e:  # noqa: BLE001
            logger.debug("[fish] 图片回填失败：%s", e)

    async def _answer_with_research(self, event: AstrMessageEvent, gid: str,
                                    uid: str, nick: str, question: str,
                                    st=None, user_text: str = "") -> None:
        """查完再答。这条路不发 request_llm，自己 send，所以不经过拆条。

        为什么要先发一句：检索实测 20~40 秒，群里静默这么久像是挂了。
        为什么答案整条发：这类答案的价值全在「第几点 + 出处链接」，切开
        就没法看了——链接断在两条消息中间，谁也点不着。
        """
        from .fish_core import research, safety_guard

        announce = random.choice(("我去查下", "等我看看", "查一下哈", "我搜搜"))
        try:
            await event.send(MessageChain(chain=[Plain(announce)]))
        except Exception as e:
            logger.warning("[fish] 检索预告发不出去：%s", e)
        prov = None
        try:
            prov = await self.context.get_using_provider_async()
        except Exception as e:
            logger.warning("[fish] 拿不到 provider：%s", e)
        text = ""
        res = {}
        if prov is not None:
            try:
                res = await research.run(
                    question, provider=prov, http=await self._http(),
                    max_rounds=int(self.cfg.get("research_max_rounds", 4) or 4),
                    budget_sec=float(self.cfg.get("research_budget_sec", 150) or 150),
                    proxy=(self.cfg.get("search_proxy") or None),
                )
                text = (res.get("text") or "").strip()
                _u = res.get("usage") or {}
                if _u.get("input"):
                    # 联网那条路的输入是聊天的好几倍，单独一类记账，
                    # 混进「单条聊天用量」会把均值拉得没法看
                    self.memory.log_token("search", _u.get("input") or 0,
                                          _u.get("hit") or 0, _u.get("output") or 0,
                                          _u.get("ms") or 0)
            except Exception as e:
                logger.warning("[fish] 检索失败：%s", e)
        # 出口闸照过：这条路是唯一「长文本 + 整段抄网页原文」的出口，
        # 以前只做了 sanitize_outgoing_text，于是 PASS、内网地址、机器路径
        # 都能原样跟着答案进群——偏偏这条路的原文最脏
        text = _SEARCH_LEFTOVER_RE.sub("", text or "").strip()
        text = _PIXIV_LEFTOVER_RE.sub("", text).strip()
        if text and safety_guard.is_pass_signal(text):
            text = ""
        ok, text = safety_guard.sanitize_outgoing_text(text)
        if not ok:
            text = ""
        leak = safety_guard.leaks_private(text) if text else ""
        if leak:
            # 抄到网页里的内网地址/机器路径：整段不发，也不拿它去猜
            logger.info("[fish] 检索答案命中隐私词（%s），整段丢弃", leak)
            text = ""
        if text:
            try:
                text = quarrel.soften(text)
            except Exception:  # noqa: BLE001
                pass
        if not text:
            text = "查了一圈没查到能确认的说法，别照网上的教程乱刷，容易变砖"
        sent = False
        try:
            await self._send_long(event, gid, text)
            sent = True
            logger.info("[fish] 群 %s 检索回答已发（%s，%d 字，%s 轮，%s ms，来源 %d 个）",
                        gid, nick, len(text), res.get("rounds"), res.get("ms"),
                        len(res.get("sources") or []))
        except Exception as e:
            logger.warning("[fish] 检索回答发不出去：%s", e)
        # 记账：不发这一步，这一轮在状态机眼里等于没发生——冷却位没动、
        # reply_times 没加，模型下一轮的 ctx 里也没有自己刚答过的那段
        if sent and st is not None:
            try:
                self._account_sent({"gid": gid, "uid": str(uid), "now": time.time(),
                                    "final": text, "reason": "检索回答",
                                    "follow": False})
            except Exception as e:  # noqa: BLE001
                logger.warning("[fish] 检索记账失败：%s", e)
        # 记忆里记的必须是用户原话：以前传的是检索关键词，
        # 于是「这卡多少瓦」在记忆里变成了一条「用户说：RTX 5090 显卡 功耗」
        await self._remember(gid, uid, user_text or question)

    async def _send_long(self, event: AstrMessageEvent, gid: str, text: str) -> None:
        """答案统一走合并聊天记录，和 /memory 一个形态。

        为什么不按字数分流：检索答案几乎次次上千字，短的那几次反而是残缺的；
        统一成折叠卡片，群里不占屏，点开才展开，观感跟真人转发聊天记录一致。
        节点里的键必须是 user_id/nickname——用 uin/name 实测 QQ 端不认。
        """
        bot = getattr(event, "bot", None)
        bot_qq = str(event.get_self_id() or "")
        # 留空就跟群昵称一致：以前硬编码「小鲸鱼」，改了 bot_nickname 卡片还是老名字
        nick = str(self.cfg.get("self_nickname")
                   or self.cfg.get("bot_nickname") or "小鲸鱼")
        chunks = self._split_for_forward(text)
        nodes = [{"type": "node", "data": {
            "user_id": bot_qq, "nickname": nick,
            "content": [{"type": "text", "data": {"text": c}}]}} for c in chunks]
        is_group = bool(gid)
        action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
        target = {"group_id": int(gid)} if is_group else {"user_id": int(event.get_sender_id())}
        started = time.time()
        if bot is not None:
            try:
                # 必须显式带 self_id：本机同时接着 qqbot/微信/飞书几个适配器，
                # aiocqhttp 有多个连接时靠它选通道，不带就抛 ApiNotAvailable
                extra = {"self_id": int(bot_qq)} if bot_qq.isdigit() else {}
                res = await asyncio.wait_for(
                    bot.call_action(action, messages=nodes, **target, **extra), timeout=20.0)
                if isinstance(res, dict) and res.get("status") == "failed":
                    logger.warning("[fish] 合并转发被拒：%s", str(res)[:150])
                else:
                    logger.info("[fish] 合并转发 %d 节点（%.1fs）", len(nodes), time.time() - started)
                    return
            except asyncio.TimeoutError:
                # 协议端常常消息已经发出去、结果迟迟不回；超时先回头看历史，
                # 免得又补一坨长文本变成双份
                if is_group and await self._forward_landed(bot, gid, started):
                    logger.info("[fish] 合并转发回得慢但已落地，不再补发")
                    return
                logger.warning("[fish] 合并转发超时且查不到历史")
            except Exception as e:
                logger.warning("[fish] 合并转发异常：%s: %s", type(e).__name__, e)
        for c in chunks:      # 兜底：逐条发，别把答案丢了
            await event.send(MessageChain(chain=[Plain(c)]))
            await asyncio.sleep(0.8)

    async def _forward_landed(self, bot, gid: str, since: float) -> bool:
        """超时兜底：查最近几条群消息里有没有自己刚发的转发卡片。"""
        try:
            res = await asyncio.wait_for(
                bot.call_action("get_group_msg_history", group_id=int(gid), count=6), timeout=6.0)
        except Exception:
            return False
        for msg in reversed((res or {}).get("messages") or []):
            if float(msg.get("time") or 0) < since - 10:
                continue
            for seg in msg.get("message") or []:
                if isinstance(seg, dict) and seg.get("type") in ("json", "forward", "node"):
                    return True
        return False

    @staticmethod
    def _split_for_forward(text: str, limit: int = 420) -> list[str]:
        """按段落切，段落太长再按句子切，保证每条不超过 limit。"""
        parts = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        out, buf = [], ""
        for p in parts:
            while len(p) > limit:
                cut = p.rfind("。", 0, limit)
                cut = cut + 1 if cut > limit // 3 else limit
                if buf:
                    out.append(buf); buf = ""
                out.append(p[:cut]); p = p[cut:]
            if len(buf) + len(p) + 1 <= limit:
                buf = f"{buf}\n{p}" if buf else p
            else:
                out.append(buf); buf = p
        if buf:
            out.append(buf)
        return out[:12]     # 再多就不是回答了，是抄书

    async def _remember(self, gid: str, uid: str, text: str) -> None:
        """自动感知：把群消息喂给记忆引擎（规则抽取事实/偏好/计划/事件）。"""
        if not text:
            return
        try:
            # log=False：抽取记忆但先不写流水，等回复出来再补，避免一条消息两行流水
            await asyncio.to_thread(self.memory.record_turn, gid, uid, text, "", False)
            # 命中的是偏好/事实，才值得单独压成一条印象原子；
            # 事件和计划塞进原子层只会把「同一个人」的档案搅浑
            kinds = set(fish_memory.FishMemoryEngine.detect_types(text))
            if kinds & {"preference", "fact"} and gid:
                # 同一人 60 秒内只提炼一次：连着刷五条消息不该烧五次小调用
                if time.time() - self._distill_ts.get(uid, 0.0) >= 60:
                    self._distill_ts[uid] = time.time()
                    atom = await self._distill_atom(text)
                    if atom:
                        keep = self._atoms_of(gid, uid)
                        self._atoms_all[f"{gid}:{uid}"] = memory_atoms.add_note(keep, atom)
                        self._save_atoms()
                        logger.info("[fish] 群 %s 攒印象 %r ← %r", gid, atom, text[:30])
        except Exception as e:
            logger.warning("[fish] 记忆自动感知失败: %s", e)

    def _addressed_to_me(self, text: str) -> bool:
        """没 @ 但明显在叫它：只有外号才算，别拿单个「鱼」字乱认。"""
        t = text or ""
        return any(k in t for k in ("小鲸鱼", "鲸宝", "大肥鱼", "肥鱼", "傻鱼", "鲸娘", "鱼娘", "笨鱼"))

    def _sd_shadow(self, gid, st, sig_kw, old_ok, old_reason):
        """开口判定对象（speak_decision）的影子比对：只记录，不改行为。

        灰度期靠这条日志判断「新判定会不会把该说的吞掉、把不该说的放出来」，
        对比完再切 enforce。任何异常都吞掉——影子日志绝不能影响线上回话。
        """
        if not self.cfg.get("speak_decision_shadow", False):
            return
        gs = self.cfg.get("speak_decision_shadow_groups") or []
        if gs and str(gid) not in [str(x) for x in gs]:
            return
        import time as _t
        from .fish_core import speak_decision as _sd
        kw = dict(sig_kw)
        kw.setdefault("now", _t.time())
        kw.setdefault("group_id", str(gid))
        kw["interject_times"] = tuple(getattr(st, "interject_times", ()) or ())
        kw["pending_interject_ts"] = float(getattr(st, "pending_interject_ts", 0.0) or 0.0)
        kw["unanswered_interjects"] = int(getattr(st, "unanswered_interjects", 0) or 0)
        kw["silent_until"] = float(getattr(st, "silent_until", 0.0) or 0.0)
        kw["last_bot_reply_ts"] = float(getattr(st, "last_bot_reply_ts", 0.0) or 0.0)
        d = _sd.decide(_sd.Signals(**kw), self.cfg)
        if old_ok is None:
            logger.info("[fish][sd] 群 %s 新判定 %s", gid, d.log_line())
        else:
            logger.info("[fish][sd] 群 %s 旧=%s/%s 新=%s %s", gid, old_ok, old_reason,
                        d.log_line(), "一致" if bool(old_ok) == bool(d.speak) else "不一致")

    def _should_interject(self, gid, st, text, now, has_image, has_media, uid: str = "") -> tuple[bool, str]:
        """旁观态接话闸门：质量分 × 时机修正 vs 意愿门槛。"""
        # adjust() 的「刚说过就 ×0.55」读的是模块里的 COOLDOWN_SEC，不是参数，
        # 不把配置同步进去，会出现「硬冷却放行了、软权重还在按 300 秒打折」的矛盾
        interject_planner.COOLDOWN_SEC = float(self.cfg.get("interject_cooldown_sec", 120))
        _cd = float(self.cfg.get("interject_cooldown_sec", 120))
        _el = now - st.last_bot_reply_ts
        _gap = float(self.cfg.get("chat_min_gap_sec", 2) or 0)
        _owner = str(self.cfg.get("owner_qq") or "")
        _aimed = self._addressed_to_me(text) or (bool(_owner) and str(uid) == _owner)
        _follow = bool(uid) and str(getattr(st, "last_reply_uid", "")) == str(uid)
        # 冷却按「人」算，不按群算。
        # 23:17 那次：我刚回完 lifefuck，十一秒后「我是神人」接了一句，
        # 被整群共用的 22 秒冷却掐掉——同一个群里换个人说话也算「刚说过话」，
        # 在多人聊天里就是集体装聋。现在只对「这个人自己刚被回过」生效。
        _same = now - float((getattr(st, "reply_uid_ts", {}) or {}).get(str(uid), 0.0))
        if _el < _gap or (_same < _cd and not (_aimed or _follow)):
            logger.info("[fish] 群 %s 冷却拦住：%s 距上次回话 %.1fs / 距他被回 %.0fs（gap=%s 叫他=%s 接着聊=%s）",
                        gid, str(uid)[-4:], _el, _same, _gap, _aimed, _follow)
            return False, "刚说过话，冷却中"

        cap = int(self.cfg.get("interject_hourly_cap", 8))
        recent = [t for t in st.interject_times if now - t < 3600]
        if len(recent) >= cap:
            return False, f"近一小时已插话 {len(recent)} 次"

        # 复读机不接：同一个人连着发同一句，接一次就够，接三次就是跟人一起刷屏
        tail = list(st.msgs)
        if len(tail) >= 2 and text:
            uid_now = tail[-1]["uid"]
            prev = [m["text"] for m in tail[-3:-1] if m["uid"] == uid_now]
            if prev and all(p == text for p in prev):
                return False, "同一句复读"

        score, why = interject_score.score_message_quality(
            text,
            has_image=has_image,
            has_media=has_media,
            recent_msgs=st.msgs_since_bot,
            now=now,
            last_msg_ts=st.last_msg_ts if st.msgs_since_bot > 1 else 0.0,
            last_bot_reply_ts=st.last_bot_reply_ts,
        )
        score, timing = interject_planner.adjust(
            score,
            now=now,
            last_bot_reply=st.last_bot_reply_ts,
            msgs_since_bot=st.msgs_since_bot,
            last_reply_answered=affect.is_last_reply_answered(gid),
        )
        # 群级 override 优先于全局意愿：一个群吵、一个群安静，只能分别调
        ov_desire = self._group_overrides.get(str(gid), {}).get("interject_desire")
        desire = float(self.cfg.get("interject_desire", 0.30) if ov_desire is None else ov_desire)
        threshold = interject_score.interject_threshold(desire)
        passed = score >= threshold
        # 影子比对要的就是这个分：判定对象按「没分就不主动插话」处理，
        # 少这一行会把所有主动插话都记成「没质量分」，灰度对比全是噪声
        self._last_quality = float(score)
        # 注意不在这里记 quota：判定通过不代表真发得出去（social_read 可能改判、
        # 生成可能失败）。真正的计数在 on_decorate 发出去那一刻
        logger.info(
            "[fish] 群 %s 接话质量 %.2f vs 门槛 %.2f（%s | 时机 %s）-> %s",
            gid, score, threshold, why, timing or "无修正", "接" if passed else "略过",
        )
        return passed, f"主动接话 {score:.2f}/{threshold:.2f}"

    # ---------------- 戳一戳 ----------------
    @filter.event_message_type(EventMessageType.ALL)
    @_handler_guard
    async def on_poke(self, event: AstrMessageEvent):
        """戳一戳必须自己接，不能交给原生流程。

        原生路径会把「被戳」当成一条普通消息丢给模型，模型不知道自己在被戳，
        回出来就是「有什么需要帮忙的」这种客服腔。这里自己拼 prompt，直接按
        「手被人戳了一下」的反应回一句。
        """
        if not self.cfg.get("enabled", True):
            return
        segs = list(getattr(event.message_obj, "message", []) or [])
        poked = [s for s in segs if isinstance(s, Poke)]
        if not poked:
            return
        # 别人互戳跟我没关系，只认戳到我自己身上的。
        # 目标 id 得走 target_id()：NapCat 有时候把目标放在旧字段 qq 上，
        # 有时候直接给 id=0，只看 .id 会漏掉一整类戳（私聊戳基本都这样）
        self_id = str(event.get_self_id())
        gid = str(event.get_group_id() or "private")

        if not any(_poke_aimed_at_me(s, self_id, gid) for s in poked):
            logger.info("[fish] 收到戳一戳段但目标不是我：%s", [repr(s) for s in poked])
            return
        if self._is_muted(gid):
            # 静默连戳都不理，但也不回戳——不然「全关」了手还在动
            return
        if gid != "private" and not self._group_allowed(gid):
            event.stop_event()
            return
        uid = str(event.get_sender_id())
        if gid == "private" and not self._private_allowed(uid):
            # 陌生号私聊戳一下也能触发一整轮模型调用，白名单外一律不理
            return

        nick = event.get_sender_name() or uid
        is_admin = self._sender_role(event) in ("admin", "owner")
        # 戳一戳在群里最常见的含义不是撒娇，是「你刚才没理我」。
        # 所以先看这人在我上次说话之后有没有发过还没被回的话，再决定这次是
        # 「补上那条没回的消息」还是「真的只是手欠」。
        now = time.time()
        st = self._state(gid)
        # 一分钟内的连戳算一串：第一下正常回，第二下简短打发，第三下起只回戳不说话。
        # 真人被连戳十几次也就是这个反应，而且再回下去只会越说越冲、越说越像坏掉的复读机
        if st.poke_uid != uid or now - st.poke_first_ts > 60:
            st.poke_uid, st.poke_count, st.poke_first_ts = uid, 1, now
        else:
            st.poke_count += 1
        poke_info = poke_context.decide(st.msgs, uid, st.last_bot_reply_ts, now)
        pending_text = poke_info["pending"]
        # 群里六个人轮流戳，每戳一次换一句「戳我干嘛」就是刷屏。25 秒内回过一次
        # 就只回戳不说话；真有没被回的留言（pending_text）照常补
        if not pending_text and now - st.last_poke_reply_ts < 25:
            logger.info("[fish] 戳一戳 <- %s(%s) 群 %s，%ds 内刚因戳说过话，只回戳",
                        nick, uid, gid, int(now - st.last_poke_reply_ts))
            favor.on_poke(gid, uid)
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
            event.stop_event()
            return
        if st.poke_count >= 3 and not pending_text:
            # 连着戳到第三下还没正事：戳回去就完事，别再刷一句「戳上瘾了是吧」
            logger.info("[fish] 戳一戳连击 %d 次 <- %s(%s) 群 %s，只回戳不说话",
                        st.poke_count, nick, uid, gid)
            self.log_event("poke", gid, f"{nick or uid} 连戳 {st.poke_count} 次", who=nick or uid)
            favor.on_poke(gid, uid)
            if self.cfg.get("poke_back", True):
                await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
            event.stop_event()
            return

        if is_admin:
            who_hint = "戳你的是你主人"
        else:
            who_hint = f"戳你的是群友「{nick}」"

        # 戳一戳直通加分，不进文本冷却——这是群友唯一不被模型误判的回血通道
        favor.on_poke(gid, uid)
        # 戳一戳等价于叫了一次人：顺手开追问窗口，否则他戳完想接着说还得再 @ 一次。
        # 同 @ 一样，窗口内反复戳只续时间不补次数，不然戳一下就又换 5 句
        st.last_poke_reply_ts = now
        if uid != st.follow_uid or now >= st.follow_until:
            st.follow_left = int(self.cfg.get("follow_max_turns", 2) or 0)
        st.follow_uid = uid
        st.follow_until = now + float(self.cfg.get("follow_window_sec", 300) or 0)
        await self._remember(gid, uid, f"（{nick} 戳了戳你）")
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"),
            bot_qq=self_id, group_id=gid, is_private=(gid == "private"),
        )
        # 拼 prompt 的逻辑放在 poke_context 里，两条分支才测得动
        user_prompt = poke_context.build_prompt(
            poke_info, who_hint,
            repeat=st.poke_count, last_said=list(st.recent_bot)[-2:],
        )
        # 记账同样要落到这次对话上：补回的那条用他的原话，纯手欠用戳的动作
        event.set_extra("fish_pending", {
            "gid": gid, "uid": uid, "nick": nick,
            "text": pending_text or f"（{nick} 戳了戳你）",
            "at": False, "reason": "戳一戳·补回未回" if pending_text else "戳一戳·手欠",
        })
        logger.info("[fish] 戳一戳 <- %s(%s) 群 %s 第 %d 下，走模型；待回消息：%s",
                    nick, uid, gid, st.poke_count, (pending_text[:30] or "无"))
        # 先回戳再回话：两件事互不等待，戳要秒到，话照旧慢慢说
        self.log_event("poke", gid, f"{nick or uid} 戳了一下", who=nick or uid)
        if self.cfg.get("poke_back", True):
            await _poke_back(getattr(event, "bot", None), gid, uid, self_id)
        # 戳一戳以前是「零记忆」地回一句，等于每次见面都重新认识；
        # 把同一段上文接上，它才知道刚才聊到哪
        # 模型是异步出的：request_llm 交出去之后这边就收不到「失败了」的回调。
        # 挂个看门狗，超时还没动静就用兜底短句补一句，别让一戳下去毫无反应。
        self._spawn_poke_watch(event, gid, uid, nick, st, now)
        yield event.request_llm(prompt=user_prompt, system_prompt=system_prompt,
                                contexts=self._ctx_of(st, ""), tool_set=NO_TOOLS)

    def _spawn_poke_watch(self, event, gid: str, uid: str, nick: str, st, now: float) -> None:
        """戳一戳走模型时的兜底看门狗。

        request_llm 是「把活交出去」，模型超时、报错、被出口闸拦掉时这边一个字都收不到。
        超时窗口内最近发言没变化就补一句本地短句，保证这一戳不是石沉大海。
        """
        timeout = float(self.cfg.get("poke_model_timeout_sec", 12) or 12)
        pool = self.cfg.get("poke_fallback_pool") or list(poke_context.FALLBACK_POOL)
        before_n = len(st.recent_bot)
        before_ts = float(getattr(st, "last_bot_reply_ts", 0) or 0)

        async def _watch():
            await asyncio.sleep(timeout)
            # 模型已经出话：正常，不用管
            if len(st.recent_bot) != before_n or float(getattr(st, "last_bot_reply_ts", 0) or 0) != before_ts:
                return
            said = [t for t in st.recent_bot if t]
            picked = _pick_poke(said, pool)
            try:
                bot = getattr(event, "bot", None)
                payload = ({"user_id": int(uid), "message": picked} if gid == "private"
                           else {"group_id": int(gid), "message": picked})
                action = "send_private_msg" if gid == "private" else "send_group_msg"
                await bot.api.call_action(action, **payload)
            except Exception as e:
                logger.warning("[fish] 戳一戳兜底发送失败 %s(%s)：%s %s", nick, uid, type(e).__name__, e)
                return
            st.last_poke_reply_ts = time.time()
            st.recent_bot.append(picked)
            logger.info("[fish] 戳一戳模型 %.0fs 没出话，兜底短句：%s", timeout, picked)
            self.log_event("poke_fallback", gid, picked, who=nick or uid)
            self._record_spoke(gid, uid, picked, reason="戳一戳·兜底")

        try:
            asyncio.get_running_loop().create_task(_watch())
        except Exception as e:
            logger.debug("[fish] 戳兜底看门狗没挂上：%s", e)

    # ---------------- prompt 拼装 ----------------
    # 开口就是「催什么」「急什么」这类问句：同一天说第四遍时群里一眼看出是复读机
    _OPENER_RE = re.compile(r"^(催什么|急什么|叫什么叫|戳什么|问什么|又怎么了)[，,。！!？?\s]*")

    def _strip_repeated_opener(self, st, text: str) -> str:
        """开场白最近八条里出现过两次以上，就把这个开场削掉，留后半句。"""
        m = self._OPENER_RE.match(text or "")
        if not m:
            return text
        word = m.group(1)
        used = sum(1 for t in list(st.recent_bot)[-8:] if word in (t or ""))
        if used < 2:
            return text
        rest = text[m.end():].lstrip("，,。 ")
        return rest or text

    # ---------------- 骂人动作层 ----------------
    def _anger_unlimited(self, gid: str) -> bool:
        """群级红温开关：anger_overrides.json 里列了这个群就放开次数上限。

        旧 bot 的 anger_overrides.json 同款。改文件即生效，不用重启——
        想现场试「骂到底能多凶」的时候，改这一行比改代码快。
        """
        p = DATA_DIR / "anger_overrides.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            self._anger_cache = (0.0, set())
            return False
        cache = getattr(self, "_anger_cache", (0.0, set()))
        if cache[0] != mtime:
            gs = set()
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                gs = {str(x).strip() for x in (data.get("groups") or []) if str(x).strip()}
            except Exception as e:
                logger.warning("[fish] anger_overrides.json 读不了，按正常阈值处理：%s", e)
            self._anger_cache = (mtime, gs)
        return str(gid) in self._anger_cache[1]

    async def _group_members(self, event, gid: str) -> list[dict]:
        """群成员表，缓存 10 分钟。拿不到就退回「最近说过话的人」，不能让骂人卡住。"""
        now = time.time()
        cached = getattr(self, "_members_cache", {})
        ts, rows = cached.get(gid, (0.0, []))
        if rows and now - ts < 600:
            return rows
        rows = []
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            data = await api.call_action("get_group_member_list", group_id=int(gid),
                                         **{"self_id": int(event.get_self_id())})
            for m in data or []:
                rows.append({"uid": str(m.get("user_id") or ""),
                             "nick": m.get("card") or m.get("nickname") or "",
                             "role": m.get("role") or "member"})
        except Exception as e:
            logger.warning("[fish] 取群成员表失败（%s），用最近发言人兜底", e)
        if rows:
            cached[gid] = (now, rows)
            self._members_cache = cached
        return rows

    async def _do_insult(self, event, gid: str, text: str, uid: str, st, act: dict) -> bool:
        """群友让它去骂人。返回 True 表示这条已经处理完，别再走正常回复。"""
        now = time.time()
        cd = float(self.cfg.get("insult_cooldown_sec", 60) or 60)
        last = getattr(self, "_insult_ts", {}).get(gid, 0.0)
        if now - last < cd:
            return False
        self_uid = str(event.get_self_id())
        owner = str(self.cfg.get("owner_qq") or "").strip()
        members = await self._group_members(event, gid)
        if not members:
            # 取不到成员表（接口超时/没权限）时别干瞪眼：用最近说过话的人兜底，
            # 否则群里一句「骂 xxx」只会在日志留一行「找不到靶子」，人看着就是犯傻
            members = [{"uid": str(x.get("uid") or ""), "nick": x.get("nick") or "",
                        "role": "member"} for x in list(st.msgs)[-12:]]

        if act.get("kind") == "target":
            who = act.get("who", "")
            if who.endswith(("他", "她", "它", "这货", "那货")):
                # 「教育教育他」这种，指的是上一句那个人，不是叫「他」的群友
                who = ""
            targets = insult_actions.find_by_name(members, who,
                                                  self_uid=self_uid, owner_uid=owner) if who else []
            if not targets and not who:
                _r = [{"uid": m.get("uid"), "nick": m.get("nick")}
                      for m in list(st.msgs)[-4:]][::-1]
                targets = insult_actions.pick_targets(members, _r, self_uid=self_uid,
                                                      owner_uid=owner, want=1)
            if not targets:
                # 以前这里回「你说的是谁啊，群里没这号人」，被旁人一句随口的
                # 「骂一句」就钓出来复读。现在宁可不说，也不当复读机
                # 以前这里 return True：指令没落地、事件被 stop，群里一个字都没有。
                # 用户发了明确指令却得到彻底沉默，只能理解成「没听见」。
                # 交回普通流程，让它自己说一句「群里没这人」
                logger.info("[fish] 群 %s 派活但找不到靶子（%r），交回普通回复", gid, act.get("who"))
                return False
        else:
            recent = [{"uid": m.get("uid"), "nick": m.get("nick")}
                      for m in list(st.msgs)[-12:]][::-1]
            targets = insult_actions.pick_targets(
                members, recent, self_uid=self_uid, owner_uid=owner,
                want=int(self.cfg.get("insult_max_targets", 3) or 3))
        if not targets:
            return False

        self._insult_ts = {**getattr(self, "_insult_ts", {}), gid: now}
        _said: list[str] = []
        for t in targets:
            ctx = " ".join(str(m.get("text") or "") for m in list(st.msgs)[-6:]
                           if str(m.get("uid")) == t["uid"])
            try:
                raw = await self._llm_once(insult_actions.gen_prompt(t["nick"], ctx),
                                           insult_actions._GEN_SYSTEM, 25.0)
            except Exception as e:
                logger.warning("[fish] 骂人短句生成失败：%s", e)
                raw = ""
            for line in insult_actions.clean_lines(raw, limit=2):
                try:
                    await event.send(MessageChain(chain=[At(qq=str(t["uid"])), Plain(" " + line)]))
                except Exception as e:
                    logger.warning("[fish] 骂人短句发不出去：%s", e)
                    break
                _said.append(f"{t['nick']}：{line}")
                await asyncio.sleep(0.8)
        logger.info("[fish] 群 %s 执行骂人（%s，靶子 %d 个）", gid, act.get("kind"), len(targets))
        if _said:
            # 骂完也要记账：不然下一轮它不知道自己刚骂过人，可能又好声好气接话
            self._record_spoke(gid, uid, "[骂人] " + " / ".join(_said), reason="骂人")
        return True

    def _push_ctx(self, st: "GroupState", role: str, content: str) -> None:
        """把一句话追加进「上文」。只追加，绝不重排。

        满了就一次砍掉四分之一：每轮都位移等于每轮都 miss，
        宁可隔十几轮 miss 一次，也不要每轮都 miss。
        """
        txt = " ".join(str(content or "").split())
        if not txt:
            return
        st.ctx.append({"role": role, "content": txt[:300]})
        limit = int(self.cfg.get("context_turns_max", 48) or 48)
        if len(st.ctx) > limit:
            del st.ctx[: max(1, limit // 4)]

    def _record_spoke(self, gid: str, uid: str, final: str, reason: str = "",
                      nick: str = "", follow: bool = False) -> None:
        """动作类回复（只发图/点歌/斗图/骂人）的记账口。

        这些路径都是自己 await event.send() 再 return，不走出口那段记账代码，
        结果下一轮模型不知道「我刚发过图/刚唱过歌」，冷却位和追问窗口也不动。
        发完直接调这里补上，跟正常出口记的是同一套账。
        """
        self._account_sent({"gid": gid, "sk": gid, "now": time.time(), "final": final,
                            "reason": reason, "nick": nick, "follow": follow,
                            "meme": 0, "parts": 1, "uid": str(uid), "text": final})

    def _last_human_line(self, st: "GroupState", exclude: str = "") -> str:
        """上文里最近一条真人发言（跳过自己说的），给「空壳指令」找检索词用。"""
        for m in reversed(list(st.dialogue)):
            if m.get("me"):
                continue
            t = str(m.get("text") or "").strip()
            if t and t != exclude and len(t) >= 4:
                return t
        return ""

    def _ctx_of(self, st: "GroupState", current_line: str = "") -> list:
        """取这一轮要带上去的上文。当前这句已经在 ingest 时压进去了，别再重复一遍。"""
        if not self.cfg.get("multi_turn_contexts", True):
            return []
        ctx = list(st.ctx)
        cur = " ".join(str(current_line or "").split())[-60:]
        if cur and ctx and ctx[-1].get("role") == "user" and cur in str(ctx[-1].get("content") or ""):
            ctx.pop()
        return ctx

    def _thin_context(self, st: "GroupState", text: str) -> str:
        """时间线为空、或只有一条且跟当前这句接不上时返回 'empty'/'thin'，否则 ''。

        只判「有没有可用的上文」，说不说仍由 decide() 和调用方定。
        """
        window_sec = float(self.cfg.get("context_window_sec", 1800) or 1800)
        now_ref = time.time()
        raw = [x for x in list(st.dialogue)[:-1]
               if now_ref - float(x.get("ts") or 0) <= window_sec]
        lines = [f"{x.get('who')}：{x.get('text')}" for x in raw]
        return composer.timeline_is_thin(lines, text)

    async def _compose(self, gid, uid, nick, text, is_at, st: GroupState, is_private: bool = False,
                       ctx_line: str = "",
                       self_id: str = "", vision_desc: str = "", media_hint: str = "",
                       social_hint: str = "",
                       cross_hint: str = "",
                       quarrel_hint: str = ""):
        affect.tick(gid)
        system_prompt = composer.build_system_prompt(
            prompts.SYSTEM_PROMPT, bot_nick=str(self.cfg.get("bot_nickname") or "小鲸鱼"), bot_qq=self_id,
            group_id=gid or "-", is_private=is_private,
        )

        # 记忆召回：按当前这句检索这个人在这个群的历史记忆
        memory_lines = []
        if gid and gid != "private":
            try:
                recalled = await asyncio.to_thread(
                    self.memory.recall, gid, uid, text,
                    int(self.cfg.get("memory_recall_topk", 2)))
                for item in recalled:
                    content = " ".join(str(item.get("content") or "").split())
                    # 刚说的那句也可能以近重形式被召回，别让模型看到自己刚说的话
                    if not content or content in text or text in content:
                        continue
                    # 记忆是按「一条 QQ 消息」存的，而人经常一句话拆成七八条发出去。
                    # 碎片原样塞进 prompt 会拼成一坨读不通的长句，模型照着这口气说话就散了：
                    # 太短的、断在逗号上的，一律不进 prompt
                    if len(content) < 10 or content[-1] in "，、,（(":
                        continue
                    if len(content) > 60:
                        cut = max(content.rfind("，", 0, 60), content.rfind("；", 0, 60),
                                  content.rfind("。", 0, 60))
                        content = content[:cut + 1] if cut >= 20 else content[:60] + "…"
                    # 同一段话被拆成好几条消息发出去时，库里会同时存「前缀碎片」和「长句」，
                    # 两条都塞进去 prompt 就是叠字。留长的，短的让位
                    dup = [m for m in memory_lines if content in m or m in content]
                    if dup:
                        for i, m in enumerate(memory_lines):
                            if m in content and len(m) < len(content):
                                memory_lines[i] = content
                        continue
                    # 只给一句话，模型分不清是「他刚说的」还是「上个月别人说的」，
                    # 于是会拿旧事当现状答。带上说话人和时间，它才知道该不该引用
                    _who = nick or "他"
                    _when = self._fmt_mem_when(item.get("created_at") or item.get("ts"))
                    memory_lines.append(f"{_who}（{_when}）说过：{content}" if _when
                                        else f"{_who}说过：{content}")
                    if len(memory_lines) >= 2:    # 给太多反而更乱，两条够了
                        break
            except Exception as e:
                logger.warning("[fish] 记忆召回失败: %s", e)

        # 心境：只给体感，不给数字（数字会被模型念出来）
        affect_parts = []
        memo = affect.prompt_memo(gid)
        if memo:
            affect_parts.append(memo)
        # 主人先算出来：好感分要跳过，主人骂两句不该把关系算凉
        _owner = str(self.cfg.get("owner_qq") or "").strip()
        _is_owner = bool(_owner) and str(uid) == _owner
        favor_memo = favor.prompt_memo(gid, uid) if (not is_private and not _is_owner) else ""
        # 有人直接问「我多少分」的时候才把数字给它：平时不报分数，
        # 免得每句话都带一句「我对你 87 分」，那比机器人还机器人
        if not is_private and _ASK_FAVOR_RE.search(text or ""):
            try:
                _cur = favor.score(gid, uid)
                favor_memo = ((favor_memo + "\n") if favor_memo else "") + (
                    f"【他刚在问自己在这儿多少分：现在 {_cur:.0f} 分（0~100）。"
                    "要问就直接报这个数，顺口带句你的态度；别解释怎么算的，别报别人的分】"
                )
            except Exception as e:
                logger.debug("[fish] 取好感分失败：%s", e)
        if favor_memo:
            affect_parts.append(favor_memo)
        # 主人频道：跟 owner 说话时的倾向排在好感分后面，压过任何「戒备/冷淡」。
        # 好感分算的是他最近对鱼怎么样，拿它对待做出的那个人，等于最熟的反倒最生分
        if _is_owner:
            affect_parts.append(
                "【说话的人】：life，是把它做出来的主人。跟他说话松、软、爱黏，"
                "小声带点谄媚也不害臊；被他骂不真记仇，嘴硬一句就顺回去，不用装客气也不用端着"
            )
        care = affect.care_hint(gid)
        if care:
            affect_parts.append(care)
        if not is_private:
            variant = affect.style_variant()
            if variant:
                affect_parts.append(f"【这次换个说法】：{variant}")

        # 印象原子：去过重、衰减之后留下的几句话，比整句记忆耐看，也不会重复提同一件事
        try:
            atoms_raw = self._atoms_of(gid, uid)
            if atoms_raw:
                atoms = memory_atoms.apply_decay(atoms_raw)
                atom_text, _used = memory_atoms.render(atoms)
                # 被注入 = 模型想起过一次，强度该回升。不写回的话，
                # 用得最多的印象也会照常衰减到被 trim 掉
                if _used:
                    memory_atoms.mark_used(atoms, _used)
                if atom_text:
                    memory_lines.append(atom_text[:260])
                if atoms != atoms_raw:
                    self._atoms_all[f"{gid}:{uid}"] = atoms
                    self._save_atoms()
        except Exception as e:
            logger.debug("[fish] 印象原子渲染失败：%s", e)

        # 关系网：谁最近爱跟谁搭话，只给一句体感，不给统计表
        try:
            if gid and gid.isdigit():
                rel = cross_interaction.get_group_relations_summary(int(gid))
                if rel:
                    memory_lines.append(rel[:120])
        except Exception as e:
            logger.debug("[fish] 关系网摘要失败：%s", e)

        # 有人问「X 是什么梗」：本地词库查不到就去梗百科，查到的只当资料、带来源
        ask_term = _meme_ask_term(text or "")
        if ask_term:
            try:
                from .fish_core import meme_wiki
                entry = await meme_wiki.lookup(ask_term)
                if entry:
                    logger.info("[fish] 查梗「%s」命中：%s", ask_term, entry.get("title"))
                else:
                    logger.info("[fish] 查梗「%s」没查到", ask_term)
                wiki_hint = meme_wiki.render(entry) if entry else ""
            except Exception as e:
                logger.warning("[fish] 查梗失败「%s」：%s", ask_term, e)
                wiki_hint = ""
        else:
            wiki_hint = ""

        # 热梗：常驻核心梗 + 这句沾边的梗，调味用，命中不了就空着
        meme_hint = ""
        try:
            # group_id 必传：本群黑话是逐群挖的，传错群等于把别人家的梗塞给它
            meme_hint = self.memes.get_prompt_snippet(text or "", group_id=gid)
        except Exception as e:
            logger.debug("[fish] 热梗取词失败：%s", e)

        # 时间线走 dialogue 而不是 msgs：模型得看见自己刚说过什么，
        # 否则它会把同一句换个说法又来一遍，或者接话接到自己头上。
        # 条数窗在热闹的群里只等于「最近三分钟」：刚聊透的「神话保底 2000」早就滚出去了，
        # 于是它过一会儿又问一遍「神话是啥」。改成以时间窗为主、条数为上限，
        # 冷清时能一直看到几十分钟前的话，刷屏时也不会把 prompt 撑爆
        window_sec = float(self.cfg.get("context_window_sec", 1800) or 1800)
        size = int(self.cfg.get("timeline_size", 40) or 40)
        raw = list(st.dialogue)[:-1]  # 最后一条是当前这句，单独摆在「当前这句」块里
        now_ref = time.time()
        window = [m for m in raw if now_ref - float(m.get("ts") or 0) <= window_sec]
        if not window:
            window = raw[-size:]   # 太久没人说话时别让模型眼前一片空白
        elif len(window) > size:
            window = window[-size:]
        timeline = []
        for m in window:
            # 我的话可能是拆条后的两段，直接塞进去会多出没署名的半行，
            # 模型会当成另一个人接的话
            _t = " ".join(str(m["text"]).split())
            timeline.append(f"{m['who']}：{_t}")

        _multi = bool(self.cfg.get("multi_turn_contexts", True))
        # 用 ingest 时压进去的那一行，别用出口改写后的 text（否则弹不掉本轮这句）
        _ctx = self._ctx_of(st, ctx_line or text) if _multi else []
        if _multi and not _ctx:
            # 还没攒下上文（刚重启/新群）：这一轮继续用整段时间线，别让它眼前一片空白
            _multi = False
        user_prompt = composer.build_user_prompt(
            timeline=timeline,
            include_timeline=not _multi,
            latest_line=text,
            sender_nick=nick,
            sender_uid=uid,
            memory_lines=memory_lines,
            affect_parts=affect_parts,
            style_hint=(composer.style_samples_hint(
                gid, int(self.cfg.get("prompt_budget_style_max", 3) or 3))
                if gid != "private" else ""),
            meme_hint=meme_hint,
            wiki_hint=wiki_hint,
            budget={
                "memory": int(self.cfg.get("prompt_budget_memory_max", 2) or 0),
                "style": int(self.cfg.get("prompt_budget_style_max", 3) or 0),
                "hint": int(self.cfg.get("prompt_budget_hint_max", 2) or 0),
            },
            anti_repeat_hint=composer.anti_repeat_hint(list(st.recent_bot)),
            games_kb=prompts.GAMES_KB if prompts.needs_games_kb(text) else "",
            is_at=is_at,
            is_private=is_private,
            vision_desc=vision_desc,
            media_hint=media_hint,
            social_hint=social_hint,
            cross_hint=cross_hint,
            quarrel_hint=quarrel_hint,
        )
        if self.cfg.get("debug_dump_prompt"):
            try:
                body = "=== system ===\n" + system_prompt + "\n\n=== user ===\n" + user_prompt
                (DATA_DIR / "last_prompt.txt").write_text(body, encoding="utf-8")
                # 每次留一份带时间戳的档：last_prompt 只留最后一次，回头看「心境/记忆到底进没进」
                # 就没证据了，尤其是要贴给别人的时候
                arch = DATA_DIR / "prompt_archive"
                arch.mkdir(exist_ok=True)
                (arch / f"{time.strftime('%Y%m%d-%H%M%S')}_{uid}.txt").write_text(body, encoding="utf-8")
                for old in sorted(arch.iterdir())[:-50]:
                    old.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("[fish] prompt 落盘失败: %s", e)
        logger.info("[fish] 上文 %d 条（%s），本轮 user 只 %d 字", len(_ctx),
                    "多轮·缓存友好" if _multi else "单轮·整段重拼", len(user_prompt))
        logger.debug("[fish] system_prompt=%r\nuser_prompt=%r", system_prompt[:80], user_prompt[:200])
        return system_prompt, user_prompt, _ctx

    # ---------------- 出口加工 ----------------
    @filter.on_decorating_result()
    async def on_decorate(self, event: AstrMessageEvent):
        # 这个钩子是「最后一道出口」。以前没有任何兜底：任一子模块抛异常，
        # 异常被框架吞掉但 result 原封不动留在原地，未清洗的原文（带 [PASS]、
        # 表情标签、超长、违禁词）就照原样发进群了——线上真发生过
        meta = event.get_extra("fish_pending")
        try:
            await self._decorate_impl(event)
        except Exception as e:
            logger.exception("[fish] 出口加工异常，走兜底清洗: %s", e)
            self._decorate_fallback(event, meta)

    def _decorate_fallback(self, event: AstrMessageEvent, meta) -> None:
        """出口异常时只做三件事：放回冷却位、PASS 检测、截断。宁可少说一句。"""
        try:
            if meta:
                # 带 token：兜底只还自己占的那一位，别把并发那条的占位一起抹掉
                self._release_claim(self._state(meta.get("sk") or meta["gid"]),
                                    meta.get("claim_tok"))
        except Exception:
            pass
        raw = ""
        try:
            res = event.get_result()
            if res and res.chain:
                raw = "".join(c.text for c in res.chain if isinstance(c, Plain))
        except Exception:
            raw = ""
        try:
            if not raw.strip() or safety_guard.is_pass_signal(raw):
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                return
            cut = (composer.sanitize_reply(raw) or "").strip()[:200]
            # 异常兜底最该拦隐私：这条路上没有后续任何一道闸，
            # 模型原文里的内网地址、机器路径、配置文件名会直接被发出去
            if cut and safety_guard.leaks_private(cut):
                logger.info("[fish] 兜底内容命中隐私词，丢弃")
                cut = ""
            event.set_result(MessageEventResult(chain=[Plain(cut)] if cut else []))
        except Exception:
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()

    @filter.on_llm_request()
    async def _mark_llm_t0(self, event: AstrMessageEvent, req):
        """记下请求发出的时刻：输出速度要用它算，框架不给我们时长。"""
        try:
            event.set_extra("fish_llm_t0", time.time())
        except Exception:
            pass

    @filter.on_llm_response()
    async def _log_cache(self, event: AstrMessageEvent, resp):
        """把每轮真实 token 和缓存命中记下来。

        网关面板不归我们管，猜它为什么 0% 只能靠这本账：
        命中为 0 且输入很长 → 前缀被谁改了；命中接近输入 → 缓存真在工作
        """
        inp, hit, outp = _usage_of(resp)
        if not inp:
            return
        try:
            pct = hit / inp * 100 if inp else 0
            logger.info("[fish] token 命中 %d / 输入 %d / 输出 %d（%s %.0f%%）",
                        hit, inp, outp, "前缀复用" if hit else "零命中", pct)
            # 落库给 /tocken 用：日志会被轮转，账本不会
            t0 = event.get_extra("fish_llm_t0")
            ms = int((time.time() - t0) * 1000) if t0 else 0
            self.memory.log_token("chat", inp, hit, outp, ms)
        except Exception:
            pass

    async def _decorate_impl(self, event: AstrMessageEvent):
        meta = event.get_extra("fish_pending")
        if not meta:
            # 不是本插件产生的回复。以前直接 return，结果戳一戳那条路上模型手写的
            # [表现:委屈] 原样进了群（16:46:59 真实事故）。这里只剥标签，不动正文
            try:
                res = event.get_result()
                if res and res.chain:
                    cleaned = []
                    for c in res.chain:
                        if isinstance(c, Plain):
                            # 只剥内部标签，别做整段清洗：兄弟插件的英文回复和 @ 要留着
                            t = _TAG_ONLY_RE.sub("", c.text or "").strip()
                            if t:
                                cleaned.append(Plain(t))
                        else:
                            cleaned.append(c)
                    if len(cleaned) != len(res.chain):
                        event.set_result(MessageEventResult(chain=cleaned))
            except Exception as e:
                logger.debug("[fish] 无 meta 出口兜底失败：%s", e)
            return  # 不是本插件产生的回复（例如 ab! 唤醒的 AstrBot 原生回复），一律不碰
        event.set_extra("fish_pending", None)

        # 状态键由 handler 带过来：私聊是 private:uid（按人分），群里就是群号
        gid = meta.get("sk") or meta["gid"]
        st = self._state(gid)
        # 本次占位的 token：只有它自己能回滚冷却位（poke 这类没 claim 的传 None，不会误伤别人）
        _tok = meta.get("claim_tok")

        result = event.get_result()
        if not result or not result.chain:
            # 模型什么都没产出：冷却位必须还回去，否则这条之后群里白静默五分钟
            logger.info("[fish] 群 %s 这轮没有产出（空链），释放冷却位", gid)
            self._release_claim(st, _tok)
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if not text.strip():
            logger.info("[fish] 群 %s 这轮只有非文本内容，释放冷却位", gid)
            self._release_claim(st, _tok)
            return

        # 0. 检索指针：模型自己判断这题得查，就吐 [搜索:关键词]。
        # 这轮它的原话作废（可能只有指针，也可能是「等我查查」），改走检索通道；
        # 指针本身绝不能进群，所以先抠掉再决定去留
        _sm = _SEARCH_MARK_RE.search(text)
        if _sm and not self.cfg.get("research_enabled", True):
            # 检索关了：指针当垃圾扔掉，回一句就得走人。
            # 以前只 set_result 不 return，后面「清洗后无正文」那一闸把
            # 这句又覆盖成空链，用户看到的是彻底沉默
            text = _SEARCH_MARK_RE.sub("", text).strip()
            event.set_result(MessageEventResult(
                chain=[Plain(text or "我查不了，联网关着呢")]))
            event.stop_event()
            self._release_claim(st, _tok)
            return
        if _sm:
            _q = (_sm.group(1) or "").strip() or (meta.get("text") or "")
            text = _SEARCH_MARK_RE.sub("", text).strip()
            logger.info("[fish] 模型要查「%s」，转检索通道", _q[:40])
            self._release_claim(st, _tok)
            # 先把 event 上挂的原文清掉再 await：检索途中一旦抛异常，
            # on_decorate 的兜底会把 event 里的原文（含 [搜索:...]）直接发进群
            event.set_result(MessageEventResult(chain=[]))
            try:
                await self._answer_with_research(
                    event, gid, meta.get("uid", ""), meta.get("nick", ""),
                    _q, st=st, user_text=str(meta.get("text") or ""))
            except Exception as e:  # noqa: BLE001
                logger.error("[fish] 检索通道异常：%s: %s", type(e).__name__, e)
            event.stop_event()
            return

        # 0.4 发图指针：[看图:角色名/标签] → pixiv 捞一张。主动触发专用，
        # 代码不猜「什么时候该发图」，只认模型写出来的标签。指针抠干净再往下走
        pixiv_tag = ""
        _pm = _PIXIV_MARK_RE.search(text)
        if _pm:
            pixiv_tag = (_pm.group(1) or "").strip()[:30]
            text = _PIXIV_MARK_RE.sub("", text).strip()
            logger.info("[fish] 模型要发图，标签「%s」", pixiv_tag)
            # 只写了指针没写话：给一句最短的兜底，否则下面「清洗后无正文」会整条吞掉
            text = text or "喏，这张"

        # 0. PASS 闸：这是模型「这轮我不接」的内部信号，一个字符都不能进群。
        # 以前清洗函数把它削成空串，但后果被 `if text2:` 丢掉、回退成原文，
        # 于是群里会看到一条孤零零的 [PASS]——真实发生过，只是概率低
        if safety_guard.is_pass_signal(text):
            logger.info("[fish] 群 %s 模型给出 PASS，这轮静默", gid)
            self.log_event("pass", gid, meta.get("text", "")[:40], who=meta.get("nick", ""))
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 1. 剥表情标签：模型经常写 [MEME:嘲讽]，不剥就原样发进群
        clean, tag = meme_catalog.extract_tag(text)
        # 1.1 剥违规上报标签：模型判完写 [REPORT:广告|理由]，这个标记一个字都不能进群
        clean, report_hit = _extract_report(clean)
        # 1.2 剥画图标签：模型写 [画图:喜报:今天没人迟到]，这句要变成一张真的图
        clean, draw_req = meme_gen.extract_tag(clean)
        if report_hit:
            label = "：".join([p for p in report_hit if p])
            if self._order_watch(gid):
                asyncio.create_task(self._report_order(
                    event, gid, meta.get("nick", ""), meta.get("uid", ""),
                    meta.get("text", ""), label,
                ))
            else:
                logger.info("[fish] 群 %s 判出违规（%s），但该群没开上报", gid, label)

        # 1.5 只发图：模型只回一个 [MEME:xxx] 的时候，这是「表情也是一句话」的正常用法。
        # 以前 clean 为空会被当成「清洗后没正文」直接静默，等于把表情包吞掉了
        if tag and not clean.strip() and self.cfg.get("meme_enabled", True):
            now0 = time.time()
            ok_meme, why_not = self._meme_gate(gid, st, tag, now0)
            b64 = meme_catalog.pick_b64(tag, text) if ok_meme else None
            if b64:
                st.last_meme_ts = now0
                st.recent_meme_tags.append(tag)
                # 统一记账：以前这段自己改状态，唯独没压 ctx，
                # 下一轮它根本不知道刚发过一张图，同一张图能连发三次
                self._record_spoke(gid, meta.get("uid", ""), f"[只发图:{tag}]",
                                   reason=meta.get("reason", ""), nick=meta.get("nick", ""))
                event.set_result(MessageEventResult(chain=[Image.fromBase64(b64)]))
                return
            logger.info("[fish] 想只发图但没成（%s），走正常流程", why_not if not ok_meme else "没挑到图")

        # 兜底：不管模型把指针写成什么怪样子（超长、未闭合、跨行），都不许进群。
        # 主正则抓不到的那种，这里按前缀整段抠掉，宁可少一句话
        clean = _SEARCH_LEFTOVER_RE.sub("", clean or "").strip()
        # 没闭合的发图指针同样不能进群
        clean = _PIXIV_LEFTOVER_RE.sub("", clean).strip()
        # 画图标签剥完正文就空了：正文本来该是空的，绝不能回退成「带标签的原句」——
        # 回退之后标签会被当正文发进群（实测「[画图:喜报:今天没人迟到]」就是这么裸奔的）
        raw_text = clean or (meme_gen.extract_tag(text)[0].strip() if draw_req else text)
        text = composer.sanitize_reply(raw_text)
        # 出口清洗：内部独白、动作旁白、Markdown 痕迹在这剥掉。
        # URL 和代码块会先变占位符再还原，联网搜到的内容不会被误删
        try:
            text2 = safety_guard.strip_internal_monologue(text)
            _changed, text2 = safety_guard.sanitize_outgoing_text(text2)
            # 空串也是清洗的结果，必须采纳：以前判 `if text2` 才赋值，
            # 于是「整句都是（甩了甩尾巴）」这种原文会被原样发进群
            if text2 is not None:
                text = text2
            # 骂人词降级：模型写嗨了带出「傻逼/死全家」这类直白词，换成谐音，
            # 而不是把整句丢掉——丢掉会让它越答越短，最后只剩一句「急什么」
            if text.strip():
                text = quarrel.soften(text)
            text = self._strip_repeated_opener(st, text)
            _gid_ts = str(meta.get("gid") or "")
            if _gid_ts:
                self._last_bot_ts = {**getattr(self, "_last_bot_ts", {}), _gid_ts: time.time()}
        except Exception as e:
            logger.debug("[fish] 出口清洗失败：%s", e)
        if not text.strip():
            # 全句都是旁白/占位符，没有一个字是能对人说的：静默，别拿原文兜底
            logger.info("[fish] 群 %s 清洗后无正文（原文：%s），静默不发", gid, raw_text[:30])
            self.log_event("empty", gid, raw_text[:40], who=meta.get("nick", ""))
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2. 反模板：先削撞车的起手/收尾，再摘重复语气词、去复述式开场
        recent = [r for r in st.recent_bot if r]
        reason = anti_template.match(text, recent)
        if reason != "none":
            reshaped = anti_template.reshape(text, reason)
            if reshaped and reshaped != text:
                logger.info("[fish] 反模板 %s，削形: %r -> %r", reason, text[:24], reshaped[:24])
                text = reshaped
        text = anti_template.trim_particle(text, recent)
        text = anti_template.strip_echo_opener(text, meta["text"])
        text = text.strip() or text

        # 2.5 自说自话闸（只拦主动插话）：草稿跟刚才那几句一个词都不沾，说明它在自说自话。
        # 被 @ 或追问时不拦——那时候它有权把话题拐走。
        # 注意别拿「来消息」去判：群里换话题很常见，那样会把正常接茬全掐掉
        vision_desc = str(meta.get("vision") or "")
        if str(meta.get("reason") or "").startswith("主动接话"):
            try:
                # 比对范围必须跟模型看到的窗口一致：只拿最近 5 条去比，
                # 就会出现「草稿其实在接 10 分钟前那个话头，却被判离题」的误杀
                ctx = " ".join(str(m.get("text", "")) for m in list(st.msgs)[-40:-1])
                # 图里的内容根本不在 msgs 里：草稿在聊刚才那张图时，纯文字 ctx 一个词都对不上，
                # 「这图也敢往群里甩」「这眼神像在审判我」这种正经接茬会被判离题吞掉
                # （实测 90 分钟吃掉十来条，全是被判接了又咽回去）
                if vision_desc:
                    ctx = f"{ctx} {vision_desc}"
                if ctx and anti_template.is_offtopic(text, ctx):
                    # 只记录，不再整条丢：这道闸实测把「你这也太惨了」这种最自然的
                    # 安慰也判成离题，静默的代价比发一句稍微跑偏的话大得多
                    logger.info("[fish] 群 %s 插话草稿可能离题（放行）：%r", gid, text[:30])
            except Exception as e:
                logger.debug("[fish] 离题判定失败：%s", e)

        # 2.6 照抄闸：判官那句「怎么接」是给模型的指令，写得太像台词就会被原样念出去。
        # 真实案例：「你最近夜宵吃多了吧，怎么突然感叹胖了」——判官写的正是这一句，
        # 模型一个字的力气都没花。这种句子一发出去就是机器人味，宁可不发
        how = str(meta.get("how") or "")
        if how and self._echoes_hint(text, how):
            logger.info("[fish] 群 %s 复述了「怎么接」，这句不发：%r <- %r", gid, text[:24], how[:24])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why="照抄怎么接")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.7 感知闸：语音、视频、直播根本没有内容进来。说「这配音听着」「我看了那视频」
        # 一律是编——真实翻车「这配音听着跟我外婆刷的广场舞视频一个味」，群里当场就笑他听不到
        fake = ""
        try:
            fake = safety_guard.claims_unperceived(text)
        except Exception as e:
            logger.debug("[fish] 感知闸异常：%s", e)
        # 这两道闸只该拦主动插话。被 @ 了还按这个丢，群里看到的就是「叫它它不理」
        _solicited = not str(meta.get("reason") or "").startswith("主动")
        if fake and not _solicited:
            logger.info("[fish] 群 %s 声称感知到收不到的东西（%r），这句不发：%r", gid, fake, text[:30])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"瞎猜感知：{fake}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.72 报障闸：「图呢，我这边空的」「一堆截图我这也看不清」。不区分主动还是被叫，
        # 一律不发——这是把「我是机器人、我看不见」自己抖出来，比装看得见还伤
        blind = ""
        try:
            blind = safety_guard.reports_blind(text)
        except Exception as e:
            logger.debug("[fish] 报障闸异常：%s", e)
        if blind:
            logger.info("[fish] 群 %s 在报自己没收到图（%r），这句不发：%r", gid, blind, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"报障：{blind}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.71 隐私闸：内网地址、路径、密钥、配置文件名，一个字都不能进群。
        # 提示词里那句「这个不方便讲」被追问几轮就守不住了，地址漏出去又收不回
        leak = ""
        try:
            leak = safety_guard.leaks_private(text)
        except Exception as e:
            logger.debug("[fish] 隐私闸异常：%s", e)
        if leak:
            logger.warning("[fish] 群 %s 的输出含%s，整句拦下：%r", gid, leak, text[:60])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"隐私：{leak}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.73 自曝闸：群友聊「AI 有没有自我意识」时，它会顺着认下来
        # （实测「没意识，那你还怕我勾走人」）。认了就等于当场承认是机器人
        disclose = ""
        try:
            disclose = safety_guard.self_discloses(text)
        except Exception as e:
            logger.debug("[fish] 自曝闸异常：%s", e)
        if disclose:
            logger.info("[fish] 群 %s 在自曝身份（%r），这句不发：%r", gid, disclose, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"自曝：{disclose}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.75 旁白闸：不接内容只点评人家「怎么说话」，或者编一段「你刚才……」。
        # 回放实测这是人机感最重的一类，宁可少发一句也不能发这种
        narr = ""
        try:
            narr = safety_guard.narrates_speaker(text)
        except Exception as e:
            logger.debug("[fish] 旁白闸异常：%s", e)
        if narr and not _solicited:
            logger.info("[fish] 群 %s 旁白/编造刚才（%r），这句不发：%r", gid, narr, text[:40])
            self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why=f"旁白：{narr}")
            event.set_result(MessageEventResult(chain=[]))
            # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
            # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
            event.stop_event()
            self._release_claim(st, _tok)
            return

        # 2.85 草稿闸：模型把「说完另发一条」当成要输出两行文本时，会漏出一整段草稿
        # （实测："什么杯子，我是鱼" + ""Two lines.什么杯子，我是鱼）。拆条按换行切，
        # 草稿就成了第二条群消息，看着像精神分裂。只留要发进群的那几行
        try:
            cleaned, drafts = safety_guard.strip_draft_lines(text)
        except Exception as e:
            cleaned, drafts = text, []
            logger.debug("[fish] 草稿闸异常：%s", e)
        if drafts:
            logger.info("[fish] 群 %s 掐掉模型草稿 %d 行：%r", gid, len(drafts), drafts[0][:40])
            text = cleaned.strip()
            if not text:
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                self._release_claim(st, _tok)
                return

        # 2.8 长度闸：群里没人一条发三百字，超了就砍在最后一个完整句上
        cap = int(self.cfg.get("reply_max_chars", 260) or 260)
        if len(text) > cap:
            before = len(text)
            cut = max(text.rfind(ch, 0, cap) for ch in "。！？!?…\n")
            text = (text[:cut + 1] if cut > 30 else text[:cap]).strip()
            logger.info("[fish] 群 %s 回复 %d 字太长，砍到 %d 字", gid, before, len(text))

        # 2.9 技术复读闸：同一套步骤换个说法再讲一遍。回放里出现过连讲三遍
        # 「Shift+F10 → oobe\\bypassnro」，真人第二遍会说「刚说了啊」。
        # 只比英文/数字标识符，中文句子不进这套逻辑，避免误杀正常聊天
        try:
            if not meta.get("at") and self._repeats_recent_tech(st, text):
                logger.info("[fish] 群 %s 这套步骤 %d 秒内已经讲过，这句不发：%r",
                            gid, int(float(self.cfg.get("tech_repeat_window_sec", 600) or 600)), text[:40])
                self.log_event("empty", gid, text[:40], who=meta.get("nick", ""), why="重复步骤")
                event.set_result(MessageEventResult(chain=[]))
                # 必须 stop：空链只是「不发」，框架的 process_stage 看到「没发过 + 被@/私聊」
                # 还会拿默认人格和默认 provider 再答一遍，群里就多出一条不是它说的回复
                event.stop_event()
                self._release_claim(st, _tok)
                return
        except Exception as e:
            logger.debug("[fish] 复读闸异常：%s", e)

        # 3. 拆条：太长就分成 2~3 条，剩下的放到 on_sent 里隔一会儿再发（像人连发）
        now = time.time()
        final = text
        parts = [final]
        if self.cfg.get("split_enabled", True):
            # 心境参与条数上限：情绪低落/犯困时只发两条，兴致高才三条。
            # 之前这里写死配置值，affect.profile() 算了却没人用，等于白算
            max_parts = int(self.cfg.get("split_max_parts", 3) or 3)
            try:
                prof = affect.profile(meta["gid"]) or {}
                max_parts = min(max_parts, int(prof.get("max_segments") or max_parts))
            except Exception as e:
                logger.debug("[fish] 心境取条数失败：%s", e)
            parts = splitter.split_reply(
                final,
                max_chars=int(self.cfg.get("split_max_chars", 26) or 26),
                max_parts=max(1, max_parts),
            ) or [final]
        chain = [Plain(parts[0])]
        tail = parts[1:]

        # 5.0 发图：有 [看图:标签] 就先取图。取到了就压掉表情包和画图——
        # 一句回复里塞两张图太花，而且这张是点名要的，优先级最高
        pixiv_b64 = ""
        if pixiv_tag and self.cfg.get("pixiv_enabled", True):
            pixiv_b64 = await self._grab_pixiv(pixiv_tag, str(meta.get("gid") or "")) or ""
            if pixiv_b64:
                tag, draw_req = "", None

        # 5. 配图：过斗图闸，一回合只发一张；有尾巴时挪到最后一条后面，别插在中间打断话
        # 画图：模型写了 [画图:模板:文字] 就真画一张。画不出来就当没这个标签，
        # 正文照发——宁可少一张图，也不能因为它把整句话吞掉
        drawn_b64 = None
        if draw_req and self.cfg.get("meme_gen_enabled", True):
            # 画在图上的字同样是「发给群里的话」，不能绕过风控：
            # 实测模型写过【画图：举牌：我是傻逼】这种，以前一路直通到出图
            _draw_ok, _draw_text = safety_guard.sanitize_outgoing_text(draw_req)
            if not _draw_ok:
                logger.info("[fish] 画图文字命中风控，整条画图丢弃：%s", draw_req[:30])
                draw_req = ""
            else:
                draw_req = _draw_text or draw_req
        if draw_req and self.cfg.get("meme_gen_enabled", True):
            drawn_b64 = await meme_gen.make(
                draw_req, float(self.cfg.get("meme_gen_timeout_sec", 25) or 25))
            logger.info("[fish] 画图「%s」%s", draw_req[:30],
                        "画好了" if drawn_b64 else "没画出来（模板没匹配或资源缺）")
        meme_b64 = None
        if tag and self.cfg.get("meme_enabled", True):
            ok_meme, why_not = self._meme_gate(gid, st, tag, now)
            if not ok_meme:
                logger.info("[fish] 表情包闸拦住（%s），这轮纯文字", why_not)
            else:
                meme_b64 = meme_catalog.pick_b64(tag, clean or text)
                if meme_b64:
                    st.last_meme_ts = now
                    st.recent_meme_tags.append(tag)
                    logger.info("[fish] 按标签 %s 配了一张表情包%s", tag,
                                "（排在拆条之后）" if tail else "")

        if pixiv_b64 and not tail:
            chain.append(Image.fromBase64(pixiv_b64))
        if meme_b64 and not tail:
            chain.append(Image.fromBase64(meme_b64))
        if drawn_b64 and not tail:
            # 单条消息的场景：文字和画好的图塞进同一条链，比分两条发自然
            chain.append(Image.fromBase64(drawn_b64))
        if tail:
            event.set_extra("fish_tail", {"gid": gid, "sk": gid, "parts": tail,
                                          "meme": meme_b64, "drawn": drawn_b64,
                                          "pixiv": pixiv_b64})
            logger.info("[fish] 长回复拆成 %d 条（%s 字）：%s", len(parts), len(final),
                        " / ".join(p[:18] for p in parts))

        event.set_result(MessageEventResult(chain=chain))

        # 6. 记账推迟到「真的发出去」之后（on_sent）。
        #    原来在这儿就确认冷却位、扣追问次数、记插话配额：一次发送失败
        #    群里一个字没有，鱼却以为自己说过了，追问窗口也白扣一条
        event.set_extra("fish_account", {
            "gid": gid, "sk": gid, "now": now, "final": final,
            "reason": meta.get("reason", ""), "nick": meta.get("nick", ""),
            "follow": bool(meta.get("follow")), "meme": 1 if meme_b64 else 0,
            "parts": len(parts), "gid_real": meta["gid"], "uid": meta["uid"],
            "text": meta["text"],
        })

    def _account_sent(self, acct: dict) -> None:
        """消息确认发出后才记账：冷却位、追问次数、插话配额、记忆、日志。

        整个包在 try 里：记账是副产品，崩了也不能让一条已经发出去的消息
        在框架里继续抛异常（之前 on_decorate 的 NameError 就是这么漏出去的）。
        """
        try:
            gid = acct["gid"]
            st = self._state(gid)
            now = acct["now"]
            final = acct["final"]
            st.recent_bot.append(final)
            st.dialogue.append({"who": str(self.cfg.get("bot_nickname") or "我"),
                                "text": final, "ts": now, "me": True})
            # 压进上文的是「真正发出去的那句」：清洗/砍长发生在出口，
            # 拿模型原文会让下一次看到的自己跟群里看到的自己不是一个嘴
            _sk = str(acct.get("gid") or "")
            if _sk.isdigit() or _sk.startswith("private"):
                self._push_ctx(st, "assistant", final)
            self._ctx_saved_at = now
            self._save_recent_context()
            st.last_bot_reply_ts = now
            st.last_reply_uid = str(acct.get("uid") or "")
            st.reply_uid_ts[st.last_reply_uid] = now
            st.claiming = False
            st.reply_times.append(now)
            if str(acct.get("reason") or "").startswith("主动接话"):
                # 配额和密度闸只认「真的发出去了」的插话
                st.interject_times.append(now)
            if acct.get("follow"):
                # 追问窗口的次数在真正回出去时才扣，不然一次生成失败就白扣一条
                st.follow_left = max(0, st.follow_left - 1)
            st.msgs_since_bot = 0
            affect.on_bot_spoke(gid)
            self.log_event("reply", gid, final, who=acct.get("nick", ""),
                           why=acct.get("reason", ""), meme=acct.get("meme", 0),
                           parts=acct.get("parts", 1))
            try:
                if acct.get("gid_real") != "private":
                    self.memory.attach_reply(acct["gid_real"], acct["uid"],
                                             acct["text"], final)
            except Exception as e:
                logger.warning("[fish] 记忆写入失败: %s", e)
        except Exception as e:
            logger.exception("[fish] 记账失败（消息已发出，不影响这轮）: %s", e)

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        # 先把 extra 全取走再动手：event.send 会再走一遍 after_message_sent，
        # 不先清空的话补发的那几条会被当成新回复重新处理，变成无限自我复制
        tail = event.get_extra("fish_tail")
        event.set_extra("fish_tail", None)
        # 记账在这里做：这个钩子只在「消息真的送出去了」之后才跑
        acct = event.get_extra("fish_account")
        event.set_extra("fish_account", None)
        if acct:
            self._account_sent(acct)
        if not tail:
            return
        # respond 阶段是同步 await 这个钩子的。sleep + 两三条补发 + 1.6MB 表情包
        # 实测要 2.3~2.8 秒，全压在这儿等于把整轮发送拖慢；丢后台跑，任务存集合防 GC
        task = asyncio.create_task(self._flush_tail(event, tail))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _flush_tail(self, event: AstrMessageEvent, tail: dict) -> None:
        if tail:
            parts = tail.get("parts") or []
            gaps = splitter.plan_gaps(
                len(parts),
                float(self.cfg.get("split_gap_min", 0.6) or 0.6),
                float(self.cfg.get("split_gap_max", 1.5) or 1.5),
            )
            sent_all = True
            for i, part in enumerate(parts):
                gap = gaps[i] if i < len(gaps) else 0.8
                await asyncio.sleep(gap)
                sent = False
                for attempt in (1, 2):
                    try:
                        await event.send(MessageChain(chain=[Plain(part)]))
                        logger.info("[fish] 拆条第 %d/%d 条补发（间隔 %.1fs）：%s",
                                    i + 2, len(parts) + 1, gap, part[:24])
                        sent = True
                        break
                    except (asyncio.TimeoutError, TimeoutError) as e:
                        # 超时只说明没等到回包，消息很可能已经进群了。这时重发就是「同一条说两遍」，
                        # 宁可少一条，也不刷屏——所以超时一律不重试
                        logger.warning("[fish] 补发第 %d/%d 条等回包超时，按已发出处理：%s",
                                       i + 2, len(parts) + 1, e)
                        sent = True
                        break
                    except (ConnectionError, OSError) as e:
                        # 连接层的错误是「确定没送出去」，可以放心重试一次
                        if attempt == 1:
                            await asyncio.sleep(0.4)
                            continue
                        logger.warning("[fish] 补发第 %d/%d 条连接失败，剩余 %d 条丢弃: %s",
                                       i + 2, len(parts) + 1, len(parts) - i, e)
                    except Exception as e:
                        # 其它异常（超长/被平台拒/内容审核）重试也不会变好，别白等
                        logger.warning("[fish] 补发第 %d/%d 条失败，剩余 %d 条丢弃: %s",
                                       i + 2, len(parts) + 1, len(parts) - i, e)
                        break
                if not sent:
                    sent_all = False
                    break
            # 半句话断掉之后再单独冒一张表情包是最难看的组合，所以只在全部发出后才补图
            meme = tail.get("meme") if sent_all else None
            if meme:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(meme)]))
                    logger.info("[fish] 拆条后补发表情包")
                except Exception as e:
                    logger.warning("[fish] 表情包补发失败: %s", e)
            drawn = tail.get("drawn") if sent_all else None
            if drawn:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(drawn)]))
                    logger.info("[fish] 拆条后补发画好的图")
                except Exception as e:
                    logger.warning("[fish] 画图补发失败: %s", e)
            pic = tail.get("pixiv") if sent_all else None
            if pic:
                await asyncio.sleep(0.5)
                try:
                    await event.send(MessageChain(chain=[Image.fromBase64(pic)]))
                    logger.info("[fish] 拆条后补发 pixiv 图")
                except Exception as e:
                    logger.warning("[fish] pixiv 补发失败: %s", e)

