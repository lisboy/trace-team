#!/usr/bin/env bash
# =============================================================================
# build-package.sh  --  在【Linux 构建机】打成「解压即用」的团队版部署包 (.tar.gz)
#
# 产物自带：Python 运行时 + 全部 pip 依赖 + MariaDB（通用二进制）+ 前端 dist，
# 目标机只需要 64 位 glibc 系统，不需要装 Python / MySQL / 任何开发工具。
#
# 用法（仓库根目录）:
#   bash packaging/linux-team/build-package.sh              # 普通包（不含 git/svn）
#   bash packaging/linux-team/build-package.sh --with-vcs   # 另加一个「一键安装版」
#
# 普通包：目标机的 git/svn 由管理员自己装（apt/dnf），或系统本来就有，Trace 直接用。
# 一键安装版（文件名带 -with-vcs）：额外内置离线 git/svn。install.sh 会扫描系统，
#   已有 git/svn 就跳过，缺哪个补哪个；不联网、不依赖系统包管理器、不需要 root。
#
# 首次构建会下载到 vendor/ 缓存（只下一次）：
#   - python-build-standalone（CPython 3.12，install_only）
#   - MariaDB 通用二进制包
#   - ncurses5/tinfo5 兼容库（mariadb-upgrade 要调交互式客户端）
# 也可以自己指定本地文件/目录：
#   --python-runtime PATH   目录（含 bin/python3）或 install_only .tar.gz
#   --mariadb PATH          已解压目录或 MariaDB bintar .tar.gz
#   --vendor DIR            缓存目录（默认 packaging/linux-team/vendor）
#   --out DIR               输出目录（默认 ./dist）
#   --no-build              不重建前端（直接用现有 web/dist）
#   --no-fetch              不联网，只用 vendor/ 里已有的包
#   --with-vcs              额外产出内置离线 git/svn 的一键安装版
#   --vcs-bundle PATH       指定离线 git/svn 包（默认 dist/Trace-team-vcs-linux-x86_64.tar.gz）
#
# 要求构建机：x86_64 Linux，有 node/npm、curl、tar、binutils（objdump）；
# pip 装依赖时会下载 manylinux2014 wheel（固定 glibc 2.17 档，适配内网老服务器）。
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
STAGE="$HERE/stage"
VENDOR="$HERE/vendor"
OUT_DIR="$ROOT/dist"
DO_BUILD=1; DO_FETCH=1; PY_SRC=""; MARIA_SRC=""
WITH_VCS=0; VCS_BUNDLE=""

# 固定版本：升级时改这里即可。URL 失效时用 --python-runtime/--mariadb 指定本地包。
PY_VER_TAG="20241016"
PY_FILE="cpython-3.12.7+${PY_VER_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_VER_TAG}/cpython-3.12.7%2B${PY_VER_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz"
MARIA_VER="10.11.9"
MARIA_TOP="mariadb-${MARIA_VER}-linux-systemd-x86_64"
MARIA_URL="https://archive.mariadb.org/mariadb-${MARIA_VER}/bintar-linux-systemd-x86_64/${MARIA_TOP}.tar.gz"
# 交互式 mariadb 客户端依赖 ncurses5 / tinfo5（新系统普遍已不带），而
# mariadb-upgrade 的升级检查会调用该客户端；把兼容库一并放进 runtime/mariadb/lib。
NCURSES_VER="6.1-1ubuntu1"
NCURSES_DEBS=("libncurses5_${NCURSES_VER}_amd64.deb" "libtinfo5_${NCURSES_VER}_amd64.deb")
NCURSES_URL="https://archive.ubuntu.com/ubuntu/pool/main/n/ncurses"

while [ $# -gt 0 ]; do
  case "$1" in
    --python-runtime) shift; PY_SRC="${1:-}" ;;
    --mariadb) shift; MARIA_SRC="${1:-}" ;;
    --vendor) shift; VENDOR="${1:-$VENDOR}" ;;
    --out) shift; OUT_DIR="${1:-$OUT_DIR}" ;;
    --no-build) DO_BUILD=0 ;;
    --no-fetch) DO_FETCH=0 ;;
    --with-vcs) WITH_VCS=1 ;;
    --vcs-bundle) shift; VCS_BUNDLE="${1:-}"; WITH_VCS=1 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
  shift
done

T0=$SECONDS
say() { printf "\n\033[36m[build %ss]\033[0m %s\n" "$((SECONDS - T0))" "$*"; }
die() { printf "\033[31m[build][x]\033[0m %s\n" "$*"; exit 1; }
need() { [ -e "$1" ] || die "缺少: $2 ($1)"; }

[ "$(uname -s)" = "Linux" ] || die "本脚本必须在 Linux 构建机上运行（Windows 请用 WSL/Docker）"
[ "$(uname -m)" = "x86_64" ] || die "当前只打 x86_64 包（当前: $(uname -m)）"

need "$ROOT/game_agent" "后端代码"
need "$ROOT/requirements.txt" "requirements.txt"
need "$ROOT/pyproject.toml" "pyproject.toml"
need "$ROOT/.env.example" ".env.example"

# 1) 前端构建
if [ "$DO_BUILD" = "1" ]; then
  need "$ROOT/frontend/package.json" "前端源码（--no-build 可跳过）"
  say "构建前端 (npm ci + build)…"
  pushd "$ROOT/frontend" >/dev/null
  if [ ! -d node_modules ]; then
    command -v npm >/dev/null 2>&1 || die "需要 node/npm，或加 --no-build 使用现有 dist"
    npm ci
  fi
  npm run build
  popd >/dev/null
fi
need "$ROOT/web/dist/index.html" "web/dist/index.html（前端未构建；去掉 --no-build 或先 npm run build）"

# 2) 准备 stage
say "准备打包目录…"
rm -rf "$STAGE"; mkdir -p "$STAGE" "$VENDOR" "$OUT_DIR"
EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' --exclude='.pytest_cache'
          --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='.venv' --exclude='venv'
          --exclude='node_modules' --exclude='.git' --exclude='*.log' --exclude='*.db'
          --exclude='*.sqlite' --exclude='*.sqlite3' --exclude='.env' --exclude='*.pid')

say "复制后端 / 前端 / 示例…"
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" game_agent | tar -C "$STAGE" -xf -
tar -C "$ROOT" -cf - "${EXCLUDES[@]}" web/dist | tar -C "$STAGE" -xf -
if [ -d "$ROOT/examples" ]; then
  tar -C "$ROOT" -cf - "${EXCLUDES[@]}" examples | tar -C "$STAGE" -xf -
fi
for f in requirements.txt pyproject.toml README.md LICENSE; do
  [ -f "$ROOT/$f" ] && cp "$ROOT/$f" "$STAGE/$f"
done

say "复制安装/启动脚本…"
cp "$HERE"/scripts/install.sh "$HERE"/scripts/run.sh "$HERE"/scripts/stop.sh \
   "$HERE"/scripts/README.txt "$STAGE/"
mkdir -p "$STAGE/scripts"
cp "$HERE/scripts/db.sh" "$HERE/scripts/backup.sh" "$STAGE/scripts/"
[ -f "$ROOT/docs/部署手册.md" ] && cp "$ROOT/docs/部署手册.md" "$STAGE/部署手册.md"
cp "$ROOT/.env.example" "$STAGE/env.template"
chmod +x "$STAGE"/install.sh "$STAGE"/run.sh "$STAGE"/stop.sh "$STAGE"/scripts/backup.sh

# 3) Python 运行时
say "准备 Python 运行时…"
mkdir -p "$STAGE/runtime"
if [ -z "$PY_SRC" ]; then PY_SRC="$VENDOR/$PY_FILE"; fi
if [ ! -e "$PY_SRC" ]; then
  [ "$DO_FETCH" = "1" ] || die "--no-fetch 但缺少 Python 运行时: $PY_SRC"
  say "下载 Python 运行时… $PY_URL"
  curl -fL --retry 3 -o "$PY_SRC" "$PY_URL" || die "Python 运行时下载失败，可手动下载后 --python-runtime 指定"
fi
if [ -d "$PY_SRC" ]; then
  cp -a "$PY_SRC" "$STAGE/runtime/python"
else
  case "$PY_SRC" in
    *.tar.gz|*.tgz)
      say "解压 Python 运行时（$(du -h "$PY_SRC" | cut -f1)）…"
      tar -xzf "$PY_SRC" -C "$STAGE/runtime"
      [ -d "$STAGE/runtime/python" ] || die "Python 包结构异常：解压后没有 python/ 目录"
      ;;
    *) die "无法识别的 Python 运行时: $PY_SRC" ;;
  esac
fi
PY3="$STAGE/runtime/python/bin/python3"
[ -x "$PY3" ] || die "Python 运行时不可用: $PY3"
"$PY3" -c 'import sys; assert sys.version_info[:2] >= (3, 12)' || die "Python 版本低于 3.12"

say "安装 Python 依赖到 runtime/site-packages …"
"$PY3" -m pip --version >/dev/null 2>&1 || "$PY3" -m ensurepip
"$PY3" -m pip install --upgrade pip >/dev/null
# 目标机是内网老服务器：固定取 manylinux2014（glibc 2.17）档位的 wheel。
# 不限定平台时，在较新的构建机上 pip 会挑 manylinux_2_28/2_34 的包，
# 产物在 CentOS 7 / 麒麟 / 欧拉等机器上会 ImportError（glibc 或 CXXABI 版本不够）。
"$PY3" -m pip install --target "$STAGE/runtime/site-packages" \
  --only-binary=:all: --platform manylinux2014_x86_64 \
  --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
  -r "$ROOT/requirements.txt"

# 4) MariaDB
say "准备 MariaDB…"
if [ -z "$MARIA_SRC" ]; then MARIA_SRC="$VENDOR/${MARIA_TOP}.tar.gz"; fi
if [ ! -e "$MARIA_SRC" ]; then
  [ "$DO_FETCH" = "1" ] || die "--no-fetch 但缺少 MariaDB 包: $MARIA_SRC"
  say "下载 MariaDB… $MARIA_URL"
  curl -fL --retry 3 -o "$MARIA_SRC" "$MARIA_URL" || die "MariaDB 下载失败，可手动下载后 --mariadb 指定"
fi
TMP_MARIA="$STAGE/.mariadb_tmp"
mkdir -p "$TMP_MARIA"
if [ -d "$MARIA_SRC" ]; then
  say "复制 MariaDB 目录（$(du -sh "$MARIA_SRC" | cut -f1)）…"
  cp -a "$MARIA_SRC/." "$TMP_MARIA/"
else
  case "$MARIA_SRC" in
    *.tar.gz|*.tgz)
      # 335 MB 的包解压到 /mnt/d（9p 挂载）要几十秒，期间没有任何输出，先把预期说清楚
      say "解压 MariaDB 缓存包（$(du -h "$MARIA_SRC" | cut -f1)），大文件落在 /mnt/d 上，约需几十秒…"
      tar -xzf "$MARIA_SRC" -C "$TMP_MARIA" ;;
    *) die "无法识别的 MariaDB 包: $MARIA_SRC" ;;
  esac
fi
MARIA_DIR="$(find "$TMP_MARIA" -maxdepth 3 -type f -path '*/bin/mariadbd' | head -1 | xargs -r dirname | xargs -r dirname)"
[ -n "$MARIA_DIR" ] || die "MariaDB 包结构异常：找不到 bin/mariadbd"
mv "$MARIA_DIR" "$STAGE/runtime/mariadb"
rm -rf "$TMP_MARIA"
say "MariaDB 已就绪"
# 只留运行必需：客户端工具用不到，去掉 include/man/测试，包小一半
rm -rf "$STAGE/runtime/mariadb/include" "$STAGE/runtime/mariadb/man" \
       "$STAGE/runtime/mariadb/mysql-test" "$STAGE/runtime/mariadb/sql-bench" \
       "$STAGE/runtime/mariadb/docs"
mkdir -p "$STAGE/runtime/mariadb/lib"

# mariadb-upgrade 是一个编译好的程序，但部分步骤会 exec bin/mariadb 客户端；
# 客户端需要 libncurses.so.5 / libtinfo.so.5（系统若只有 ncurses6 就会失败）。
say "准备 ncurses5 兼容库（mariadb-upgrade 需要客户端）…"
NC_TMP="$STAGE/.ncurses_tmp"; rm -rf "$NC_TMP"; mkdir -p "$NC_TMP"
for deb in "${NCURSES_DEBS[@]}"; do
  src="$VENDOR/$deb"
  if [ ! -e "$src" ]; then
    [ "$DO_FETCH" = "1" ] || die "--no-fetch 但缺少兼容库: $src"
    say "下载 $deb …"
    curl -fL --retry 3 -o "$src" "$NCURSES_URL/$deb" || die "$deb 下载失败，可手动下载后放进 $VENDOR/"
  fi
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb -x "$src" "$NC_TMP"
  else
    ( cd "$NC_TMP" && ar p "$src" data.tar.xz | tar -xJf - )
  fi
done
for so in libncurses.so.5 libtinfo.so.5; do
  f="$(find "$NC_TMP" -name "$so" | head -1)"
  [ -n "$f" ] || die "兼容库里找不到 $so（包结构异常）"
  cp -L "$f" "$STAGE/runtime/mariadb/lib/$so"
done
rm -rf "$NC_TMP"

# 只审计运行期真正调用的可执行文件：mariadbd / 备份 / 升级检查（含它调用的 mariadb 客户端）。
# garbd/ldb/sst_dump 等工具目标机用不到，不打包也不参与检查。
say "检查运行期依赖…"
RUNTIME_BINS=("$STAGE/runtime/mariadb/bin/mariadbd"
              "$STAGE/runtime/mariadb/bin/mariadb-upgrade"
              "$STAGE/runtime/mariadb/bin/mariadb-dump"
              "$STAGE/runtime/mariadb/bin/mariadb")
for extra in "$STAGE/runtime/mariadb/bin/mariadb-install-db" "$STAGE/runtime/mariadb/scripts/mysql_install_db"; do
  [ -e "$extra" ] && RUNTIME_BINS+=("$extra")
done
RUNTIME_BINS+=("$PY3")
MISSING_LIBS=""
for b in "${RUNTIME_BINS[@]}"; do
  [ -e "$b" ] || continue
  [ "$(head -c 4 "$b" 2>/dev/null)" = $'\x7fELF' ] || continue
  while read -r lib; do
    [ -n "$lib" ] && MISSING_LIBS+="$(basename "$b"):$lib "
  done < <(LD_LIBRARY_PATH="$STAGE/runtime/mariadb/lib:$STAGE/runtime/mariadb/lib/private" \
           ldd "$b" 2>/dev/null | awk '/not found/{print $1}')
done
if [ -n "$MISSING_LIBS" ]; then
  die "运行期依赖缺失（目标机上同样会缺，包不可用）-> $MISSING_LIBS"
fi
say "运行期依赖检查通过"

# 依赖存在不代表能用：老机器的 glibc / libstdc++ 会缺符号版本（例如 greenlet 要
# CXXABI_1.3.9，CentOS 7 自带 libstdc++ 没有 -> ImportError）。这里直接按目标基线
# 校验：glibc 2.17（内置 Python 运行时自身的下限）、GLIBCXX 3.4.19、CXXABI 1.3.7。
say "检查二进制符号版本（目标基线 glibc 2.17）…"
command -v objdump >/dev/null 2>&1 || die "需要 objdump（binutils）做符号版本检查，请先安装 binutils"
ABI_LIMITS=("GLIBC:2.17" "GLIBCXX:3.4.19" "CXXABI:1.3.7")
declare -A ABI_MAX=()
ABI_BAD=""
ABI_N=0
while IFS= read -r f; do
  ABI_N=$((ABI_N + 1))
  [ "$(head -c 4 "$f" 2>/dev/null)" = $'\x7fELF' ] || continue
  for lim in "${ABI_LIMITS[@]}"; do
    pre="${lim%%:*}"; maxv="${lim#*:}"
    v="$(objdump -T "$f" 2>/dev/null | grep -oE "${pre}_[0-9][0-9.]*" | sed "s/^${pre}_//" | sort -V | tail -1 || true)"
    [ -n "$v" ] || continue
    cur="${ABI_MAX[$pre]:-}"
    if [ -z "$cur" ] || [ "$(printf '%s\n%s\n' "$cur" "$v" | sort -V | tail -1)" = "$v" ]; then
      ABI_MAX[$pre]="$v"
    fi
    if [ "$(printf '%s\n%s\n' "$maxv" "$v" | sort -V | tail -1)" != "$maxv" ]; then
      ABI_BAD+="${f#$STAGE/}:${pre}_$v "
    fi
  done
done < <(find "$STAGE/runtime/site-packages" "$STAGE/runtime/python" \
             "$STAGE/runtime/mariadb/bin" "$STAGE/runtime/mariadb/lib" \
             -type f \( -name '*.so' -o -name '*.so.*' -o -name 'python3*' \) 2>/dev/null)
if [ -n "$ABI_BAD" ]; then
  die "符号版本超出目标基线，老机器会 ImportError -> $ABI_BAD"
fi
say "符号版本检查通过（扫描 $ABI_N 个文件；GLIBC_${ABI_MAX[GLIBC]:-none} / GLIBCXX_${ABI_MAX[GLIBCXX]:-none} / CXXABI_${ABI_MAX[CXXABI]:-none}）"

# 5) 版本信息
VER="0.4.0"
if grep -qE '^version\s*=' "$ROOT/pyproject.toml"; then
  VER="$(grep -E '^version\s*=' "$ROOT/pyproject.toml" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
fi
cat > "$STAGE/runtime/VERSIONS.txt" <<EOF
trace_version=$VER
python=$("$PY3" -V 2>&1)
mariadb=${MARIA_VER}
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
git_commit=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
compat_libs=ncurses5+libtinfo5 ${NCURSES_VER}
abi_baseline=glibc2.17/glibcxx3.4.19/cxxabi1.3.7
abi_actual=GLIBC_${ABI_MAX[GLIBC]:-none},GLIBCXX_${ABI_MAX[GLIBCXX]:-none},CXXABI_${ABI_MAX[CXXABI]:-none}
runtime_deps=ok
EOF

# 6) 打包（普通包，不含 git/svn）
STAMP="$(date +%Y%m%d-%H%M)"
OUT="$OUT_DIR/Trace-team-linux-${VER}-${STAMP}.tar.gz"
say "打包 -> $OUT（压缩约 1.4 GB，静默 1~2 分钟）"
tar -C "$STAGE" -czf "$OUT" .
( cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" 2>/dev/null || true )

# 7) 一键安装版（可选）：内置离线 git/svn，install.sh 扫描系统、缺哪个补哪个
OUT_VCS=""
if [ "$WITH_VCS" = "1" ]; then
  say "组装一键安装版（内置离线 git/svn）…"
  if [ -z "$VCS_BUNDLE" ]; then
    for c in "$OUT_DIR/Trace-team-vcs-linux-x86_64.tar.gz" \
             "$ROOT/dist/Trace-team-vcs-linux-x86_64.tar.gz" \
             "$VENDOR/Trace-team-vcs-linux-x86_64.tar.gz"; do
      [ -f "$c" ] && { VCS_BUNDLE="$c"; break; }
    done
  fi
  [ -n "$VCS_BUNDLE" ] && [ -f "$VCS_BUNDLE" ] \
    || die "找不到离线 git/svn 包: ${VCS_BUNDLE:-Trace-team-vcs-linux-x86_64.tar.gz}（先运行 bash packaging/linux-team/build-vcs-bundle.sh，或 --vcs-bundle 指定）"
  say "离线 git/svn 包: $VCS_BUNDLE"
  rm -rf "$STAGE/offline-vcs"; mkdir -p "$STAGE/offline-vcs"
  tar -xzf "$VCS_BUNDLE" -C "$STAGE/offline-vcs"
  [ -x "$STAGE/offline-vcs/Trace-team-vcs/install.sh" ] || die "离线 git/svn 包结构异常（缺少 Trace-team-vcs/install.sh）"
  OUT_VCS="$OUT_DIR/Trace-team-linux-${VER}-${STAMP}-with-vcs.tar.gz"
  say "打包 -> $OUT_VCS（再压缩一遍，静默 1~2 分钟）"
  tar -C "$STAGE" -czf "$OUT_VCS" .
  ( cd "$OUT_DIR" && sha256sum "$(basename "$OUT_VCS")" > "$(basename "$OUT_VCS").sha256" 2>/dev/null || true )
fi

SIZE="$(du -h "$OUT" | awk '{print $1}')"
echo
echo "================ 打包完成 ================"
echo "  产物: $OUT"
echo "  版本: $VER   大小: $SIZE   耗时: $((SECONDS - T0))s"
[ -n "$OUT_VCS" ] && echo "  一键安装版: $OUT_VCS（$(du -h "$OUT_VCS" | awk '{print $1}')，内置 git/svn）"
echo "  目标机: 解压 -> bash install.sh -> bash run.sh"
echo "  升级  : 解压覆盖到同一目录 -> bash install.sh（会保留 data/ 与 .env）"
echo "========================================="
