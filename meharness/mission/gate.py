"""确定性质量门 + 收敛判定（P2 核心）。

评审反馈来自 reviewer 专家（LLM），但"达不达标 / 收不收敛"由这里
的纯函数判定——这是整个"论文质量够不够"的负责人，不交给任何 agent
（避免写作者自我感觉良好地提前交付）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RubricDim(BaseModel):
    weight: float = 1.0


class GateSpec(BaseModel):
    min_per_dim: float = 7.0
    min_weighted: float = 8.0


class ConvergenceSpec(BaseModel):
    max_iterations: int = 5
    min_improvement: float = 0.5
    window: int = 2


class Rubric(BaseModel):
    dims: dict[str, RubricDim] = Field(description="评审维度 -> 权重")
    gate: GateSpec = Field(default_factory=GateSpec)
    convergence: ConvergenceSpec = Field(default_factory=ConvergenceSpec)


def weighted_score(scores: dict[str, float], rubric: Rubric) -> float:
    """加权平均分（只统计 rubric 定义的维度；缺失维度记 0）。"""
    total_w = sum(d.weight for d in rubric.dims.values()) or 1.0
    acc = sum(scores.get(dim, 0.0) * spec.weight for dim, spec in rubric.dims.items())
    return round(acc / total_w, 2)


def gate_passed(scores: dict[str, float], rubric: Rubric) -> bool:
    """全维度 >= min_per_dim 且 加权平均 >= min_weighted 才算通过。"""
    if not rubric.dims:
        return True
    per_dim = all(scores.get(dim, 0.0) >= rubric.gate.min_per_dim for dim in rubric.dims)
    if not per_dim:
        return False
    return weighted_score(scores, rubric) >= rubric.gate.min_weighted


def failing_dims(scores: dict[str, float], rubric: Rubric) -> list[str]:
    """列出未达标的维度（供定向修订）。"""
    out = []
    for dim, spec in rubric.dims.items():
        s = scores.get(dim, 0.0)
        if s < rubric.gate.min_per_dim:
            out.append(f"{dim}({s}<{rubric.gate.min_per_dim})")
    return out


def convergence_stalled(history: list[float], rubric: Rubric) -> bool:
    """连续 window 轮加权分提升 < min_improvement → 收益递减，停止迭代。"""
    c = rubric.convergence
    if len(history) <= c.window:
        return False
    return all(
        history[-1] - history[i] < c.min_improvement
        for i in range(-c.window, 0)
    )


def best_index(history: list[float]) -> int:
    """评分最高轮次的下标（保留最佳版本）。"""
    return max(range(len(history)), key=lambda i: history[i])


def default_rubric() -> Rubric:
    """论文评审默认 rubric（可按领域/目标场合替换）。"""
    return Rubric(
        dims={
            "contribution": RubricDim(weight=0.20),   # 创新性与贡献
            "rigor": RubricDim(weight=0.25),          # 假设-方法-结果一致性
            "experiments": RubricDim(weight=0.25),    # 基线/消融/显著性
            "clarity": RubricDim(weight=0.15),        # 结构与论证
            "related_work": RubricDim(weight=0.10),   # 综述完整性
            "writing": RubricDim(weight=0.05),        # 语言/图表/格式
        },
        gate=GateSpec(min_per_dim=7.0, min_weighted=8.0),
        convergence=ConvergenceSpec(max_iterations=5, min_improvement=0.5, window=2),
    )
