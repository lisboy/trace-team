"""团队版没有 .env 全局兜底模型：未绑定档位必须明确报错，不能悄悄用环境变量。

为什么这条必须有：以前 `.env` 的 OPENAI_API_KEY / OPENAI_BASE_URL 会兜住所有「没绑定」
的情况，导致界面显示「已接通」、实际却打到一个早已失效的旧端点（403），
排查成本极高。现在模型 / 地址 / Key 只能来自管理员绑定的模型档位。
"""
from __future__ import annotations

import asyncio

import pytest

from game_agent.llm.base import Ctx
from game_agent.llm.factory import build_llm, profile_issue


def test_profile_issue_requires_model_url_and_key() -> None:
    assert profile_issue(None)
    assert profile_issue({})
    assert "Base URL" in (profile_issue({"model": "m", "api_key": "k"}) or "")
    assert "API Key" in (profile_issue({"model": "m", "base_url": "http://x/v1"}) or "")
    assert profile_issue({"model": "m", "base_url": "http://x/v1", "api_key": "k"}) is None


def test_build_llm_refuses_without_bound_profile(monkeypatch) -> None:
    # 即便环境里还留着旧 Key，也不能被当成默认模型
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    with pytest.raises(RuntimeError, match="未绑定模型档位"):
        build_llm("main", Ctx())
    with pytest.raises(RuntimeError, match="全局兜底"):
        build_llm("main", None)


def test_build_llm_uses_bound_profile_only() -> None:
    llm = build_llm("main", Ctx(model_profile={
        "model": "m", "base_url": "http://x/v1", "api_key": "k"}))
    assert llm.base_url == "http://x/v1"
    assert llm.api_key == "k"


def test_doctor_not_ready_without_binding() -> None:
    """没有任何项目/智能体绑定 → 未就绪，且不再返回 .env 兜底字段。"""
    from game_agent.api.routers.doctor import llm_diagnostics

    res = asyncio.run(llm_diagnostics("00000000-0000-0000-0000-000000000000"))
    assert res["ready"] is False
    assert res["profile_ok"] is False
    assert "fallback_model" not in res
    assert "fallback_has_key" not in res
    assert res["issues"], "未绑定时必须给出可读的 issue"
