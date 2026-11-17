"""会话按账号隔离 + 需求角色可以插话（2026-11-17 用户要求）。

现场两件事：
1. 同一团队里换个账号还能翻到别人的会话——会话只按 team_id 过滤，没有按人隔离；
2. 需求（viewer）在运行卡片上点「插话」被 403「权限不足：需要 run:create」，
   而需求需要的恰恰是「边看分析边补充口径」。

这里钉死：会话列表/读取/改删/导出都只认创建者（历史 NULL 只给管理员）；
viewer 能插自己会话里正在跑的只读分析，但插不进别人的 run，也改不了 modify。

数据直接在库里造（账号/项目/会话/run）：这组测试只验权限门，不走管理员建号流程，
免得改动 admin 密码影响同库的其它用例。
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from game_agent.api.app import create_app
from game_agent.api.state import state
from game_agent.auth.passwords import hash_password
from game_agent.bootstrap import bootstrap
from game_agent.core.lite.container import build_lite_services
from game_agent.db.models import Project, ProjectMember, Run, Session, Team, TeamMember, User
from game_agent.db.session import SessionLocal


@pytest.fixture()
def app(monkeypatch):
    # 走真实 Bearer 鉴权（conftest 默认是免登录 owner）
    monkeypatch.setenv("GA_AUTH_DISABLED", "0")
    state.session_factory = SessionLocal
    asyncio.run(bootstrap())
    state.services = asyncio.run(build_lite_services(SessionLocal, "test-worker"))
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
    return _run(_req(app, "POST", "/api/auth/login",
                     json={"username": username, "password": password}))


def _member(role: str, pid: str) -> tuple[str, str, str]:
    """造一个已改密的成员并登录，返回 (user_id, token)。"""
    uname = f"{role}_{uuid.uuid4().hex[:8]}"
    pw = f"Pw-{uuid.uuid4().hex[:8]}"

    async def go():
        async with SessionLocal() as s:
            team_id = await s.scalar(select(Team.id).order_by(Team.created_at))
            u = User(username=uname, email=f"{uname}@t.local", display_name=uname,
                     password_hash=hash_password(pw), must_change_password=False,
                     is_active=True)
            s.add(u)
            await s.flush()
            s.add(TeamMember(team_id=team_id, user_id=u.id, role=role))
            s.add(ProjectMember(project_id=uuid.UUID(pid), user_id=u.id,
                                role=role if role != "owner" else "developer"))
            await s.commit()
            return str(u.id)

    uid = _run(go())
    return uid, uname, pw


def _project(team_id: str) -> str:
    pname = f"p_{uuid.uuid4().hex[:8]}"

    async def go():
        async with SessionLocal() as s:
            p = Project(team_id=uuid.UUID(team_id), name=pname, slug=pname, repo_path=pname)
            s.add(p)
            await s.commit()
            return str(p.id)

    return _run(go())


def _team_id() -> str:
    async def go():
        async with SessionLocal() as s:
            return str(await s.scalar(select(Team.id).order_by(Team.created_at)))

    return _run(go())


def _seed_session(pid: str, created_by: str, *,
                  run_type: str = "analyze", status: str = "running") -> tuple[str, str]:
    async def go():
        async with SessionLocal() as s:
            team_id = await s.scalar(select(Team.id).order_by(Team.created_at))
            sess = Session(team_id=team_id, project_id=uuid.UUID(pid),
                           title="隔离测试", created_by=uuid.UUID(created_by))
            s.add(sess)
            await s.flush()
            run = Run(team_id=team_id, project_id=sess.project_id,
                      session_id=sess.id, triggered_by=uuid.UUID(created_by),
                      run_type=run_type, status=status)
            s.add(run)
            await s.commit()
            return str(sess.id), str(run.id)

    return _run(go())


def _session_ids(app, token: str, pid: str) -> list[str]:
    r = _run(_req(app, "GET", f"/api/projects/{pid}/sessions", token=token))
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()]


def test_sessions_are_isolated_per_account(app):
    pid = _project(_team_id())
    uid_a, aname, apw = _member("developer", pid)
    uid_b, bname, bpw = _member("developer", pid)
    tok_a = _login(app, aname, apw).json()["token"]
    tok_b = _login(app, bname, bpw).json()["token"]
    sid_a, _ = _seed_session(pid, uid_a, run_type="modify", status="succeeded")
    sid_b, _ = _seed_session(pid, uid_b, run_type="modify", status="succeeded")

    assert _session_ids(app, tok_a, pid) == [sid_a]
    assert _session_ids(app, tok_b, pid) == [sid_b]

    # 跨账号一律 404（不暴露「存在但不是你的」）
    assert _run(_req(app, "GET", f"/api/sessions/{sid_a}/messages",
                     token=tok_b)).status_code == 404
    assert _run(_req(app, "PATCH", f"/api/sessions/{sid_a}", token=tok_b,
                     json={"status": "archived"})).status_code == 404
    assert _run(_req(app, "DELETE", f"/api/sessions/{sid_a}", token=tok_b)).status_code == 404
    assert _run(_req(app, "GET", f"/api/sessions/{sid_a}/export",
                     token=tok_b)).status_code == 404

    # 管理员也只多看到「没有 created_by」的历史会话，不翻别人的私人会话
    uid_adm, admname, admpw = _member("owner", pid)
    tok_adm = _login(app, admname, admpw).json()["token"]
    admin_ids = _session_ids(app, tok_adm, pid)
    assert sid_a not in admin_ids and sid_b not in admin_ids


def test_viewer_can_steer_own_analyze_run(app):
    pid = _project(_team_id())
    uid_v, vname, vpw = _member("viewer", pid)
    tok_v = _login(app, vname, vpw).json()["token"]
    sid, rid = _seed_session(pid, uid_v, run_type="analyze")

    r = _run(_req(app, "POST", f"/api/runs/{rid}/steer", token=tok_v,
                  json={"content": "补充一下：口径按 B 方案", "client_msg_id": "t-1"}))
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True

    # 插话落成会话里的 user 消息（刷新/续跑都能看到）
    msgs = _run(_req(app, "GET", f"/api/sessions/{sid}/messages", token=tok_v)).json()
    steers = [m for m in msgs if (m.get("meta") or {}).get("steer")]
    assert len(steers) == 1 and "口径按 B 方案" in steers[0]["content"]

    # 幂等：同一个 client_msg_id 重试不会再落一条
    r = _run(_req(app, "POST", f"/api/runs/{rid}/steer", token=tok_v,
                  json={"content": "补充一下：口径按 B 方案", "client_msg_id": "t-1"}))
    assert r.status_code == 200 and r.json().get("duplicate") is True
    msgs = _run(_req(app, "GET", f"/api/sessions/{sid}/messages", token=tok_v)).json()
    assert len([m for m in msgs if (m.get("meta") or {}).get("steer")]) == 1


def test_viewer_steer_rejects_modify_run(app):
    pid = _project(_team_id())
    uid_v, vname, vpw = _member("viewer", pid)
    tok_v = _login(app, vname, vpw).json()["token"]
    # viewer 正常入口只会产生 analyze；这里造一条 modify 模拟历史/越权数据
    _, rid = _seed_session(pid, uid_v, run_type="modify")
    r = _run(_req(app, "POST", f"/api/runs/{rid}/steer", token=tok_v,
                  json={"content": "把那个也改了"}))
    assert r.status_code == 403, r.text

    # 也不能借插话把 analyze 关掉（等于放开改代码）
    _, rid2 = _seed_session(pid, uid_v, run_type="analyze")
    r = _run(_req(app, "POST", f"/api/runs/{rid2}/steer", token=tok_v,
                  json={"content": "", "analyze": False}))
    assert r.status_code == 403, r.text


def test_viewer_cannot_steer_someone_elses_run(app):
    pid = _project(_team_id())
    uid_d, dname, dpw = _member("developer", pid)
    uid_v, vname, vpw = _member("viewer", pid)
    tok_v = _login(app, vname, vpw).json()["token"]
    # 同样是 analyze，但属于别人的会话：run id 猜到了也插不进去
    _sid, rid = _seed_session(pid, uid_d, run_type="analyze")
    r = _run(_req(app, "POST", f"/api/runs/{rid}/steer", token=tok_v,
                  json={"content": "偷插一句"}))
    assert r.status_code == 404, r.text
