#!/usr/bin/env bash
# 备份插件运行数据到插件目录之外。
#
# 为什么单独一份：fish_memory.db 是唯一的长期记忆（只增不减），
# style_fewshot.json / favor_state.json / relations.json 都是跑出来的东西，
# 丢了没法重建。原来唯一那条 cron 备的是旧仓库 qq-deepseek-bot 的数据，
# 跟线上这插件毫无关系——等于线上数据一直没有备份。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PLUGIN_DIR/data"
OUT="${FISH_BACKUP_DIR:-$HOME/backups/astrbot-plugin-fish}"
STAMP="$(date +%Y%m%d-%H%M)"
TARGET="$OUT/fish-data-$STAMP.tar.gz"
TMP="$TARGET.part"
mkdir -p "$OUT"

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
gzip -c "$TARFILE" > "$TMP"
rm -f "$TARFILE"

# 能完整读出来才算数
gzip -t "$TMP" && tar -tzf "$TMP" >/dev/null
mv -f "$TMP" "$TARGET"
trap - EXIT

# 只留最近 7 份
ls -1t "$OUT"/fish-data-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
echo "$(date '+%F %T') 备份完成：$TARGET ($(du -h "$TARGET" | cut -f1))"
