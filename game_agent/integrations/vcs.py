"""项目仓库同步：对话开始前把远端最新内容拉到工作区（Git / SVN）。

团队版的约定：项目在「团队 → 项目配置」里绑定仓库后，每个 run 开跑前先同步一次，
模型看到的是最新代码，而不是几周前 checkout 的旧快照。

设计取舍：
- **尽力而为**：同步失败（令牌失效、网络不通、本地有冲突改动）绝不阻断对话，
  只把原因写进日志并作为一句提示交给模型/用户；否则一次网络抖动就让整个项目不可用。
- **不覆盖本地改动**：Git 只做 ``--ff-only``；SVN 用 ``update``。真正的冲突交给用户处理，
  这里绝不 ``reset --hard`` / ``revert``。
- 令牌只通过 ``http.extraheader`` 传给本次命令，不写进 remote URL、不落日志。
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from game_agent.logging_setup import get_logger

log = get_logger("integrations.vcs")

DEFAULT_TIMEOUT = 180.0


def _scrub(text: str, secret: str | None) -> str:
    t = (text or "").strip()
    if secret:
        t = t.replace(secret, "***")
    return t[-600:]


@lru_cache(maxsize=1)
def _git_allows_empty_config() -> bool:
    # git <= 2.0（如 CentOS 7 自带的 1.8）把 -c key= 当成缺值，直接
    # "fatal: unable to parse command-line config"，只有 2.1+ 支持空值。
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 1)


def _git_auth_args(username: str | None, secret: str | None) -> list[str]:
    if not secret:
        return []
    user = (username or "oauth2").strip() or "oauth2"
    token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
    args = ["-c", f"http.extraheader=Authorization: Basic {token}"]
    if _git_allows_empty_config():
        # 空 credential.helper：避免机器上配好的凭据管理器弹窗/覆盖本次令牌
        args[:0] = ["-c", "credential.helper="]
    return args


def _run_blocking(cmd: list[str], cwd: str | None, timeout: float,
                  env: dict | None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError:
        return 127, f"找不到命令：{cmd[0]}（目标机未安装或不在 PATH）"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace")
        return 124, out + f"\n同步超时（>{int(timeout)}s）"
    return proc.returncode or 0, (proc.stdout or b"").decode("utf-8", "replace")


async def _run(cmd: list[str], *, cwd: Path | None, timeout: float,
               secret: str | None) -> tuple[int, str]:
    # 走线程而不是 create_subprocess_exec：Windows 上主程序用的是 Selector 事件循环，
    # 它不支持子进程（asyncio 直接抛 NotImplementedError），同步会静默全失败。
    env = None
    if cmd and cmd[0].endswith(("git", "git.exe")):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    rc, text = await asyncio.to_thread(
        _run_blocking, cmd, str(cwd) if cwd else None, timeout, env)
    return rc, _scrub(text, secret)


def _non_empty(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


async def _sync_git(project, workspace: Path, timeout: float) -> dict:
    remote = (getattr(project, "vcs_remote", None) or getattr(project, "git_remote", None) or "").strip()
    if not remote:
        return {"ok": False, "action": "git", "message": "没有填写 Git 地址"}
    branch = (getattr(project, "base_branch", None) or "main").strip() or "main"
    secret = getattr(project, "vcs_secret", None)
    auth = _git_auth_args(getattr(project, "vcs_username", None), secret)
    workspace.mkdir(parents=True, exist_ok=True)

    if not (workspace / ".git").exists():
        if _non_empty(workspace):
            # 工作区已有文件（如示例工程/手工放的脚手架）：克隆到临时目录再覆盖复制，
            # 不删除既有文件，避免「一接仓库就把本地东西清空」。
            tmp = Path(tempfile.mkdtemp(prefix="vcs-clone-", dir=str(workspace.parent)))
            try:
                rc, out = await _run(
                    ["git", *auth, "clone", "--branch", branch, "--single-branch",
                     remote, str(tmp)],
                    cwd=None, timeout=timeout, secret=secret)
                if rc != 0:
                    return {"ok": False, "action": "git", "message": out}
                shutil.copytree(tmp, workspace, dirs_exist_ok=True)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return {"ok": True, "action": "git", "message": f"已克隆 {branch} 分支到工作区"}
        rc, out = await _run(
            ["git", *auth, "clone", "--branch", branch, "--single-branch",
             remote, str(workspace)],
            cwd=None, timeout=timeout, secret=secret)
        return {"ok": rc == 0, "action": "git",
                "message": "已克隆最新代码" if rc == 0 else out}

    # 已是仓库：先确保在目标分支（有本地改动时 checkout 会失败，此时不覆盖）
    rc, cur = await _run(["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=None, timeout=30, secret=None)
    if rc == 0 and cur.strip() and cur.strip() != branch:
        rc, out = await _run(["git", "-C", str(workspace), "checkout", branch],
                             cwd=None, timeout=60, secret=None)
        if rc != 0:
            return {"ok": False, "action": "git",
                    "message": f"切到 {branch} 失败（可能有本地改动）：{out}"}
    rc, out = await _run(
        ["git", "-C", str(workspace), *auth, "pull", "--ff-only", "origin", branch],
        cwd=None, timeout=timeout, secret=secret)
    return {"ok": rc == 0, "action": "git",
            "message": "已拉取最新提交" if rc == 0 else out}


async def _sync_svn(project, workspace: Path, timeout: float) -> dict:
    url = (getattr(project, "vcs_remote", None) or "").strip()
    if not url:
        return {"ok": False, "action": "svn", "message": "没有填写 SVN 地址"}
    secret = getattr(project, "vcs_secret", None)
    cred: list[str] = []
    if getattr(project, "vcs_username", None):
        cred += ["--username", str(project.vcs_username)]
    if secret:
        cred += ["--password", str(secret)]
    workspace.mkdir(parents=True, exist_ok=True)
    common = ["--non-interactive", "--no-auth-cache", *cred]
    if not (workspace / ".svn").exists():
        rc, out = await _run(["svn", "checkout", *common, url, str(workspace)],
                             cwd=None, timeout=timeout, secret=secret)
        return {"ok": rc == 0, "action": "svn",
                "message": "已检出最新版本" if rc == 0 else out}
    rc, out = await _run(["svn", "update", *common, str(workspace)],
                         cwd=None, timeout=timeout, secret=secret)
    return {"ok": rc == 0, "action": "svn",
            "message": "已更新到最新版本" if rc == 0 else out}


async def sync_project_workspace(project, workspace: Path,
                                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """按项目配置同步工作区。永远返回 dict，不抛异常（调用点在 run 启动路径上）。"""
    kind = (getattr(project, "vcs_type", None) or "none").strip().lower()
    if kind not in ("git", "svn"):
        return {"ok": True, "action": "none", "message": "未绑定仓库"}
    try:
        if kind == "git":
            result = await _sync_git(project, workspace, timeout)
        else:
            result = await _sync_svn(project, workspace, timeout)
    except Exception as exc:  # noqa: BLE001 - 同步失败不阻断对话
        result = {"ok": False, "action": kind, "message": f"{type(exc).__name__}: {exc}"}
    if result["ok"]:
        log.info("vcs synced", extra={"project": str(getattr(project, "id", "")),
                                      "kind": kind, "action": result["action"]})
    else:
        log.warning("vcs sync failed: %s", result["message"])
    return result
