# -*- coding: utf-8 -*-
"""连接/模型异常时不许刷「系统自动续跑」。

真实事故（2026-11-17 用户截图）：模型端点返回 302（Base URL 填错），聊天流里
连续出现 5 条「系统自动续跑：上一轮只勘察没落地，已排队接着做」，最后才弹一条
执行失败。用户看到的就是「连接失败就一堆这个提示」。

根因两条：
  1. 模型一个字都没吐、也没落地时，run 仍按「只勘察没落地」排一条续跑；端点坏着，
     每轮都空手而归，于是续跑提示一条接一条。这种轮次应该停下来、给一句可操作的说明。
  2. 302/404 这类「地址不对」被当成可重试错误，同一次失败要连撞 5 次。

本文件钉住：
  1. 302/404 归类为不可重试的端点错误，且消息直指 Base URL；
  2. 错误响应体按响应头 → utf-8 → gb18030 解码，中文原因不再变成「□□」；
  3. 模型整轮无输出且零落地时，run 收尾不再排自动续跑，只留一条系统说明；
  4. 「要不要落地」按意图识别判断：survey（问结论/调研）零落地是正常交付，不续跑；
     modify 且模型自称改过却零落地（自相矛盾）才续跑；
  5. MySQL 的整轮上下文用 LONGTEXT 建列（TEXT 只有 64KB，中文两万字就 1406）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Coroutine

import pytest

from game_agent.llm.base import LLM, LLMResponse


def _run(coro: Coroutine[Any, Any, Any], timeout: float = 180) -> Any:
    async def _wrapped() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_wrapped())


# ---------------------------------------------------------------- 1. 端点错误分类
def test_redirect_is_non_retryable_bad_endpoint() -> None:
    from game_agent.llm.openai_compat import LlmBadEndpoint, _classify_http_error

    err = _classify_http_error(302, "", hint="端点=https://example.com 模型=m")
    assert isinstance(err, LlmBadEndpoint)
    assert "Base URL" in str(err)
    assert "不重试" in str(err)


@pytest.mark.parametrize("status", [301, 307, 404, 405])
def test_other_deterministic_http_errors_are_bad_endpoint(status: int) -> None:
    from game_agent.llm.openai_compat import LlmBadEndpoint, _classify_http_error

    assert isinstance(_classify_http_error(status, ""), LlmBadEndpoint)


def test_error_body_falls_back_to_gbk() -> None:
    """头里声明 utf-8、正文其实是 GBK 时，错误原因必须能读出来。"""
    from game_agent.llm.openai_compat import _decode_body

    raw = "请求被重定向到登录页".encode("gbk")
    assert _decode_body(raw, "utf-8") == "请求被重定向到登录页"
    assert _decode_body(raw, None) == "请求被重定向到登录页"


# ---------------------------------------------------------------- 2. 静默轮次不续跑
class _SilentLLM(LLM):
    """每一轮都返回空文本、零工具调用（模拟端点坏了/档位不可用）。"""

    def __init__(self) -> None:
        self.model = "silent-test"
        self.context_window = 32000
        self.reasoning_effort = ""
        self.tool_names: set[str] = set()

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None, *,
                   timeout_s=None, max_attempts=None) -> LLMResponse:  # type: ignore[no-untyped-def]
        return LLMResponse(text="", usage={"prompt_tokens": 10, "completion_tokens": 0},
                           latency_ms=1)


@pytest.mark.slow
def test_silent_round_does_not_enqueue_auto_continue(session_factory, monkeypatch) -> None:
    """模型整轮无输出 + 零落地：不排续跑，只落一条「已停止自动续跑」的系统说明。"""
    import game_agent.agents.runtime as rt_mod
    from game_agent.core.lite.container import build_lite_services
    from game_agent.db.models import Message, Project, Run, Session, Team, User
    from game_agent.orchestration.orchestrator import Orchestrator

    monkeypatch.setattr(rt_mod, "build_llm", lambda role, ctx=None: _SilentLLM())

    async def flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"silent-{suffix}"
        async with session_factory() as s:
            user = User(email=f"silent-{suffix}@example.com", display_name="静默用户")
            team = Team(name="静默团队", slug=f"silent-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="静默项目", slug=repo_path,
                              repo_path=repo_path, project_type="python",
                              created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id,
                              title="静默会话", created_by=user.id)
            s.add(session)
            await s.flush()
            msg = Message(session_id=session.id, author_type="user",
                          author_id=user.id, content="你好")
            s.add(msg)
            await s.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, trigger_msg_id=msg.id,
                      run_type="modify", status="queued")
            s.add(run)
            await s.commit()
            ids = {"session_id": str(session.id), "run_id": str(run.id),
                   "repo_path": repo_path}

        svcs = await build_lite_services(session_factory, "silent-worker")
        orch = Orchestrator(svcs, "silent-worker", session_factory)
        await orch.execute_run(ids["run_id"])
        run_after = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        return ids, run_after, messages

    ids, run_after, messages = _run(flow())

    assert run_after is not None and run_after.status == "succeeded", \
        f"静默轮次应正常收尾：status={getattr(run_after, 'status', None)}"
    auto = [m for m in messages if (m.meta_json or {}).get("auto_continue_from")]
    assert not auto, f"模型无输出时不该排队续跑，实际排了 {len(auto)} 条"
    notes = [m for m in messages if m.author_type == "system"
             and "已停止自动续跑" in (m.content or "")]
    assert notes, "没有留下「已停止自动续跑」的系统说明"


@pytest.mark.slow
def test_auto_continue_depth_counts_chain(session_factory) -> None:
    """续跑链深度要数得准：这是 AUTO_CONTINUE_MAX 的唯一刹车。

    截图事故里刷了 5 条续跑；如果这里数错（永远返回 0），AUTO_CONTINUE_MAX=2
    就形同虚设，端点坏着会一直往后排。
    """
    from game_agent.core.lite.container import build_lite_services
    from game_agent.db.models import Message, Project, Run, Session, Team, User
    from game_agent.orchestration.orchestrator import Orchestrator

    async def flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"depth-{suffix}"
        async with session_factory() as s:
            user = User(email=f"depth-{suffix}@example.com", display_name="深度用户")
            team = Team(name="深度团队", slug=f"depth-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name="深度项目", slug=repo_path,
                              repo_path=repo_path, project_type="python",
                              created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id,
                              title="深度会话", created_by=user.id)
            s.add(session)
            await s.flush()
            first = Message(session_id=session.id, author_type="user",
                            author_id=user.id, content="原始需求")
            s.add(first)
            await s.flush()
            runs = []
            prev_run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                           triggered_by=user.id, trigger_msg_id=first.id,
                           run_type="modify", status="succeeded")
            s.add(prev_run)
            await s.flush()
            runs.append(prev_run)
            for _ in range(3):
                note = Message(session_id=session.id, author_type="user",
                               author_id=user.id, content="继续完成任务目标",
                               meta_json={"auto_continue_from": str(prev_run.id)})
                s.add(note)
                await s.flush()
                nxt = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                          triggered_by=user.id, trigger_msg_id=note.id,
                          run_type="modify", status="succeeded")
                s.add(nxt)
                await s.flush()
                runs.append(nxt)
                prev_run = nxt
            await s.commit()
            ids = {"run_id": str(runs[-1].id)}

        svcs = await build_lite_services(session_factory, "depth-worker")
        orch = Orchestrator(svcs, "depth-worker", session_factory)
        run = await svcs.repo.get_run(ids["run_id"])
        return await orch._auto_continue_depth(run)

    assert _run(flow()) == 3


class _ResearchLLM(LLM):
    """按设定文本作答、零工具调用（正常回答轮次）。"""

    def __init__(self, text: str) -> None:
        self.model = "research-test"
        self.context_window = 32000
        self.reasoning_effort = ""
        self.tool_names: set[str] = set()
        self._text = text

    async def chat(self, messages, tools, ctx, on_delta=None, summarizer=None, *,
                   timeout_s=None, max_attempts=None) -> LLMResponse:  # type: ignore[no-untyped-def]
        return LLMResponse(text=self._text,
                           usage={"prompt_tokens": 100, "completion_tokens": 50},
                           latency_ms=1)


def _run_once(session_factory, monkeypatch, *, user_text: str, answer: str, tag: str):
    """跑一轮 execute_run（零工具调用），返回 (run, messages)。"""
    import game_agent.agents.runtime as rt_mod
    from game_agent.core.lite.container import build_lite_services
    from game_agent.db.models import Message, Project, Run, Session, Team, User
    from game_agent.orchestration.orchestrator import Orchestrator

    monkeypatch.setattr(rt_mod, "build_llm",
                        lambda role, ctx=None: _ResearchLLM(answer))

    async def flow():
        suffix = uuid.uuid4().hex[:8]
        repo_path = f"{tag}-{suffix}"
        async with session_factory() as s:
            user = User(email=f"{tag}-{suffix}@example.com", display_name="测试用户")
            team = Team(name=f"{tag}团队", slug=f"{tag}-{suffix}")
            s.add_all([user, team])
            await s.flush()
            project = Project(team_id=team.id, name=f"{tag}项目", slug=repo_path,
                              repo_path=repo_path, project_type="python",
                              created_by=user.id)
            s.add(project)
            await s.flush()
            session = Session(team_id=team.id, project_id=project.id,
                              title=f"{tag}会话", created_by=user.id)
            s.add(session)
            await s.flush()
            msg = Message(session_id=session.id, author_type="user",
                          author_id=user.id, content=user_text)
            s.add(msg)
            await s.flush()
            run = Run(team_id=team.id, project_id=project.id, session_id=session.id,
                      triggered_by=user.id, trigger_msg_id=msg.id,
                      run_type="modify", status="queued")
            s.add(run)
            await s.commit()
            ids = {"session_id": str(session.id), "run_id": str(run.id)}

        svcs = await build_lite_services(session_factory, f"{tag}-worker")
        orch = Orchestrator(svcs, f"{tag}-worker", session_factory)
        await orch.execute_run(ids["run_id"])
        run_after = await svcs.repo.get_run(ids["run_id"])
        messages = await svcs.repo.list_messages(ids["session_id"])
        return run_after, messages

    return _run(flow())


@pytest.mark.slow
def test_survey_intent_answer_does_not_enqueue_auto_continue(
        session_factory, monkeypatch) -> None:
    """用户在问结论（意图识别=survey）时，没改文件就是正常交付，不许续跑。

    真实事故（2026-11-17 用户截图）：调研汇报正常交卷后，聊天流里还连排两条
    「系统自动续跑：上一轮只勘察没落地」。用户指出：要不要落地取决于用户需求，
    系统自己已经有意图识别，不能拿「没改文件」当没做完。
    """
    run_after, messages = _run_once(
        session_factory, monkeypatch,
        user_text="分析一下这个项目的结构，给个结论",
        # 汇报里带「已生成」这类动作措辞也很常见（生成了报告本身就是交付），
        # 但用户只要结论 → 意图 survey，零落地同样是正常收尾。
        answer="本轮是纯调研，没有改动任何文件。已生成完整结论：这个项目分两层……",
        tag="survey")

    assert run_after is not None and run_after.status == "succeeded"
    auto = [m for m in messages if (m.meta_json or {}).get("auto_continue_from")]
    assert not auto, f"survey 轮次正常回答不该排队续跑，实际排了 {len(auto)} 条"
    assert any("纯调研" in (m.content or "") for m in messages), "正常回答应落在聊天里"


@pytest.mark.slow
def test_modify_intent_claim_without_landing_still_continues(
        session_factory, monkeypatch) -> None:
    """用户要改东西、模型却说「已修改」却零落地：自相矛盾，仍要自动续跑把活做完。"""
    run_after, messages = _run_once(
        session_factory, monkeypatch,
        user_text="把登录页改成深色主题",
        answer="已修改登录页的背景色。",
        tag="claim")

    assert run_after is not None and run_after.status == "succeeded"
    auto = [m for m in messages if (m.meta_json or {}).get("auto_continue_from")]
    assert len(auto) == 1, f"声称改过却没落地应续跑一轮，实际 {len(auto)} 条"


def test_mysql_ddl_uses_longtext_for_transcript_columns() -> None:
    """MySQL 的 TEXT 只有 64KB：整轮上下文落库会 1406 Data too long（实测事故）。"""
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable

    from game_agent.db.models import LlmTranscript

    ddl = str(CreateTable(LlmTranscript.__table__).compile(dialect=mysql.dialect()))
    assert "request_json LONGTEXT NOT NULL" in ddl
    assert "response_json LONGTEXT NOT NULL" in ddl
