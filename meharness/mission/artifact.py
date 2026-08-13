"""Artifact 结构化流转（A2A Task/Artifact 模型内部化）。

专家之间不靠纯文本消息传递，而是通过 ArtifactStore 交换结构化工件：
每个专家按 output_contract 产出工件并注册，后续专家按 input_contract
从 store 读取。工件在 artifacts/ 目录（所有 worktree 通过 symlink 共享）。

配合质量门，工件路径 + 元数据持久化为 JSON，支撑可复现归档。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Artifact(BaseModel):
    kind: str = Field(description="工件类型，如 processed-data / paper-draft / review-report")
    path: str = Field(description="工件文件路径")
    producer: str = Field(default="", description="产出者（专家名）")
    description: str = Field(default="")
    metadata: dict = Field(default_factory=dict)


class ArtifactStore:
    """以 kind 为键的工件仓库；latest-wins，持久化为 JSON。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)
        self._index_path = self._base / "artifacts_index.json"
        self._artifacts: dict[str, Artifact] = {}
        self._load()

    def _load(self) -> None:
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
                self._artifacts = {k: Artifact(**v) for k, v in data.items()}
            except Exception as e:
                log.warning("工件索引加载失败（重置）: %s", e)
                self._artifacts = {}

    def register(self, artifact: Artifact) -> None:
        self._artifacts[artifact.kind] = artifact
        self._base.mkdir(parents=True, exist_ok=True)
        try:
            self._index_path.write_text(
                json.dumps({k: v.model_dump() for k, v in self._artifacts.items()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("工件索引写入失败: %s", e)

    def get(self, kind: str) -> Artifact | None:
        return self._artifacts.get(kind)

    def get_path(self, kind: str) -> str | None:
        art = self._artifacts.get(kind)
        return art.path if art else None

    def all(self) -> list[Artifact]:
        return list(self._artifacts.values())

    def has(self, *kinds: str) -> bool:
        return all(k in self._artifacts for k in kinds)

    def describe(self) -> str:
        """给专家的任务提示：当前可用的工件清单。"""
        if not self._artifacts:
            return "(暂无工件)"
        lines = ["当前工件："]
        for k, art in self._artifacts.items():
            lines.append(f"- {k}: {art.path} {('(' + art.description + ')') if art.description else ''}")
        return "\n".join(lines)

    def ensure_dir(self, subdir: str) -> Path:
        p = self._base / subdir
        p.mkdir(parents=True, exist_ok=True)
        return p
