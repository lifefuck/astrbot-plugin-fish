# astrbot_plugin_fish

QQ 群聊拟真插件本体。完整介绍、安装、配置项说明见仓库根目录的 [README](../../README.md)。

## 目录

```
main.py             插件入口：事件接入、接话判定、出口加工、拆条、上报
fish_core/          各功能模块，互不 import main
  prompts.py        人设与提示词组装
  splitter.py       长回复拆条，保护链接 / 英文词 / ZWJ emoji
  composer.py       出口清洗：表情标签、PASS、复述式开场
  fish_memory.py    长期记忆引擎（抽取 + 检索 + 衰减）
  meme_catalog.py   表情包索引与选图
  meme_wiki.py      梗百科查询（带缓存与递退）
  song.py           点歌：搜索、下载、转码、发语音
  vision.py         识图（Gemini，失败静默降级）
  style_learner.py  群表达样例学习
  safety_guard.py   护栏与 PASS 判定
  panel.py          控制台后端接口
pages/console/      控制台前端，单文件无构建
scripts/            自检、离线注入、隐私扫描
data/               运行时数据（记忆库、样例池、prompt 存档），不进版本库
```

## 运行时依赖

- 插件不往 AstrBot 源码目录写任何东西，逻辑全在插件目录内。
- 表情包素材是可选的：把目录放到 `fish_core/meme-pack/`，索引会自动载入；没有就只发文字。仓库不带素材。
- 点歌需要 `ffmpeg` 和 `yt-dlp`（或系统里的同类工具）在 PATH 上。
- 识图需要 Gemini API key，从环境变量或 AstrBot 面板配置读，不落盘到仓库。

## 开发

```bash
python3 -m compileall -q astrbot_plugin/astrbot_plugin_fish     # 语法
python3 scripts/check_plugin.py                                 # 结构自检
python3 scripts/privacy_scan.py --all --audit                   # 隐私扫描
```

`scripts/smoke_check.py` 会在本地跑一遍关键模块的冒烟用例，不连群、不调模型。
