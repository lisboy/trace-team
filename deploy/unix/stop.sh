#!/usr/bin/env bash
# deploy/unix/stop.sh —— 停止已安装的团队版（默认 /opt/trace，可用 TRACE_DIR 覆盖）。
set -euo pipefail
DIR="${TRACE_DIR:-/opt/trace}"
[ -f "$DIR/stop.sh" ] || { echo "未找到 $DIR/stop.sh：请设置 TRACE_DIR 指向安装目录" >&2; exit 1; }
exec bash "$DIR/stop.sh" "$@"
