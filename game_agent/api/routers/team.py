"""团队管理（仅管理员）：成员账号、角色、项目分派、项目模型绑定。

角色口径：
  admin     管理员（owner/admin）——可管理团队与全部项目、模型配置
  developer 开发——在被分派项目里可发起修改
  viewer    需求——只读问答（后端强制 analyze）
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, or_, select

from game_agent.api.deps import _u, require_admin
from game_agent.auth.passwords import DEFAULT_PASSWORD, hash_password
from game_agent.config import PROJECTS_ROOT
from game_agent.core.context import RequestContext
from game_agent.db.models import (
    AuthSession, ModelProfile, Project, ProjectMember, TeamMember, User,
)
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api/team", tags=["team"])

_ROLES = ("admin", "developer", "viewer")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,40}$")
DEFAULT_ROLE = "viewer"


def _role_label(role: str) -> str:
    return {"owner": "管理员", "admin": "管理员",
            "developer": "开发", "viewer": "需求"}.get(role, role)


def _parse_role(role: str | None, *, allow_owner: bool = False) -> str:
    r = (role or "").strip().lower()
    if allow_owner and r == "owner":
        return "owner"
    if r not in _ROLES:
        raise HTTPException(status_code=400, detail="角色只能是 admin / developer / viewer")
    return r


async def _member_payloads(s, team_id) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = (await s.execute(
        select(User, TeamMember)
        .join(TeamMember, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team_id)
        .order_by(User.created_at))).all()
    members = []
    for user, tm in rows:
        pids = (await s.execute(select(ProjectMember.project_id).where(
            ProjectMember.user_id == user.id))).scalars().all()
        members.append({
            "id": str(user.id),
            "username": user.username,
            "display_name": user.display_name,
            "avatar": user.avatar or None,
            "role": tm.role,
            "role_label": _role_label(tm.role),
            "is_active": user.is_active,
            "must_change_password": bool(user.must_change_password),
            "last_login_at": str(user.last_login_at) if user.last_login_at else None,
            "project_ids": [str(p) for p in pids],
        })
    return members


@router.get("/members")
async def list_members(ctx: RequestContext = Depends(require_admin())):
    async with SessionLocal() as s:
        return await _member_payloads(s, _u(ctx.team_id))


class MemberIn(BaseModel):
    username: str
    display_name: str | None = None
    role: str = DEFAULT_ROLE
    password: str | None = None  # 不填 = 初始密码 666666（首次登录强制改）
    project_ids: list[str] = []


@router.post("/members")
async def create_member(body: MemberIn, ctx: RequestContext = Depends(require_admin())):
    username = (body.username or "").strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="用户名只能包含字母、数字、下划线、点或短横线（2-40 位）")
    role = _parse_role(body.role)
    password = (body.password or DEFAULT_PASSWORD)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        exists = await s.scalar(select(User.id).where(User.username == username))
        if exists is not None:
            raise HTTPException(status_code=409, detail="用户名已存在")
        user = User(
            username=username,
            email=f"{username}@{uuid.uuid4().hex[:8]}.trace.local",
            display_name=(body.display_name or username).strip() or username,
            password_hash=hash_password(password),
            must_change_password=True,
            is_active=True,
        )
        s.add(user)
        await s.flush()
        s.add(TeamMember(team_id=_u(ctx.team_id), user_id=user.id, role=role))
        for pid in body.project_ids:
            proj = await s.get(Project, _u(pid))
            if proj is not None and str(proj.team_id) == ctx.team_id:
                s.add(ProjectMember(project_id=proj.id, user_id=user.id,
                                    role="developer" if role == "developer" else "viewer"))
        await s.commit()
        return {"id": str(user.id), "username": username,
                "initial_password": password if not body.password else None}


class MemberPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


@router.patch("/members/{user_id}")
async def update_member(user_id: str, body: MemberPatch,
                        ctx: RequestContext = Depends(require_admin())):
    async with SessionLocal() as s:
        user = await s.get(User, _u(user_id))
        tm = await s.scalar(select(TeamMember).where(
            TeamMember.user_id == _u(user_id), TeamMember.team_id == _u(ctx.team_id)))
        if user is None or tm is None:
            raise HTTPException(status_code=404, detail="成员不存在")
        if body.display_name is not None:
            name = body.display_name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            user.display_name = name[:120]
        if body.role is not None:
            new_role = _parse_role(body.role, allow_owner=True)
            if tm.role in ("owner", "admin") and new_role not in ("owner", "admin"):
                admins = (await s.execute(select(TeamMember.user_id).where(
                    TeamMember.team_id == _u(ctx.team_id),
                    TeamMember.role.in_(("owner", "admin"))))).all()
                if len(admins) <= 1:
                    raise HTTPException(status_code=400, detail="至少要保留一个管理员")
            tm.role = new_role
        if body.is_active is not None:
            if str(user.id) == ctx.user_id and not body.is_active:
                raise HTTPException(status_code=400, detail="不能停用自己的账号")
            user.is_active = body.is_active
        await s.commit()
    return {"ok": True}


@router.post("/members/{user_id}/reset-password")
async def reset_password(user_id: str, ctx: RequestContext = Depends(require_admin())):
    """把密码重置为 666666，并在下次登录时强制修改。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(user_id))
        tm = await s.scalar(select(TeamMember).where(
            TeamMember.user_id == _u(user_id), TeamMember.team_id == _u(ctx.team_id)))
        if user is None or tm is None:
            raise HTTPException(status_code=404, detail="成员不存在")
        user.password_hash = hash_password(DEFAULT_PASSWORD)
        user.must_change_password = True
        user.failed_logins = 0
        user.locked_until = None
        now = datetime.now(timezone.utc)
        for sess in (await s.execute(select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None)))).scalars().all():
            sess.revoked_at = now
        await s.commit()
    return {"ok": True, "initial_password": DEFAULT_PASSWORD}


class ProjectsIn(BaseModel):
    project_ids: list[str] = []


@router.put("/members/{user_id}/projects")
async def assign_projects(user_id: str, body: ProjectsIn,
                          ctx: RequestContext = Depends(require_admin())):
    """全量覆盖该成员被分派的项目（只保留属于本团队、仍存在的项目）。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(user_id))
        tm = await s.scalar(select(TeamMember).where(
            TeamMember.user_id == _u(user_id), TeamMember.team_id == _u(ctx.team_id)))
        if user is None or tm is None:
            raise HTTPException(status_code=404, detail="成员不存在")
        valid = []
        for pid in body.project_ids:
            proj = await s.get(Project, _u(pid))
            if proj is not None and str(proj.team_id) == ctx.team_id:
                valid.append(proj.id)
        await s.execute(delete(ProjectMember).where(ProjectMember.user_id == user.id))
        for pid in valid:
            s.add(ProjectMember(project_id=pid, user_id=user.id,
                                role="developer" if tm.role == "developer" else "viewer"))
        await s.commit()
    return {"ok": True, "project_ids": [str(p) for p in valid]}


@router.get("/projects")
async def list_all_projects(ctx: RequestContext = Depends(require_admin())):
    async with SessionLocal() as s:
        projects = (await s.execute(
            select(Project).where(Project.team_id == _u(ctx.team_id))
            .order_by(Project.created_at.desc()))).scalars().all()
        # 档位标签按「项目专属优先、团队默认兜底」查：项目绑定可能指向任一层级的 key
        labels: dict[tuple[str, str], str] = {}
        for pf in (await s.execute(
                select(ModelProfile).where(
                    ModelProfile.team_id == _u(ctx.team_id),
                    ModelProfile.kind == "llm"))).scalars().all():
            labels[(str(pf.project_id) if pf.project_id else "", pf.key)] = pf.label
        return [{
            "id": str(p.id), "name": p.name, "slug": p.slug,
            "repo_path": p.repo_path,
            "archived_at": str(p.archived_at) if p.archived_at else None,
            "model_profile_key": p.model_profile_key,
            "model_label": (labels.get((str(p.id), p.model_profile_key or ""))
                            or labels.get(("", p.model_profile_key or ""))),
            "vcs_type": p.vcs_type or "none",
            "vcs_remote": p.vcs_remote or p.git_remote or "",
            "vcs_branch": p.base_branch or "main",
            "vcs_username": p.vcs_username or "",
            # 令牌/密码只回显「是否已配置」，绝不返回明文
            "vcs_secret_ready": bool(p.vcs_secret),
            "max_concurrency": int(p.max_concurrency or 0),
        } for p in projects]


class ProjectVcsIn(BaseModel):
    vcs_type: str = "none"                 # none|git|svn
    vcs_remote: str | None = None          # git 地址 / svn URL
    vcs_branch: str | None = None          # 仅 git
    vcs_username: str | None = None
    # None = 不改；"" = 清除；非空 = 更新
    vcs_secret: str | None = None


@router.put("/projects/{project_id}/vcs")
async def set_project_vcs(project_id: str, body: ProjectVcsIn,
                          ctx: RequestContext = Depends(require_admin())):
    """项目仓库绑定：Git（地址 + 分支 + 令牌）或 SVN（地址 + 账号密码）。"""
    kind = (body.vcs_type or "none").strip().lower()
    if kind not in ("none", "git", "svn"):
        raise HTTPException(status_code=400, detail="仓库类型只能是 none / git / svn")
    remote = (body.vcs_remote or "").strip()
    if kind != "none" and not remote:
        raise HTTPException(status_code=400,
                            detail="请填写 Git 地址或 SVN URL" if kind == "git" else "请填写 SVN URL")
    if kind == "git" and remote.lower().startswith("svn"):
        raise HTTPException(status_code=400, detail="Git 项目请填写 git 仓库地址")
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None or str(proj.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        proj.vcs_type = kind
        proj.vcs_remote = None if kind == "none" else remote
        proj.vcs_username = ((body.vcs_username or "").strip() or None)
        if kind == "git":
            proj.base_branch = ((body.vcs_branch or "").strip() or "main")
            proj.git_remote = remote
        if body.vcs_secret is not None:
            sec = body.vcs_secret.strip()
            proj.vcs_secret = sec or None
        if kind == "none":
            proj.vcs_secret = None
            proj.vcs_username = None
        await s.commit()
        return {"ok": True, "vcs_type": proj.vcs_type,
                "vcs_secret_ready": bool(proj.vcs_secret)}


@router.post("/projects/{project_id}/vcs/test")
async def test_project_vcs(project_id: str,
                           ctx: RequestContext = Depends(require_admin())):
    """立刻按当前配置同步一次，让管理员当场确认地址/令牌可用。"""
    from game_agent.integrations import vcs as vcs_mod
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None or str(proj.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        if (proj.vcs_type or "none") == "none":
            return {"ok": False, "message": "还没有绑定仓库"}
        # 脱离会话再跑：同步可能耗时几十秒，别占着数据库连接
        snapshot = type("ProjectSnapshot", (), {
            "id": proj.id, "vcs_type": proj.vcs_type, "vcs_remote": proj.vcs_remote,
            "git_remote": proj.git_remote, "base_branch": proj.base_branch,
            "vcs_username": proj.vcs_username, "vcs_secret": proj.vcs_secret,
        })()
        workspace = PROJECTS_ROOT / proj.repo_path
    return await vcs_mod.sync_project_workspace(snapshot, workspace)



class ProjectModelIn(BaseModel):
    model_profile_key: str | None = None


@router.put("/projects/{project_id}/model")
async def set_project_model(project_id: str, body: ProjectModelIn,
                            ctx: RequestContext = Depends(require_admin())):
    """给项目绑定唯一可用的对话模型；传 null 解除绑定（回退角色默认）。"""
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None or str(proj.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        key = (body.model_profile_key or "").strip() or None
        if key is not None:
            # 允许绑定该项目的专属档位，也允许绑定团队默认档位
            prof = await s.scalar(select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id),
                ModelProfile.key == key, ModelProfile.kind == "llm",
                or_(ModelProfile.project_id == proj.id,
                    ModelProfile.project_id.is_(None))))
            if prof is None:
                raise HTTPException(status_code=400, detail="模型档位不存在")
        proj.model_profile_key = key
        await s.commit()
    return {"ok": True, "model_profile_key": key}


class ProjectConcurrencyIn(BaseModel):
    # 同时执行的 run 上限；0 = 不限（超出的 run 留在队列里，按创建顺序等）
    max_concurrency: int = 0


@router.put("/projects/{project_id}/concurrency")
async def set_project_concurrency(project_id: str, body: ProjectConcurrencyIn,
                                  ctx: RequestContext = Depends(require_admin())):
    """设置项目级并发上限。排队逻辑在 LiteQueue.claim 里按这个值限流。"""
    limit = int(body.max_concurrency)
    if limit < 0 or limit > 50:
        raise HTTPException(status_code=400, detail="并发上限需在 0~50 之间（0 = 不限）")
    async with SessionLocal() as s:
        proj = await s.get(Project, _u(project_id))
        if proj is None or str(proj.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="项目不存在")
        proj.max_concurrency = limit
        await s.commit()
    return {"ok": True, "max_concurrency": limit}


@router.get("/model-profiles")
async def list_llm_profiles(ctx: RequestContext = Depends(require_admin())):
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id),
                ModelProfile.kind == "llm").order_by(ModelProfile.created_at))).scalars().all()
        return [{"key": p.key, "label": p.label, "model": p.model,
                 "provider": p.provider,
                 "project_id": str(p.project_id) if p.project_id else None}
                for p in rows]
