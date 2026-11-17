#!/usr/bin/env bash
# =============================================================================
# install.sh -- 循迹 Trace 团队版 一键安装（在解压目录内运行）
#
# 目标机零依赖：包内自带 Python 运行时、全部 pip 依赖与 MariaDB。
# 本脚本做的事：
#   1. 建好数据目录（默认 ~/.trace-team，可用 --data-dir 指定）
#   2. 首次安装初始化内置 MariaDB；已存在数据库时【只补程序、绝不动数据】
#   3. 生成 .env（数据目录 / 密钥 / 数据库连接串）
#
# 用法:
#   bash install.sh                 # 安装/升级（默认数据目录 ~/.trace-team）
#   bash install.sh --data-dir DIR  # 首次安装时指定数据目录
#   bash install.sh --db-port 33306 # 首次安装时指定数据库端口
#   bash install.sh --no-vcs        # 不碰 git/svn（一键安装版默认会自动补缺）
#
# 一键安装版（文件名带 -with-vcs）包里带了离线 git/svn：本脚本会扫描系统，
# 已有就跳过，缺哪个补哪个；普通包里没有离线 git/svn，只在结尾提示安装方式。
#
# 升级: 把新包解压覆盖到同一目录后重新运行本脚本即可；也可在新目录安装时把
#       --data-dir 指到旧数据目录（.env 里的 GA_DATA_DIR 优先）。
# =============================================================================
set -uo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PKG_DIR"

if [ -t 1 ]; then
  RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYN=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'
else
  RED=; GRN=; YEL=; CYN=; BLD=; RST=
fi
info() { printf "%s[install]%s %s\n" "$CYN" "$RST" "$*"; }
ok()   { printf "%s[ ok ]%s %s\n" "$GRN" "$RST" "$*"; }
warn() { printf "%s[warn]%s %s\n" "$YEL" "$RST" "$*"; }
err()  { printf "%s[ err]%s %s\n" "$RED" "$RST" "$*"; }
die()  { err "$*"; exit 1; }

DATA_DIR_ARG=""; PORT_ARG=""; NO_VCS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --data-dir) shift; DATA_DIR_ARG="${1:-}" ;;
    --db-port) shift; PORT_ARG="${1:-}" ;;
    --no-vcs) NO_VCS=1 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) warn "忽略未知参数: $1" ;;
  esac
  shift
done

[ "$(uname -s)" = "Linux" ] || die "本安装包只支持 Linux"
[ "$(uname -m)" = "x86_64" ] || die "本安装包只支持 x86_64（当前 $(uname -m)）"
[ "$(id -u)" -eq 0 ] && warn "正在用 root 运行。建议用普通账号部署；若坚持 root，数据目录也会归 root 所有。"

# ---------- 0. 包完整性 ----------
[ -x "$PKG_DIR/runtime/python/bin/python3" ] || die "缺少内置 Python 运行时（包不完整，请重新解压）"
[ -x "$PKG_DIR/runtime/mariadb/bin/mariadbd" ] || die "缺少内置 MariaDB（包不完整，请重新解压）"
[ -f "$PKG_DIR/scripts/db.sh" ] || die "缺少 scripts/db.sh（包不完整）"
[ -f "$PKG_DIR/env.template" ] || die "缺少 env.template（包不完整）"

# shellcheck source=scripts/db.sh
source "$PKG_DIR/scripts/db.sh"

# --data-dir 只在首次安装（还没有 .env）时生效；已有 .env 以里面记录的目录为准
if [ -n "$DATA_DIR_ARG" ] && [ ! -f "$PKG_DIR/.env" ]; then
  mkdir -p "$DATA_DIR_ARG" || die "无法创建数据目录: $DATA_DIR_ARG"
  DATA_DIR="$(cd "$DATA_DIR_ARG" && pwd)"
  DB_DIR="$DATA_DIR/mysql"; DB_PORT_FILE="$DATA_DIR/mysql_port"
  DB_PASS_FILE="$DATA_DIR/mysql_password"; CNF="$DB_DIR/my.cnf"
  SOCK="$DB_DIR/mysqld.sock"; PIDF="$DB_DIR/mysqld.pid"; LOG_DIR="$DATA_DIR/logs"
  DB_PASS="$(cat "$DB_PASS_FILE" 2>/dev/null || true)"
  DB_PORT="$(cat "$DB_PORT_FILE" 2>/dev/null || true)"
fi

glibc_ver="$(ldd --version 2>/dev/null | head -1 | grep -Eo '[0-9]+\.[0-9]+$' || true)"
[ -n "$glibc_ver" ] && info "glibc: $glibc_ver（需 >= 2.17）"

PY_VERSION="$("$PY" -V 2>&1 || true)"
[ -n "$PY_VERSION" ] || die "内置 Python 无法运行（架构/glibc 不匹配？）"
"$PY" -c 'import fastapi, sqlalchemy, aiomysql, pymysql' 2>/dev/null \
  || die "内置 Python 依赖不完整，请重新解压完整安装包（不要只复制部分文件）"
ok "$PY_VERSION（内置，无需系统 Python）"

info "数据目录: $DATA_DIR"
mkdir -p "$DATA_DIR/mysql" "$DATA_DIR/chat_uploads" "$DATA_DIR/projects" "$DATA_DIR/logs" "$DATA_DIR/tmp" \
  || die "无法创建数据目录 $DATA_DIR（权限不足？换 --data-dir 或修正权限）"

# ---------- 1. 数据库账号密码 / 端口 ----------
# 优先沿用 .env 里已有的连接串（升级/搬目录时密码和端口必须先对齐，再谈初始化）
ENV_URL="$(env_get DATABASE_URL)"
if [ -z "${DB_PASS:-}" ] && [ -n "$ENV_URL" ]; then
  case "$ENV_URL" in
    mysql+aiomysql://trace:*) DB_PASS="$(printf '%s' "$ENV_URL" | sed -E 's#^mysql\+aiomysql://trace:([^@]*)@.*#\1#')" ;;
  esac
  if [ -z "$(cat "$DB_PORT_FILE" 2>/dev/null || true)" ]; then
    p="$(printf '%s' "$ENV_URL" | sed -nE 's#^mysql\+aiomysql://[^@]*@[^:]*:([0-9]+)/.*#\1#p')"
    [ -n "$p" ] && DB_PORT="$p"
  fi
fi
[ -n "${DB_PASS:-}" ] || DB_PASS="$("$PY" -c 'import secrets; print(secrets.token_hex(12))')"
printf '%s\n' "$DB_PASS" > "$DB_PASS_FILE"
chmod 600 "$DB_PASS_FILE" 2>/dev/null || true

if [ -f "$DB_PORT_FILE" ]; then
  DB_PORT="$(cat "$DB_PORT_FILE")"
elif [ -n "$PORT_ARG" ]; then
  DB_PORT="$PORT_ARG"
else
  DB_PORT="$(db_pick_port 33306)" || die "找不到空闲的数据库端口"
fi
printf '%s\n' "$DB_PORT" > "$DB_PORT_FILE"

DB_URL="mysql+aiomysql://${DB_USER}:${DB_PASS}@127.0.0.1:${DB_PORT}/${DB_NAME}?charset=utf8mb4"
db_write_cnf

# ---------- 2. 初始化 / 升级数据库 ----------
if db_initialized; then
  ok "检测到已有数据库（$DB_DIR）——保留原数据，只更新程序"
  EXISTING=1
else
  info "首次初始化数据库（数据目录: $DB_DIR）…"
  db_init || die "数据库初始化失败，请查看上面的输出"
  EXISTING=0
fi

info "启动内置 MariaDB（127.0.0.1:${DB_PORT}）…"
DB_STARTED_BY_INSTALL=0
db_ping || DB_STARTED_BY_INSTALL=1
if ! db_start; then
  err "MariaDB 启动失败。最近日志："
  tail -n 30 "$LOG_DIR/mariadb.err" 2>/dev/null || tail -n 30 "$LOG_DIR/mariadb.out" 2>/dev/null || true
  die "请按上面的日志排查（常见原因：数据目录权限、端口被占用、架构不匹配）"
fi
ok "MariaDB 已就绪"

if [ "$EXISTING" = "1" ] && [ -x "$MARIADB/bin/mariadb-upgrade" ]; then
  info "检查数据库结构升级（mariadb-upgrade，幂等）…"
  "$MARIADB/bin/mariadb-upgrade" --defaults-file="$CNF" >/dev/null 2>&1 \
    && ok "数据库结构已是最新" \
    || warn "mariadb-upgrade 未成功（数据未改动）。若之后启动报结构问题，请保留数据目录并联系维护者。"
fi

# ---------- 3. 建库 / 建账号（幂等） ----------
info "准备数据库与账号…"
"$PY" - "$SOCK" "$DB_NAME" "$DB_USER" "$DB_PASS" <<'PY' || die "创建数据库/账号失败"
import sys
import pymysql

sock, db, user, pw = sys.argv[1:5]
conn = pymysql.connect(unix_socket=sock, user="root", password="", autocommit=True)
with conn.cursor() as cur:
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    for host in ("127.0.0.1", "localhost"):
        cur.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s", (user, host, pw))
        cur.execute("ALTER USER %s@%s IDENTIFIED BY %s", (user, host, pw))
        cur.execute(f"GRANT ALL PRIVILEGES ON `{db}`.* TO %s@%s", (user, host))
    cur.execute("FLUSH PRIVILEGES")
conn.close()
PY
ok "数据库 trace / 账号 trace 已就绪"

# 安装脚本只负责结构；若本次是我们启动的库就停掉，真正启动交给 run.sh
[ "$DB_STARTED_BY_INSTALL" = "1" ] && db_stop

# ---------- 4. .env ----------
ENV_PATH="$PKG_DIR/.env"
if [ ! -f "$ENV_PATH" ]; then
  cp "$PKG_DIR/env.template" "$ENV_PATH"
  SECRET="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
  "$PY" - "$ENV_PATH" "$DATA_DIR" "$SECRET" "$DB_URL" <<'PY'
import sys
path, data_dir, secret, db_url = sys.argv[1:5]
wanted = {
    "GA_DATA_DIR": data_dir,
    "GA_SECRET_KEY": secret,
    "DATABASE_URL": db_url,
    "GA_OPEN_BROWSER": "0",
}
lines = open(path, encoding="utf-8").read().splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip()
    if "=" in line and not line.lstrip().startswith("#") and key in wanted:
        out.append(f"{key}={wanted[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, val in wanted.items():
    if key not in seen:
        out.append(f"{key}={val}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  ok ".env 已生成（数据目录 / 密钥 / 数据库连接串已自动填好）"
else
  info ".env 已存在，保留你的配置（只同步数据库连接串）"
  "$PY" - "$ENV_PATH" "$DB_URL" <<'PY'
import sys
path, db_url = sys.argv[1:3]
lines = open(path, encoding="utf-8").read().splitlines()
out, done = [], False
for line in lines:
    if line.split("=", 1)[0].strip() == "DATABASE_URL" and not line.lstrip().startswith("#"):
        # 只覆盖本包自己生成过的连接串，用户自定义的连接串不动
        cur = line.split("=", 1)[1].strip()
        if cur == "" or cur.startswith("mysql+aiomysql://trace:"):
            out.append(f"DATABASE_URL={db_url}")
        else:
            out.append(line)
        done = True
    else:
        out.append(line)
if not done:
    out.append(f"DATABASE_URL={db_url}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
fi

# 已有 .env 但 GA_SECRET_KEY 为空时补一个：团队版是 lite 模式，空密钥会落到
# 代码里的固定开发密钥，登录令牌可被伪造。补密钥只会让已登录用户重新登录，不动数据。
if [ -z "$(env_get GA_SECRET_KEY)" ]; then
  info "补齐 GA_SECRET_KEY（登录令牌签名用，现有登录会失效一次）…"
  SECRET="$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
  "$PY" - "$ENV_PATH" "$SECRET" <<'PY'
import sys
path, secret = sys.argv[1:3]
lines = open(path, encoding="utf-8").read().splitlines()
out, done = [], False
for line in lines:
    if line.split("=", 1)[0].strip() == "GA_SECRET_KEY" and not line.lstrip().startswith("#"):
        out.append(f"GA_SECRET_KEY={secret}")
        done = True
    else:
        out.append(line)
if not done:
    out.append(f"GA_SECRET_KEY={secret}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
  ok "GA_SECRET_KEY 已补齐"
fi

# ---------- 5. 可选的 git / svn ----------
# 一键安装版内置 offline-vcs（离线 git/svn）：扫描系统，已有就跳过，缺哪个补哪个。
# 普通包没有 offline-vcs，只提示安装方式，不碰系统。
VCS_DIR="$PKG_DIR/vendor/vcs"
OFFLINE_VCS="$PKG_DIR/offline-vcs/Trace-team-vcs"
if [ "$NO_VCS" = "1" ]; then
  info "已跳过 git/svn（--no-vcs）"
elif [ -d "$OFFLINE_VCS" ]; then
  info "检查 git / svn：系统已有就跳过，缺的用包内离线包补装…"
  if bash "$OFFLINE_VCS/install.sh" "$PKG_DIR"; then
    ok "git/svn 就绪（启动时系统优先，系统没有的才用包内）"
  else
    warn "git/svn 离线补装没完成，不影响 Trace 启动；可稍后单独重跑："
    warn "  bash $OFFLINE_VCS/install.sh $PKG_DIR"
  fi
elif command -v git >/dev/null 2>&1 && command -v svn >/dev/null 2>&1; then
  ok "git/svn: 使用系统已装的（$(command -v git) / $(command -v svn)）"
else
  MISS=""
  command -v git >/dev/null 2>&1 || MISS="$MISS git"
  command -v svn >/dev/null 2>&1 || MISS="$MISS svn"
  info "git/svn: 系统缺少:${MISS}（只有仓库同步才需要）"
  info "  apt install git subversion  或  dnf install git subversion"
  info "  或用离线包 Trace-team-vcs-linux-x86_64.tar.gz（见手册「Git / SVN」）"
fi

# ---------- 6. 完成 ----------
echo
printf "%s================ 安装完成 ================%s\n" "$GRN" "$RST"
echo "  程序目录: $PKG_DIR"
echo "  数据目录: $DATA_DIR   （升级时不会被改动；备份就备份这个目录）"
echo "  数据库  : 内置 MariaDB，127.0.0.1:${DB_PORT}，库/账号 trace"
echo
echo "  启动:  bash run.sh            # 前台，Ctrl+C 停止"
echo "         bash run.sh --daemon   # 后台，日志 $DATA_DIR/logs/run.out"
echo "  停止:  bash stop.sh"
echo "  备份:  bash scripts/backup.sh  # 数据库一致性快照 + 上传文件"
echo "  手册:  部署手册.md（安装 / 升级 / 备份恢复 / systemd）"
if [ -x "$VCS_DIR/bin/git" ] || [ -x "$VCS_DIR/bin/svn" ]; then
  echo "  Git/SVN: 系统优先；系统没有的用包内（$VCS_DIR）"
elif [ -d "$OFFLINE_VCS" ]; then
  echo "  Git/SVN: 离线包在 $OFFLINE_VCS，需要时再装：bash $OFFLINE_VCS/install.sh $PKG_DIR"
else
  echo "  Git/SVN: 用系统已装的；没装可 apt/dnf，或用离线包 Trace-team-vcs"
fi
echo
echo "  首次登录: 浏览器打开 http://<本机IP>:18888/"
echo "            管理员账号 admin，初始密码 666666（首次登录必须修改）"
echo "            登录后在「团队」页创建开发/需求账号、新建项目并绑定对话模型"
printf "%s=========================================%s\n" "$GRN" "$RST"
