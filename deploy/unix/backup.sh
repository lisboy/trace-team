#!/usr/bin/env bash
# deploy/unix/backup.sh —— 备份已安装的团队版数据（默认 /opt/trace，可用 TRACE_DIR 覆盖）。
# 数据库走事务一致性快照，不用停服务；详见包内《部署手册.md》。
set -euo pipefail
DIR="${TRACE_DIR:-/opt/trace}"
[ -f "$DIR/scripts/backup.sh" ] || { echo "未找到 $DIR/scripts/backup.sh：请设置 TRACE_DIR 指向安装目录" >&2; exit 1; }
exec bash "$DIR/scripts/backup.sh" "$@"
