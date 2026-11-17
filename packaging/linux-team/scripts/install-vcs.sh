#!/usr/bin/env bash
# =============================================================================
# install.sh -- 离线补装 Git / SVN 到 Trace 目录（系统已有的不动）
#
# 默认只补系统里缺的命令：
#   - 系统 PATH 里已有 git  → 不装 git，Trace 继续用系统的
#   - 系统 PATH 里已有 svn  → 不装 svn，Trace 继续用系统的
#   - 两个都有            → 什么都不做
# Trace 启动时也是这个规则：系统有就用系统的，包内的只作为缺省补位。
#
# 用法:
#   bash install.sh                  # 自动找 Trace 目录（TRACE_DIR 或 /opt/trace）
#   bash install.sh /path/to/trace   # 显式指定 Trace 安装目录
#   bash install.sh --all            # 两个都装（系统有也不管，但运行仍优先系统的）
#   bash install.sh --force ...      # 覆盖重装（自动包含 --all）
#   bash install.sh --prefer-bundled # 两个都装，并让运行优先用包内的 git/svn
#
# 安装位置: <Trace目录>/vendor/vcs （全部自带，不需要系统包管理器/联网/root）
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -t 1 ]; then
  RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; CYN=$'\e[36m'; BLD=$'\e[1m'; RST=$'\e[0m'
else
  RED=; GRN=; YEL=; CYN=; BLD=; RST=
fi
info() { printf "%s[ vcs ]%s %s\n" "$CYN" "$RST" "$*"; }
ok()   { printf "%s[ ok  ]%s %s\n" "$GRN" "$RST" "$*"; }
warn() { printf "%s[warn ]%s %s\n" "$YEL" "$RST" "$*"; }
err()  { printf "%s[ err ]%s %s\n" "$RED" "$RST" "$*"; }
die()  { err "$*"; exit 1; }

TRACE_ARG=""; FORCE=0; ALL=0; PREFER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force|-f) FORCE=1; ALL=1 ;;
    --all) ALL=1 ;;
    --prefer-bundled) PREFER=1; ALL=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) TRACE_ARG="$1" ;;
  esac
  shift
done

[ "$(uname -s)" = "Linux" ] || die "本包只支持 Linux（当前 $(uname -s)）"
[ "$(uname -m)" = "x86_64" ] || die "本包只支持 x86_64（当前 $(uname -m)）"

glibc_ver="$(ldd --version 2>/dev/null | head -1 | grep -Eo '[0-9]+\.[0-9]+$' || true)"
if [ -n "$glibc_ver" ]; then
  if [ "$(printf '%s\n' "2.17" "$glibc_ver" | sort -V | head -1)" != "2.17" ]; then
    die "glibc $glibc_ver 太旧，需要 >= 2.17（Alpine/musl 不支持）"
  fi
  info "glibc: $glibc_ver"
fi

# ---------- 找 Trace 目录 ----------
find_trace() {
  for d in "$TRACE_ARG" "${TRACE_DIR:-}" /opt/trace "$HOME/trace"; do
    [ -n "$d" ] || continue
    if [ -d "$d" ] && { [ -f "$d/run.sh" ] || [ -f "$d/scripts/run.sh" ]; }; then
      (cd "$d" && pwd)
      return 0
    fi
  done
  return 1
}
TRACE="$(find_trace)" || die "没找到 Trace 安装目录。请显式指定：bash install.sh /opt/trace"
DEST="$TRACE/vendor/vcs"
info "Trace 目录: $TRACE"
info "安装位置: $DEST"

MAMBA="$HERE/payload/micromamba"
CHANNEL="$HERE/payload/channel"
[ -x "$MAMBA" ] || { chmod +x "$MAMBA" 2>/dev/null || true; }
[ -x "$MAMBA" ] || die "缺少 payload/micromamba（包不完整，请重新解压）"
[ -f "$CHANNEL/linux-64/repodata.json" ] || die "缺少 payload/channel（包不完整，请重新解压）"

# ---------- 系统已有的不重复装 ----------
sys_git=0; sys_svn=0
command -v git >/dev/null 2>&1 && sys_git=1
command -v svn >/dev/null 2>&1 && sys_svn=1
[ "$sys_git" = "1" ] && info "系统已有 git：$(command -v git)（$("git" --version 2>/dev/null)）"
[ "$sys_svn" = "1" ] && info "系统已有 svn：$(command -v svn)"

PKGS=()
if [ "$ALL" = "1" ] || [ "$sys_git" = "0" ]; then PKGS+=(git); fi
if [ "$ALL" = "1" ] || [ "$sys_svn" = "0" ]; then PKGS+=(subversion); fi
if [ ${#PKGS[@]} -eq 0 ]; then
  ok "系统已经有 git 和 svn，不用装（Trace 默认优先用系统的）"
  info "想改用包内的：bash install.sh --prefer-bundled $TRACE"
  exit 0
fi
info "需要补装：${PKGS[*]}"

# 已装过且可用的包跳过
NEED=0
for p in "${PKGS[@]}"; do
  b="$p"; [ "$p" = "subversion" ] && b="svn"
  if [ -x "$DEST/bin/$b" ] && "$DEST/bin/$b" --version >/dev/null 2>&1; then :; else NEED=1; fi
done
if [ "$FORCE" = "0" ] && [ "$NEED" = "0" ]; then
  ok "包内已经装好：${PKGS[*]}"
  [ "$PREFER" = "1" ] && touch "$DEST/prefer-bundled"
  info "要覆盖重装：bash install.sh --force $TRACE"
  exit 0
fi

if [ "$FORCE" = "1" ] && [ -d "$DEST" ]; then
  case "$DEST" in
    "$TRACE"/vendor/vcs) rm -rf "$DEST" ;;
    *) die "拒绝删除意外路径: $DEST" ;;
  esac
fi

mkdir -p "$TRACE/vendor" || die "无法写入 $TRACE/vendor（权限不足？用有权限的账号或 sudo）"

# ---------- 离线安装 ----------
# 用包内 micromamba 从本地 channel 解包：所有依赖都在 payload/channel 里，
# 全程不联网；前缀改写由 micromamba 在目标机本地完成（搬目录/换机器只需重装一次）。
MAMBA_ROOT_PREFIX="$HERE/payload/.mamba"
export MAMBA_ROOT_PREFIX
mkdir -p "$MAMBA_ROOT_PREFIX/pkgs" || die "无法创建临时目录 $MAMBA_ROOT_PREFIX（包所在目录不可写？请解压到可写目录再运行）"
# 只使用包内 file:// channel；显式指定包缓存目录，避免 micromamba 因用户目录里的
# 其它 pkgs_dirs 不存在而直接报错。不加 --offline：离线模式会忽略 channel，只看缓存。
export CONDA_PKGS_DIRS="$MAMBA_ROOT_PREFIX/pkgs"
export MAMBA_NO_BANNER=1

info "离线解包 ${PKGS[*]}（首次约 30~90 秒）…"
if [ -f "$DEST/conda-meta/history" ]; then
  MAMBA_CMD=(install)
else
  MAMBA_CMD=(create)
fi
if ! "$MAMBA" "${MAMBA_CMD[@]}" -y --no-rc -p "$DEST" --override-channels \
       -c "file://$CHANNEL" "${PKGS[@]}"; then
  err "解包失败。若提示 glibc/架构不匹配，说明目标机不满足要求；"
  err "也可改用系统包管理器安装（apt install git subversion / dnf install git subversion）。"
  exit 1
fi
rm -rf "$MAMBA_ROOT_PREFIX/pkgs" 2>/dev/null || true

# ---------- 自检 ----------
WANT_GIT=0; WANT_SVN=0
for p in "${PKGS[@]}"; do [ "$p" = "git" ] && WANT_GIT=1; [ "$p" = "subversion" ] && WANT_SVN=1; done
if [ "$WANT_GIT" = "1" ]; then
  [ -x "$DEST/bin/git" ] || die "安装后找不到 $DEST/bin/git"
  GIT_V="$("$DEST/bin/git" --version 2>&1)" || die "git 无法运行：$GIT_V"
  ok "$GIT_V"
fi
if [ "$WANT_SVN" = "1" ]; then
  [ -x "$DEST/bin/svn" ] || die "安装后找不到 $DEST/bin/svn"
  SVN_V="$("$DEST/bin/svn" --version --quiet 2>&1)" || die "svn 无法运行：$SVN_V"
  ok "svn $SVN_V"
fi

if [ -f "$DEST/ssl/cacert.pem" ]; then
  # conda 的 openssl 默认 CA 文件是 <prefix>/ssl/cert.pem，svn 走 OpenSSL 默认路径；
  # 指过去就够了（git 的 CA 由 run.sh 通过 GIT_SSL_CAINFO 指定）。
  ln -sfn cacert.pem "$DEST/ssl/cert.pem" 2>/dev/null || true
  ok "CA 证书: $DEST/ssl/cacert.pem"
else
  warn "没找到内置 CA 证书；HTTPS 仓库可能需要系统证书（run.sh 会自动检测）"
fi

# git 本地闭环：init → commit → clone（不联网）；只测这次装的包内 git
TMP="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/trace-vcs-$RANDOM")"
mkdir -p "$TMP/home"
if [ "$WANT_GIT" = "1" ]; then
  if HOME="$TMP/home" GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
     bash -c '
       set -e
       G="$1/bin/git"
       mkdir -p "$2/src" && cd "$2/src"
       "$G" init -q
       "$G" config user.email t@example.com
       "$G" config user.name tester
       echo hello > README.md
       "$G" add README.md
       "$G" commit -q -m init
       "$G" clone -q "$2/src" "$2/dst"
       [ -f "$2/dst/README.md" ]
     ' _ "$DEST" "$TMP" >/dev/null 2>&1; then
    ok "git 自检通过（init/commit/clone）"
  else
    warn "git 本地自检没通过。git --version 正常但 clone 失败，请把上面的版本号反馈给维护者"
  fi
fi

# svn 本地闭环（有 svnadmin 时做，file:// 模块个别构建会省掉，失败只提示）
if [ "$WANT_SVN" = "1" ] && [ -x "$DEST/bin/svnadmin" ]; then
  if "$DEST/bin/svnadmin" create "$TMP/repo" >/dev/null 2>&1 \
     && "$DEST/bin/svn" checkout -q "file://$TMP/repo" "$TMP/wc" >/dev/null 2>&1; then
    ok "svn 自检通过（svnadmin + checkout file://）"
  else
    warn "svn 本地 file:// 自检未通过（不影响 https/svn:// 远端）；首次连远端时留意报错"
  fi
fi
rm -rf "$TMP" 2>/dev/null || true

[ "$PREFER" = "1" ] && touch "$DEST/prefer-bundled"

echo
printf "%s================ 完成 ================%s\n" "$GRN" "$RST"
if [ "$PREFER" = "1" ]; then
  echo "  运行优先：包内 git/svn（已写 $DEST/prefer-bundled）"
else
  echo "  运行优先：系统已有就用系统的，缺的才用包内"
fi
[ -x "$DEST/bin/git" ] && echo "  包内 git : $DEST/bin/git"
[ -x "$DEST/bin/svn" ] && echo "  包内 svn : $DEST/bin/svn"
echo "  已经在运行 Trace 的，重启一次让它生效：bash $TRACE/stop.sh && bash $TRACE/run.sh --daemon"
echo "  想改回优先系统：删掉 $DEST/prefer-bundled 再重启"
printf "%s======================================%s\n" "$GRN" "$RST"
