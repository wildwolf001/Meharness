"""子 agent 模型选择的 provider 感知回退。

背景：内置 explore 子代理曾写死 model: haiku（claude-code 遗产），在 DeepSeek
openai-compat 端点上映射成不存在的 claude-haiku-4-5-20251001 → 子代理一调 LLM
就 0.2s 秒挂（真机日志：两个并行 Explore 子代理都 ✗）。修复后：
- 别名（haiku/sonnet/opus）只在 anthropic 协议下有意义；非 anthropic 端点
  下 _create_client_for_model 返回 None → _select_llm 回退到父模型（inherit）。
- 显式传 provider 原生模型名（如 deepseek-v4-flash）仍正常构造客户端。
"""

from __future__ import annotations

from meharness.agents.parser import AgentDef
from meharness.config import ProviderConfig
from meharness.tools.agent_tool import AgentTool, AgentToolParams


def _tool(protocol: str, model: str = "deepseek-v4-flash") -> AgentTool:
    cfg = ProviderConfig(
        name="t",
        protocol=protocol,
        base_url="https://api.deepseek.com",
        model=model,
        api_key="sk-test",
    )
    parent = type("P", (), {"client": object()})()
    return AgentTool(
        agent_loader=None,
        task_manager=None,
        trace_manager=None,
        parent_agent=parent,
        provider_config=cfg,
    )


def _defn(model: str) -> AgentDef:
    return AgentDef(
        agent_type="explore",
        when_to_use="x",
        system_prompt="",
        disallowed_tools=[],
        model=model,
        max_turns=30,
        permission_mode="default",
        source="builtin",
    )


def test_haiku_alias_returns_none_on_non_anthropic() -> None:
    """非 anthropic 端点：haiku 别名不构造客户端（避免必挂的 claude 模型 ID）。"""
    tool = _tool("openai-compat")
    assert tool._create_client_for_model("haiku") is None


def test_select_llm_falls_back_to_parent_on_haiku_alias() -> None:
    """定义写死 haiku 时（如旧的 explore），deepseek 端点下回退到父模型。"""
    tool = _tool("openai-compat")
    p = AgentToolParams(prompt="explore codebase", description="explore")
    assert tool._select_llm(p, _defn("haiku")) is tool._parent_agent.client


def test_select_llm_uses_provider_native_model_override() -> None:
    """显式传 provider 原生模型名：构造对应客户端（不走别名）。"""
    tool = _tool("openai-compat")
    p = AgentToolParams(prompt="x", description="d", model="deepseek-v4-flash")
    client = tool._select_llm(p, _defn("inherit"))
    assert client is not None
    assert getattr(client, "model", "") == "deepseek-v4-flash"


def test_haiku_alias_works_on_anthropic() -> None:
    """anthropic 端点下 haiku 别名正常构造（有 claude 访问权时该路径成立）。"""
    tool = _tool("anthropic")
    assert tool._create_client_for_model("haiku") is not None
