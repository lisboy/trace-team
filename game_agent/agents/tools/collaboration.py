"""协作与汇报工具。

v3 直连架构下唯一保留的协作工具是 report_to_user（主agent 收尾汇报 / 子agent
回传调研结论）。图片、音频、视频生成能力已在团队版中移除。
"""
from __future__ import annotations

from game_agent.agents.tools import ToolContext, ToolError


async def report_to_user(args: dict, t: ToolContext) -> str:
    if t.orchestrator is None:
        raise ToolError("编排器不可用")
    report = args.get("report", "")
    # 子agent 的「汇报」其实是交给主agent 的调研结论：单独存到 task 维度。
    # 以前和主agent 共用 set_final_summary(run_id)，子agent 一汇报就把用户看到的
    # 最终总结顶成子任务的片段（run 54fbf901 实测：主agent 还没收尾，final_summary
    # 已经是某个子agent 的结论）。
    if getattr(t, "role", "") == "sub" and getattr(t, "task_id", ""):
        await t.orchestrator.set_sub_summary(str(t.task_id), report)
        return "已汇报给主agent。"
    await t.orchestrator.set_final_summary(t.run_ctx.run_id, report)
    return "已汇报给用户。"
