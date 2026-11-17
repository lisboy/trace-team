"""项目级配置解析：模型档位 / 智能体（提示词 + 绑定）按项目覆盖，缺省回落团队默认。

团队版把「模型」和「智能体」下沉到项目维度：同一 key 在项目里建了专属行就优先用它，
没建就走团队默认，再没有才用出厂值。这里钉住三条解析路径，避免以后又把项目行忽略掉。
"""
from __future__ import annotations

import asyncio
import uuid

from game_agent.agents.roles import ROLE_PROMPTS
from game_agent.core.registry import get_agent_config
from game_agent.db.models import Agent, ModelProfile, Project, Team
from game_agent.orchestration.orchestrator import Orchestrator


def _run(coro, timeout: float = 20):
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


async def _seed_team(session_factory) -> tuple[str, str]:
    tag = uuid.uuid4().hex[:8]
    async with session_factory() as s:
        team = Team(name="scope-team", slug=f"scope-{tag}")
        s.add(team)
        await s.flush()
        proj = Project(team_id=team.id, name="scope-proj", slug=f"scope-p-{tag}",
                       repo_path=f"scope-p-{tag}")
        s.add(proj)
        await s.commit()
        team_id, project_id = str(team.id), str(proj.id)
    return team_id, project_id


def test_agent_config_prefers_project_row(session_factory) -> None:
    async def _flow():
        team_id, project_id = await _seed_team(session_factory)
        async with session_factory() as s:
            s.add_all([
                Agent(team_id=uuid.UUID(team_id), project_id=None, key="main",
                      system_prompt="TEAM PROMPT", model_profile_key="team-key"),
                Agent(team_id=uuid.UUID(team_id), project_id=uuid.UUID(project_id),
                      key="main", system_prompt="PROJECT PROMPT",
                      model_profile_key="proj-key"),
            ])
            await s.commit()
        async with session_factory() as s:
            scoped = await get_agent_config(s, team_id, "main", project_id=project_id)
            team = await get_agent_config(s, team_id, "main")
        return scoped, team

    scoped, team = _run(_flow())
    assert scoped.system_prompt == "PROJECT PROMPT"
    assert scoped.model_profile_key == "proj-key"
    assert team.system_prompt == "TEAM PROMPT"
    assert team.model_profile_key == "team-key"


def test_agent_config_project_row_can_override_only_model(session_factory) -> None:
    """项目行只绑模型、提示词留空 = 继承团队提示词（不是继承出厂）。"""

    async def _flow():
        team_id, project_id = await _seed_team(session_factory)
        async with session_factory() as s:
            s.add_all([
                Agent(team_id=uuid.UUID(team_id), project_id=None, key="main",
                      system_prompt="TEAM PROMPT"),
                Agent(team_id=uuid.UUID(team_id), project_id=uuid.UUID(project_id),
                      key="main", system_prompt="", model_profile_key="proj-key"),
            ])
            await s.commit()
        async with session_factory() as s:
            return await get_agent_config(s, team_id, "main", project_id=project_id)

    cfg = _run(_flow())
    assert cfg.system_prompt == "TEAM PROMPT"
    assert cfg.model_profile_key == "proj-key"


def test_agent_config_falls_back_to_factory_prompt(session_factory) -> None:
    """团队行也没有时用出厂提示词（seed 前的极端情况）。"""

    async def _flow():
        team_id, _ = await _seed_team(session_factory)
        async with session_factory() as s:
            return await get_agent_config(s, team_id, "main")

    cfg = _run(_flow())
    assert cfg.system_prompt == ROLE_PROMPTS["main"]


def test_load_profile_prefers_project_profile(session_factory) -> None:
    """同 key 的档位：项目专属优先，没有才回落团队默认。"""

    async def _flow():
        team_id, project_id = await _seed_team(session_factory)
        async with session_factory() as s:
            s.add_all([
                ModelProfile(team_id=uuid.UUID(team_id), project_id=None, key="shared",
                             label="team", kind="llm", provider="openai",
                             model="team-model", base_url="http://team.invalid/v1"),
                ModelProfile(team_id=uuid.UUID(team_id), project_id=uuid.UUID(project_id),
                             key="shared", label="project", kind="llm", provider="openai",
                             model="project-model", base_url="http://proj.invalid/v1"),
            ])
            await s.commit()
        orch = Orchestrator(None, "scope-test", session_factory)  # type: ignore[arg-type]
        scoped = await orch._load_profile(team_id, "shared", project_id=project_id)
        team = await orch._load_profile(team_id, "shared")
        other = await orch._load_profile(team_id, "shared",
                                        project_id=str(uuid.uuid4()))
        return scoped, team, other

    scoped, team, other = _run(_flow())
    assert scoped["model"] == "project-model"
    assert scoped["base_url"] == "http://proj.invalid/v1"
    # 没有项目行 / 项目不存在时都必须回落到团队默认档位
    assert team["model"] == "team-model"
    assert other["model"] == "team-model"
