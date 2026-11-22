# -*- coding: utf-8 -*-
"""行为探针：拆 main.py 前后各跑一次，逐项对照，任何一项不一样就说明改出事了。

只打纯逻辑：判定、提示词拼装、拆句、兜底池、配置默认值。
不连网、不发消息、不碰 data/ 里的线上状态。

用法：
    python3 scripts/probe_behavior.py            # 打印 JSON
    python3 scripts/probe_behavior.py > base.json
"""
import hashlib
import re
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(ROOT))

try:  # 以包的方式导入（拆文件后必须这样，主模块和 Mixin 都要能互相看见）
    from astrbot_plugin_fish import main as M  # noqa: E402
    from astrbot_plugin_fish.fish_core import composer, poke_context, prompts  # noqa: E402
    from astrbot_plugin_fish.fish_core import speak_decision as sd  # noqa: E402
    from astrbot_plugin_fish.fish_core import splitter  # noqa: E402
except ImportError:  # 老的单文件结构
    import main as M  # noqa: E402
    from fish_core import composer, poke_context, prompts, speak_decision as sd, splitter  # noqa: E402


def _fix_time(s: str) -> str:
    """提示词末尾那句「当前真实时间」用的是系统时间，探针里必须抹掉，
    否则每次跑都变，对照就没意义。"""
    return re.sub(r"【当前真实时间】.*", "【当前真实时间】<抹掉>", s)


def sha(s: str) -> str:
    s = _fix_time(s)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# 拆分后最容易出的事故是「某个顶层定义没跟着搬走」：import 不报错，
# 只有跑到那行才 NameError（GroupState 就这么丢过一次，靠线上 Traceback 才发现）。
# 把 main 命名空间里必须存在的顶层名钉死，探针跑一遍就能发现
_MUST_EXIST = ("GroupState", "_ImgRef", "_UNSET", "DEFAULTS", "PLUGIN_DIR", "DATA_DIR",
               "MEME_PATH", "_TECH_TOKEN_RE", "_BUILTIN_COMMANDS", "flatten_conf")


def check_exports():
    missing = [n for n in _MUST_EXIST if not hasattr(M, n)]
    # 只查「名字在不在」抓不到装饰器丢失：@dataclass 那行被吞掉时，
    # 类是建出来了，但字段全是 Field 对象，跑到 .append 才炸。这里直接验实例
    st = M.GroupState()
    for attr in ("msgs", "dialogue", "recent_bot"):
        if not hasattr(getattr(st, attr, None), "append"):
            missing.append(f"GroupState.{attr}")
    return missing


def out():
    r = {}
    r["exports.missing"] = check_exports()

    # ---- 提示词：正文与拼装 ----
    r["prompt.system_prompt"] = sha(prompts.SYSTEM_PROMPT)
    r["prompt.system_len"] = len(prompts.SYSTEM_PROMPT)
    sys_g = composer.build_system_prompt(prompts.SYSTEM_PROMPT, "大肥鱼", "10001", "123456", False)
    sys_p = composer.build_system_prompt(prompts.SYSTEM_PROMPT, "大肥鱼", "10001", "private", True)
    r["prompt.system_group"] = sha(sys_g)
    r["prompt.system_private"] = sha(sys_p)
    r["prompt.system_group_len"] = len(sys_g)

    timeline = ["甲：今天加班到九点", "乙：我也刚下班", "甲：明天还得去"]
    for name, kw in {
        "plain": dict(),
        "at": dict(is_at=True),
        "thin": dict(timeline=["甲：在吗"]),
    }.items():
        up = composer.build_user_prompt(
            timeline=kw.get("timeline", timeline),
            latest_line="甲：今天好累",
            sender_nick="甲", sender_uid="20002",
            memory_lines=["乙（昨天）说过：我在减肥"],
            affect_parts=["他跟你熟"],
            style_hint="「买了没」→「买了 还在路上」",
            anti_repeat_hint=composer.anti_repeat_hint(["刚睡醒"]),
            games_kb="", is_at=kw.get("is_at", False), is_private=False,
            now=1700000000.0,
        )
        r[f"prompt.user_{name}"] = sha(up)
        r[f"prompt.user_{name}_len"] = len(up)

    r["prompt.timeline_thin"] = composer.timeline_is_thin(timeline, "今天好累")
    r["prompt.anti_repeat"] = sha(composer.anti_repeat_hint(["我不困", "你才困", "睡了"]))
    for t in ("今天好累", "你可以去死", "这歌好听 [搜索:周杰伦]", "看看这个 [看图:蔚蓝档案 白子]"):
        r[f"prompt.sanitize.{t}"] = composer.sanitize_reply(t)
        r[f"prompt.games_kb.{t}"] = composer.needs_games_kb(t)

    # ---- 开口判定：栅格 ----
    grid = []
    for mode in ("被@", "接话", "裸@", ""):
        for prio in (0, 2, 3):
            for replied in (False, True):
                for silent in (0.0, time.time() + 60):
                    grid.append(dict(
                        group_id="123456", now=1_700_000_000.0, mode=mode, priority=prio,
                        is_at=(mode in ("被@", "裸@")), is_reply=replied,
                        replied_recently=replied, ambient_gate_blocked=replied,
                        silent_until=silent, last_bot_reply_ts=1_699_999_990.0,
                        pending_interject_ts=0.0, unanswered_interjects=0,
                        last_interject_ts=0.0, interject_times=(),
                        is_follow=False, text_len=8, has_media=False,
                    ))
    res = []
    for g in grid:
        try:
            sig = sd.Signals(**g)
        except TypeError:
            sig = None
        if sig is None:
            continue
        d = sd.decide(sig, {})
        res.append([g["mode"], g["priority"], g["replied_recently"], g["silent_until"] > 0,
                    d.speak, d.reason, d.mode, d.prio])
    r["decide.grid"] = res
    r["decide.n"] = len(res)
    r["decide.speak_true"] = sum(1 for x in res if x[4])

    # ---- 戳一戳 ----
    pk = poke_context.decide([], "20002", 0.0, 1_700_000_000.0, "大肥鱼")
    r["poke.decide"] = {k: (v if not isinstance(v, (list, dict)) else str(v)) for k, v in pk.items()}
    r["poke.fallback_pool"] = list(poke_context.FALLBACK_POOL)
    r["poke.pick_empty"] = poke_context.pick_fallback([])
    r["poke.pick_said"] = poke_context.pick_fallback(["手欠"])
    r["poke.prompt"] = sha(poke_context.build_prompt(pk, "甲", 1, "手欠"))
    r["poke.main_pool_same"] = (getattr(M, "_pick_poke", None) is poke_context.pick_fallback)

    # ---- 拆句 ----
    for t in ("今天好累啊，明天还得去加班，烦死了真的不想动了",
              "第一句。第二句！第三句？第四句，第五句还在后面呢",
              "看这个```python\nprint(1)\n```然后再说两句"):
        r[f"split.{sha(t)}"] = splitter.split_reply(t)

    # ---- main 里的纯函数 ----
    r["fn.search_query"] = M._search_query("帮我搜一下今天的热搜 [搜索:今天热搜]")
    for t in ("/tocken", "memes", "#点歌 晴天", "普通聊天"):
        r[f"fn.builtin.{t}"] = M._is_builtin_cmd(t)
    r["fn.song"] = M._parse_song_request("#点歌 晴天")
    r["fn.tech"] = sorted(M.FishPlugin._tech_tokens("这个 API 的 timeout 设 300 行不行"))
    r["fn.echoes"] = [M.FishPlugin._echoes_hint("你尾巴好粗", "尾巴好粗啊"), M.FishPlugin._echoes_hint("睡了", "我睡了")]
    r["fn.tattle"] = [M._is_tattle_request("他骂我"), M._is_tattle_request("今天天气不错")]
    r["fn.split_forward"] = M.FishPlugin._split_for_forward("甲" * 900)
    r["fn.mem_when"] = [M.FishPlugin._fmt_mem_when(time.time() - 3600),
                        M.FishPlugin._fmt_mem_when(time.time() - 86400 * 3),
                        M.FishPlugin._fmt_mem_when(0)]

    # ---- 配置默认值：schema / 代码 ----
    sch = json.load(open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8-sig"))
    r["cfg.schema_keys"] = len(sch)
    r["cfg.schema"] = {k: sch[k].get("default") for k in sorted(sch)}
    r["cfg.code_defaults"] = {k: v for k, v in sorted(dict(getattr(M, "DEFAULTS", {})).items())}

    return r


def _redact(o):
    """基线文件要进版本库，家目录路径和像 QQ 号的长数字一律打码。

    只在输出侧做，两侧（拆分前/拆分后）同样打码，所以照样能逐项对照。
    """
    if isinstance(o, dict):
        return {k: _redact(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_redact(v) for v in o]
    if isinstance(o, str):
        s = re.sub(r"/home/[^/\s\"']+", "/home/<USER>", o)
        return re.sub(r"(?<!\d)\d{7,11}(?!\d)", "<NUM>", s)
    return o


if __name__ == "__main__":
    print(json.dumps(_redact(out()), ensure_ascii=False, indent=1, sort_keys=True))
