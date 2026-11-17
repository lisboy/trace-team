"""WebSocket 实时事件扇出（设计 §8.1）。DB 是权威源；此处为加速通道。

浏览器 WebSocket 无法带 Authorization 头，令牌走 ``?token=`` 查询参数。
订阅前校验：令牌有效、项目属于本人团队、开发/需求只能订阅被分派到的项目。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from game_agent.api.state import state
from game_agent.auth.local import ensure_local_identity
from game_agent.auth.passwords import hash_token
from game_agent.api.deps import ADMIN_ROLES, auth_disabled
from game_agent.db.models import (
    AuthSession, Project, ProjectMember, TeamMember, User,
)
from game_agent.db.session import SessionLocal

router = APIRouter()


async def _authorize(token: str | None, project_id: str) -> bool:
    async with SessionLocal() as s:
        if auth_disabled():
            _user, team = await ensure_local_identity(s)
            project = await s.get(Project, _u(project_id))
            return project is not None and str(project.team_id) == str(team.id)
        if not token:
            return False
        sess = await s.scalar(select(AuthSession).where(
            AuthSession.token_hash == hash_token(token)))
        if sess is None or sess.revoked_at is not None or sess.expires_at <= datetime.now(timezone.utc):
            return False
        user = await s.get(User, sess.user_id)
        project = await s.get(Project, _u(project_id))
        if user is None or not user.is_active or project is None:
            return False
        tm = await s.scalar(select(TeamMember).where(
            TeamMember.user_id == user.id, TeamMember.team_id == project.team_id))
        if tm is None:
            return False
        if tm.role in ADMIN_ROLES:
            return True
        return await s.scalar(select(ProjectMember.user_id).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id)) is not None


@router.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: str,
                         token: str | None = Query(None)):
    await websocket.accept()
    if not await _authorize(token, project_id):
        await websocket.close(code=4401)
        return
    await websocket.send_json({"type": "connected", "project_id": project_id})
    try:
        async for env in state.services.bus.subscribe(project_id):
            await websocket.send_json({
                "seq": env.payload.get("_seq"),
                "type": env.type,
                "run_id": str(env.run_id) if env.run_id else None,
                "task_id": str(env.task_id) if env.task_id else None,
                "agent": env.payload.get("agent_key"),
                "payload": env.payload,
            })
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return


def _u(v):  # type: ignore[no-untyped-def]
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v
