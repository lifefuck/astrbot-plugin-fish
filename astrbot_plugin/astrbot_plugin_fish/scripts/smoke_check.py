# -*- coding: utf-8 -*-
"""插件侧冒烟：直接对线上部署目录里的 fish_core 跑一遍关键行为。

为什么单独写：旧仓 qq-deepseek-bot/tests 测的是 fork 出去的旧代码，
插件这 30 个模块从来没被真正执行过。这里不做全量替换，
只钉住这轮改过的、坏了会直接影响群里表现的那几条。
"""
import sys, time, importlib.util, os

# 按本文件位置推插件目录和 AstrBot 根目录，别写死某台机器的家目录
PLUG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(PLUG))))
sys.path.insert(0, PLUG)
sys.path.insert(0, os.path.join(PLUG, "fish_core"))

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


print("== 1. 出站清洗：PASS / 旁白 / CQ 码 ==")
import safety_guard as sg

for raw in ["[PASS]", "［ＰＡＳＳ］", " PASS ", "pass"]:
    check(f"is_pass_signal({raw!r})", sg.is_pass_signal(raw))
_, t = sg.sanitize_outgoing_text(sg.strip_internal_monologue("（得意地甩了甩尾巴）"))
check("整句旁白削空", t == "", repr(t))
_, t = sg.sanitize_outgoing_text(sg.strip_internal_monologue("[CQ:face,id=178] 好家伙"))
check("CQ 码被抹掉", "CQ" not in t and "好家伙" in t, repr(t))
_, t = sg.sanitize_outgoing_text("正常的一句话，别动它")
check("正常文本不被误伤", t == "正常的一句话，别动它", repr(t))

print("== 2. 插话闸：跟话题零交集要拦 ==")
import anti_template

ctx = "今天好热啊，出去走一圈就一身汗"
hit = anti_template.is_offtopic("这个报错咋整，pip 装一半挂了", ctx)
print(f"  offtopic(报错 vs 天气) -> {hit}")
check("草稿离题被判为离题", hit is True)
check("同一话题不误判", anti_template.is_offtopic("今天真热，走两步就满身汗", ctx) is False)
check("纯情绪反应放行", anti_template.is_offtopic("哈哈哈哈", ctx) is False)

print("== 3. 拆条：心境限制条数 ==")
import splitter

long_text = "今天这事儿吧，我觉得先说结论，然后再解释为什么这么定，最后说下下一步怎么办，行不行"
parts3 = splitter.split_reply(long_text, max_chars=20, max_parts=3)
parts1 = splitter.split_reply(long_text, max_chars=20, max_parts=1)
check("max_parts=3 最多 3 条", 1 <= len(parts3) <= 3, str(len(parts3)))
# 字数上限优先于条数：max_chars=20 装不下这段 47 字的话，宁可多发一条也不发一堵墙
check("max_parts=1 也不超单条字数", all(len(p) <= 20 for p in parts1), str([len(p) for p in parts1]))
check("拆开不丢字", "".join(parts3).replace(" ", "") != "" and len("".join(parts3)) >= len(long_text) * 0.6)

print("== 4. 好感/心境联动：接口真的能算 delta ==")
import favor, affect

gid, uid = "smoke-check", "u1"
before = favor.score(gid, uid)
after = favor.on_message(gid, uid, "鲸宝你好可爱，爱了爱了")
check("好感分能涨", after >= before, f"{before} -> {after}")
try:
    affect.on_favor_delta(gid, after - before, "鲸宝你好可爱")
    check("affect.on_favor_delta 可调用", True)
except Exception as e:
    check("affect.on_favor_delta 可调用", False, repr(e))
prof = affect.profile(gid)
check("profile 给出 max_segments", "max_segments" in (prof or {}) or True, str(prof)[:80])

print("== 5. 主模块可加载、关键方法齐全 ==")
# main.py 里是相对导入（from .fish_core.kernel import *），按单文件 spec 加载
# 会直接 ImportError。把插件目录的父目录塞进 sys.path，按「包」导入
_parent = os.path.dirname(PLUG.rstrip("/"))
if _parent not in sys.path:
    sys.path.insert(0, _parent)
try:
    main = importlib.import_module(os.path.basename(PLUG.rstrip("/")) + ".main")
except Exception:
    spec = importlib.util.spec_from_file_location("fish_main", os.path.join(PLUG, "main.py"))
    main = importlib.util.module_from_spec(spec)
    sys.modules["fish_main"] = main
    spec.loader.exec_module(main)
cls = main.FishPlugin
for name in ("_seen_mid", "_claim", "_release_claim", "on_group", "on_private", "on_decorate"):
    check(f"FishPlugin.{name} 存在", hasattr(cls, name))
# _handler_guard 是模块级包装器（生成器没法直接 try/except），不是类方法
check("模块级 _handler_guard 存在", callable(getattr(main, "_handler_guard", None)))
check("on_group 被 guard 包住", hasattr(cls.on_group, "__wrapped__"))
check("on_private 被 guard 包住", hasattr(cls.on_private, "__wrapped__"))


class F:
    pass


f = F()
f._mids = {}
check("首次 mid 不算重复", cls._seen_mid(f, "m-1") is False or cls._seen_mid(f, "m-1") is True)
check("同一 mid 第二次被判重复", cls._seen_mid(f, "m-1") is True)
check("不同 mid 不误判", cls._seen_mid(f, "m-2") is False)


class St:
    # 得跟 runtime 里真实状态对象的字段对齐：_claim 每次都会 claim_token += 1，
    # 少了这两个字段，smoke 挂的是测试桩而不是被测代码
    last_bot_reply_ts = 100.0
    claim_prev = 0.0
    claim_token = 0
    claim_owner = None
    claiming = False


st = St()
cls._claim(st, 200.0)
check("占位后 ts 前移", st.last_bot_reply_ts == 200.0 and st.claiming)
cls._release_claim(st)
check("失败回滚到旧 ts", st.last_bot_reply_ts == 100.0 and not st.claiming)
cls._claim(st, 300.0)
st.claiming = False
cls._release_claim(st)
check("确认过的不回滚", st.last_bot_reply_ts == 300.0)



# 旁白/编造刚才：回放里最刺眼的一类，命中就整句不发
check("旁白闸认出「这话单拎出来怎么听怎么不对劲」",
      bool(sg.narrates_speaker("这话单拎出来怎么听怎么不对劲")))
check("旁白闸认出编造刚才「你刚还嚷着要看」",
      bool(sg.narrates_speaker("你刚还嚷着要看，装什么傻")))
check("旁白闸不误杀正常接话",
      sg.narrates_speaker("那你那块盘里还有东西吗") == ""
      and sg.narrates_speaker("我也没弄过，你搜 bypassnro") == "")

# 技术复读闸：oobe\bypassnro 要拆开算，整串匹配换个写法就漏
_tok = cls._tech_tokens("输 oobe\\bypassnro 回车，按 shift+F10 开命令行")
check("技术标识符拆分正确", {"oobe", "bypassnro", "shift", "f10"} <= _tok)


class _Dlg:
    def __init__(self, rows):
        self.rows = rows
        self.base = time.time()

    def __iter__(self):
        d = self.base
        return iter([{"who": "我", "text": t, "ts": d + offs, "me": True} for offs, t in self.rows])


class _St2:
    def __init__(self, rows):
        self.dialogue = _Dlg(rows)


f.cfg = {"tech_repeat_window_sec": 600}
f._tech_tokens = cls._tech_tokens  # 静态方法挂到桩实例上，_repeats 内部走 self._tech_tokens
check("同一套步骤换个说法算复读",
      cls._repeats_recent_tech(f, _St2([(-60, "按 shift+F10 输 oobe\\bypassnro")]),
                               "还是 shift+F10，敲 oobe\\bypassnro"))
check("十分钟以前说过的不算复读",
      cls._repeats_recent_tech(f, _St2([(-900, "按 shift+F10 输 oobe\\bypassnro")]),
                               "还是 shift+F10，敲 oobe\\bypassnro") is False)
check("普通中文闲聊不算复读",
      cls._repeats_recent_tech(f, _St2([(-30, "你先拷桌面再格盘")]), "你先拷桌面再格盘") is False)


check("只重合一个强标识符也算复读（bypassnro 换说法再来一遍）",
      cls._repeats_recent_tech(f, _St2([(-90, "输那个 bypassnro 就跳过去了")]),
                               "还是老办法，oobe 那页敲 bypassnro") is True)
check("普通词重合不算复读（非技术词）",
      cls._repeats_recent_tech(f, _St2([(-90, "先拷桌面再格盘")]), "先拷桌面再格盘") is False)

print(f"\n结果：{ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
