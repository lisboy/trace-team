"""登录 / 退出 / 改密码 / 个人资料。

团队版账号由管理员创建（用户名 + 初始密码 666666），没有邮箱注册。
首次登录（或管理员重置密码后）必须先改密码，否则后端拒绝其他业务接口。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from game_agent.api.deps import _u, get_current_context, is_admin_role
from game_agent.auth.passwords import (
    TOKEN_TTL_DAYS, hash_password, hash_token, needs_rehash, new_token,
    validate_new_password, verify_password,
)
from game_agent.core.context import RequestContext
from game_agent.db.models import AuthSession, Team, TeamMember, User
from game_agent.db.session import SessionLocal

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_payload(u: User, role: str | None = None) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "email": u.email,
        "display_name": u.display_name,
        "avatar": u.avatar or None,
        "is_active": u.is_active,
        "role": role,
        "is_admin": is_admin_role(role or ""),
        "must_change_password": bool(u.must_change_password),
    }


async def _teams_for(s, user_id) -> list[dict]:  # type: ignore[no-untyped-def]
    rows = await s.execute(
        select(Team.id, Team.name, Team.slug, TeamMember.role)
        .join(TeamMember, TeamMember.team_id == Team.id)
        .where(TeamMember.user_id == user_id)
    )
    return [{"id": str(t[0]), "name": t[1], "slug": t[2], "role": t[3]} for t in rows.all()]


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request):
    """用户名 + 密码登录，返回 Bearer 令牌（30 天有效）。"""
    username = (body.username or "").strip()
    if not username or not body.password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        user = await s.scalar(select(User).where(User.username == username))
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if user.locked_until is not None and user.locked_until > now:
            raise HTTPException(status_code=423, detail="尝试次数过多，请稍后再试")
        if not verify_password(user.password_hash, body.password):
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= 10:
                user.locked_until = now + timedelta(minutes=5)
                user.failed_logins = 0
            await s.commit()
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if needs_rehash(user.password_hash or ""):
            user.password_hash = hash_password(body.password)
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        token = new_token()
        s.add(AuthSession(
            user_id=user.id, token_hash=hash_token(token),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            expires_at=now + timedelta(days=TOKEN_TTL_DAYS),
        ))
        await s.commit()
        teams = await _teams_for(s, user.id)
        role = teams[0]["role"] if teams else None
        payload = _user_payload(user, role)
    return {
        "token": token,
        "user": payload,
        "teams": teams,
        "current_team": teams[0]["id"] if teams else None,
        "must_change_password": payload["must_change_password"],
    }


@router.post("/logout")
async def logout(request: Request):
    """吊销当前令牌（幂等：没带/无效令牌也返回 ok）。"""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            async with SessionLocal() as s:
                sess = await s.scalar(select(AuthSession).where(
                    AuthSession.token_hash == hash_token(token)))
                if sess is not None and sess.revoked_at is None:
                    sess.revoked_at = datetime.now(timezone.utc)
                    await s.commit()
    return {"ok": True}


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/change-password")
async def change_password(body: ChangePasswordIn, request: Request,
                          ctx: RequestContext = Depends(get_current_context)):
    """修改自己的密码；成功后解除「首次登录必须改密」。"""
    err = validate_new_password(body.new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        if not verify_password(user.password_hash, body.old_password):
            raise HTTPException(status_code=400, detail="当前密码不正确")
        user.password_hash = hash_password(body.new_password)
        user.must_change_password = False
        # 改密后让其它设备上的旧登录失效，只保留当前令牌
        header = request.headers.get("authorization") or ""
        cur_hash = hash_token(header[7:].strip()) if header.lower().startswith("bearer ") else ""
        now = datetime.now(timezone.utc)
        others = (await s.execute(select(AuthSession).where(
            AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)))).scalars().all()
        for sess in others:
            if sess.token_hash != cur_hash:
                sess.revoked_at = now
        await s.commit()
    return {"ok": True}


@router.get("/me")
async def me(ctx: RequestContext = Depends(get_current_context)):
    """当前账号 + 所在团队 + 本人角色（前端据此决定显示哪些入口）。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        teams = await _teams_for(s, user.id)
        payload = _user_payload(user, ctx.role)
    return {"user": payload, "teams": teams, "current_team": ctx.team_id}


class ProfileIn(BaseModel):
    display_name: str | None = None
    avatar: str | None = None


@router.patch("/me")
async def update_me(body: ProfileIn, ctx: RequestContext = Depends(get_current_context)):
    """改昵称 / 头像地址（头像文件走 /api/auth/me/avatar 上传）。"""
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="本机身份不存在")
        if body.display_name is not None:
            name = (body.display_name or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            if len(name) > 120:
                raise HTTPException(status_code=400, detail="名称不能超过 120 个字符")
            user.display_name = name
        if body.avatar is not None:
            user.avatar = (body.avatar or "").strip() or None
        await s.commit()
        await s.refresh(user)
        payload = _user_payload(user, ctx.role)
    return {"ok": True, "user": payload}


_AVATAR_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_AVATAR_BYTES = 8 * 1024 * 1024


@router.post("/me/avatar")
async def upload_avatar(file: UploadFile, ctx: RequestContext = Depends(get_current_context)):
    """上传头像（图片，≤8MB）。返回可直接用于 <img src> 的 /uploads/avatars/... 地址。"""
    import time
    from pathlib import Path as _Path
    from game_agent.config import DATA_DIR

    avatars = DATA_DIR / "uploads" / "avatars"
    avatars.mkdir(parents=True, exist_ok=True)
    ext = _Path(file.filename or "").suffix.lower()
    ctype = (file.content_type or "").lower()
    if ext not in _AVATAR_EXTS:
        if ctype.startswith("image/"):
            ext = ".png"
        else:
            raise HTTPException(status_code=400, detail="只支持图片文件（png/jpg/webp/gif）")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像不能超过 8MB")
    uid = str(ctx.user_id).replace("-", "")[:16]
    # 文件名带时间戳：同 URL 才不会被浏览器/静态缓存住旧图
    name = f"avatar_{uid}_{int(time.time())}{ext}"
    (avatars / name).write_bytes(data)
    url = f"/uploads/avatars/{name}"
    async with SessionLocal() as s:
        user = await s.get(User, _u(ctx.user_id))
        if user is not None:
            user.avatar = url
            await s.commit()
    return {"ok": True, "avatar": url}
