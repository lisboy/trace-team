#!/usr/bin/env bash
# =============================================================================
# build-vcs-bundle.sh -- 组装「Git / SVN 离线安装包」(Trace-team-vcs-linux-x86_64.tar.gz)
#
# 产物用法（目标机）:
#   tar xzf Trace-team-vcs-linux-x86_64.tar.gz
#   cd Trace-team-vcs && bash install.sh /opt/trace
#
# 原理：从 conda-forge 拉 linux-64 的 git / subversion 及其全部依赖，连同
# 静态版 micromamba 一起打进包；目标机由 micromamba 从包内本地 channel 离线解包，
# 前缀改写在本机完成，所以后续不需要联网，也不依赖系统包管理器。
#
# 构建机要求：conda、python3、curl、tar（x86_64 Linux，或 Windows Git Bash + conda）。
# 用法（仓库根目录）:
#   bash packaging/linux-team/build-vcs-bundle.sh
#   bash packaging/linux-team/build-vcs-bundle.sh --out dist
#   bash packaging/linux-team/build-vcs-bundle.sh --micromamba /path/to/micromamba
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WORK="$HERE/vcs-build"
OUT_DIR="$ROOT/dist"
MICROMAMBA_SRC=""
NO_FETCH=0

# 固定版本：升级时改这里，并重新生成。channel 里只放这些包。
GIT_VER="2.45.2"
SVN_VER="1.14.3"
MICROMAMBA_VER="2.0.5"
CHANNEL_URL="https://conda.anaconda.org/conda-forge"
BUNDLE="Trace-team-vcs-linux-x86_64"
TOP="Trace-team-vcs"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) shift; OUT_DIR="${1:-$OUT_DIR}" ;;
    --micromamba) shift; MICROMAMBA_SRC="${1:-}" ;;
    --no-fetch) NO_FETCH=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
  shift
done

say() { printf "\n\033[36m[build]\033[0m %s\n" "$*"; }
die() { printf "\033[31m[build][x]\033[0m %s\n" "$*"; exit 1; }

command -v conda >/dev/null || die "需要 conda 来解依赖（Windows 可用 conda 的 Git Bash）"
PYBIN=""
for c in python3 python; do
  # Windows 的 python3 可能是应用商店占位符：必须真的能执行
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
    PYBIN="$(command -v "$c")"; break
  fi
done
[ -n "$PYBIN" ] || die "需要可运行的 python3"
command -v curl >/dev/null || die "需要 curl"
command -v tar >/dev/null || die "需要 tar"

mkdir -p "$WORK" "$OUT_DIR"
STAGE="$WORK/stage"
rm -rf "$STAGE" "$WORK/channel"
mkdir -p "$WORK/pkgs"

# ---------- 1. 解依赖（不下载，只用空缓存拿到完整包清单） ----------
if [ "$NO_FETCH" = "1" ]; then
  [ -f "$WORK/plan.json" ] || die "--no-fetch 需要已有的 $WORK/plan.json"
  say "复用已有 plan.json（--no-fetch）"
else
  mkdir -p "$WORK/empty-cache"
  say "解析 git=$GIT_VER subversion=$SVN_VER 的 linux-64 依赖…"
  CONDA_PKGS_DIRS="$WORK/empty-cache" conda create --dry-run --json \
    -p "$WORK/plan-prefix" --platform linux-64 --override-channels \
    -c "$CHANNEL_URL" "git=$GIT_VER" "subversion=$SVN_VER" > "$WORK/plan.json" \
    || die "conda 解依赖失败（网络或 channel 不可用）"
fi

# ---------- 2. 下载并校验（curl 重试；sha256 与 conda 元数据一致） ----------
say "下载 $(grep -c '"fn"' "$WORK/plan.json" || true) 个包…"
"$PYBIN" - "$WORK" <<'PY'
import hashlib, json, os, subprocess, sys
work = sys.argv[1]
plan = json.load(open(os.path.join(work, "plan.json"), encoding="utf-8"))
fetch = plan["actions"]["FETCH"]
dest = os.path.join(work, "pkgs")

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

for i, pkg in enumerate(fetch, 1):
    out = os.path.join(dest, pkg["fn"])
    if os.path.isfile(out) and os.path.getsize(out) == pkg["size"] and sha256(out) == pkg["sha256"]:
        continue
    print(f"  [{i}/{len(fetch)}] {pkg['fn']}", flush=True)
    rc = subprocess.call(["curl", "-sSL", "--fail", "--retry", "10", "--retry-all-errors",
                          "--retry-delay", "2", "--connect-timeout", "30", "--max-time", "900",
                          "-C", "-", "-o", out, pkg["url"]])
    if rc != 0 or os.path.getsize(out) != pkg["size"] or sha256(out) != pkg["sha256"]:
        sys.exit(f"下载/校验失败: {pkg['fn']}")
print(f"  {len(fetch)} 个包全部校验通过", flush=True)
PY

# ---------- 3. 生成本地 channel ----------
say "生成本地 channel…"
"$PYBIN" - "$WORK" <<'PY'
import json, os, shutil, sys
work = sys.argv[1]
plan = json.load(open(os.path.join(work, "plan.json"), encoding="utf-8"))
pkgs, channel = os.path.join(work, "pkgs"), os.path.join(work, "channel")
KEEP = ("build", "build_number", "constrains", "depends", "license", "md5",
        "name", "sha256", "size", "subdir", "timestamp", "version")
repos = {s: {"info": {"subdir": s}, "packages": {}, "packages.conda": {}, "repodata_version": 1}
         for s in ("linux-64", "noarch")}
for rec in plan["actions"]["FETCH"]:
    fn, sub = rec["fn"], rec["subdir"]
    os.makedirs(os.path.join(channel, sub), exist_ok=True)
    shutil.copy2(os.path.join(pkgs, fn), os.path.join(channel, sub, fn))
    entry = {k: rec[k] for k in KEEP if k in rec}
    entry["subdir"] = sub
    if sub == "linux-64":
        entry["arch"], entry["platform"] = "x86_64", "linux"
    else:
        entry.setdefault("noarch", "generic")
    repos[sub]["packages.conda" if fn.endswith(".conda") else "packages"][fn] = entry
for sub, repo in repos.items():
    with open(os.path.join(channel, sub, "repodata.json"), "w", encoding="utf-8") as f:
        json.dump(repo, f, ensure_ascii=False, sort_keys=True, indent=1)
PY

# ---------- 4. micromamba ----------
say "准备 micromamba $MICROMAMBA_VER…"
[ -n "$MICROMAMBA_SRC" ] || [ ! -x "$WORK/bin/micromamba" ] || MICROMAMBA_SRC="$WORK/bin/micromamba"
mkdir -p "$WORK/stage/payload"
if [ -n "$MICROMAMBA_SRC" ]; then
  cp "$MICROMAMBA_SRC" "$STAGE/payload/micromamba"
else
  curl -sSL --fail --retry 10 --retry-all-errors --connect-timeout 30 --max-time 600 \
    -o "$WORK/micromamba.tar.bz2" \
    "https://micro.mamba.pm/api/micromamba/linux-64/$MICROMAMBA_VER"
  tar -xjf "$WORK/micromamba.tar.bz2" -C "$WORK" bin/micromamba
  cp "$WORK/bin/micromamba" "$STAGE/payload/micromamba"
fi
chmod +x "$STAGE/payload/micromamba"

# ---------- 5. 组装 ----------
say "组装 $BUNDLE…"
cp -r "$WORK/channel" "$STAGE/payload/channel"
cp "$HERE/scripts/install-vcs.sh" "$STAGE/install.sh"
cp "$HERE/vcs/README.txt" "$STAGE/README.txt"
chmod +x "$STAGE/install.sh"
mkdir -p "$OUT_DIR"
rm -rf "$WORK/bundle" && mkdir -p "$WORK/bundle/$TOP"
cp -r "$STAGE/." "$WORK/bundle/$TOP/"
tar -czf "$OUT_DIR/$BUNDLE.tar.gz.tmp" -C "$WORK/bundle" "$TOP"
mv "$OUT_DIR/$BUNDLE.tar.gz.tmp" "$OUT_DIR/$BUNDLE.tar.gz"
( cd "$OUT_DIR" && sha256sum "$BUNDLE.tar.gz" > "$BUNDLE.tar.gz.sha256" 2>/dev/null || true )

say "完成: $OUT_DIR/$BUNDLE.tar.gz ($(du -h "$OUT_DIR/$BUNDLE.tar.gz" | cut -f1))"
echo "  目标机: tar xzf $BUNDLE.tar.gz && cd $TOP && bash install.sh /opt/trace"
