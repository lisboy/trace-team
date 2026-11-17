"""内置目录浏览器（/api/fs/dirs、/api/fs/mkdir）的回归测试。

Linux 上「设置 → 存储空间 → 浏览」曾经是个死胡同：顶层视图只列数据目录自己的
子目录，返回的 parent 是空串，「上一级」只是原地重刷，永远选不到 /mnt 之类的盘。
这里锁住三件事：顶层有根目录入口、parent 是真父目录、能在浏览器里新建空文件夹。
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI

from game_agent.api.deps import get_current_context
from game_agent.api.routers import projects
from game_agent.core.context import RequestContext


@pytest.fixture
def fs_api(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "proj-a").mkdir(parents=True)
    monkeypatch.setattr(projects, "PROJECTS_ROOT", root)
    monkeypatch.setattr(projects, "MODE", "lite")

    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user_id="u-test", team_id="t-test", role="owner")
    return {"app": app, "root": root, "tmp": tmp_path}


def request(api, method, path, **kwargs):
    async def send():
        transport = httpx.ASGITransport(app=api["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.request(method, path, **kwargs)

    return asyncio.run(send())


def test_top_view_lists_projects_and_roots(fs_api):
    body = request(fs_api, "GET", "/api/fs/dirs").json()
    names = [d["name"] for d in body["dirs"]]
    assert "proj-a" in names
    if os.name == "nt":
        assert any(n.startswith("磁盘 ") for n in names)
    else:
        assert "/" in [d["path"] for d in body["dirs"]]
    # 「上一级」必须能真的往上走，而不是回到顶层视图原地重刷
    assert body["parent"] == str(fs_api["root"].parent)


def test_navigate_up_from_data_dir_reaches_filesystem(fs_api):
    """模拟用户点「上一级」：从数据目录内一路向上，最终 parent 为空（已到根）。"""
    cur = str(fs_api["root"])
    seen = []
    for _ in range(64):
        body = request(fs_api, "GET", "/api/fs/dirs", params={"path": cur}).json()
        seen.append(body["path"])
        if not body["parent"]:
            break
        cur = body["parent"]
    else:
        pytest.fail("一直上不去，parent 链成环")
    assert seen[0] == str(fs_api["root"])
    assert len(seen) > 1


def test_mkdir_creates_empty_folder(fs_api):
    body = request(fs_api, "POST", "/api/fs/mkdir", json={
        "parent": str(fs_api["root"]), "name": "TraceData"}).json()
    assert body["path"].endswith("TraceData")
    assert (fs_api["root"] / "TraceData").is_dir()
    assert list((fs_api["root"] / "TraceData").iterdir()) == []


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "..", ".hidden", ""])
def test_mkdir_rejects_bad_names(fs_api, bad):
    r = request(fs_api, "POST", "/api/fs/mkdir", json={"parent": str(fs_api["root"]), "name": bad})
    assert r.status_code == 400
