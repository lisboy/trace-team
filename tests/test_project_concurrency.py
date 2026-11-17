"""项目级并发上限：达到上限的 run 留在队列里，跑完一个才领下一个。"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select, update

from game_agent.core.lite.queue import LiteQueue
from game_agent.db.models import Project, Run, Session, Team, User


def _seed(session_factory, projects: list[tuple[int, int]]):
    """projects: [(并发上限, run 数)]，返回 (queue, 每个项目的 run id 列表)。"""
    key = uuid.uuid4().hex[:8]

    async def go():
        async with session_factory() as db:
            # 队列是全局的：先清掉其它用例遗留的 queued run，否则 claim 会领到别人的 run
            await db.execute(update(Run).where(Run.status == "queued")
                             .values(status="cancelled"))
            team = Team(name="Q", slug=f"q-{key}")
            db.add(team)
            await db.flush()
            user = User(email=f"q-{key}@test.local", display_name="并发测试",
                        username=f"q_{key}")
            db.add(user)
            await db.flush()
            out = []
            for i, (limit, n) in enumerate(projects):
                proj = Project(team_id=team.id, name=f"P{i}", slug=f"p-{key}-{i}",
                               repo_path=f"p-{key}-{i}", max_concurrency=limit,
                               created_by=user.id)
                db.add(proj)
                await db.flush()
                ids = []
                for j in range(n):
                    sess = Session(team_id=team.id, project_id=proj.id,
                                   created_by=user.id, title=f"s{i}-{j}")
                    db.add(sess)
                    await db.flush()
                    run = Run(team_id=team.id, project_id=proj.id, session_id=sess.id,
                              triggered_by=user.id, run_type="analyze", status="queued")
                    db.add(run)
                    await db.flush()
                    ids.append(str(run.id))
                out.append(ids)
            await db.commit()
            return out

    return asyncio.run(go())


def _claim(queue: LiteQueue) -> str | None:
    return asyncio.run(queue.claim())


def _finish(session_factory, run_id: str) -> None:
    async def go():
        async with session_factory() as db:
            await db.execute(update(Run).where(Run.id == uuid.UUID(run_id))
                             .values(status="succeeded"))
            await db.commit()

    asyncio.run(go())


def _status(session_factory, run_id: str) -> str:
    async def go():
        async with session_factory() as db:
            return await db.scalar(select(Run.status).where(Run.id == uuid.UUID(run_id)))

    return asyncio.run(go())


def test_project_limit_queues_extra_runs(session_factory):
    queue = LiteQueue(session_factory)
    [runs] = _seed(session_factory, [(2, 3)])

    first, second = _claim(queue), _claim(queue)
    assert first and second and first != second
    assert {first, second} <= set(runs)
    remaining = (set(runs) - {first, second}).pop()
    # 上限 2：第三个会话的 run 必须继续排队
    assert _claim(queue) is None
    assert _status(session_factory, remaining) == "queued"

    _finish(session_factory, first)
    assert _claim(queue) == remaining
    assert _status(session_factory, remaining) == "claimed"


def test_zero_means_unlimited(session_factory):
    queue = LiteQueue(session_factory)
    [runs] = _seed(session_factory, [(0, 3)])
    claimed = {_claim(queue) for _ in range(3)}
    assert claimed == set(runs)


def test_limit_does_not_block_other_projects(session_factory):
    queue = LiteQueue(session_factory)
    p1, p2 = _seed(session_factory, [(1, 2), (1, 2)])
    first, second = _claim(queue), _claim(queue)
    # 两个项目各领一个；各自第二个 run 被限流跳过
    assert {first, second} == {p1[0], p2[0]}
    assert _claim(queue) is None
    _finish(session_factory, p1[0])
    assert _claim(queue) == p1[1]
