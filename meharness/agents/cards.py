"""论文专家团队：6 个静默专家的 AgentCard 定义。

这些专家默认不运行；主 agent 检测到论文类任务时，按能力索引自动唤醒。
卡片是确定性代码清单（非用户配置），能力/工具/契约在此声明。
"""

from __future__ import annotations

from meharness.agents.capability import AgentCard, CapabilityRegistry

# 各专家拥有的工具（名字与 meharness.tools 注册名一致）
# 只读/写/命令按角色职能裁剪；WebSearch 供文献检索使用。
_COMMON_READ = ["ReadFile", "Glob", "Grep", "ToolSearch"]
_COMMON_WRITE = ["WriteFile", "EditFile"]
_COMMAND = ["Bash"]
_COMMUNICATION = ["SendMessage", "SyntheticOutput"]


def _build_registry() -> CapabilityRegistry:
    cards = [
        AgentCard(
            name="data-engineer",
            display_name="数据工程师",
            description=(
                "清洗与处理实验数据、计算统计量、产出统计表和图表。"
                "遇到原始数据/CSV/JSON/日志需要整理、统计、可视化时唤醒。"
            ),
            capabilities=[
                "data-cleaning", "data-processing", "statistics", "data-analysis",
                "python-pandas", "visualization", "figure",
            ],
            owned_tools=_COMMON_READ + _COMMON_WRITE + _COMMAND + _COMMUNICATION,
            input_contract=["raw-data", "dataset", "experiment-log"],
            output_contract=["processed-data", "statistics-table", "figure", "data-report"],
        ),
        AgentCard(
            name="literature-researcher",
            display_name="文献检索",
            description=(
                "检索相关领域论文、总结研究方法、整理相关工作与引用。"
                "遇到需要查找文献、综述背景、补引用、调研方法时唤醒。"
            ),
            capabilities=[
                "literature-search", "related-work", "citation", "paper-summary",
                "survey", "web-search",
            ],
            owned_tools=_COMMON_READ + _COMMON_WRITE + ["WebSearch", "WebFetch"] + _COMMUNICATION,
            input_contract=["research-topic", "paper-draft", "research-question"],
            output_contract=["literature-review", "related-work-section", "citation-list", "method-summary"],
        ),
        AgentCard(
            name="experiment-designer",
            display_name="实验设计",
            description=(
                "基于研究假设设计实验方案、运行实验、做消融与基线对比、分析结果。"
                "遇到需要设计/补跑实验、消融、对比基线、解释结果时唤醒。"
            ),
            capabilities=[
                "experiment-design", "experiment-run", "ablation-study",
                "baseline-comparison", "results-analysis", "metric-evaluation",
            ],
            owned_tools=_COMMON_READ + _COMMON_WRITE + _COMMAND + _COMMUNICATION,
            input_contract=["hypothesis", "processed-data", "research-question"],
            output_contract=["experiment-plan", "experiment-results", "ablation-table", "metric-summary"],
        ),
        AgentCard(
            name="writer",
            display_name="论文写作",
            description=(
                "撰写论文正文、按审稿意见逐节定向修改、保证论证与结构清晰。"
                "遇到需要写初稿、改稿、组织章节、润色论证时唤醒。"
            ),
            capabilities=[
                "paper-writing", "drafting", "revision", "scientific-writing",
                "argumentation", "section-editing",
            ],
            owned_tools=_COMMON_READ + _COMMON_WRITE + _COMMAND + _COMMUNICATION,
            input_contract=["experiment-results", "literature-review", "outline", "review-issues"],
            output_contract=["paper-draft", "paper-section", "revised-draft", "outline"],
        ),
        AgentCard(
            name="formatter",
            display_name="格式规范",
            description=(
                "处理 LaTeX/格式/图表/引文样式，使论文符合投稿模板与排版要求。"
                "遇到需要排版、LaTeX 编译、图表规范、引用格式时唤醒。"
            ),
            capabilities=[
                "latex-formatting", "formatting", "style", "templates",
                "citation-style", "bibliography", "figure-layout",
            ],
            owned_tools=_COMMON_READ + _COMMON_WRITE + _COMMAND + _COMMUNICATION,
            input_contract=["paper-draft", "latex-source"],
            output_contract=["formatted-paper", "latex-source", "bibliography"],
        ),
        AgentCard(
            name="reviewer",
            display_name="审核",
            description=(
                "独立评审论文、按 rubric 评分、给出逐节问题清单，并核对论文数据声明与实验结果。"
                "只读，不修改论文。每轮修订后由 lead 唤醒，提供 fresh 视角。"
            ),
            capabilities=[
                "review", "critique", "quality-assessment", "rubric-scoring",
                "claim-audit", "consistency-check",
            ],
            owned_tools=_COMMON_READ + _COMMUNICATION,  # 刻意不含写工具——只读评审
            input_contract=["paper-draft", "experiment-results", "rubric"],
            output_contract=["review-report", "score", "issues-list", "claim-audit-report"],
        ),
    ]
    return CapabilityRegistry(cards)


# 模块级单例：团队静态清单
REGISTRY = _build_registry()


_TEAM_AWARENESS_TEMPLATE = """\
## 内置论文专家团队（静默）

你内置了一组**静默的论文专家**。默认它们不运行，只有遇到**论文/科研写作/实验分析类任务**时才按需唤醒协作；非论文任务不要使用它们（你自己直接完成即可）。

__INDEX__

唤醒方式：调用 Agent 工具，subagent_type 填专家名（如 "data-engineer"），或按能力描述让模型选合适的专家。

论文任务建议流程（SOP，按序推进）：
1. 数据工程师 处理/统计实验数据 → 2. 文献检索 整理相关工作与引用 → 3. 实验设计 设计/补跑实验、消融 → 4. 写作 写稿 → 5. 格式 规范排版 → 6. 审核 独立评审。

质量要求：审核（reviewer）在每轮修订后以 fresh 视角独立评审并评分，**全部维度达标才算完成**——不要因为"你觉得写完了"就提前交付。评审意见要定向传给写作专家逐节修改。"""


def build_team_awareness_prompt() -> str:
    """主 agent 的系统提示片段：团队能力索引 + 唤醒/编排指引。"""
    return _TEAM_AWARENESS_TEMPLATE.replace("__INDEX__", REGISTRY.describe_index())
