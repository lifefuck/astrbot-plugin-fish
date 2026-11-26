# -*- coding: utf-8 -*-
"""模块级共享件：常量、正则、数据类、帮助函数（从 main.py 顶部整段搬出，一字未改）。"""

from __future__ import annotations

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

_URL_RE = re.compile(
    r"https?://|www\.|v\.douyin\.com|xhslink\.com|b23\.tv|bili2233\.cn"
    r"|\bBV[a-zA-Z0-9]{10}\b|\bav\d{3,}")

_ASK_OPINION_RE = re.compile(
    r"(评价|点评|说说|讲讲|分析|怎么看|怎么评价|你觉得|你的看法|什么看法|如何评价|讲讲看|说说看|聊聊)", re.I)

_REALTIME_RE = re.compile(
    r"联网|网上|搜索|查一下|查查|查下|帮我查|搜一下|搜搜|搜个|百度|谷歌|必应|bing|google|"
    r"最新|新闻|比分|夺冠|结果|进展|开播|发布会|上市|多少钱|价格|天气|几点|什么时候|"
    r"真的假的|是不是真的|有没有发生")

_NO_SEARCH_RE = re.compile(r"搜索算法|复杂度|正则|排序算法|二叉树|哈希表|动态规划|"
                           r"sql 注入|prompt 注入|依赖注入")

_EXPLICIT_SEARCH_RE = re.compile(
    r"联网|网上搜|搜一下|搜一搜|搜搜|搜索一下|帮我查|查一下|查查|百度一下|谷歌|必应|google|bing",
    re.I)

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

# kernel 在 fish_core/ 下，插件根目录要再往上一层
PLUGIN_DIR = Path(__file__).resolve().parent.parent

FISH_CORE = PLUGIN_DIR / "fish_core"

# 曾经这里 sys.path.insert(0, FISH_CORE)，让 fish_core 里的模块能 `import backup`。
# 副作用是 backup/vision/prompt 成了进程级通用名，别家插件 import 到的是本插件的。
# 现在一律走 _load() 的限定名，路径不再进 sys.path

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

_WARM_PNG = _warm_png_b64()

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
# 本群自己的梗（scripts/mine_group_memes.py 从真实聊天记录统计出来的）
group_memes = _load("group_memes", "group_memes.py")
# 发送前排版归一化（空格/换行/句号/感叹号），确定性清洗，与提示词无关
typeset = _load("typeset", "typeset.py")
# 收件人判定表：这条消息到底说给谁（@/引用 > 点名 > 接续 > 相邻 > 无信号）
addressee = _load("addressee", "addressee.py")
# 第 3 步：调度器（说不说 + 发言率）与生成器侧的打字延迟/草稿重判
scheduler = _load("scheduler", "scheduler.py")
tease = _load("tease", "tease.py")
presence = _load("presence", "presence.py")
generator = _load("generator", "generator.py")

meme_gate = _load("meme_gate", "meme_gate.py")

prompts = _load("prompts", "prompts.py")

composer = _load("composer", "composer.py")

# 人味化后处理：拆条/去 AI 腔/删八股句/静默标签。规则搬自大肥鱼 humanizer
# 与 MaiBot cleaner，见模块头注释
humanize = _load("humanize", "humanize.py")

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
    "judge_skip_on_at": True,
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
    "panel_event_buffer": 200,
    "topic_hint_enabled": True,   # 判官给的话题释义注入表达层；关掉就退回不带释义
    "reply_when_at": True,
    "reply_private": True,
    "meme_enabled": True,
    # --- 气氛：冷场怎么补 ---
    # 荤段子砍掉之后，气氛全靠本群自己的梗（scripts/mine_group_memes.py 从真实
    # 聊天记录统计）+ 表情包垫场。只影响提示词，判定/冷却/密度一概不碰。
    "group_memes_enabled": True,
    "cold_atmosphere_enabled": True,
    "cold_field_gap_sec": 900,      # 上一条人话过去多久算冷场；15 分钟，跟「午休/上课」一个量级
    "cold_meme_first": True,        # 冷场时模型没给标签也先垫一张没攻击性的图
    # --- 收件人判定：这条到底是不是说给鱼的 ---
    # 判定链只管「多该说」，没管「是不是冲我说的」。两个人聊得正热时按话题相邻
    # 一头扎进去抢答，比漏接更伤。灰区默认不是，宁可漏接不可抢答。
    "addressee_guard_enabled": True,   # 总开关：群里不对劲随时关掉，退回只看判定链
    "addressee_hard_threshold": 70,    # ≥ 这个分认定说给鱼
    "addressee_gray_threshold": 40,    # 灰区下限：追问窗口内 + 判官过线才放，否则默认不是
    "addressee_thread_window_sec": 180,  # A↔B 线程多少秒没新消息算散场
    "addressee_topic_gap_sec": 60,     # 鱼说过话之后多少秒内算「同一段话口」
    "addressee_alias_enabled": True,   # 从 At 昵称/群名片学别名（不猜外号）
    # —— 调度器（第 3 步）——
    # 默认不说：没有 @/引用/追问/卡话题/明确求助就闭嘴。这是行为的总开关，
    # 关掉就退回「判官给分就接」的旧口径
    "scheduler_default_silent": True,
    "rate_guard_enabled": True,        # 发言率闸（占比 + 连续 + 主动冷却）
    "rate_window_sec": 600,            # 发言率统计窗口
    "rate_window_msgs": 50,            # 窗口条数上限，先到者为准
    "proactive_gap_sec": 90,           # 主动发言之后至少隔多久才允许下一次
    # 热聊插话档：两人一来一回时收件人分会被「A↔B在聊」扣到 0，卡话题那档又只认问句，
    # 于是群里越热闹它越插不上（实测 13 分钟零发言）。这一档不看问句，只看热度和自己沉默了多久。
    "warm_chat_enabled": True,
    "warm_window_sec": 180,            # 只看最近几分钟的热度
    "warm_min_msgs": 6,                # 窗口内至少几条人话算热聊
    "warm_quiet_sec": 240,             # 自己沉默满这么久才补一句
    # —— 打字延迟 ——
    "typing_delay_enabled": True,
    "typing_max_sec": 3.0,             # 延迟封顶，长段也不要让人等太久
    "draft_ttl_sec": 25,               # 草稿最长存活时间，超了就不发
    # —— 调侃许可（第 4 步）——
    # 三级：无许可只中性、低许可能自嘲、高许可能挤对行为。外貌/性别/家庭/工作能力
    # 永远是禁区，跟级别无关
    "tease_guard_enabled": True,
    "tease_high_favor": 80,            # 好感到这个数直接给高许可
    "tease_low_favor": 60,             # 到这个数给低许可
    "tease_ban_days": 30,              # 被明确拒绝后多久不许再试（不随道歉/投喂缩短）
    "self_tease_per_hour": 1,          # 自嘲每小时至多一次
    # —— 在场（第 4 步）——
    # 不需要回答但值得有反应的消息，只给一张表情或一个短词，不调模型
    "presence_enabled": True,
    "presence_gap_sec": 120,           # 两次在场之间至少隔多久
    "presence_per_10min": 2,           # 10 分钟内最多几次
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
    # --- 记忆补全（第 5 步）：出处、可引用性、目的地记忆 ---
    "memory_source_required": True,   # 关掉=退回没有出处也能主动提的老行为
    "memory_min_confidence": 0.5,     # 主动引用要求的最低置信度
    "memory_said_repeat_warn": 2,     # 同一件事跟同一个人说过几次后先自查
    "memory_conf_decay_days": 30,     # 低置信度记忆多久没人印证就降一档
    "memory_prompt_budget": 160,      # 记忆块字数上限（条数之外的第二道闸）
    "style_learning": True,
    "debug_dump_prompt": False,
    # 云崽等面板指令前缀：这些消息归别的插件，绝不插嘴
    "command_prefixes": ["#", "*", "%", "~"],
    # --- 长回复拆条 ---
    "split_enabled": True,
    "split_max_chars": 45,          # 线上实测 45 最稳：26 一拆就碎，60 又像一坨
    "split_max_parts": 3,
    "split_gap_min": 0.6,
    "split_gap_max": 1.5,
    # --- Gemini 看图 ---
    "vision_enabled": True,
    "vision_model": "gemini-3.5-flash-lite",   # 4-hydrogen 长年 429，别再留
    # 识图主通道改成 WB 池（Gemini 常年限流）；顺序 vision_wb_models → 框架 provider → Gemini 直连
    "vision_wb_enable": True,
    "vision_wb_base": "http://127.0.0.1:3425/v1",
    "vision_wb_key": "",
    "vision_wb_models": ["wb/glm-5v-turbo", "wb/minimax-m3", "google/models/gemini-flash-lite-latest"],
    "vision_wb_timeout_sec": 16,
    "vision_fallback_models": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "vision_max_images": 2,
    "vision_timeout_sec": 12,
    "vision_total_budget_sec": 20,
    "vision_proxy": "http://127.0.0.1:7890",
    "vision_api_key": "",
    "vision_key_env_file": "~/.config/qq-deepseek-bot.env",   # 读的时候 expanduser，别把家目录写死进默认值
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
    # A 组（salttove 词表 / dsh 四件套）落地的开关。默认值都按「不误杀」挑的：
    # humanize_short_cut 关（「好」「行」在 fish 里是正经回复，全删等于装死）、
    # humanize_fiction 关（小说腔本来就不出现在群聊里，先不参与）、
    # aiflavour 开在 strip 档（只剥不整条拦，刹车要连拦两次才触发）
    "humanize_short_cut": False,
    "humanize_fiction": False,
    "aiflavour_enabled": True,
    "aiflavour_mode": "strip",
    "aiflavour_learn_min": 3,
    "aiflavour_deactivate_days": 7,
    "aiflavour_short_window": 180,
    "human_comma_split": True,
    "ctx_keep_turns": 30,
    # 毛边：错别字（一次最多一个 + 自我更正）。
    # 0.06 是「三十条里来一条」，再高就不像手滑像文盲了
    "huoren_typo_enabled": True,
    "huoren_typo_rate": 0.06,
    "huoren_correction_rate": 0.5,
    # 心流：energy/warmth 按会话落盘，压成一句提示词
    "huoren_mood": True,
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
    # 0600：这些文件里有好感度、记忆、别人说过的话，机器上还有别的账号，
    # 默认 umask 002 会写成 664（全机可读）
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)
    os.replace(tmp, path)

_POKE_BACK_TS: dict[str, float] = {}

_pick_poke = poke_context.pick_fallback

_BUILTIN_COMMANDS = frozenset({
    "help", "reset", "new", "stop", "sid", "stats",
    "provider", "set", "unset", "name", "ping", "dashboard_update",
    # 记忆查询由 astrbot_plugin_memory_cmd 接管，聊天插件必须让路
    "memary", "memory",
})

_SONG_INTENT_RE = re.compile(
    r"(?:我想听|我要听|想听|要听|听一下|听个|听首|听一首|来一首|来首|来一个|"
    r"放一首|放首|放个|点一首|点首|点歌|唱一首|唱首|给我放|给我来"
    # 「能放」以前是裸片段，群里一句「我才能放心的把活交给它」被切成歌名
    # 「心的把活交给它」真的下载并发了一条 30 秒语音。要求前面不是「才/怎/可」、
    # 后面紧跟量词或书名号才算点歌意图
    r"|(?<![才怎可])(?:能不能放|能放)(?=[首个一《「\"']))"
    r"\s*[《「\"']?\s*([^《》「」\"'，,。！!？?\n]{1,24})"
)

_SONG_STOP_WORDS = {
    "歌", "首歌", "歌曲", "一首", "个", "点", "音乐", "一下", "听听", "什么",
    "啥", "这个", "那个", "点儿", "点儿歌", "歌儿", "吧", "呗",
    "视频", "片子", "电影", "番剧", "直播",
}

_SEARCH_MARK_RE = re.compile(r"[\[【]\s*(?:搜索|搜|查)\s*[:：]\s*([^\]】]{1,80})[\]】]")

_SEARCH_LEFTOVER_RE = re.compile(r"[\[【]\s*(?:搜索|搜|查)\s*[:：][^\]】]{0,80}[\]】]?")

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

_TAG_ONLY_RE = re.compile(
    r"[\[【]\s*(?:表现|动作|神态|心理|记忆|回想|MEME|表情|画图|REPORT|PASS)[:：]?[^\]】\n]{0,40}[\]】]",
    re.I)

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

_ASK_FAVOR_RE = re.compile(r"好感|喜不喜欢我|讨厌我|多讨厌我|我(?:在你?这|这)多少分|给我打?几分")

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
    # 消息版本号：每进一条群消息 +1。打字延迟醒来只比对这一个数字就知道
    # 「等这几秒里群里有没有新动静」，不用去 diff 整个对话流
    msg_ver: int = 0
    # 还没发出去的草稿（每群一份，后到的覆盖先到的）。只在内存，重启丢掉是对的
    draft: dict = field(default_factory=dict)
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
    # 干 @ 去重按人算：上面那个是全局的，别人刚被应过，我 @ 一下就被判
    # 「45 秒内已应过」不理我——群里感知就是「@ 了两次没反应」
    bare_at_by: dict = field(default_factory=dict)
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
    # 占位的时间戳：发送失败时框架不会回调插件，「发出去才算数」的第二段永远不会来，
    # 冷却位就一直挂着。留个时间戳好让入口处按超时作废（见 _expire_stale_claim）
    claim_ts: float = 0.0

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
            # 异常发生时冷却位往往已经被 claim 走了，不还回去这群要白静默一个周期。
            # 但只能还本轮自己的那一份：同群两条消息并发时，先挂的那条无脑回滚会把
            # 后一条的占位一起抹掉，密度闸就白设了。token 由 handler 在 _claim 后挂到
            # event 上；取不到说明这条消息压根没占位，不必回滚。
            try:
                gid = event.get_group_id()
                tok = None
                try:
                    tok = event.get_extra("fish_claim_tok")
                except Exception:
                    tok = None
                if gid and tok is not None:
                    self._release_claim(self._state(str(gid)), tok)
            except Exception:
                pass
            try:
                event.stop_event()
            except Exception:
                pass

    return wrapper


def cfg_int(cfg: dict, key: str, default: int) -> int:
    """取整数配置，0 是合法值。

    `cfg.get(key, 45) or 45` 这种写法会把 0 当成「没配」，于是 split_max_chars=0
    （配置文档里写的「不拆」）实际变成最激进的拆法。空串/None 才回落到默认。
    """
    v = cfg.get(key, default)
    if v is None or v == "" or isinstance(v, bool):
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def flatten_conf(raw: dict) -> dict:
    """把配置的对象分组摊平成扁平键。

    面板上把 78 个调优项收进了 advanced_* 五个对象节，但上层代码只认扁平键
    （self.cfg["meme_pool_size"]）。这里在入口摊一次，调用点一个都不用改；
    老配置里的扁平键优先，升级时不用迁移。
    """
    flat: dict = {}
    for key, val in (raw or {}).items():
        if isinstance(val, dict):
            flat.update(val)
        else:
            flat[key] = val
    return flat


__all__ = ["ToolSet", "cfg_int", "_guess_lan_ip", "_URL_RE", "_ASK_OPINION_RE", "_REALTIME_RE", "_NO_SEARCH_RE", "_EXPLICIT_SEARCH_RE", "_VAGUE_Q", "_usage_of", "_search_query", "PLUGIN_DIR", "FISH_CORE", "DATA_DIR", "_warm_png_b64", "_WARM_PNG", "_load", "affect", "favor", "poke_context", "anti_template", "interject_planner", "style_learner", "meme_catalog", "group_memes", "typeset", "addressee", "scheduler", "generator", "tease", "presence", "meme_gate", "prompts", "composer", "humanize", "quarrel", "insult_actions", "interject_score", "fish_memory", "splitter", "token_report", "token_card", "vision", "hearing", "pixiv", "_media_parser_pixiv_cookie", "meme_gen", "order_watch", "social_read", "peer_bots", "cross_memory", "panel_api", "admin_actions", "yunzai_link", "backup", "games", "fun_features", "connectors", "safety_guard", "cross_interaction", "memory_atoms", "meme_manager", "DEFAULTS", "MEME_PATH", "_noop", "_write_json", "_POKE_BACK_TS", "_pick_poke", "_BUILTIN_COMMANDS", "_SONG_INTENT_RE", "_SONG_STOP_WORDS", "_SEARCH_MARK_RE", "_SEARCH_LEFTOVER_RE", "_PIXIV_MARK_RE", "_PIXIV_LEFTOVER_RE", "_ImgRef", "_TAG_ONLY_RE", "_SONG_META_HEAD", "_SONG_META_CONTAINS", "_parse_song_request", "_MEME_ASK_RE", "_meme_ask_term", "_REPORT_RE", "_extract_report", "_TATTLE_RE", "_is_tattle_request", "_is_builtin_cmd", "_poke_back", "_ASK_FAVOR_RE", "_TECH_TOKEN_RE", "GroupState", "_poke_aimed_at_me", "_UNSET", "_handler_guard", "flatten_conf", "NO_TOOLS"]
