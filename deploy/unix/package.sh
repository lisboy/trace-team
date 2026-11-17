#!/usr/bin/env bash
# deploy/unix/package.sh —— 打「解压即用」的 Linux 团队版部署包。
# 真正的构建逻辑在 packaging/linux-team/build-package.sh（需要 Linux x86_64 构建机）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec bash "$ROOT/packaging/linux-team/build-package.sh" "$@"
