#!/usr/bin/env bash
# =============================================================================
# backup.sh -- 备份循迹团队版数据（数据库一致性快照 + 上传文件 + 项目工作区）
#
# 用法:
#   bash scripts/backup.sh                    # 备份到 <数据目录>/backups
#   bash scripts/backup.sh --out /mnt/backup  # 备份到别的盘
#   bash scripts/backup.sh --no-projects      # 不打包项目工作区（体积大时用）
#
# 产物:
#   db-<时间>.sql.gz        数据库逻辑备份（InnoDB 事务一致性，不用停服务）
#   files-<时间>.tar.gz     chat_uploads / uploads / 数据库端口与密码文件
#   env-<时间>.bak          .env（含 GA_SECRET_KEY，恢复登录态需要）
#   projects-<时间>.tar.gz  项目工作区（--no-projects 时跳过）
#
# 恢复见包内《部署手册.md》「备份与恢复」。
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PKG_DIR"

if [ -t 1 ]; then RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYN=$'\e[36m'; RST=$'\e[0m'; else RED=; GRN=; YEL=; CYN=; RST=; fi
info() { printf "%s[backup]%s %s\n" "$CYN" "$RST" "$*"; }
ok()   { printf "%s[backup]%s %s\n" "$GRN" "$RST" "$*"; }
warn() { printf "%s[backup]%s %s\n" "$YEL" "$RST" "$*"; }
die()  { printf "%s[backup][x]%s %s\n" "$RED" "$RST" "$*"; exit 1; }

[ -f "$PKG_DIR/.env" ] || die "缺少 .env，请先运行: bash install.sh"
# shellcheck source=scripts/db.sh
source "$PKG_DIR/scripts/db.sh"

OUT_DIR="$DATA_DIR/backups"
WITH_PROJECTS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --out) shift; OUT_DIR="${1:-}" ;;
    --no-projects) WITH_PROJECTS=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) warn "忽略未知参数: $1" ;;
  esac
  shift
done
[ -n "$OUT_DIR" ] || die "--out 不能为空"
mkdir -p "$OUT_DIR" || die "无法创建备份目录: $OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="$MARIADB/bin/mariadb-dump"
[ -x "$DUMP" ] || DUMP="$MARIADB/bin/mysqldump"
[ -x "$DUMP" ] || die "包内缺少 mariadb-dump（包不完整，请重新解压）"

STARTED_DB=0
if ! db_ping; then
  info "数据库未运行，临时启动（备份完成后按原状态停回）…"
  db_start || die "内置 MariaDB 启动失败，无法做一致性备份"
  STARTED_DB=1
fi

DB_OUT="$OUT_DIR/db-$STAMP.sql.gz"
info "导出数据库（一致性快照）…"
if ! MYSQL_PWD="$DB_PASS" "$DUMP" --defaults-file="$CNF" -u "$DB_USER" \
      --single-transaction --quick --routines --triggers --events \
      --databases "$DB_NAME" 2>"$OUT_DIR/.dump.err" | gzip -c > "$DB_OUT"; then
  rm -f "$DB_OUT"
  tail -n 20 "$OUT_DIR/.dump.err" 2>/dev/null || true
  [ "$STARTED_DB" = "1" ] && db_stop
  die "数据库导出失败"
fi
rm -f "$OUT_DIR/.dump.err"
ok "数据库 -> $DB_OUT（$(du -h "$DB_OUT" | awk '{print $1}')）"

FILES_OUT="$OUT_DIR/files-$STAMP.tar.gz"
ITEMS=()
for d in chat_uploads uploads; do
  [ -e "$DATA_DIR/$d" ] && ITEMS+=("$d")
done
for f in mysql_password mysql_port; do
  [ -e "$DATA_DIR/$f" ] && ITEMS+=("$f")
done
if [ "${#ITEMS[@]}" -gt 0 ]; then
  tar -czf "$FILES_OUT" -C "$DATA_DIR" "${ITEMS[@]}" || die "上传文件打包失败"
  ok "上传/凭证 -> $FILES_OUT"
else
  warn "没有 chat_uploads / uploads，跳过文件包"
fi

ENV_OUT="$OUT_DIR/env-$STAMP.bak"
cp "$PKG_DIR/.env" "$ENV_OUT" && chmod 600 "$ENV_OUT" && ok ".env -> $ENV_OUT"

if [ "$WITH_PROJECTS" = "1" ] && [ -d "$DATA_DIR/projects" ]; then
  PROJ_OUT="$OUT_DIR/projects-$STAMP.tar.gz"
  info "打包项目工作区（大，可用 --no-projects 跳过）…"
  tar -czf "$PROJ_OUT" -C "$DATA_DIR" projects || die "项目工作区打包失败"
  ok "项目工作区 -> $PROJ_OUT（$(du -h "$PROJ_OUT" | awk '{print $1}')）"
fi

[ "$STARTED_DB" = "1" ] && db_stop

echo
printf "%s备份完成 -> %s%s\n" "$GRN" "$OUT_DIR" "$RST"
echo "  恢复方法见《部署手册.md》「备份与恢复」；请把备份目录同步到另一台机器/对象存储。"
