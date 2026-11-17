"""项目仓库同步：对话开始前把远端最新内容拉到工作区。

用本机临时裸仓库当「远端」，不联网；覆盖首次克隆、增量拉取、工作区已有文件时
不清理本地内容、以及远端不可达时只返回失败不抛异常。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from game_agent.integrations.vcs import sync_project_workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 未安装")

_ID = ["-c", "user.name=trace-test", "-c", "user.email=trace@test.local"]


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *_ID, *args], cwd=str(cwd) if cwd else None,
                   check=True, capture_output=True)


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    _git("clone", str(bare), str(seed))
    (seed / "a.txt").write_text("v1", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "v1", cwd=seed)
    _git("push", "origin", "HEAD:main", cwd=seed)
    return bare


def _project(remote: str) -> SimpleNamespace:
    return SimpleNamespace(vcs_type="git", vcs_remote=remote, git_remote=None,
                           base_branch="main", vcs_username=None, vcs_secret=None)


def test_clone_then_pull_latest(origin: Path, tmp_path: Path):
    workspace = tmp_path / "ws"
    result = asyncio.run(sync_project_workspace(_project(str(origin)), workspace))
    assert result["ok"] is True, result["message"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
    assert (workspace / ".git").is_dir()

    # 远端再提交一次，第二次同步应把新内容拉下来
    _git("clone", str(origin), str(tmp_path / "again"))
    again = tmp_path / "again"
    (again / "a.txt").write_text("v2", encoding="utf-8")
    _git("add", ".", cwd=again)
    _git("commit", "-m", "v2", cwd=again)
    _git("push", "origin", "HEAD:main", cwd=again)

    result = asyncio.run(sync_project_workspace(_project(str(origin)), workspace))
    assert result["ok"] is True, result["message"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "v2"


def test_existing_workspace_files_are_kept(origin: Path, tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("local", encoding="utf-8")

    result = asyncio.run(sync_project_workspace(_project(str(origin)), workspace))
    assert result["ok"] is True, result["message"]
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "local"


def test_unreachable_remote_does_not_raise(tmp_path: Path):
    workspace = tmp_path / "ws"
    result = asyncio.run(sync_project_workspace(
        _project(str(tmp_path / "no-such-repo.git")), workspace))
    assert result["ok"] is False
    assert result["action"] == "git"
    assert result["message"]


def test_unbound_project_is_noop(tmp_path: Path):
    result = asyncio.run(sync_project_workspace(
        SimpleNamespace(vcs_type="none"), tmp_path / "ws"))
    assert result["ok"] is True and result["action"] == "none"


def test_git_auth_args_reset_helper_on_new_git(monkeypatch: pytest.MonkeyPatch):
    from game_agent.integrations import vcs

    monkeypatch.setattr(vcs, "_git_allows_empty_config", lambda: True)
    args = vcs._git_auth_args("weizhaopeng", "tok")
    assert "credential.helper=" in args
    assert any(a.startswith("http.extraheader=Authorization: Basic ") for a in args)


def test_git_auth_args_skip_empty_helper_on_old_git(monkeypatch: pytest.MonkeyPatch):
    # 旧版 git（<=2.0）遇到 -c credential.helper= 会报 unable to parse command-line config
    from game_agent.integrations import vcs

    monkeypatch.setattr(vcs, "_git_allows_empty_config", lambda: False)
    args = vcs._git_auth_args("weizhaopeng", "tok")
    assert "credential.helper=" not in args
    assert any(a.startswith("http.extraheader=Authorization: Basic ") for a in args)
