"""首启：创建默认团队与管理员账号，并保证 5 个角色配置存在。

团队版不再有「本机默认身份」：首次初始化建一个团队 + 管理员 ``admin``
（初始密码 666666，首次登录强制重置），之后的账号都由管理员在团队页创建。
已有团队/管理员时只补齐缺失的角色配置，不动任何已有数据（升级安全）。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from game_agent.auth.passwords import DEFAULT_PASSWORD, hash_password
from game_agent.config import PROJECTS_ROOT, SEED_DEMO, default_python_bin
from game_agent.core.registry import seed_default_agents
from game_agent.db.models import Project, Team, TeamMember, User
from game_agent.db.session import SessionLocal
from game_agent.logging_setup import get_logger

log = get_logger("bootstrap")

DEMO_PROJECT_SLUG = "cardgame"
ADMIN_USERNAME = "admin"
TEAM_NAME = "循迹团队"


def _safe_print(text: str) -> None:
    """控制台输出兜底：Windows 中文控制台是 GBK，遇到 ⚠/emoji 等字符会
    UnicodeEncodeError 直接把进程带崩（打包版首启实测）。统一兜底成可替换，
    任何编码问题都只影响这一行显示，绝不阻断服务启动。"""
    try:
        print(text)
    except Exception:  # noqa: BLE001 - 打印失败不允许影响启动
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


async def bootstrap(auth=None) -> None:  # type: ignore[no-untyped-def]
    """准备团队/管理员（+ 示例项目）。``auth`` 参数只为兼容旧调用方。"""
    created_admin = False
    async with SessionLocal() as s:
        team = await s.scalar(select(Team).order_by(Team.created_at))
        if team is None:
            team = Team(name=TEAM_NAME, slug=f"team-{uuid.uuid4().hex[:8]}")
            s.add(team)
            await s.flush()

        # 团队必须至少有一个管理员；没有就补默认 admin（首次初始化就是这条路径）
        has_admin = await s.scalar(
            select(TeamMember.user_id).where(
                TeamMember.team_id == team.id,
                TeamMember.role.in_(("owner", "admin"))).limit(1))
        if has_admin is None:
            admin = await s.scalar(select(User).where(User.username == ADMIN_USERNAME))
            if admin is None:
                admin = User(
                    username=ADMIN_USERNAME,
                    email=f"{ADMIN_USERNAME}@trace.local",
                    display_name="管理员",
                    password_hash=hash_password(DEFAULT_PASSWORD),
                    must_change_password=True,
                    is_active=True,
                    is_superuser=True,
                )
                s.add(admin)
                await s.flush()
            s.add(TeamMember(team_id=team.id, user_id=admin.id, role="owner"))
            created_admin = True

        await s.commit()
        await seed_default_agents(s, team.id)
    log.info("bootstrap ready", extra={"team": str(team.id), "admin_created": created_admin})

    if SEED_DEMO:
        await seed_demo()
    if created_admin:
        _safe_print("\n" + "=" * 60)
        _safe_print(" 循迹（Trace）团队版已初始化")
        _safe_print(f" 管理员账号：{ADMIN_USERNAME}")
        _safe_print(f" 初始密码：{DEFAULT_PASSWORD}（首次登录必须修改）")
        _safe_print("=" * 60 + "\n")


async def seed_demo() -> None:
    """拷贝示例项目并登记到默认团队（幂等：已存在就跳过）。"""
    src = Path(__file__).resolve().parent.parent / "examples" / "cardgame"
    dst = PROJECTS_ROOT / DEMO_PROJECT_SLUG
    if src.exists() and not dst.exists():
        try:
            shutil.copytree(src, dst)
        except Exception:  # noqa: BLE001 - 示例项目拷贝失败不应阻断首启
            log.exception("示例项目拷贝失败（忽略）")

    async with SessionLocal() as s:
        team = await s.scalar(select(Team).order_by(Team.created_at))
        if team is None or not dst.exists():
            return
        existing = await s.scalar(select(Project).where(
            Project.team_id == team.id, Project.slug == DEMO_PROJECT_SLUG))
        if existing is None:
            s.add(Project(team_id=team.id, name="CardGame 示例",
                          slug=DEMO_PROJECT_SLUG, repo_path=DEMO_PROJECT_SLUG,
                          project_type="python",
                          python_bin=default_python_bin() or None,
                          commands_json={"build": "python -m compileall -q .",
                                         "test": "python -m pytest -q"}))
            await s.commit()
            log.info("示例项目已登记")
        await seed_default_agents(s, team.id)
