"""FastAPI 依赖：解析登录身份并按权限闸门。

团队版用 Bearer 令牌（auth_sessions 表存 SHA-256）。前端登录后把令牌放
``Authorization: Bearer <token>``；团队另外用 ``X-Team-Id`` 选择（当前只有一个团队）。

历史遗留：冒烟测试在 ``tests/conftest.py`` 里设 ``GA_AUTH_DISABLED=1``，
此时退回本机默认身份（owner 全权限），无需登录。生产环境不要设这个变量。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from game_agent.auth.local import ensure_local_identity
from game_agent.auth.passwords import hash_token
from game_agent.core.context import RequestContext
from game_agent.db.models import AuthSession, Project, ProjectMember, TeamMember, User
from game_agent.db.session import SessionLocal

ADMIN_ROLES = ("owner", "admin")
# 首登强制改密期间允许访问的接口前缀
_PASSWORD_CHANGE_ALLOWED = ("/api/auth/",)


def auth_disabled() -> bool:
    return os.environ.get("GA_AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


def is_admin_role(role: str) -> bool:
    return role in ADMIN_ROLES


def can_access_project(ctx: RequestContext, project_id) -> bool:
    return str(project_id) in ctx.project_ids


def owns_session(ctx: RequestContext, created_by) -> bool:
    """会话按创建者隔离：只有本人能看、能操作。

    历史会话可能没有 created_by（字段是后加的）：这种只给管理员看——既不让
    普通成员翻到别人的老聊天，也不至于一改就把老数据从所有人视野里抹掉。
    """
    if created_by is None:
        return is_admin_role(ctx.role)
    return str(created_by) == ctx.user_id


async def _project_scope(user: User, role: str) -> list[str]:
    """团队角色：管理员看全部项目；开发/需求只看被分派（project_members）的项目。"""
    async with SessionLocal() as s:
        memberships = (await s.execute(
            select(TeamMember).where(TeamMember.user_id == user.id)
            .order_by(TeamMember.joined_at))).scalars().all()
    team_ids = [m.team_id for m in memberships]
    if not team_ids:
        return []
    async with SessionLocal() as s:
        if is_admin_role(role):
            rows = await s.execute(select(Project.id).where(Project.team_id.in_(team_ids)))
        else:
            rows = await s.execute(
                select(Project.id).join(ProjectMember, ProjectMember.project_id == Project.id)
                .where(ProjectMember.user_id == user.id))
        return [str(r[0]) for r in rows.all()]


def _pick_membership(memberships: list[TeamMember], requested_team: str | None):
    if requested_team:
        for m in memberships:
            if str(m.team_id) == requested_team:
                return m
    return memberships[0] if memberships else None


async def _context_for(user: User, request: Request,
                       requested_team: str | None = None) -> RequestContext:
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已停用")
    async with SessionLocal() as s:
        memberships = (await s.execute(
            select(TeamMember).where(TeamMember.user_id == user.id)
            .order_by(TeamMember.joined_at))).scalars().all()
    tm = _pick_membership(list(memberships), requested_team)
    if tm is None:
        raise HTTPException(status_code=403, detail="该账号未加入任何团队")
    role = tm.role
    project_ids = await _project_scope(user, role)
    return RequestContext(
        user_id=str(user.id), team_id=str(tm.team_id), role=role,
        project_ids=project_ids, request_id=_rid(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


async def local_identity() -> tuple[str, str, str]:
    """测试/免登录模式的 (user_id, team_id, role)。生产分支不使用。"""
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        return str(user.id), str(team.id), "owner"


async def get_current_context(request: Request) -> RequestContext:
    if auth_disabled():
        return await _local_context(request)

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录或登录已过期",
                            headers={"WWW-Authenticate": "Bearer"})
    token = header[7:].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        sess = await s.scalar(select(AuthSession).where(
            AuthSession.token_hash == hash_token(token)))
        if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="登录已过期，请重新登录",
                                headers={"WWW-Authenticate": "Bearer"})
        user = await s.get(User, sess.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="账号不存在")

    # 首次登录/管理员重置密码后：除改密、退出、看自己资料外，一律拒绝
    path = request.url.path
    if user.must_change_password and not path.startswith(_PASSWORD_CHANGE_ALLOWED):
        raise HTTPException(status_code=403, detail={
            "code": "password_change_required",
            "message": "首次登录需要先修改初始密码",
        })

    return await _context_for(user, request, request.headers.get("x-team-id"))


async def _local_context(request: Request) -> RequestContext:
    async with SessionLocal() as s:
        user, team = await ensure_local_identity(s)
        proj_rows = await s.execute(select(Project.id).where(Project.team_id == team.id))
        project_ids = [str(r[0]) for r in proj_rows.all()]
    return RequestContext(
        user_id=str(user.id), team_id=str(team.id), role="owner",
        project_ids=project_ids, request_id=_rid(request),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def require(perm: str):
    async def _dep(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not ctx.can(perm):
            raise HTTPException(status_code=403, detail=f"权限不足：需要 {perm}")
        return ctx
    return _dep


def require_admin():
    """仅管理员（owner/admin）可访问：账号、角色、项目分派、模型配置。"""
    async def _dep(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        if not is_admin_role(ctx.role):
            raise HTTPException(status_code=403, detail="仅管理员可操作")
        return ctx
    return _dep


def require_chat():
    """发消息：开发可发起修改型 run；需求（viewer）只能发起只读分析（调用方强制 analyze）。"""
    async def _dep(ctx: RequestContext = Depends(get_current_context)) -> RequestContext:
        if ctx.role not in ("owner", "admin", "developer", "viewer"):
            raise HTTPException(status_code=403, detail="无权发起对话")
        return ctx
    return _dep


async def get_optional_context(request: Request) -> Optional[RequestContext]:
    """签名 URL 自授权路径用：拿不到身份返回 None，不抛 401。"""
    try:
        return await get_current_context(request)
    except HTTPException:
        return None
    except Exception:  # noqa: BLE001 - 拿不到身份也不该让静态读取 500
        return None


def _u(v):  # type: ignore[no-untyped-def]
    """宽松 UUID 转换：字符串能转就转，转不了原样返回（路由层大量使用）。"""
    import uuid
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


def _rid(request: Request) -> str:
    return request.headers.get("X-Request-Id") or "local"
