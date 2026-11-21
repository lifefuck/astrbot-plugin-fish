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
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PLUGIN_DIR/data"
OUT="${FISH_BACKUP_DIR:-$HOME/backups/astrbot-plugin-fish}"
STAMP="$(date +%Y%m%d-%H%M)"
TARGET="$OUT/fish-data-$STAMP.tar.gz"
TMP="$TARGET.part"
# 插件配置在框架的 config 目录里，不在插件目录下
CONFIG_DIR="$(cd "$PLUGIN_DIR/../.." && pwd)/config"
CONFIG_FILE="$CONFIG_DIR/astrbot_plugin_fish_config.json"
PACK_DIR="$PLUGIN_DIR/fish_core/meme-pack"
PACK_MARK="$OUT/.meme-pack.stamp"
mkdir -p "$OUT"
chmod 700 "$OUT" 2>/dev/null || true

SNAP="$(mktemp -d)"
# 中途失败留下的坏包会按时间把好备份挤掉，先清干净
trap 'rm -rf "$SNAP"; rm -f "$TMP"' EXIT

# 直接把 db 打进 tar 可能拷到写了一半的页（WAL 模式尤其），
# 先用 sqlite3 .backup 导出一致快照；没有 sqlite3 就退回普通拷贝
if [ -f "$DATA_DIR/fish_memory.db" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DATA_DIR/fish_memory.db" ".backup '$SNAP/fish_memory.db'"
  else
    cp "$DATA_DIR/fish_memory.db" "$SNAP/fish_memory.db"
  fi
fi

# 先打成未压缩 tar 再 gzip：中途失败时 .part 不会被当成完整包
TARFILE="$OUT/.fish-$STAMP.tar"
tar -cf "$TARFILE" -C "$DATA_DIR" --ignore-failed-read $(cd "$DATA_DIR" && ls *.json 2>/dev/null) 2>/dev/null || true
if [ -f "$SNAP/fish_memory.db" ]; then
  tar -rf "$TARFILE" -C "$SNAP" fish_memory.db
fi
# t2i 渲染模板（小，天天备）
if [ -d "$PLUGIN_DIR/fish_core/data" ]; then
  tar -rf "$TARFILE" -C "$PLUGIN_DIR/fish_core" --ignore-failed-read data 2>/dev/null || true
fi
# 插件配置（含 Pixiv 词条与闸门阈值；包本身 600，不外流）
if [ -f "$CONFIG_FILE" ]; then
  tar -rf "$TARFILE" -C "$CONFIG_DIR" astrbot_plugin_fish_config.json 2>/dev/null || true
fi
gzip -c "$TARFILE" > "$TMP"
rm -f "$TARFILE"

# 能完整读出来才算数
gzip -t "$TMP" && tar -tzf "$TMP" >/dev/null
mv -f "$TMP" "$TARGET"
chmod 600 "$TARGET" 2>/dev/null || true

# 只留最近 7 份
ls -1t "$OUT"/fish-data-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
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
    ls -1t "$OUT"/fish-memes-*.tar.gz 2>/dev/null | tail -n +4 | xargs -r rm -f
    echo "$(date '+%F %T') 表情包已备份：$PACK_TARGET ($(du -h "$PACK_TARGET" | cut -f1))"
  fi
fi
trap - EXIT
