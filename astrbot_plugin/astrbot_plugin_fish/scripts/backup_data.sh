#!/usr/bin/env bash
# 备份插件运行数据到插件目录之外。
#
# 为什么单独一份：fish_memory.db 是唯一的长期记忆（只增不减），
# style_fewshot.json / favor_state.json / relations.json 都是跑出来的东西，
# 丢了没法重建。原来唯一那条 cron 备的是旧仓库 qq-deepseek-bot 的数据，
# 跟线上这插件毫无关系——等于线上数据一直没有备份。
#
# 2026-11 补的窟窿：原来只打 *.json + db，漏了三样跑出来的东西——
#   1) fish_core/data/      t2i 渲染模板，丢了所有文字图卡都渲染不出来
#   2) 插件配置             data/config/astrbot_plugin_fish_config.json（Pixiv 词条、闸门阈值）
#   3) fish_core/meme-pack/ 79MB 表情包本体，丢了梗库索引就指向一堆不存在的文件
# 前两样小，天天备；表情包大，只在 manifest 变动时单独打一份，留 3 份。
#
# 包内结构（恢复前先看这个，别再拿 -C data/ 直接解）：
#   data/*.json              运行态
#   data/fish_memory.db      sqlite .backup 出来的一致快照
#   config/astrbot_plugin_fish_config.json
#   plugin/fish_core/data/   t2i 渲染模板
# 以前全部平铺在包根，`tar -xzf -C data/` 会直接覆盖正在跑的库，
# 旁边的 -wal 还是旧库的，一开就 malformed。恢复走 scripts/restore_data.sh。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PLUGIN_DIR/data"
OUT="${FISH_BACKUP_DIR:-$HOME/backups/astrbot-plugin-fish}"
# 带秒：一分钟内连备份两次（改完配置再改代码）不会互相覆盖
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$OUT/fish-data-$STAMP.tar.gz"
TMP="$TARGET.part"
# 插件配置在框架的 config 目录里，不在插件目录下
CONFIG_DIR="$(cd "$PLUGIN_DIR/../.." && pwd)/config"
CONFIG_FILE="$CONFIG_DIR/astrbot_plugin_fish_config.json"
PACK_DIR="$PLUGIN_DIR/fish_core/meme-pack"
PACK_MARK="$OUT/.meme-pack.stamp"
mkdir -p "$OUT"
chmod 700 "$OUT" 2>/dev/null || true

# 单实例：cron 与手工在同一秒开跑会抢同一个文件名
exec 9>"$OUT/.backup.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') 已有一个备份在跑，本次跳过"
  exit 0
fi

SNAP="$(mktemp -d)"
# 中途失败留下的坏包会按时间把好备份挤掉，先清干净
trap 'rm -rf "$SNAP"; rm -f "$TMP"' EXIT

# 直接把 db 打进 tar 可能拷到写了一半的页（WAL 模式尤其），
# 先用 sqlite3 .backup 导出一致快照；没有 sqlite3 就把 db+wal+shm 一起拷，
# 只拷 .db 会漏掉 WAL 里最近的事务——备份出来的库比重启后少一截
if [ -f "$DATA_DIR/fish_memory.db" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DATA_DIR/fish_memory.db" ".backup '$SNAP/fish_memory.db'"
  else
    echo "$(date '+%F %T') [WARN] 没有 sqlite3，改为 db+wal 直拷（恢复时由 sqlite 回放）" >&2
    cp "$DATA_DIR/fish_memory.db" "$SNAP/fish_memory.db"
    # 注意不能写成 `[ -f x ] && cp`：set -e 下条件为假会让整脚本静默退出，
    # 表现是「一条日志都没有、包也没生成」
    if [ -f "$DATA_DIR/fish_memory.db-wal" ]; then cp "$DATA_DIR/fish_memory.db-wal" "$SNAP/"; fi
    if [ -f "$DATA_DIR/fish_memory.db-shm" ]; then cp "$DATA_DIR/fish_memory.db-shm" "$SNAP/"; fi
  fi
fi

# 先落到带前缀的暂存树，再整棵打包
STAGE="$SNAP/pkg"
mkdir -p "$STAGE/data" "$STAGE/config" "$STAGE/plugin/fish_core"
if [ -f "$SNAP/fish_memory.db" ]; then
  cp -p "$SNAP/fish_memory.db" "$STAGE/data/fish_memory.db"
  if [ -f "$SNAP/fish_memory.db-wal" ]; then cp -p "$SNAP/fish_memory.db-wal" "$STAGE/data/"; fi
  if [ -f "$SNAP/fish_memory.db-shm" ]; then cp -p "$SNAP/fish_memory.db-shm" "$STAGE/data/"; fi
fi
if compgen -G "$DATA_DIR/*.json" >/dev/null; then
  cp -p "$DATA_DIR"/*.json "$STAGE/data/"
fi
# t2i 渲染模板（小，天天备）
if [ -d "$PLUGIN_DIR/fish_core/data" ]; then
  cp -a "$PLUGIN_DIR/fish_core/data" "$STAGE/plugin/fish_core/data"
fi
# 插件配置（含 Pixiv 词条与闸门阈值；包本身 600，不外流）
if [ -f "$CONFIG_FILE" ]; then
  cp -p "$CONFIG_FILE" "$STAGE/config/"
fi

# 缺库直接失败：以前只做 gzip -t，包里有三个空文件也报「备份完成」，
# 等真出事才发现最近 7 份全是空壳
if [ ! -f "$STAGE/data/fish_memory.db" ]; then
  echo "$(date '+%F %T') [FAIL] 快照里没有 fish_memory.db，放弃本次备份" >&2
  exit 1
fi

# 先打成未压缩 tar 再 gzip：中途失败时 .part 不会被当成完整包
TARFILE="$OUT/.fish-$STAMP.tar"
tar -cf "$TARFILE" -C "$STAGE" data config plugin 2>/dev/null || true
gzip -c "$TARFILE" > "$TMP"
rm -f "$TARFILE"

# 能完整读出来、且库里确实有快照，才算数。
# 注意别把 tar 直接管进 grep -q：grep 命中就退出，tar 收 SIGPIPE，
# 在 pipefail 下整条管道返回 141，脚本看着像「什么都没发生」
gzip -t "$TMP"
tar -tzf "$TMP" > "$SNAP/list.txt"
grep -qx 'data/fish_memory.db' "$SNAP/list.txt"
mv -f "$TMP" "$TARGET"
chmod 600 "$TARGET" 2>/dev/null || true

# 只留最近 7 份
# 只轮转自动命名的包（fish-data-YYYYmmdd-HHMM[SS].tar.gz），按名字排=按时间排。
# 原来用 ls -1t 按 mtime：手工留的整目录应急包只要被 touch 过就会被算进 7 份里，
# 排到后面就被删了——应急底稿不该由轮转策略决定生死
datas=$(ls -1 "$OUT"/fish-data-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].tar.gz \
          "$OUT"/fish-data-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].tar.gz 2>/dev/null || true)
[ -n "$datas" ] && printf '%s\n' "$datas" | sort -r | tail -n +8 | xargs -r rm -f
unset datas
echo "$(date '+%F %T') 备份完成：$TARGET ($(du -h "$TARGET" | cut -f1))"

# 表情包：只在 manifest 变动时打一份。79MB 天天打等于每天白占 79MB，
# 而它几乎不变——变了才说明有人加/删了图，也才是真正需要留档的时刻
if [ -f "$PACK_DIR/manifest.json" ]; then
  NOW_MARK="$(stat -c %Y.%s "$PACK_DIR/manifest.json")"
  OLD_MARK="$(cat "$PACK_MARK" 2>/dev/null || echo none)"
  if [ "$NOW_MARK" != "$OLD_MARK" ]; then
    PACK_TARGET="$OUT/fish-memes-$STAMP.tar.gz"
    PACK_TMP="$PACK_TARGET.part"
    tar -czf "$PACK_TMP" -C "$PLUGIN_DIR/fish_core" meme-pack
    gzip -t "$PACK_TMP"
    mv -f "$PACK_TMP" "$PACK_TARGET"
    chmod 600 "$PACK_TARGET" 2>/dev/null || true
    echo "$NOW_MARK" > "$PACK_MARK"
    # 表情包留 3 份就够：它不随时间变，多留没意义
    mems=$(ls -1 "$OUT"/fish-memes-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].tar.gz \
        "$OUT"/fish-memes-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].tar.gz 2>/dev/null || true)
    [ -n "$mems" ] && printf '%s\n' "$mems" | sort -r | tail -n +4 | xargs -r rm -f
    unset mems
    echo "$(date '+%F %T') 表情包已备份：$PACK_TARGET ($(du -h "$PACK_TARGET" | cut -f1))"
  fi
fi
# 正常收尾也要清快照：解除 trap 之前先自己删，
# 否则每次备份都在临时目录留一份含聊天原文的 db 副本，越攒越多
rm -rf "$SNAP"
rm -f "$TMP"
trap - EXIT
