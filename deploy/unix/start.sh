#!/usr/bin/env bash
# deploy/unix/start.sh —— 启动已安装的团队版（默认 /opt/trace，可用 TRACE_DIR 覆盖）。
# 未安装请先按《部署手册.md》解压部署包并运行 install.sh。
set -euo pipefail
DIR="${TRACE_DIR:-/opt/trace}"
[ -f "$DIR/run.sh" ] || { echo "未找到 $DIR/run.sh：请先解压部署包并运行 install.sh（或设置 TRACE_DIR）" >&2; exit 1; }
exec bash "$DIR/run.sh" "$@"
