# shellcheck shell=bash
# =============================================================================
# db.sh -- install.sh / run.sh / stop.sh 共用：路径解析 + 内置 MariaDB 启停
#
# 数据位置（升级时只替换程序，绝不删这里）：
#   <DATA_DIR>/mysql/          MariaDB 数据目录（datadir）
#   <DATA_DIR>/mysql_port      实际使用的数据库端口
#   <DATA_DIR>/mysql_password  数据库账号 trace 的密码
# =============================================================================

: "${PKG_DIR:?PKG_DIR 未设置（db.sh 需被 install.sh/run.sh/stop.sh source）}"

RUNTIME="$PKG_DIR/runtime"
MARIADB="$RUNTIME/mariadb"
PY="$RUNTIME/python/bin/python3"
export LD_LIBRARY_PATH="$MARIADB/lib:$MARIADB/lib/private:${LD_LIBRARY_PATH:-}"
# 包内依赖装在 runtime/site-packages：install.sh/run.sh/stop.sh 调内置 Python 前都要带上
export PYTHONPATH="$RUNTIME/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1

# 读 .env 里的一个值（去掉行尾 \r，兼容在 Windows 上编辑过）
env_get() {
  local key="$1" f="$PKG_DIR/.env" line
  [ -f "$f" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$f" | tail -1 || true)"
  [ -n "$line" ] || return 0
  line="${line#*=}"
  line="${line%$'\r'}"
  line="${line%\"}"; line="${line#\"}"
  printf '%s' "$line"
}

# 读应用写的「数据目录指针」$HOME/.trace_team_location：JSON {"data_dir": "..."} 或纯路径。
# 设置页「迁移数据目录」写的是这个文件（以及 .env）；脚本必须认同一份，
# 否则重启后脚本还在旧目录起 MariaDB，应用却按新目录找数据。
env_pointer_dir() {
  local f="$HOME/.trace_team_location" raw
  [ -f "$f" ] || return 0
  raw="$(head -c 4096 "$f" 2>/dev/null | tr -d '\r\n')"
  case "$raw" in
    \{*) printf '%s' "$raw" | sed -n 's/.*"data_dir"[[:space:]]*:[[:space:]]*"\(.*\)".*/\1/p' ;;
    *)   printf '%s' "$raw" ;;
  esac
}

# 数据目录优先级：环境变量 > .env > 指针文件 > ~/.trace-team
GA_DATA_DIR_RESOLVED="${GA_DATA_DIR:-$(env_get GA_DATA_DIR)}"
if [ -z "$GA_DATA_DIR_RESOLVED" ]; then
  GA_DATA_DIR_RESOLVED="$(env_pointer_dir)"
fi
GA_DATA_DIR_RESOLVED="${GA_DATA_DIR_RESOLVED:-$HOME/.trace-team}"
mkdir -p "$GA_DATA_DIR_RESOLVED" 2>/dev/null || true
DATA_DIR="$(cd "$GA_DATA_DIR_RESOLVED" 2>/dev/null && pwd || printf '%s' "$GA_DATA_DIR_RESOLVED")"

DB_DIR="$DATA_DIR/mysql"
DB_PORT_FILE="$DATA_DIR/mysql_port"
DB_PASS_FILE="$DATA_DIR/mysql_password"
CNF="$DB_DIR/my.cnf"
SOCK="$DB_DIR/mysqld.sock"
PIDF="$DB_DIR/mysqld.pid"
LOG_DIR="$DATA_DIR/logs"

DB_PORT="$(cat "$DB_PORT_FILE" 2>/dev/null || true)"
[ -n "${DB_PORT:-}" ] || DB_PORT="${GA_DB_PORT:-33306}"
DB_USER="trace"
DB_NAME="trace"
DB_PASS="$(cat "$DB_PASS_FILE" 2>/dev/null || true)"

DB_URL="mysql+aiomysql://${DB_USER}:${DB_PASS}@127.0.0.1:${DB_PORT}/${DB_NAME}?charset=utf8mb4"

db_write_cnf() {
  local user_line=""
  # mariadbd 默认拒绝以 root 运行；确实用 root 部署时显式放行（README 建议普通账号）
  [ "$(id -u)" -eq 0 ] && user_line="user=root"
  mkdir -p "$DB_DIR" "$LOG_DIR"
  cat > "$CNF" <<EOF
[mysqld]
basedir=$MARIADB
datadir=$DB_DIR
port=$DB_PORT
bind-address=127.0.0.1
socket=$SOCK
pid-file=$PIDF
log-error=$LOG_DIR/mariadb.err
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
skip-name-resolve
max_connections=200
$user_line

[client]
socket=$SOCK
port=$DB_PORT
EOF
}

db_initialized() { [ -d "$DB_DIR/mysql" ]; }

db_init() {
  local installer="$MARIADB/bin/mariadb-install-db"
  [ -x "$installer" ] || installer="$MARIADB/scripts/mysql_install_db"
  [ -x "$installer" ] || { echo "找不到 mariadb-install-db（包不完整）"; return 1; }
  db_write_cnf
  "$installer" --defaults-file="$CNF" --basedir="$MARIADB" --datadir="$DB_DIR" \
      --auth-root-authentication-method=normal --skip-test-db
}

db_ping() {
  "$PY" - "$DB_PORT" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
PY
}

db_start() {
  db_ping && return 0
  db_write_cnf
  mkdir -p "$LOG_DIR"
  nohup "$MARIADB/bin/mariadbd" --defaults-file="$CNF" >>"$LOG_DIR/mariadb.out" 2>&1 &
  local i
  for i in $(seq 1 120); do
    db_ping && return 0
    sleep 0.5
  done
  return 1
}

db_stop() {
  local pid
  pid="$(cat "$PIDF" 2>/dev/null || true)"
  [ -n "${pid:-}" ] || return 0
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 40); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
}

# 选定未被占用的数据库端口（首次安装用；已有 mysql_port 时不动）
db_pick_port() {
  local p="$1"
  "$PY" - "$p" <<'PY'
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 200):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        s.close()
    print(port)
    break
else:
    raise SystemExit(1)
PY
}
