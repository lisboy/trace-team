#!/usr/bin/env bash
# =============================================================================
# stop.sh -- 停止循迹 Trace 团队版（Web 服务 + 本包的内置 MariaDB）
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"
if [ -t 1 ]; then RED=$'\e[31m'; YEL=$'\e[33m'; GRN=$'\e[32m'; RST=$'\e[0m'; else RED=; YEL=; GRN=; RST=; fi

# shellcheck source=scripts/db.sh
source "$PKG_DIR/scripts/db.sh"

STOPPED=0
PIDF_APP="$PKG_DIR/.run.pid"

stop_pid() {
  local pid="$1"
  [ -n "${pid:-}" ] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
}

if [ -f "$PIDF_APP" ]; then
  PID="$(cat "$PIDF_APP" 2>/dev/null || true)"
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    stop_pid "$PID"
    printf "%s[stop]%s 已停止 Web 服务 PID %s\n" "$GRN" "$RST" "$PID"
    STOPPED=1
  fi
  rm -f "$PIDF_APP"
fi

# 兜底：只匹配本目录的运行时路径，不误伤其它安装
if [ -x "$PY" ]; then
  PIDS="$(ps -eo pid=,args= 2>/dev/null | grep -F "$PY -m game_agent.main" | grep -v grep | awk '{print $1}' || true)"
  for p in $PIDS; do
    stop_pid "$p"
    printf "%s[stop]%s 已结束匹配进程 PID %s\n" "$YEL" "$RST" "$p"
    STOPPED=1
  done
fi

if [ -f "$PIDF" ] || db_ping; then
  db_stop
  printf "%s[stop]%s 已停止内置 MariaDB\n" "$GRN" "$RST"
  STOPPED=1
fi

[ "$STOPPED" = "1" ] \
  && printf "%s[stop]%s 完成\n" "$GRN" "$RST" \
  || printf "%s[stop]%s 未发现运行中的循迹\n" "$YEL" "$RST"
