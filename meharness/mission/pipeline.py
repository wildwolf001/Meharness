"""论文工作流：修订环管线（SOP + Task/Artifact + 确定性质量门）。

结构：
- 可选前置阶段：数据工程师（处理原始数据）、文献检索（相关工作）
- 核心修订环：写作 → 审核（结构化 JSON 评分）→ 质量门判定
  → 未过则把问题清单定向传给写作专家逐节修改 → 再审核 → 直到
  rubric 全维度达标或收敛（收益递减）→ 格式规范排版
- 全程工件经 ArtifactStore 流转，best 版本 + 评分历史归档
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meharness.agents.spawn import build_specialist_agent
from meharness.mission.artifact import Artifact, ArtifactStore
from meharness.mission.gate import (
    Rubric,
    best_index,
    convergence_stalled,
    default_rubric,
    failing_dims,
    gate_passed,
    weighted_score,
)

log = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    scores: dict[str, float]
    issues: list[dict]
    verdict: str = ""
    raw: str = ""


@dataclass
class PipelineResult:
    final_path: str
    best_path: str
    score_history: list[float]
    iterations: int
    passed: bool
    review_summary: str


class PaperPipeline:
    def __init__(
        self,
        parent_agent: Any,
        objective: str,
        artifacts_dir: str,
        doc_path: str,
        rubric: Rubric | None = None,
        source_data: str | None = None,
        doc_format: str = "markdown",
        skip_literature: bool = False,
        skip_data: bool = False,
    ) -> None:
        self.parent = parent_agent
        self.objective = objective
        self.store = ArtifactStore(artifacts_dir)
        self.doc_path = Path(doc_path)
        self.rubric = rubric or default_rubric()
        self.source_data = source_data
        self.doc_format = doc_format
        self.skip_literature = skip_literature
        self.skip_data = skip_data
        self._iter = 0
        self._draft_path: Path | None = None

    # -------------------------------------------------------------
    # 阶段执行
    # -------------------------------------------------------------

    async def _run_specialist(
        self,
        card_name: str,
        task: str,
        *,
        background: bool = False,
    ) -> str:
        """唤醒一个专家并等待其完成，返回最终文本。"""
        from meharness.agents.cards import REGISTRY
        card = REGISTRY.get(card_name)
        if card is None:
            return f"(未知专家 {card_name})"
        agent = build_specialist_agent(
            card,
            parent_agent=self.parent,
            parent_registry=self.parent.registry,
        )
        return await agent.run_to_completion(task)

    # -------------------------------------------------------------
    # 修订环核心
    # -------------------------------------------------------------

    async def _write(self, *, revise_issues: list[dict] | None = None) -> Path:
        """写作专家产出/修订论文草稿，写入 artifacts/draft/paper_v{N}。"""
        self._iter += 1
        draft_dir = self.store.ensure_dir("draft")
        suffix = ".tex" if self.doc_format == "latex" else ".md"
        path = draft_dir / f"paper_v{self._iter}{suffix}"
        ext = "LaTeX" if self.doc_format == "latex" else "Markdown"

        if revise_issues:
            task = (
                f"修订论文草稿（第 {self._iter} 轮）。目标：{self.objective}\n\n"
                f"当前草稿：{self._draft_path}\n"
                f"评审意见（逐节定向修改，不要泛泛重写）：\n{json.dumps(revise_issues, ensure_ascii=False)}\n\n"
                f"把修订后的完整论文写入：{path}（{ext} 格式）。\n"
                f"确保解决每条意见；报告你改了什么。"
            )
        else:
            inputs = self.store.describe()
            task = (
                f"撰写论文初稿（第 {self._iter} 轮）。目标：{self.objective}\n\n"
                f"可用素材：\n{inputs}\n\n"
                f"把完整论文写入：{path}（{ext} 格式）。\n"
                f"结构：标题/摘要/引言/方法/实验/结论/参考文献。"
            )
        await self._run_specialist("writer", task)
        self._draft_path = path
        self.store.register(Artifact(kind="paper-draft", path=str(path), producer="writer"))
        return path

    async def _review(self, draft_path: Path) -> ReviewResult:
        """审核专家（只读）独立评审，输出结构化 JSON 评分到 artifacts/review/。"""
        review_dir = self.store.ensure_dir("review")
        path = review_dir / f"review_v{self._iter}.json"
        dims = ", ".join(self.rubric.dims.keys())
        task = (
            f"以 fresh 视角独立评审论文草稿：{draft_path}\n"
            f"任务目标：{self.objective}\n\n"
            f"按维度 {dims} 各打 0-10 分，并给出逐节问题清单。\n"
            f"【必须】用 WriteFile 把评审结果以 JSON 写入文件 {path}，结构：\n"
            f'{{"scores": {{"contribution": 0, "rigor": 0, ...}}, '
            f'"issues": [{{"section": "...", "severity": "high|medium|low", "description": "..."}}], '
            f'"verdict": "accept|needs_revision"}}\n'
            f"同时，在回复文本里也逐维度列出分数，格式：contribution: 8, rigor: 7, ...\n"
            f"只读评审，不要修改论文文件。"
        )
        result = await self._run_specialist("reviewer", task)
        return self._parse_review(path, result)

    def _parse_review(self, path: Path, fallback_text: str) -> ReviewResult:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                scores = {k: float(v) for k, v in (data.get("scores") or {}).items()}
                issues = data.get("issues") or []
                verdict = data.get("verdict", "")
                return ReviewResult(scores=scores, issues=issues, verdict=verdict, raw=fallback_text)
            except Exception as e:
                log.warning("评审 JSON 解析失败，回退文本提取: %s", e)
        # 回退 1：从文本里找内联 JSON 块
        m = re.search(r"\{[^{}]*\"scores\"[^{}]*\}", fallback_text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                scores = {k: float(v) for k, v in (data.get("scores") or {}).items()}
                return ReviewResult(scores=scores, issues=data.get("issues") or [],
                                    verdict=data.get("verdict", ""), raw=fallback_text)
            except Exception:
                pass
        # 回退 2：从文本逐维度提取数字（如 "contribution: 8"）
        scores = {}
        for dim in self.rubric.dims:
            m = re.search(rf"{dim}\s*[:：]?\s*(\d+(?:\.\d+)?)", fallback_text, re.I)
            if m:
                scores[dim] = float(m.group(1))
        return ReviewResult(scores=scores, issues=[], verdict="needs_revision", raw=fallback_text)

    async def _format(self, draft_path: Path) -> Path:
        """格式专家规范排版最终稿。"""
        ext = ".tex" if self.doc_format == "latex" else ".md"
        final = self.store.ensure_dir("final") / f"paper_final{ext}"
        task = (
            f"规范排版论文最终稿。目标：{self.objective}\n"
            f"来源草稿：{draft_path}\n"
            f"统一格式（{'LaTeX 排版' if self.doc_format == 'latex' else 'Markdown 规范'}）、"
            f"图表编号、参考文献格式，输出到：{final}"
        )
        await self._run_specialist("formatter", task)
        # 格式专家若没写，回退用草稿
        return final if final.exists() else draft_path

    # -------------------------------------------------------------
    # 主入口
    # -------------------------------------------------------------

    async def run(self) -> PipelineResult:
        # 前置：数据
        if self.source_data and not self.skip_data:
            data_dir = self.store.ensure_dir("data")
            data_task = (
                f"处理实验数据 {self.source_data}：清洗、统计、生成统计表。"
                f"目标：{self.objective}\n"
                f"输出统计结果到 {data_dir / 'statistics.json'} 和必要图表。"
            )
            await self._run_specialist("data-engineer", data_task)
            self.store.register(Artifact(
                kind="processed-data", path=str(data_dir / "statistics.json"),
                producer="data-engineer",
            ))

        # 前置：文献
        if not self.skip_literature:
            lit_dir = self.store.ensure_dir("literature")
            lit_task = (
                f"检索与目标相关的领域研究并总结方法论：{self.objective}\n"
                f"输出相关工作笔记到 {lit_dir / 'related_work.md'}。"
            )
            await self._run_specialist("literature-researcher", lit_task)
            self.store.register(Artifact(
                kind="literature-review", path=str(lit_dir / "related_work.md"),
                producer="literature-researcher",
            ))

        # 修订环
        history: list[float] = []
        passed = False
        draft = await self._write()
        max_iter = self.rubric.convergence.max_iterations

        for _ in range(max_iter):
            review = await self._review(draft)
            score = weighted_score(review.scores, self.rubric)
            history.append(score)
            log.info("[pipeline] iter=%d score=%s pass=%s", self._iter, score,
                     gate_passed(review.scores, self.rubric))

            if gate_passed(review.scores, self.rubric):
                passed = True
                break
            if convergence_stalled(history, self.rubric):
                break
            # 未过：定向修订再审
            draft = await self._write(revise_issues=review.issues)

        final = await self._format(draft)

        best = best_index(history) if history else 0
        best_draft = f"{self.store._base / 'draft' / ('paper_v' + str(best + 1))}"
        return PipelineResult(
            final_path=str(final),
            best_path=best_draft,
            score_history=history,
            iterations=self._iter,
            passed=passed,
            review_summary=self._summarize(history, passed),
        )

    @staticmethod
    def _summarize(history: list[float], passed: bool) -> str:
        if not history:
            return "(无评审轮次)"
        trend = " → ".join(str(s) for s in history)
        verdict = "达标" if passed else "未达标（已达收敛或上限）"
        return f"评分历史: [{trend}] 结论: {verdict}"
