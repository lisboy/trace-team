"""LLM 工厂：按配置与角色选择大脑（设计 §2.4 统一适配层）。

模型只来自管理员绑定的「模型档位」（经 ctx.model_profile 传入）；团队版没有 .env
全局兜底——拿不到可用档位就直接报错，由上层（run 收尾 / 设置页自检）告诉用户去绑定。
协议：profile["protocol"]（来自供应商）决定用哪套适配器——
  - anthropic → AnthropicMessagesLLM（/v1/messages 原生协议）
  - 其它/缺省 → OpenAICompatibleLLM（/v1/chat/completions，OpenAI 兼容）
档位 params_json 支持：context_window（上下文窗口 tokens）、vision（是否支持图片输入）、
reasoning_effort（推理强度：none/minimal/low/medium/high，空=不传走端点默认）、
max_tokens（仅 anthropic：单次回复上限）。
"""
from __future__ import annotations

from game_agent.config import (
    LLM_MAX_OUTPUT_TOKENS, LLM_MODEL, LLM_SUB_MAX_OUTPUT_TOKENS,
    LLM_SUB_REASONING_EFFORT, ROLE_MODELS,
)
from game_agent.llm.anthropic_compat import AnthropicMessagesLLM
from game_agent.llm.base import Ctx, LLM
from game_agent.llm.openai_compat import OpenAICompatibleLLM


def _effort_for(role: str, params: dict) -> str:
    """推理强度：档位显式配了就用档位的；否则 sub 角色走 GA_SUB_REASONING_EFFORT。

    sub（只读调研）实测把思考关掉后延迟 -32%、输出 token -61%，而它的产物只是
    「查到的证据 + 结论」，由主agent 复核——这是整条链上最划算的一档。
    主agent / 其它角色保持端点默认（不传），不替用户做质量取舍。
    """
    explicit = str(params.get("reasoning_effort") or "").strip().lower()
    if explicit:
        return explicit
    if role == "sub":
        return (LLM_SUB_REASONING_EFFORT or "").strip().lower()
    return ""


def profile_issue(profile: dict | None) -> str | None:
    """档位可用性检查：返回问题描述（可用返回 None）。

    团队版没有 .env 全局兜底模型，所以「模型 / 地址 / Key」缺一不可；缺了就不要
    构造适配器——否则请求会在很深的地方失败，界面看起来像「模型不响应」。
    """
    if not profile:
        return "未绑定模型档位"
    if not str(profile.get("model") or "").strip():
        return "档位没有模型名"
    if not str(profile.get("base_url") or "").strip():
        return "档位没有可用的 Base URL"
    if not str(profile.get("api_key") or profile.get("api_key_ref") or "").strip():
        return "档位没有可用的 API Key"
    return None


def build_llm(role: str, ctx: Ctx | None = None) -> LLM:
    """构建真实大模型适配器。

    没有 mock 模式，也没有 .env 全局兜底：拿不到可用的绑定档位就抛错，
    由调用方（run 收尾 / 纯文本辅助功能）决定「报给用户」还是「跳过这一步」。
    """
    if ctx is not None and isinstance(ctx.model_profile, LLM):
        # 测试/调用方直接注入了一个假大脑：原样返回（本仓库冒烟测试靠它离线跑整轮）。
        return ctx.model_profile
    profile = (ctx.model_profile if ctx else None) or {}
    issue = profile_issue(profile)
    if issue:
        raise RuntimeError(f"{issue}：团队版不提供 .env 全局兜底模型，"
                           "请管理员在「项目配置 / 智能体」里绑定模型档位。")
    params = profile.get("params") or {}
    model = profile.get("model") or ROLE_MODELS.get(role) or LLM_MODEL
    protocol = str(profile.get("protocol") or "openai").lower()
    common = dict(
        role=role, model=model,
        api_key=(profile.get("api_key") or profile.get("api_key_ref")),
        base_url=profile.get("base_url"),
        context_window=int(params.get("context_window") or 0),
        vision=bool(params.get("vision")),
        user_agent=profile.get("user_agent"),
        reasoning_effort=_effort_for(role, params),
    )
    if protocol == "anthropic":
        return AnthropicMessagesLLM(max_tokens=int(params.get("max_tokens") or 0), **common)
    # 输出上限：档位显式配了就用它；否则 sub 角色默认走 LLM_SUB_MAX_OUTPUT_TOKENS
    # （只读调研，产出只是一份报告，长尾 60k tokens 的报告纯属浪费，见 config 注释），
    # 其余角色仍用全局 GA_LLM_MAX_TOKENS（默认 0 = 不设，避免截断代码写入/工具调用）。
    max_out = int(params.get("max_tokens") or 0)
    if not max_out:
        max_out = LLM_SUB_MAX_OUTPUT_TOKENS if role == "sub" else LLM_MAX_OUTPUT_TOKENS
    return OpenAICompatibleLLM(max_tokens=max_out, **common)
