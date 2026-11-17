"""团队版账号体系：登录、首次改密、角色隔离、项目分派、项目模型绑定。"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from game_agent.api.state import state
from game_agent.api.app import create_app
from game_agent.bootstrap import bootstrap
from game_agent.db.session import SessionLocal


@pytest.fixture()
def app():
    state.session_factory = SessionLocal
    asyncio.run(bootstrap())
    return create_app()


def _run(coro):
    return asyncio.run(coro)


async def _req(app, method: str, path: str, token: str | None = None, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, headers=headers, **kw)


def _login(app, username: str, password: str):
    async def go():
        r = await _req(app, "POST", "/api/auth/login",
                       json={"username": username, "password": password})
        return r
    return _run(go())


def _change(app, token: str, old: str, new: str):
    async def go():
        return await _req(app, "POST", "/api/auth/change-password", token=token,
                          json={"old_password": old, "new_password": new})
    return _run(go())


ADMIN_NEW_PW = "AdminPass-12345"


def _admin_token(app, monkeypatch) -> str:
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    r = _login(app, "admin", "666666")
    if r.status_code == 200 and r.json()["must_change_password"]:
        token = r.json()["token"]
        assert _change(app, token, "666666", ADMIN_NEW_PW).status_code == 200
        return token
    r = _login(app, "admin", ADMIN_NEW_PW)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_admin_login_and_forced_password_change(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    r = _login(app, "admin", "666666")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["must_change_password"] is True
    token = body["token"]

    # 首登未改密：除 /api/auth/* 外一律拒绝，并带专用 code 供前端跳改密页
    r = _run(_req(app, "GET", "/api/team/members", token=token))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "password_change_required"

    r = _change(app, token, "666666", ADMIN_NEW_PW)
    assert r.status_code == 200, r.text

    r = _run(_req(app, "GET", "/api/auth/me", token=token))
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is False
    assert r.json()["user"]["is_admin"] is True


def test_create_member_assign_project_and_role_isolation(app, monkeypatch):
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    team_id = me["current_team"]

    uname = f"req_{uuid.uuid4().hex[:6]}"
    r = _run(_req(app, "POST", "/api/team/members", token=at,
                  json={"username": uname, "role": "viewer", "display_name": "需求小王"}))
    assert r.status_code == 200, r.text

    pname = f"p_{uuid.uuid4().hex[:6]}"
    r = _run(_req(app, "POST", f"/api/teams/{team_id}/projects", token=at,
                  json={"name": pname, "slug": pname, "repo_path": pname}))
    assert r.status_code == 200, r.text
    pid = r.json()["id"]

    members = _run(_req(app, "GET", "/api/team/members", token=at)).json()
    uid = next(m["id"] for m in members if m["username"] == uname)
    r = _run(_req(app, "PUT", f"/api/team/members/{uid}/projects", token=at,
                  json={"project_ids": [pid]}))
    assert r.status_code == 200, r.text

    # 新账号初始密码 666666，首登强制改密
    r = _login(app, uname, "666666")
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is True
    ut = r.json()["token"]
    assert _change(app, ut, "666666", f"Pw-{uuid.uuid4().hex[:8]}").status_code == 200

    # 需求角色不能进团队管理
    assert _run(_req(app, "GET", "/api/team/members", token=ut)).status_code == 403
    # 只看得到被分派的那一个项目
    projects = _run(_req(app, "GET", f"/api/teams/{team_id}/projects", token=ut)).json()
    assert [p["id"] for p in projects] == [pid]


def test_project_model_binding(app, monkeypatch):
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    projects = _run(_req(app, "GET", "/api/team/projects", token=at)).json()
    if not projects:
        pytest.skip("没有可用项目")
    pid = projects[0]["id"]
    # 不存在的档位必须拒绝
    r = _run(_req(app, "PUT", f"/api/team/projects/{pid}/model", token=at,
                  json={"model_profile_key": "no-such-profile"}))
    assert r.status_code == 400
    # 解除绑定
    r = _run(_req(app, "PUT", f"/api/team/projects/{pid}/model", token=at,
                  json={"model_profile_key": None}))
    assert r.status_code == 200
    assert me["user"]["role"] in ("owner", "admin")


def _new_member(app, at: str, role: str = "viewer") -> tuple[str, str, str]:
    """管理员建一个新成员，返回 (username, uid, 已改密后的 token)。"""
    uname = f"m_{uuid.uuid4().hex[:6]}"
    r = _run(_req(app, "POST", "/api/team/members", token=at,
                  json={"username": uname, "role": role, "display_name": uname}))
    assert r.status_code == 200, r.text
    members = _run(_req(app, "GET", "/api/team/members", token=at)).json()
    uid = next(m["id"] for m in members if m["username"] == uname)
    r = _login(app, uname, "666666")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert _change(app, token, "666666", f"Pw-{uuid.uuid4().hex[:8]}").status_code == 200
    return uname, uid, token


def _new_project(app, at: str, team_id: str) -> str:
    pname = f"p_{uuid.uuid4().hex[:6]}"
    r = _run(_req(app, "POST", f"/api/teams/{team_id}/projects", token=at,
                  json={"name": pname, "slug": pname, "repo_path": pname}))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_usage_visibility_and_filters(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    team_id = me["current_team"]
    _, uid, viewer = _new_member(app, at, "viewer")
    p1 = _new_project(app, at, team_id)
    p2 = _new_project(app, at, team_id)
    r = _run(_req(app, "PUT", f"/api/team/members/{uid}/projects", token=at,
                  json={"project_ids": [p1]}))
    assert r.status_code == 200, r.text

    # facets 用 32 位 hex，团队接口用带连字符的 UUID，比较前统一。
    hx = lambda v: str(v).replace("-", "").lower()  # noqa: E731

    # 管理员：全团队，带成员/项目筛选。
    r = _run(_req(app, "GET", "/api/usage", token=at))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "all"
    assert isinstance(body["by_user"], list)
    assert any(hx(x["user_id"]) == hx(uid) for x in body["user_facets"])
    assert {hx(x["project_id"]) for x in body["project_facets"]} >= {hx(p1), hx(p2)}
    assert _run(_req(app, "GET", f"/api/usage?project_id={p1}", token=at)).status_code == 200
    assert _run(_req(app, "GET", f"/api/usage?user_id={uid}", token=at)).status_code == 200
    # 前端传的是 facets 里的 hex 形式，也必须被接受
    own_facet = next(hx(x["user_id"]) for x in body["user_facets"] if hx(x["user_id"]) == hx(uid))
    assert _run(_req(app, "GET", f"/api/usage?user_id={own_facet}",
                     token=viewer)).status_code == 200

    # 需求角色：强制只看自己，facets 也只露出被分派项目的口径。
    r = _run(_req(app, "GET", "/api/usage", token=viewer))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "self"
    assert {hx(x["user_id"]) for x in body["user_facets"]} == {hx(uid)}
    assert {hx(x["project_id"]) for x in body["project_facets"]} <= {hx(p1)}
    assert _run(_req(app, "GET", f"/api/usage?project_id={p1}", token=viewer)).status_code == 200
    # 看别人 / 看未分派项目 → 403
    other_id = me["user"].get("id") or "someone-else"
    assert _run(_req(app, "GET", f"/api/usage?user_id={other_id}",
                     token=viewer)).status_code == 403
    assert _run(_req(app, "GET", f"/api/usage?project_id={p2}",
                     token=viewer)).status_code == 403
    # 操作审计仅管理员可见
    assert _run(_req(app, "GET", "/api/audit", token=viewer)).status_code == 403
    assert _run(_req(app, "GET", "/api/audit", token=at)).status_code == 200


def test_project_vcs_binding_validation(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    pid = _new_project(app, at, me["current_team"])
    url = f"/api/team/projects/{pid}/vcs"

    r = _run(_req(app, "PUT", url, token=at, json={"vcs_type": "hg"}))
    assert r.status_code == 400
    r = _run(_req(app, "PUT", url, token=at, json={"vcs_type": "git"}))
    assert r.status_code == 400 and "地址" in r.json()["detail"]

    r = _run(_req(app, "PUT", url, token=at, json={
        "vcs_type": "git", "vcs_remote": "https://git.example.com/x.git",
        "vcs_branch": "main", "vcs_username": "bot", "vcs_secret": "tok-123"}))
    assert r.status_code == 200, r.text
    assert r.json()["vcs_secret_ready"] is True

    proj = next(p for p in _run(_req(app, "GET", "/api/team/projects", token=at)).json()
                if p["id"] == pid)
    assert proj["vcs_type"] == "git"
    assert proj["vcs_remote"] == "https://git.example.com/x.git"
    assert proj["vcs_branch"] == "main"
    assert proj["vcs_username"] == "bot"
    assert proj["vcs_secret_ready"] is True
    assert "vcs_secret" not in proj  # 令牌只回「是否已就绪」，绝不回显

    # 不带 vcs_secret = 保留旧令牌；空串 = 清除
    r = _run(_req(app, "PUT", url, token=at, json={
        "vcs_type": "git", "vcs_remote": "https://git.example.com/x.git"}))
    assert r.json()["vcs_secret_ready"] is True
    r = _run(_req(app, "PUT", url, token=at, json={
        "vcs_type": "git", "vcs_remote": "https://git.example.com/x.git", "vcs_secret": ""}))
    assert r.json()["vcs_secret_ready"] is False

    # 非管理员不能改仓库配置
    _, _, dev = _new_member(app, at, "developer")
    assert _run(_req(app, "PUT", url, token=dev,
                     json={"vcs_type": "none"})).status_code == 403


def test_project_concurrency_setting(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    pid = _new_project(app, at, me["current_team"])
    url = f"/api/team/projects/{pid}/concurrency"

    r = _run(_req(app, "PUT", url, token=at, json={"max_concurrency": 2}))
    assert r.status_code == 200, r.text
    assert r.json()["max_concurrency"] == 2
    proj = next(p for p in _run(_req(app, "GET", "/api/team/projects", token=at)).json()
                if p["id"] == pid)
    assert proj["max_concurrency"] == 2

    # 0 = 不限；负数 / 过大都要拒绝
    assert _run(_req(app, "PUT", url, token=at,
                     json={"max_concurrency": 0})).status_code == 200
    assert _run(_req(app, "PUT", url, token=at,
                     json={"max_concurrency": -1})).status_code == 400
    assert _run(_req(app, "PUT", url, token=at,
                     json={"max_concurrency": 51})).status_code == 400

    _, _, dev = _new_member(app, at, "developer")
    assert _run(_req(app, "PUT", url, token=dev,
                     json={"max_concurrency": 1})).status_code == 403


def test_channels_are_project_scoped(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    p1 = _new_project(app, at, me["current_team"])
    p2 = _new_project(app, at, me["current_team"])

    r = _run(_req(app, "GET", f"/api/channels?project_id={p1}", token=at))
    assert r.status_code == 200, r.text
    assert r.json()["links"] == []

    r = _run(_req(app, "POST", "/api/channels/dingtalk/bind", token=at,
                  json={"webhook": "https://example.invalid/robot", "project_id": p1}))
    assert r.status_code == 200, r.text
    link = r.json()["link"]
    assert link["project_id"] == p1
    ext = link["external_id"]

    l1 = _run(_req(app, "GET", f"/api/channels?project_id={p1}", token=at)).json()["links"]
    assert [x["id"] for x in l1] == [link["id"]]
    l2 = _run(_req(app, "GET", f"/api/channels?project_id={p2}", token=at)).json()["links"]
    assert l2 == []

    # 同一个外部标识不能同时被两个项目占用（否则提醒会推错项目）
    r = _run(_req(app, "POST", "/api/channels/dingtalk/bind", token=at,
                  json={"webhook": "https://example.invalid/robot", "external_id": ext,
                        "project_id": p2}))
    assert r.status_code == 400

    # 通道配置属于团队管理，非管理员不可见
    _, _, viewer = _new_member(app, at, "viewer")
    assert _run(_req(app, "GET", "/api/channels", token=viewer)).status_code == 403


def test_workspace_scope_is_forced(app, monkeypatch):
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    pid = _new_project(app, at, me["current_team"])
    monkeypatch.setattr(state, "services", SimpleNamespace(queue=SimpleNamespace(
        enqueue=AsyncMock(return_value=None))))

    # 旧前端/手工请求即使传 full，也必须落成 workspace
    r = _run(_req(app, "POST", f"/api/projects/{pid}/chat", token=at,
                  json={"content": "看一下项目结构", "permission_scope": "full"}))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    r = _run(_req(app, "GET", f"/api/projects/{pid}/sessions", token=at))
    assert r.status_code == 200, r.text
    sess = next(x for x in r.json() if x["id"] == sid)
    assert sess["permission_scope"] == "workspace"

    # PATCH 不再接受改作用域（只接受 status）
    r = _run(_req(app, "PATCH", f"/api/sessions/{sid}", token=at,
                  json={"permission_scope": "full"}))
    assert r.status_code == 400


def test_viewer_chat_forced_analyze_and_readonly(app, monkeypatch):
    """需求角色发消息：run_type 只能是 analyze，且 options.readonly=true（工具硬只读）。

    开发角色同项目发消息仍是 modify，不受影响。
    """
    at = _admin_token(app, monkeypatch)
    me = _run(_req(app, "GET", "/api/auth/me", token=at)).json()
    pid = _new_project(app, at, me["current_team"])
    monkeypatch.setattr(state, "services", SimpleNamespace(queue=SimpleNamespace(
        enqueue=AsyncMock(return_value=None))))

    from game_agent.db.models import Run

    def _fetch_run(run_id: str):
        async def go():
            async with SessionLocal() as s:
                return await s.get(Run, uuid.UUID(run_id))
        return _run(go())

    _, vid, viewer = _new_member(app, at, "viewer")
    assert _run(_req(app, "PUT", f"/api/team/members/{vid}/projects", token=at,
                     json={"project_ids": [pid]})).status_code == 200
    # 手工传 analyze=False 也不能把自己提升成修改型 run
    r = _run(_req(app, "POST", f"/api/projects/{pid}/chat", token=viewer,
                  json={"content": "把 README 改成 hacked", "analyze": False}))
    assert r.status_code == 200, r.text
    vrun = _fetch_run(r.json()["run_id"])
    assert vrun is not None
    assert vrun.run_type == "analyze"
    assert (vrun.options_json or {}).get("readonly") is True

    _, did, dev = _new_member(app, at, "developer")
    assert _run(_req(app, "PUT", f"/api/team/members/{did}/projects", token=at,
                     json={"project_ids": [pid]})).status_code == 200
    r = _run(_req(app, "POST", f"/api/projects/{pid}/chat", token=dev,
                  json={"content": "把 README 改成 hacked"}))
    assert r.status_code == 200, r.text
    drun = _fetch_run(r.json()["run_id"])
    assert drun is not None
    assert drun.run_type == "modify"
    assert not (drun.options_json or {}).get("readonly")
