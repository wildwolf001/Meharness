"""P2 论文工作流：质量门 / 工件存储 / 修订环控制逻辑的确定性测试。

（不调用真实 LLM——用 mock 评分序列验证控制逻辑。）
"""

from __future__ import annotations

import asyncio

from meharness.mission.artifact import Artifact, ArtifactStore
from meharness.mission.gate import (
    best_index,
    convergence_stalled,
    default_rubric,
    failing_dims,
    gate_passed,
    weighted_score,
)
from meharness.mission.pipeline import ReviewResult


# -------------------------------------------------------------
# 质量门
# -------------------------------------------------------------

R = default_rubric()
OK = {"contribution": 8, "rigor": 8, "experiments": 8, "clarity": 8, "related_work": 8, "writing": 8}
LOW = {"contribution": 5, "rigor": 5, "experiments": 5, "clarity": 5, "related_work": 5, "writing": 5}


def test_gate_passed_when_all_dims_ok():
    assert gate_passed(OK, R) is True


def test_gate_fails_when_one_dim_low():
    bad = dict(OK)
    bad["rigor"] = 5
    assert gate_passed(bad, R) is False
    assert "rigor" in failing_dims(bad, R)[0]


def test_weighted_score():
    assert weighted_score(OK, R) == 8.0


def test_convergence_stalled_on_flat_scores():
    assert convergence_stalled([7, 7, 7.2], R) is True
    assert convergence_stalled([7, 7, 9], R) is False


def test_best_index():
    assert best_index([7.5, 8.2, 8.0]) == 1


# -------------------------------------------------------------
# 工件存储
# -------------------------------------------------------------

def test_artifact_store_roundtrip(tmp_path):
    store = ArtifactStore(tmp_path)
    store.register(Artifact(kind="processed-data", path=str(tmp_path / "a.json"), producer="data-engineer"))
    store.register(Artifact(kind="paper-draft", path=str(tmp_path / "p.md"), producer="writer"))
    assert store.has("processed-data", "paper-draft")
    assert store.get_path("processed-data") == str(tmp_path / "a.json")
    # 从磁盘重载
    reloaded = ArtifactStore(tmp_path)
    assert reloaded.get("paper-draft").producer == "writer"


# -------------------------------------------------------------
# 修订环控制逻辑（mock 评分，不走 LLM）
# -------------------------------------------------------------

class MockLoop:
    def __init__(self, schedule):
        self.schedule = schedule
        self.calls = 0
        self.rubric = R

    async def run(self):
        history = []
        passed = False
        for _ in range(self.rubric.convergence.max_iterations):
            scores = self.schedule[min(self.calls, len(self.schedule) - 1)]
            self.calls += 1
            score = weighted_score(scores, self.rubric)
            history.append(score)
            if gate_passed(scores, self.rubric):
                passed = True
                break
            if convergence_stalled(history, self.rubric):
                break
        return passed, history


def test_loop_passes_immediately():
    passed, history = asyncio.run(MockLoop([OK]).run())
    assert passed is True and len(history) == 1


def test_loop_converges_on_flat_low():
    # 低分持平 → 收益递减，第 3 轮收敛停止（不是无脑跑满 5 轮）
    passed, history = asyncio.run(MockLoop([LOW]).run())
    assert passed is False and len(history) == 3


def test_loop_passes_on_third_iteration():
    passed, history = asyncio.run(MockLoop([LOW, LOW, OK]).run())
    assert passed is True and len(history) == 3
