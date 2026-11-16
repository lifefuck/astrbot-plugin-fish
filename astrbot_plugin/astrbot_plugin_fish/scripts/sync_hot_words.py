#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络热梗 / 流行词库同步脚本（独立命令行工具）

移植自旧仓库 sync_hot_words.py，改动点：
1. 不依赖仓库位置：输出路径默认「脚本上一级的 data/hot_words.json」，即插件 data/，
   可用 --output 覆盖；脚本里不留任何旧服务绝对路径或内网代理地址。
2. 代理由环境变量决定（HTTP_PROXY / HTTPS_PROXY / NO_PROXY），默认直连，
   不再把本机代理端口写死在代码里。
3. 支持 --dry-run（只统计不落盘、不调 LLM 补释义）与 --from-file DIR
   （用本地快照跑流程，离线自测用）。
4. 落盘仍是「先写 tmp，备份旧文件，再一次 os.replace」，中途死掉不会留下半份词库。

用法：
    python3 scripts/sync_hot_words.py                  # 正式同步
    python3 scripts/sync_hot_words.py --dry-run        # 只看看这次能拿到多少
    python3 scripts/sync_hot_words.py --from-file fixtures --dry-run   # 离线演练
    python3 scripts/sync_hot_words.py --output /tmp/hot_words.json

数据源：
    CHIME 结构化梗库、搜狗网络新词、newsnow 热榜(B站/贴吧/抖音)、website-hot-hub
    环境变量 DEEPSEEK_API_KEY 存在时才补一遍热搜短释义（--dry-run / --no-enrich 会跳过）。

作者: life (lifefuck)
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request

logger = logging.getLogger("sync_hot_words")

# 脚本在 <plugin>/scripts/ 下，默认输出到 <plugin>/data/
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(_PLUGIN_ROOT, "data", "hot_words.json")

CHIME_URL = "https://raw.githubusercontent.com/yuboxie/chime/main/data/chime_full.json"
SOGOU_URL = "https://raw.githubusercontent.com/ASC8384/SogouPopularDict/main/data/sogou_network_words_current.txt"
HOT_HUB_URL = "https://raw.githubusercontent.com/cxyfreedom/website-hot-hub/main/README.md"
NEWSNOW_URL = "https://newsnow.busiyi.world/api/s?id={sid}"

USER_AGENT = "Mozilla/5.0 (compatible; fish-hot-words-sync/1.0)"

# 核心涉政与严重风控违禁词（坚决拦截，保住 QQ 号防封）
BLOCKED_KEYWORDS = {
    "政治", "国家领导", "领导人", "反华", "涉政", "台独", "港独", "自杀", "献忠",
    "暴恐", "毒品", "枪支",
}

# 热搜标题会被当成「梗」注入群聊，事故/刑案/塌房类一律别进来：
# 一是模型会照着调侃，二是群里真聊起来有舆情风险
HOT_SEARCH_BLOCK = (
    "外交部", "气象", "地震", "总书记", "政治", "事故", "遇难", "身亡", "死亡",
    "坠亡", "失踪", "刑拘", "案件", "通报", "塌房", "恋情", "离婚", "出轨",
    "猝死", "自杀", "轻生", "性侵", "猥亵", "拐卖", "中毒", "火灾", "爆炸",
    "车祸", "孕妇", "婴儿", "遗体", "通缉", "枪击", "战争", "导弹", "袭击",
    "裁员", "欠薪", "维权", "举报",
)

# 搜狗词库混杂大量地名、机构名，只留具备网络社交特征的词
NETWORK_MARKERS = (
    "哥", "姐", "酱", "帝", "王", "神", "批", "门", "症", "学", "文学", "体",
    "吃瓜", "逆天", "离谱", "破防", "硬控", "偷感", "红温", "包的", "那咋了",
    "老铁", "草台", "淡人", "浓人", "整活", "上头", "下头", "绝绝", "塌房",
    "开大", "对线", "拉踩", "画饼", "牛马", "打工人", "emo", "搞子", "串子",
)

# 常青经典梗（保底库，含别名变体与圈子归类）。
# 自动同步会整份重写词库，所以这些只放在脚本里作为固定补充，不落成手改文件。
CLASSIC_MEMES = [
    {"name": "打瓦", "desc": "指玩射击游戏《无畏契约》(Valorant)，常伴随瓦学弟/叫妈妈/笨蛋奶妈等抽象逆天二创梗", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["达瓦", "我要打瓦", "瓦罗兰特", "valorant", "打瓦叫妈", "贤者"]},
    {"name": "瓦学弟", "desc": "《无畏契约》玩家的戏称（与GO学长相对），常被二创调侃在游戏里爱认妈或叫妈妈带飞", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["瓦批", "无畏契约学弟"]},
    {"name": "三角洲堵桥", "desc": "《三角洲行动》航天基地发射桥玩法，乌鲁鲁/狙击手卡桥头封锁撤离点只杀人不吃包", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["发射桥", "航天基地堵桥", "三角洲封桥"]},
    {"name": "堵桥", "desc": "战术撤离或吃鸡游戏中守在必经桥头截杀撤离玩家（三角洲航天基地乌鲁鲁堵桥最出名）", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["守桥", "卡桥头", "桥头战神"]},
    {"name": "乌鲁鲁", "desc": "《三角洲行动》工程干员，因巡飞弹技能在发射桥堵桥极其恶心闻名", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["巡飞弹", "大胖子", "三角洲乌鲁鲁"]},
    {"name": "老六", "desc": "指游戏中游离于团队之外、行为猥琐阴险的伏击者或苟分老银币", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["当老六", "真老六", "老六行为", "老六架枪"]},
    {"name": "尊嘟假嘟", "desc": "真的假的谐音，可爱卖萌语气", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["尊嘟", "假嘟", "zundu"]},
    {"name": "泰裤辣", "desc": "太酷了谐音，调侃热血或中二名场面", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["太裤辣", "太酷啦"]},
    {"name": "电子布洛芬", "desc": "指看了让人治愈开心、缓解疲惫的视频或段子", "source": "classic", "type": "修辞", "category": "acg", "aliases": ["精神布洛芬"]},
    {"name": "偷感很重", "desc": "形容小心翼翼、生怕被发现的害羞或心虚状态", "source": "classic", "type": "现象", "category": "general", "aliases": ["偷感"]},
    {"name": "硬控", "desc": "形容被某事物深深吸引或震撼，完全动弹不得", "source": "classic", "type": "现象", "category": "general", "aliases": ["硬控我", "被硬控"]},
    {"name": "水灵灵地", "desc": "调侃某人毫无预兆、理直气壮地做出某事", "source": "classic", "type": "现象", "category": "general", "aliases": ["水灵灵"]},
    {"name": "班味", "desc": "上班久了身上散发出的疲惫、憔悴与沧桑感", "source": "classic", "type": "现象", "category": "general", "aliases": ["去班味", "班味重"]},
    {"name": "疯狂星期四", "desc": "肯德基特惠活动演变出的各种离谱反转求请客文案（v我50）", "source": "classic", "type": "梗", "category": "general", "aliases": ["v我50", "狂四", "肯德基星期四"]},
    {"name": "CPU/KTV", "desc": "调侃PUA的代称（“你在CPU我吗”）", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["cpu我", "你在cpu我"]},
    {"name": "纯爱战士", "desc": "崇尚专一美好爱情、拒绝牛头人剧情的二次元同好", "source": "classic", "type": "二次元", "category": "acg", "aliases": ["纯爱战神"]},
    {"name": "牛头人(NTR)", "desc": "调侃被横刀夺爱、绿帽背叛情节", "source": "classic", "type": "二次元", "category": "acg", "aliases": ["ntr", "牛头人"]},
    {"name": "发癫/发疯文学", "desc": "情绪饱满、逻辑混乱却极具感染力的情绪发泄对话", "source": "classic", "type": "文风", "category": "abstract", "aliases": ["发癫", "发疯"]},
    {"name": "赛博菩萨", "desc": "在网上无偿分享资源、攻略或解决疑难的好心人", "source": "classic", "type": "网络赞誉", "category": "general", "aliases": ["网络菩萨"]},
    {"name": "义父", "desc": "对送福利、借账号、请客大佬的尊称调侃", "source": "classic", "type": "群聊称呼", "category": "abstract", "aliases": ["请客义父", "活义父"]},
    {"name": "汗流浃背了吧", "desc": "对局或对线占据上风时调侃对方心虚紧张", "source": "classic", "type": "对线调侃", "category": "game", "aliases": ["汗流浃背", "老弟汗流浃背了吧"]},
    {"name": "老登/小登", "desc": "对群内老油条与新人群友的调侃称呼", "source": "classic", "type": "称呼", "category": "abstract", "aliases": ["老登", "小登"]},
    {"name": "红温", "desc": "源自电竞选手Uzi比赛失利脸色涨红，引申为情绪急躁、破防、愤怒", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["红温了", "急了急了", "破大防"]},
]


# ---------------- 网络 ----------------
def fetch_url(url: str, timeout: int = 15) -> str:
    """拉取远程文本。

    代理只认环境变量（urllib 默认读 HTTP_PROXY/HTTPS_PROXY/NO_PROXY），
    没配就是直连。不要在这里写死本机代理端口，换台机器就跑不动。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def local_fetcher(directory: str):
    """把 URL 映射到本地快照文件，供离线演练使用。

    认这几个文件名：chime.json / sogou.txt / bilibili.json / tieba.json /
    douyin.json / hot_hub.md。缺哪个就当那个源拉取失败（和线上一样的降级路径）。
    """
    mapping = {
        CHIME_URL: "chime.json",
        SOGOU_URL: "sogou.txt",
        HOT_HUB_URL: "hot_hub.md",
        NEWSNOW_URL.format(sid="bilibili-hot-search"): "bilibili.json",
        NEWSNOW_URL.format(sid="tieba"): "tieba.json",
        NEWSNOW_URL.format(sid="douyin"): "douyin.json",
    }

    def _fetch(url: str, timeout: int = 15) -> str:
        name = mapping.get(url)
        path = os.path.join(directory, name) if name else None
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"离线快照缺失: {name or url}")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    return _fetch


# ---------------- 过滤 ----------------
def is_safe_meme(term: str, meaning: str = "") -> bool:
    """安全性与可用性校验：风控词、长度、纯数字/符号"""
    term = (term or "").strip()
    text = f"{term} {meaning or ''}"
    if any(k in text for k in BLOCKED_KEYWORDS):
        return False
    if not (2 <= len(term) <= 20):
        return False
    if re.fullmatch(r"[\d\W_]+", term):
        return False
    return True


def _should_abort(old_count: int, new_count: int) -> bool:
    """新结果比存量缩水太多就判定数据源异常，保留旧库。

    阈值按存量比例算而不是写死 200：词库小于 200 条时（新部署或上次被搞丢过），
    写死的保护等于不存在，一次翻车就覆盖到底。
    """
    return old_count >= 30 and new_count < old_count * 0.6


# ---------------- 各数据源 ----------------
def _collect_chime(fetch, seen: set, out: list) -> int:
    logger.info("正在获取高质量结构化网络梗库 (CHIME)...")
    added = 0
    try:
        chime_data = json.loads(fetch(CHIME_URL))
        for item in chime_data if isinstance(chime_data, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("meme") or "").strip()
            meaning = str(item.get("meaning") or "").strip()
            meaning_short = re.split(r"[。；\n]", meaning)[0].strip()
            if len(meaning_short) > 40:
                meaning_short = meaning_short[:37] + "..."
            if name and name not in seen and is_safe_meme(name, meaning):
                seen.add(name)
                out.append({
                    "name": name, "desc": meaning_short,
                    "source": "chime", "type": item.get("type_cn") or "网络梗",
                })
                added += 1
        logger.info("CHIME 载入 %d 条", added)
    except Exception as e:
        logger.error("拉取 CHIME 梗库失败: %s", e)
    return added


def _collect_sogou(fetch, seen: set, out: list, cap: int = 500) -> int:
    logger.info("正在获取搜狗最新流行新词...")
    added = 0
    try:
        for line in fetch(SOGOU_URL).splitlines():
            word = line.strip().split("\t")[0].strip()
            if not word or word in seen or not is_safe_meme(word):
                continue
            if not any(k in word for k in NETWORK_MARKERS):
                continue
            if 2 <= len(word) <= 10:
                seen.add(word)
                out.append({"name": word, "desc": "", "source": "sogou_popular",
                            "type": "近期流行词"})
                added += 1
                if added >= cap:
                    break
        logger.info("搜狗流行新词补充 %d 条", added)
    except Exception as e:
        logger.error("拉取搜狗流行词失败: %s", e)
    return added


def _collect_trending(fetch, seen: set, out: list) -> int:
    logger.info("正在获取社区与亚文化高频热榜 (Bilibili/贴吧/抖音)...")
    added = 0
    for sid, sname in (("bilibili-hot-search", "B站热搜"), ("tieba", "贴吧热议"), ("douyin", "抖音热点")):
        try:
            data = json.loads(fetch(NEWSNOW_URL.format(sid=sid), timeout=8))
            for it in (data.get("items") or [])[:15]:
                title = str(it.get("title") or "").strip()
                if not (2 <= len(title) <= 20):
                    continue
                if any(w in title for w in HOT_SEARCH_BLOCK):
                    continue
                if title in seen or not is_safe_meme(title):
                    continue
                seen.add(title)
                out.append({"name": title, "desc": f"近期{sname}高频热点事件/讨论话题",
                            "source": sid, "type": "社区热搜"})
                added += 1
        except Exception as e:
            logger.warning("拉取 %s 失败: %s", sname, e)

    # 从 GitHub website-hot-hub 的 README 里再捞一批 B 站二创标题
    try:
        hub_text = fetch(HOT_HUB_URL, timeout=10)
        bili_m = re.search(r"## Bilibili.*?(?=## |\Z)", hub_text, re.DOTALL)
        if bili_m:
            for line in bili_m.group(0).splitlines()[:50]:
                m_t = re.search(r"^\d+\.\s*(?:《|【)?([^】》\n(（]{2,18})", line.strip())
                if not m_t:
                    continue
                name = m_t.group(1).strip()
                if name in seen or any(bad in name for bad in ("PV", "http", "宣传片", "自驾", "评测")):
                    continue
                if not is_safe_meme(name):
                    continue
                seen.add(name)
                out.append({"name": name,
                            "desc": f"近期B站高频二创热点/爆款内容：{line.strip()[:35]}",
                            "source": "github_hot_hub", "type": "二创热梗"})
                added += 1
    except Exception as e:
        logger.warning("拉取 website-hot-hub 失败: %s", e)
    logger.info("多平台热搜与二创热点补充 %d 条", added)
    return added


# 新闻标题特征词：命中就当新闻，不当梗
_NEWS_HINTS = ("夺冠", "战胜", "击败", "赛后", "回应", "宣布", "官宣", "网传", "为何", "如何",
               "决赛", "收购", "选手", "战队", "晋级", "淘汰", "首现", "荣获", "评", "通报",
               "最新", "曝光", "揭秘", "告吹", "全部", "首次")


def _tag_news(memes: list) -> int:
    """把热搜源里的新闻标题标成 news，返回标了几条。

    判断两条：长度超过 12 字基本是句子；含赛事/时政常用词的基本是新闻。
    这条规则故意只作用于热搜源——手写的 classic 和核心梗绝不碰。
    """
    hot_sources = {"bilibili-hot-search", "douyin", "tieba", "chime",
                   "sogou_popular", "github_hot_hub"}
    tagged = 0
    for m in memes:
        if m.get("source") not in hot_sources:
            continue
        name = str(m.get("name") or "")
        if len(name) > 12 or any(h in name for h in _NEWS_HINTS):
            m["source"] = "news"
            tagged += 1
    return tagged


def _enrich_with_llm(memes: list) -> int:
    """给没有背景的热搜补一句 20 字以内的释义，返回补上的条数。

    只有配了 DEEPSEEK_API_KEY 才走；按名字回填（旧版按行号对齐，
    模型少输出一行就张冠李戴，还会带着错解释永久入库）。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return 0
    targets = [m for m in memes
               if m.get("source") in ("bilibili-hot-search", "douyin", "tieba")
               and not m.get("desc_generated")][:15]
    if not targets:
        return 0

    api_url = os.environ.get("DEEPSEEK_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
    model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V3")
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "你是一个懂各种网络热搜、游戏黑话和流行梗的年轻人。请为每个给出的网络热词或热搜标题写一句极其简炼的背景（20字以内）。输出格式：每行「原词|解释」，原词必须一字不差照抄，不要编号、不要多余的话。"},
            {"role": "user", "content": "\n".join(f"{i+1}. {m['name']}" for i, m in enumerate(targets))},
        ],
        "temperature": 0.5,
        "max_tokens": 500,
        "thinking": {"type": "disabled"},
    }
    try:
        req = urllib.request.Request(
            api_url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            ans = json.loads(resp.read().decode("utf-8"))
        text = ans["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("热搜释义补全跳过或失败: %s", e)
        return 0

    by_name = {m["name"]: m for m in targets}
    filled = 0
    for line in text.splitlines():
        line = line.strip()
        if "|" in line:
            name, brief = line.split("|", 1)
            target = by_name.get(name.strip())
        else:
            m_idx = re.match(r"^(\d+)[.、]\s*(.*)", line)
            target, brief = None, ""
            if m_idx:
                idx = int(m_idx.group(1)) - 1
                target = targets[idx] if 0 <= idx < len(targets) else None
                brief = m_idx.group(2)
        brief = (brief or "").strip()
        if target is not None and brief:
            target["desc"] = brief[:40]
            target["desc_generated"] = True
            filled += 1
    if filled:
        logger.info("已为 %d 条热搜补充一句话背景", filled)
    return filled


# ---------------- 主流程 ----------------
def sync_memes(output: str = None, dry_run: bool = False, fetch=None,
               enrich: bool = None) -> dict:
    """拉取 + 过滤 + 合并，返回 payload；数据源异常导致中止时返回 None。

    - dry_run: 只算不写，也不调 LLM（有成本和副作用）
    - fetch: 可注入的取数函数，默认 fetch_url；离线自测传 local_fetcher(dir)
    - enrich: 是否补释义，None 表示「非 dry-run 且配了 key 才补」
    """
    output = os.path.abspath(os.path.expanduser(output or DEFAULT_OUTPUT))
    data_dir = os.path.dirname(output)
    fetch = fetch or fetch_url
    if enrich is None:
        enrich = not dry_run

    memes: list = []
    seen: set = set()
    counts = {}

    counts["chime"] = _collect_chime(fetch, seen, memes)
    counts["sogou"] = _collect_sogou(fetch, seen, memes)
    counts["trending"] = _collect_trending(fetch, seen, memes)

    # 常青梗在最前面：它们最稳，也最可能被后面同名的热搜顶掉
    classic_added = 0
    for m in CLASSIC_MEMES:
        if m["name"] not in seen:
            seen.add(m["name"])
            memes.append(dict(m))
            classic_added += 1
    counts["classic"] = classic_added

    # 手写的核心梗单独放 memes_core.json（自动同步整份重写词库，写进主库会被冲掉）
    core_added = 0
    core_path = os.path.join(data_dir, "memes_core.json")
    try:
        with open(core_path, "r", encoding="utf-8") as f:
            core = json.load(f)
        core = core.get("core", []) if isinstance(core, dict) else (core or [])
        for m in core:
            if isinstance(m, dict) and m.get("name") and m["name"] not in seen:
                seen.add(m["name"])
                memes.append(m)
                core_added += 1
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("核心梗合并失败: %s", e)
    counts["core"] = core_added

    # 热搜榜里大量是新闻标题，不是梗：群里没人会把「某队夺冠」当梗接。
    # 标成 news 后不进轮换池，只留在文件里备查
    counts["news"] = _tag_news(memes)

    if enrich:
        counts["enriched"] = _enrich_with_llm(memes)

    payload = {"updated_at": time.time(), "total_count": len(memes), "memes": memes}

    # 只剩几十条多半是数据源挂了。旧版无条件整体覆盖，一次失败就把上千条冲没且无法恢复
    old_count = 0
    try:
        with open(output, "r", encoding="utf-8") as f:
            old = json.load(f)
        old_count = len(old.get("memes") or []) if isinstance(old, dict) else len(old or [])
    except Exception:
        old_count = 0

    logger.info("本次结果 %d 条（原库 %d 条）；来源明细 %s", len(memes), old_count,
                ", ".join(f"{k}={v}" for k, v in counts.items()))

    if dry_run:
        old_names = set()
        try:
            with open(output, "r", encoding="utf-8") as f:
                old = json.load(f)
            old_names = {str(m.get("name")) for m in (old.get("memes") or []) if isinstance(m, dict)}
        except Exception:
            pass
        new_names = {m["name"] for m in memes}
        logger.info("[dry-run] 未写盘。新增 %d 条，消失 %d 条，目标 %s",
                    len(new_names - old_names), len(old_names - new_names), output)
        # 让调用方（含测试）能直接看到会不会被保护机制拦下
        payload["dry_run"] = True
        payload["would_abort"] = _should_abort(old_count, len(memes))
        return payload

    if _should_abort(old_count, len(memes)):
        logger.error("同步结果只有 %d 条（原有 %d），疑似数据源异常，已中止，保留旧词库",
                     len(memes), old_count)
        # 返回 None 而不是 {"total_count": old, "memes": []}：后者谁拿去落盘就是清库
        return None

    os.makedirs(data_dir, exist_ok=True)
    tmp = f"{output}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    # 旧版先把正式文件 rename 成 .bak 再顶 tmp：两步之间文件短暂不存在，
    # 进程恰好死在这中间词库就整体消失。先拷备份，再单次 os.replace。
    try:
        if os.path.exists(output):
            shutil.copy2(output, output + ".bak")
    except OSError:
        pass
    os.replace(tmp, output)
    logger.info("词库同步完成，已落盘 %s，总词数 %d", output, len(memes))
    return payload


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同步网络热梗/流行词库到 hot_words.json")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                    help=f"输出文件路径（默认 {DEFAULT_OUTPUT}）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落盘，也不调 LLM 补释义")
    ap.add_argument("--from-file", metavar="DIR",
                    help="用本地快照目录代替网络（离线演练）")
    ap.add_argument("--no-enrich", action="store_true", help="跳过 LLM 补释义")
    ap.add_argument("-q", "--quiet", action="store_true", help="只打警告和错误")
    args = ap.parse_args(argv)

    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

    fetch = local_fetcher(args.from_file) if args.from_file else None
    payload = sync_memes(output=args.output, dry_run=args.dry_run, fetch=fetch,
                         enrich=False if (args.no_enrich or args.dry_run) else None)
    if payload is None:
        return 2
    if args.dry_run:
        print(json.dumps({"total_count": payload["total_count"],
                          "would_abort": payload.get("would_abort", False),
                          "output": os.path.abspath(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
