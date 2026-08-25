# Meharness

Meharness 是一个运行在终端里的 AI 编程助手，支持多 LLM 提供商、MCP 协议、子 Agent 调度、Agent 团队协作等能力。灵感来源于 Claude Code，使用 Python + Textual 构建 TUI 交互界面。

## 功能特性

- **多提供商支持** — Anthropic、OpenAI、OpenAI 兼容协议均可接入，支持多模型热切换
- **终端图形界面** — 基于 Textual 框架的 TUI，支持鼠标/键盘操作、命令补全、文件 `@` 引用
- **权限控制** — 四种权限模式：`default`（默认询问）、`accept-edits`（自动接受编辑）、`plan`（规划模式）、`bypass`（YOLO 全自动）
- **Slash 命令** — 内置 `/help`、`/compact`、`/clear`、`/plan`、`/session`、`/memory`、`/mcp`、`/status`、`/rewind`、`/permission`、`/tasks`、`/trace`、`/worktree`、`/skill`、`/review` 等命令
- **MCP 协议** — 完整支持 Model Context Protocol，可接入外部工具服务器
- **技能系统** — 内置 `commit`、`review`、`test`、`backend-interview` 等技能包，支持自定义扩展
- **子 Agent** — 内置 Explore（代码探索）、Plan（架构设计）、General-purpose（通用任务）、Verification（代码审查）子 Agent
- **Agent 团队** — 支持 Coordinator 模式，Leader Agent 可创建多个 Teammate 并行协作
- **Git Worktree** — 集成 Git Worktree 管理，子 Agent 可在隔离的 worktree 中工作
- **会话管理** — 会话持久化、恢复、摘要生成、对话回退（Rewind）
- **上下文压缩** — 自动压缩超长对话，突破上下文窗口限制
- **记忆系统** — 用户记忆、项目记忆、反馈记忆，跨会话持久化
- **Prompt 缓存** — Anthropic 协议下自动启用 prompt caching，降低费用
- **Hook 系统** — 支持 `startup`、`shutdown` 等生命周期 Hook
- **非交互模式** — 通过 `-p` 参数直接执行 prompt 并输出结果，适合脚本/CI 场景

## 环境要求

- Python >= 3.11
- 至少一个 LLM API Key（Anthropic 或 OpenAI）

## 快速开始

```bash
git clone <repo-url> meharness && cd meharness && uv sync && uv run meharness
```

## 安装

```bash
# 克隆项目
git clone <repo-url> meharness
cd meharness

# 安装依赖（项目使用 uv 管理）
uv sync
```

## 配置

Meharness 通过 YAML 文件配置，加载优先级从低到高（后者覆盖前者）：

1. `~/.meharness/config.yaml` — 用户级全局配置
2. `.meharness/config.yaml` — 项目级配置
3. `.meharness/config.local.yaml` — 本地覆盖（建议加入 .gitignore）

### 最简配置

```yaml
providers:
  - name: anthropic
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}

permission_mode: default
```

### 完整配置示例

```yaml
# 可配置多个 Provider，启动时选择
providers:
  - name: anthropic
    protocol: anthropic
    base_url: https://api.anthropic.com
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}
    thinking: true          # 启用 extended thinking
    context_window: 0       # 0=自动检测（推荐），或手动指定
    max_output_tokens: 8192

  - name: openai
    protocol: openai
    base_url: https://api.openai.com/v1
    model: gpt-4o
    api_key: ${OPENAI_API_KEY}

# 权限模式: default | accept-edits | plan | bypass
permission_mode: default

# MCP 工具服务器（可选）
mcp_servers:
  - name: filesystem
    command: npx
    args: ["-y", "@anthropic-ai/mcp-server-filesystem", "/path/to/allowed"]
  - name: github
    url: https://api.github.com/mcp
    headers:
      Authorization: Bearer ${GITHUB_TOKEN}

# Git Worktree 配置
worktree:
  symlink_directories:
    - node_modules
    - .venv
    - vendor
  stale_cleanup_interval: 3600   # 秒
  stale_cutoff_hours: 24

# Agent 团队模式
teammate_mode: ""              # 空字符串 = 默认, "in-process" = 进程内协作
enable_coordinator_mode: false # 是否启用协调者模式

# 功能开关
enable_fork: false             # 是否允许 fork 对话
enable_verification_agent: true # 是否启用验证 Agent

# 生命周期 Hook
hooks:
  - event: startup
    command: echo "Meharness started"
  - event: shutdown
    command: echo "Meharness stopped"
```

### API Key

配置中的 `api_key` 支持 `${ENV_VAR}` 语法引用环境变量。环境变量映射：

| 协议 | 环境变量 |
|------|----------|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `openai-compat` | `OPENAI_API_KEY` |

## 使用

```bash
# 交互模式（TUI）
uv run meharness

# 非交互模式：执行单次 prompt 并输出结果
uv run meharness -p "解释一下这个项目的架构"

# 指定权限模式
uv run meharness --mode plan
uv run meharness --mode bypass
```

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 发送消息 |
| `Shift+Enter` / `Ctrl+J` | 换行 |
| `Tab` | 命令/@文件补全 |
| `Shift+Tab` | 切换权限模式 |
| `Ctrl+O` | 展开/收起工具调用详情 |
| `Esc` | 取消当前操作 |
| `Ctrl+C` | 退出程序 |
| `Up` / `Down` | 浏览历史 / 补全菜单 |

### Slash 命令

| 命令 | 别名 | 说明 |
|------|------|------|
| `/help` | `/h`, `/?` | 显示帮助信息 |
| `/clear` | | 清除对话历史 |
| `/compact` | `/c` | 压缩上下文 |
| `/plan` | `/p` | 切换到 Plan 模式 |
| `/session` | | 会话管理（列表/恢复/删除） |
| `/memory` | | 记忆管理 |
| `/mcp` | | 查看 MCP 服务器状态 |
| `/permission` | | 权限规则管理 |
| `/status` | `/s` | 显示当前状态 |
| `/rewind` | | 回退到之前的对话节点 |
| `/tasks` | `/task` | 查看/取消后台任务 |
| `/trace` | `/tree` | 查看 Agent 父子追踪树 |
| `/worktree` | `/wt` | Git Worktree 管理 |
| `/skill` | `/skills` | 技能包管理 |
| `/review` | | 审查代码变更 |

### 文件 @ 引用

在输入框中输入 `@` 可以快速引用项目中的文件，支持自动补全。

```
帮我重构 @app.py 中的这个类
```

## 项目结构

```
meharness/
├── pyproject.toml          # 项目元信息和依赖
├── uv.lock                 # 依赖锁文件
├── meharness/
│   ├── __main__.py         # 入口：参数解析和启动
│   ├── app.py              # TUI 主应用（Textual App）
│   ├── agent.py            # 核心 Agent 引擎
│   ├── client.py           # LLM 客户端（Anthropic / OpenAI）
│   ├── config.py           # 配置加载和模型
│   ├── conversation.py     # 对话管理器
│   ├── prompts.py          # System Prompt 构建
│   ├── serialization.py    # 消息序列化
│   ├── validator.py        # 配置校验
│   ├── cache.py            # 文件缓存
│   ├── driver.py           # Textual Driver
│   ├── teammate_tree.py    # 队友树 UI 组件
│   ├── styles.tcss         # TUI 样式表
│   ├── agents/             # 子 Agent 系统
│   │   ├── loader.py       # Agent 加载器
│   │   ├── task_manager.py # 后台任务管理器
│   │   ├── trace.py        # Agent 追踪树
│   │   └── builtins/       # 内置 Agent 定义
│   ├── commands/           # Slash 命令系统
│   │   ├── registry.py     # 命令注册表
│   │   ├── completion.py   # 命令补全
│   │   └── handlers/       # 命令处理器
│   ├── context/            # 上下文窗口管理
│   ├── hooks/              # Hook 引擎
│   ├── mcp/                # MCP 协议支持
│   ├── memory/             # 记忆系统
│   ├── permissions/        # 权限系统
│   ├── skills/             # 技能系统
│   │   ├── loader.py       # 技能加载器
│   │   ├── executor.py     # 技能执行器
│   │   └── builtins/       # 内置技能包
│   ├── teams/              # Agent 团队协作
│   ├── tools/              # 工具系统
│   │   ├── agent_tool.py   # 子 Agent 调用工具
│   │   ├── team_create.py  # 创建团队工具
│   │   └── ...             # 其他内置工具
│   └── worktree/           # Git Worktree 管理
└── tests/                  # 测试
```

## 开发者

### 运行测试

```bash
uv run pytest
```

### 技术栈

- **TUI**: [Textual](https://textual.textualize.io/) — Python 终端 UI 框架
- **LLM SDK**: `anthropic` + `openai` — 双协议支持
- **序列化**: Pydantic — 配置和数据模型校验
- **包管理**: uv + hatchling

## License

MIT
