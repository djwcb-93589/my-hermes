# MyHermes

> 一个可自托管、带明确安全边界的个人 Agent 运行框架。

MyHermes 将交互式命令行、消息 Gateway、工具执行、持久化状态和安全治理组合在同一套运行时中。它适合需要在可控范围内读写工作区、调用工具、安排定时任务，并通过本地或消息平台持续协作的个人 Agent 场景。

> [!IMPORTANT]
> 本项目是独立的学习与工程实现，参考了 [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) 的部分设计思路。它不是 NousResearch 的官方版本，也不保证与其配置、接口或扩展生态兼容。

## 能力概览

- **多种交互入口**：提供流式 CLI、统一 Gateway，以及可选的飞书 / Lark、个人微信、Dashboard 和 Gateway Supervisor。
- **受控工具执行**：支持本地、Docker、SSH 执行环境，以及文件操作、后台进程、浏览器自动化、媒体分析、记忆和本地 Skill。
- **任务与协作**：支持 Delegate 子 Agent、最小权限 Cron 定时任务、持久化任务编排，以及 Memory / Skill 的后台 Review。
- **可靠状态管理**：会话、审批、工具执行、消息投递、任务队列和 Cron 状态通过 SQLite 保存；Gateway 提供恢复、租约和队列保护。
- **安全治理**：工具按入口、运行环境和 toolset 暴露；路径访问、敏感信息、高风险命令、审批、会话和文件状态均有边界控制。
- **可选扩展**：支持 Python Hook Plugin、Browser、Computer Use，以及只在用户明确要求时才开放的受管 Claude Code 工作流。

## 快速开始

### 1. 准备环境

开始前请准备：

- Python 3.13 或更高版本；
- 一个 OpenAI 兼容模型服务的 API Key；
- Git；
- 使用本地 Terminal 时可用的 Bash。Windows 建议安装 Git Bash；
- 推荐安装 [uv](https://docs.astral.sh/uv/)，也可使用普通虚拟环境和 pip。

克隆项目并安装依赖：

~~~bash
git clone https://github.com/djwcb-93589/my-hermes.git
cd my-hermes

# 推荐：使用 uv 创建并同步环境
uv sync

# 或：使用你自己的虚拟环境
python -m pip install -e .
~~~

Browser 功能默认关闭；需要启用时，请按 Playwright 的安装说明准备对应浏览器运行时。

### 2. 创建本地配置

首次运行前，在项目根目录复制配置模板：

~~~bash
cp config.yaml.example config.yaml
cp .env.example .env
~~~

在 .env 中至少填写模型凭据：

~~~dotenv
OPENAI_API_KEY=your-api-key
MODEL=your-model

# 仅在使用非默认 OpenAI 兼容服务时设置
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
~~~

config.yaml 保存运行策略、平台和安全配置；.env 用于本地凭据和环境覆盖。环境变量优先于 config.yaml。不要提交这两个本地文件，也不要把真实 Token、密码或 API Key 写进任务正文。

### 3. 启动命令行

~~~bash
# 使用 uv
uv run python main.py

# 或在已激活的虚拟环境中
python main.py
~~~

CLI 会在当前工作区中处理任务。常用命令包括：

| 命令 | 作用 |
| --- | --- |
| /new | 新建会话。 |
| /stop | 请求中断当前任务。 |
| /approve | 处理待审批操作。 |
| /deny | 拒绝待审批操作。 |
| /quit | 退出 CLI。 |

## 运行入口

以下命令均以 uv run python main.py 为例；不使用 uv 时，将其替换为 python main.py。

| 命令 | 用途 |
| --- | --- |
| uv run python main.py | 启动默认交互式 CLI。 |
| uv run python main.py --gateway | 按 gateway.platforms 配置启动统一 Gateway。 |
| uv run python main.py dashboard | 启动本地 Dashboard，默认监听 127.0.0.1:8000。 |
| uv run python main.py supervisor | 启动独立 Supervisor，受控管理本地 Gateway 进程。 |
| uv run python main.py plugins list | 查看已发现的 Python Hook Plugin。 |
| uv run python main.py plugins enable &lt;name&gt; | 启用指定 Plugin；重启 CLI 或 Gateway 后生效。 |
| uv run python main.py --gateway-console | 启动兼容的 Console Gateway。 |
| uv run python main.py --simulate | 使用模拟 Adapter 验证 Gateway 行为。 |
| uv run python main.py --weixin-login | 登录个人微信 iLink Bot 并写入本地凭据。 |

远程平台、Browser、Plugin 和其他高权限能力默认不会因普通启动而自动开放。请在 config.yaml 中显式启用所需能力，并为每个入口配置最小 toolset。

## 配置档案与运行状态

HERMES_HOME 指定 MyHermes 的配置档案目录。默认值是项目根目录；需要隔离不同账号、环境或实验数据时，可在启动前设置：

~~~bash
export HERMES_HOME='D:/profiles/my-hermes'
~~~

一个档案通常包含：

~~~text
<HERMES_HOME>/
├── .env                 # 凭据和环境覆盖项
├── config.yaml          # 运行、平台和安全配置
├── SOUL.md              # 系统提示前缀
├── database/hermes.db   # SQLite 运行状态
├── memories/            # MEMORY.md 与 USER.md
├── skills/              # 用户本地 Skill
├── plugins/             # 用户 Python Hook Plugin
└── cache/               # Gateway、浏览器等运行缓存
~~~

完整字段和默认值请查看 [config.yaml.example](config.yaml.example)。飞书、微信、Browser、Docker / SSH、Cron、Dashboard、Plugin 和安全策略均在该文件中按需配置。

## 安全边界

MyHermes 可以执行文件和终端操作，但它不是操作系统级沙箱。部署和使用时请特别注意：

- 处理不可信任务时，优先使用 Docker、独立系统账号、虚拟机或其他隔离环境；不要把 Local Terminal 当成安全沙箱。
- 只授予 Gateway、Cron、Delegate 和 Plugin 完成任务所需的最小 toolset；高风险命令、写入和外部访问应保留审批。
- Dashboard 绑定到非回环地址时必须配置强认证 Token；不要把 Token 放到 URL、日志或仓库中。
- Python Plugin 属于进程内可信代码，启用前应审查源码。Browser、媒体分析和外部消息平台也可能把数据发送给第三方服务。
- 不应把生产凭据、私钥、Token、数据库文件或系统关键路径暴露给不可信任务。

### 受管 Claude Code

Claude Code 不是普通 Terminal 命令的替代入口。只有当前真实用户明确要求使用、查询、中断或终止 Claude Code 时，可信调用链才会临时开放受管 claude_code Tool。

该流程不会自动安装或登录 Claude Code，不会回退到裸 CLI，也不会自动批准权限、输入凭据、提交代码、推送、发布或部署。活动任务轮次只能观察、中断或终止；后续任务必须在前一轮终态后显式创建新轮次。详情见 bundled Claude Code Skill 的运行时合同。

## 项目结构

~~~text
my-hermes/
├── main.py                 # 命令分发与默认 CLI 入口
├── config.yaml.example     # 配置参考
├── hermes/
│   ├── agent_loop.py        # Agent 运行核心
│   ├── tools/               # 工具注册、执行和审批适配
│   ├── backends/            # Local、Docker、SSH 执行环境
│   ├── processes.py         # 后台进程管理
│   ├── session_resources.py # 会话资源清理
│   ├── gateway/             # 平台 Adapter、队列、投递和恢复
│   ├── cron/                # 定时任务与能力约束
│   ├── persistence/         # SQLite schema、迁移和仓储
│   ├── plugins/             # Python Hook Plugin 运行时
│   ├── claude_code/         # 受管 Claude Code 控制链
│   ├── web/                 # Dashboard
│   └── supervisor/          # Gateway 进程监督
├── browser/                 # Playwright 浏览器会话与页面能力
├── documents/               # 文档处理能力
├── docs/                    # 专题文档
├── memories/                # Memory 说明与示例
└── skills/                  # 本地 Skill 说明与示例
~~~

## 延伸文档

- [配置参考](config.yaml.example)
- [Python Plugin Hook 使用说明](docs/plugins.md)
- [飞书操作手册](飞书操作手册.md)
- [Skills 目录说明](skills/README.md)
- [Memory 目录说明](memories/README.md)
- [Computer Use 说明](hermes/computer_use/README.md)

如果计划将 MyHermes 部署为公网高权限服务，请先针对实际网络边界、身份认证、凭据管理、工具权限和数据流向完成独立安全审计。
