# RunPaperPipeline 工具：主 agent 检测到论文任务时调用，触发论文工作流。

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from meharness.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from meharness.agent import Agent


class RunPaperPipelineParams(BaseModel):
    objective: str = Field(description="论文任务目标（一句话）")
    doc_path: str = Field(description="最终论文输出路径（如 artifacts/paper.md）")
    source_data: str | None = Field(default=None, description="原始数据路径（可选）")
    doc_format: str = Field(default="markdown", description="markdown 或 latex")
    skip_literature: bool = Field(default=False, description="跳过文献检索阶段（省时/省费用）")
    skip_data: bool = Field(default=False, description="跳过数据处理阶段")


class RunPaperPipelineTool(Tool):
    name = "RunPaperPipeline"
    description = (
        "Run the full paper workflow: data processing (optional) -> literature "
        "(optional) -> writing -> independent review -> targeted revision loop "
        "-> deterministic quality gate -> formatting. Returns the final paper "
        "path, score history, and pass/fail verdict. Use for paper-writing tasks."
    )
    params_model = RunPaperPipelineParams
    category = "command"
    is_concurrency_safe = False

    def __init__(self, parent_agent: Agent) -> None:
        self._parent_agent = parent_agent

    async def execute(self, params: BaseModel) -> ToolResult:
        p: RunPaperPipelineParams = params  # type: ignore[assignment]

        from meharness.mission.pipeline import PaperPipeline

        artifacts_dir = f"{self._parent_agent.work_dir}/artifacts/pipeline"
        try:
            pipeline = PaperPipeline(
                parent_agent=self._parent_agent,
                objective=p.objective,
                artifacts_dir=artifacts_dir,
                doc_path=p.doc_path,
                source_data=p.source_data,
                doc_format=p.doc_format,
                skip_literature=p.skip_literature,
                skip_data=p.skip_data,
            )
            result = await pipeline.run()
        except Exception as e:
            return ToolResult(output=f"论文工作流失败: {e}", is_error=True)

        return ToolResult(output=(
            f"论文工作流完成。\n"
            f"最终稿: {result.final_path}\n"
            f"最佳稿: {result.best_path}\n"
            f"评审轮次: {result.iterations} 轮\n"
            f"评分历史: {result.score_history}\n"
            f"是否达标: {'✅ 是' if result.passed else '❌ 否'}\n"
            f"{result.review_summary}"
        ))
