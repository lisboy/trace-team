#!/usr/bin/env bash
# =============================================================================
# run.sh -- 循迹 Trace 团队版 启动
#   用法: bash run.sh            （前台，Ctrl+C 停止）
#         bash run.sh --daemon   （后台，日志 <数据目录>/logs/run.out）
#
# 会先启动包内 MariaDB（只有它没在跑时才启动），等数据库就绪后再起 Web 服务。
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"

if [ -t 1 ]; then RED=$'\e[31m'; YEL=$'\e[33m'; CYN=$'\e[36m'; GRN=$'\e[32m'; RST=$'\e[0m'; else RED=; YEL=; CYN=; GRN=; RST=; fi
die() { printf "%s[err]%s %s\n" "$RED" "$RST" "$*"; exit 1; }

[ -x "$PKG_DIR/runtime/python/bin/python3" ] || die "缺少内置运行时，请先运行: bash install.sh"
[ -f "$PKG_DIR/.env" ] || die "缺少 .env，请先运行: bash install.sh"

# shellcheck source=scripts/db.sh
source "$PKG_DIR/scripts/db.sh"

export GA_MODE="${GA_MODE:-lite}"
# 应用进程据此找到 scripts/db.sh：设置页迁移数据目录时要停/起内置 MariaDB
export GA_PKG_DIR="$PKG_DIR"
export PYTHONPATH="$RUNTIME/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1

# 离线补装的 git/svn（vendor/vcs）。默认【系统优先】：
#   系统 PATH 里已有 git/svn 就一直用系统的，包内的只补缺位（追加到 PATH 末尾）；
#   只有当 vendor/vcs/prefer-bundled 存在时才反过来强制用包内的。
# 这样服务器自己装的、发行版自带的、我们补装的可以共存，互不覆盖。
VCS_HOME="$PKG_DIR/vendor/vcs"
VCS_BIN="$VCS_HOME/bin"
if [ -d "$VCS_BIN" ]; then
  if [ -f "$VCS_HOME/prefer-bundled" ]; then
    export PATH="$VCS_BIN:$PATH"
  else
    case ":$PATH:" in *":$VCS_BIN:"*) ;; *) export PATH="$PATH:$VCS_BIN" ;; esac
  fi
  # 只有实际会执行包内 git 时才指内置 CA（conda 版 openssl 只认自己的 ssl 目录）。
  # 系统 git 自带证书配置，不去动它。
  VCS_CA="$VCS_HOME/ssl/cacert.pem"
  if [ -f "$VCS_CA" ] && [ "$(command -v git 2>/dev/null || true)" = "$VCS_BIN/git" ]; then
    export GIT_SSL_CAINFO="${GIT_SSL_CAINFO:-$VCS_CA}"
    export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$VCS_CA}"
  fi
fi

if [ -d "$VCS_BIN" ]; then
  printf "%s[run]%s git=%s  svn=%s\n" "$CYN" "$RST" \
    "$(command -v git 2>/dev/null || echo '未安装')" "$(command -v svn 2>/dev/null || echo '未安装')"
fi

mkdir -p "$LOG_DIR" "$DATA_DIR/tmp" 2>/dev/null || true

DAEMON=0
[ "${1:-}" = "--daemon" ] && DAEMON=1

DB_STARTED=0
db_ping || DB_STARTED=1
printf "%s[run]%s 启动内置 MariaDB（127.0.0.1:%s）…\n" "$CYN" "$RST" "$DB_PORT"
if ! db_start; then
  printf "%s[err]%s MariaDB 启动失败，最近日志：\n" "$RED" "$RST"
  tail -n 30 "$LOG_DIR/mariadb.err" 2>/dev/null || tail -n 30 "$LOG_DIR/mariadb.out" 2>/dev/null || true
  die "数据库没起来（数据目录: $DB_DIR）。请按日志排查。"
fi
printf "%s[run]%s 数据库就绪\n" "$GRN" "$RST"

HTTP_PORT="$(env_get GA_PORT)"; HTTP_PORT="${HTTP_PORT:-18888}"
HTTP_HOST="$(env_get GA_HOST)"; HTTP_HOST="${HTTP_HOST:-127.0.0.1}"

if [ "$DAEMON" = "1" ]; then
  OUT="$LOG_DIR/run.out"
  PIDF_APP="$PKG_DIR/.run.pid"
  if [ -f "$PIDF_APP" ] && kill -0 "$(cat "$PIDF_APP" 2>/dev/null)" 2>/dev/null; then
    printf "%s[run]%s 已在运行 (PID %s)。如需重启先 bash stop.sh\n" "$YEL" "$RST" "$(cat "$PIDF_APP")"
    exit 0
  fi
  nohup "$PY" -m game_agent.main >>"$OUT" 2>&1 &
  echo $! > "$PIDF_APP"
  printf "%s[run]%s 后台启动 PID %s，日志: %s\n" "$GRN" "$RST" "$(cat "$PIDF_APP")" "$OUT"
  # 端口被占用会自动顺延：从日志里读实际地址
  URL=""
  for _ in $(seq 1 40); do
    URL="$(grep -Eo 'Uvicorn running on http://[0-9.]+:[0-9]+' "$OUT" 2>/dev/null | tail -1 | grep -Eo 'http://[0-9.]+:[0-9]+' || true)"
    [ -n "$URL" ] && break
    if ! kill -0 "$(cat "$PIDF_APP" 2>/dev/null)" 2>/dev/null; then
      printf "%s[err]%s 进程已退出，日志末尾：\n" "$RED" "$RST"
      tail -n 30 "$OUT" 2>/dev/null || true
      exit 1
    fi
    sleep 0.5
  done
  if [ -n "$URL" ]; then
    printf "%s[run]%s 已监听: %s/  （首次登录 admin / 666666）\n" "$GRN" "$RST" "$URL"
  else
    printf "%s[run]%s 启动中… 默认 http://%s:%s/（端口被占用会顺延，见日志 %s）\n" "$YEL" "$RST" "$HTTP_HOST" "$HTTP_PORT" "$OUT"
  fi
else
  printf "%s[run]%s 启动中… http://%s:%s/（被占用会自动顺延，见启动日志）（Ctrl+C 停止）\n" "$CYN" "$RST" "$HTTP_HOST" "$HTTP_PORT"
  "$PY" -m game_agent.main
  RC=$?
  # 前台退出时把本次启动的数据库也收掉，避免留下孤儿进程
  [ "$DB_STARTED" = "1" ] && db_stop
  exit $RC
fi
