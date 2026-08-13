"""AgentCard 模型 + 能力注册表（A2A 范式内部化）。

每个静默专家用一张结构化 AgentCard 描述"身份"，而非仅靠 .md 提示词：
- capabilities / owned_tools / input_contract / output_contract 构成可查询、
  确定性校验的能力声明，供主 agent 按能力发现与选择专家。
- 能力注册表是代码里的静态清单（非用户配置、非 LLM 动态注入），
  规避多 agent 研究里最常见的 agent-in-the-middle / 能力漂移问题。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentCard(BaseModel):
    """专家能力卡：一个静默专家的完整身份声明。"""

    name: str = Field(description="唯一标识，如 data-engineer")
    display_name: str = Field(description="展示名，如 数据工程师")
    description: str = Field(description="一句话说明它做什么、何时被唤醒")
    capabilities: list[str] = Field(
        default_factory=list,
        description="结构化能力关键词，供主 agent 按能力发现（如 data-cleaning, latex-formatting）",
    )
    owned_tools: list[str] = Field(
        default_factory=list,
        description="该专家拥有的工具名（构建其独立注册表，而非共享过滤）",
    )
    input_contract: list[str] = Field(
        default_factory=list,
        description="该专家消费的工件类型（artifact kinds），如 processed-data, paper-draft",
    )
    output_contract: list[str] = Field(
        default_factory=list,
        description="该专家产出的工件类型，如 statistics-table, review-report",
    )
    prompt: str = Field(default="", description="系统提示词（源自 .md，仅作提示，不作身份）")
    model: str = Field(default="inherit", description="inherit 继承主 agent 模型，或指定别名")
    max_turns: int = Field(default=50, description="最大迭代轮数")
    permission_mode: str = Field(default="dontAsk", description="权限模式，默认专家全自动")
    worktree: bool = Field(default=True, description="是否在隔离 worktree 中运行")
    background: bool = Field(default=False, description="是否默认后台运行")

    def capability_hint(self) -> str:
        """给主 agent 的能力索引行。"""
        caps = ", ".join(self.capabilities) if self.capabilities else "-"
        return f"{self.name}: {self.description} [caps: {caps}]"


class CapabilityRegistry:
    """确定性能力注册表：主 agent 从这里按能力发现专家，而非读 .md。"""

    def __init__(self, cards: list[AgentCard] | None = None) -> None:
        self._cards: dict[str, AgentCard] = {}
        self._by_capability: dict[str, list[str]] = {}
        for card in cards or []:
            self.add(card)

    def add(self, card: AgentCard) -> None:
        self._cards[card.name] = card
        for cap in card.capabilities:
            self._by_capability.setdefault(cap, []).append(card.name)

    def get(self, name: str) -> AgentCard | None:
        return self._cards.get(name)

    def all(self) -> list[AgentCard]:
        return list(self._cards.values())

    def names(self) -> list[str]:
        return list(self._cards.keys())

    def find_by_capability(self, query: str) -> list[AgentCard]:
        """按能力关键词查找（精确 + 子串匹配），按匹配数排序。"""
        q = query.lower()
        scored: list[tuple[int, AgentCard]] = []
        for card in self._cards.values():
            hits = sum(
                1 for cap in card.capabilities
                if q in cap.lower() or cap.lower() in q
            )
            if hits:
                scored.append((hits, card))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored]

    def describe_index(self) -> str:
        """给主 agent 的系统提示注入的能力索引（不暴露完整 prompt）。"""
        lines = ["可用专家（按能力发现，遇到对应任务自动唤醒）："]
        for card in self._cards.values():
            lines.append(f"- {card.capability_hint()}")
        return "\n".join(lines)
