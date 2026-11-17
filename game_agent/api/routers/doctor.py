"""运行自检：一眼看清「这一轮到底会不会走真实大模型、还差什么」。

本程序只有一种运行方式：**永远调用真实大模型**。团队版**没有 .env 全局兜底模型**：
模型 / 地址 / Key 只能来自管理员绑定的模型档位（团队默认档位或项目绑定）。所以这里
的判断只有一件事——**绑定的档位是否可用**。

把结论算给前端用：
  * 团队默认档位可用，或至少有一个项目绑定了可用档位 → 就绪；
  * 团队默认没绑、又有项目没绑档位 → 那些工作区里的对话会直接失败，列出来让管理员补；
  * 一个可用档位都没有 → 未就绪，明确提示「去设置 → 模型配置 添加供应商并绑定」。

只读，不发任何真实请求，不会额外花钱。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from game_agent.api.deps import _u, require
from game_agent.auth.rbac import PERM_VIEW
from game_agent.core.context import RequestContext

router = APIRouter(prefix="/api", tags=["doctor"])


async def llm_diagnostics(team_id: str) -> dict:
    """团队视角的 LLM 就绪度快照（供前端顶栏提示 / 设置页自检复用）。"""
    issues: list[str] = []
    hints: list[str] = []

    # ---- 绑定档位 ----
    from game_agent.db.models import Agent, LlmProvider, ModelProfile, Project
    from game_agent.db.session import SessionLocal

    bound_label = None
    bound_model = None
    profile_ok = False
    profile_issue = None
    any_provider = False
    unbound_projects: list[str] = []
    async with SessionLocal() as s:
        any_provider = (await s.scalar(
            select(LlmProvider.id).where(LlmProvider.team_id == _u(team_id)).limit(1))) is not None
        agent = await s.scalar(select(Agent).where(
            Agent.team_id == _u(team_id), Agent.key == "main"))
        team_key = agent.model_profile_key if agent else None
        rows = (await s.execute(select(Project.id, Project.name, Project.model_profile_key).where(
            Project.team_id == _u(team_id), Project.archived_at.is_(None)))).all()
        # 团队默认档位优先；没有就取任一已绑定的项目档位作为就绪度样本。
        pkey, sample_pid = team_key, None
        if not pkey:
            for pid, _name, key in rows:
                if key:
                    pkey, sample_pid = key, pid
                    break
        # 团队默认没绑时，没绑档位的项目都会在发消息时失败——列出来让管理员补。
        if not team_key:
            unbound_projects = [str(name) for _pid, name, key in rows if not key]
        if not pkey:
            profile_issue = "未绑定模型档位：还没有任何项目或智能体绑定模型。"
        else:
            q = select(ModelProfile).where(
                ModelProfile.team_id == _u(team_id), ModelProfile.key == pkey)
            p = (await s.scalar(q.where(ModelProfile.project_id == _u(sample_pid)))
                 if sample_pid else None)
            if p is None:
                p = await s.scalar(q.where(ModelProfile.project_id.is_(None)))
            if p is None:
                profile_issue = f"绑定的模型档位「{pkey}」已不存在（被删但没解绑）。"
            else:
                params = p.params_json or {}
                base_url, api_key, prov_label, prov_enabled = p.base_url, None, None, True
                if p.provider_id:
                    pv = await s.get(LlmProvider, p.provider_id)
                    if pv is None:
                        profile_issue = f"档位「{pkey}」绑定的供应商已被删除。"
                    else:
                        prov_label = pv.label
                        prov_enabled = bool(pv.enabled)
                        base_url = base_url or pv.base_url
                        api_key = pv.api_key
                api_key = api_key or params.get("api_key") or p.api_key_ref
                bound_label = prov_label or p.provider or None
                bound_model = p.model
                if not prov_enabled:
                    profile_issue = f"档位「{pkey}」绑定的供应商「{prov_label}」已停用。"
                elif not p.model:
                    profile_issue = f"档位「{pkey}」没填模型名。"
                elif not api_key:
                    profile_issue = f"档位「{pkey}」没有可用的 API Key（供应商和档位都没配 Key）。"
                elif not base_url:
                    profile_issue = f"档位「{pkey}」没有可用的 Base URL。"
                else:
                    profile_ok = True

    ready = profile_ok
    real_model = bound_model if profile_ok else None
    if not profile_ok and profile_issue:
        issues.append(profile_issue)
    if unbound_projects:
        shown = "、".join(unbound_projects[:5]) + (" 等" if len(unbound_projects) > 5 else "")
        issues.append(f"这些工作区还没绑定模型档位，在里面发消息会直接失败：{shown}。")
    if not (profile_ok and not unbound_projects):
        if any_provider:
            hints.append("供应商已经加好了：到「设置 → 智能体」把作用范围切到该项目，"
                         "选一个已就绪的对话模型。")
        else:
            hints.append("去「设置 → 模型配置」添加一个供应商（填好地址和 Key），"
                         "再到「设置 → 智能体」按团队默认或某个项目绑定模型即可。")

    return {
        # ready = 这一轮确实能调到真实大模型（必须来自绑定档位，没有 .env 兜底）
        "ready": ready,
        "configured": any_provider,
        "profile_ok": profile_ok,
        "profile_issue": profile_issue,
        "provider": bound_label,
        "model": real_model,
        "issues": issues,
        "hints": hints,
    }


@router.get("/doctor/llm")
async def doctor_llm(ctx: RequestContext = Depends(require(PERM_VIEW))):
    """这一轮走不走真实大模型、用的哪个模型/地址、还差什么配置。"""
    return await llm_diagnostics(ctx.team_id)
