# MyHermes

> 支持远端和本地终端入口接入与自进化的个人Agent助手。

MyHermes 将交互式 CLI、消息 Gateway、工具执行、持久化状态和安全治理组合在同一套 Agent 运行框架中。它适合构建能够在受控边界内读写工作区、执行任务、安排定时工作，并通过飞书或个人微信等渠道持续协作的 Agent。

> [!IMPORTANT]
> 本项目是独立的学习与工程实现，参考了 [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) 的部分设计思路；并非其官方版本，也不保证配置和扩展生态兼容。

## 核心能力

- **多入口交互**：提供带流式输出和会话控制的 CLI，以及统一 Gateway；Gateway 可按配置接入本地 CLI、飞书 / Lark 和个人微信。
- **受控工具执行**：支持 Local、Docker、SSH Terminal，后台进程管理、文件操作、浏览器自动化、媒体分析、长期记忆和本地 Skills。
- **Agent 工作流**：支持同步或后台 Delegate 子 Agent、受最小权限约束的 Cron 定时任务、持久化任务 DAG 编排，以及受治理的 Memory / Skill 后台 Review。
- **可靠运行**：会话、消息队列、审批、工具执行、投递结果和 Cron 状态使用 SQLite 持久化；Gateway 支持重启恢复与多实例租约保护。
- **安全边界**：工具按运行环境和 toolset 授权，高风险操作可要求审批；内置路径限制、敏感信息脱敏、文件状态校验和会话隔离。
- **运维与扩展**：提供 Dashboard、独立 Gateway Supervisor、Python Hook Plugin 管理，以及默认关闭、仅在可信用户请求下启用的 Claude Code 托管工作流。

## 快速开始

### 1. 准备环境

- Python 3.13 或更高版本；
- 一个 OpenAI 兼容的模型服务和 API Key；
- 使用 Local Terminal 时需要 Bash；Windows 推荐安装 Git Bash。

克隆项目并安装依赖：

```bash
git clone https://github.com/djwcb-93589/my-hermes.git
cd my-hermes

# 推荐使用 uv
uv sync

# 或在已激活的虚拟环境中安装
python -m pip install -e .
```

### 2. 创建本地配置

首次启动前，复制示例文件并填写模型信息。PowerShell：

```powershell
Copy-Item config.yaml.example config.yaml
Copy-Item .env.example .env
```

在 `.env` 中至少设置：

```dotenv
OPENAI_API_KEY=your-api-key
MODEL=your-model

# 使用非默认 OpenAI 服务时再设置
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
```

`config.yaml` 用于保存运行策略和平台配置，`.env` 用于保存凭据。进程环境变量优先于 `config.yaml`；不要提交这两个本地文件。

### 3. 启动 CLI

```bash
python main.py
```

输入任务后，Agent 会在当前工作区执行；可使用 `/new` 创建会话、`/stop` 中断任务、`/approve` 或 `/deny` 处理审批、`/quit` 退出。

## 运行入口

| 命令 | 用途 |
| --- | --- |
| `python main.py` | 默认交互式 CLI。 |
| `python main.py --gateway` | 按 `gateway.platforms` 配置启动统一 Gateway。 |
| `python main.py dashboard` | 启动本地 Dashboard，默认监听 `127.0.0.1:8000`。 |
| `python main.py supervisor` | 启动独立 Supervisor，负责受控管理本地 Gateway 进程。 |
| `python main.py plugins list` | 查看发现到的 Python Hook Plugin。 |
| `python main.py plugins enable <name>` | 启用发现到的 Plugin；重启 CLI 或 Gateway 后生效。 |
| `python main.py --gateway-console` | 启动兼容的 Console Gateway。 |
| `python main.py --simulate` | 使用模拟 Adapter 验证 Gateway 行为。 |
| `python main.py --weixin-login` | 登录个人微信 iLink Bot，写入本地凭据。 |

默认不会启用远程平台或 Browser。请在 `config.yaml` 中显式开启相应能力和最小所需 toolset 后再启动 Gateway。

## 配置与运行状态

`HERMES_HOME` 定义配置档案目录，默认是项目根目录；需要隔离多个账号或环境时，可在启动前设置它：

```powershell
$env:HERMES_HOME = "D:\profiles\my-hermes"
```

一个档案通常包含：

```text
<HERMES_HOME>/
├── .env                 # 凭据与环境覆盖项
├── config.yaml          # 运行、平台和安全配置
├── SOUL.md              # 系统提示前缀
├── database/hermes.db   # SQLite 运行状态
├── memories/            # MEMORY.md 与 USER.md
├── skills/              # 本地 Skills
├── plugins/             # 用户 Python Hook Plugin
└── cache/               # Gateway、浏览器等运行缓存
```

完整字段说明请直接查看 [config.yaml.example](config.yaml.example)。飞书、微信、Browser、Docker / SSH、Cron 和 Dashboard 都通过该文件按需配置。

## 安全提示

- Local Terminal 是执行环境，不是操作系统级沙箱；处理不可信任务时请使用 Docker、独立账号或虚拟机等隔离边界。
- 仅授予 Gateway、Cron 和子 Agent 完成任务所需的最小 toolset；高风险命令、写入和外部操作应保留审批。
- 非回环地址运行 Dashboard 时必须配置强认证 Token；不要将 Token 放进 URL 或提交到仓库。
- Python Plugin 是进程内可信代码，启用前应审查源码。Browser、媒体分析和外部消息平台也可能发送数据到第三方服务。

## 项目结构

```text
my-hermes/
├── main.py                 # 命令分发入口
├── config.yaml.example     # 配置参考
├── hermes/
│   ├── agent_loop.py        # 同步 / 异步 Agent 运行核心
│   ├── tools/               # 工具实现与审批适配
│   ├── backends/            # Local、Docker、SSH 执行环境
│   ├── gateway/             # 平台 Adapter、队列、投递与恢复
│   ├── cron/                # 定时任务与能力约束
│   ├── persistence/         # SQLite schema、迁移和仓储
│   ├── plugins/             # Python Hook Plugin 运行时
│   ├── web/                 # Dashboard
│   └── supervisor/          # Gateway 进程监督
├── docs/
└── skills/                  # 本地 Skill 示例
```

## 延伸文档

- [配置参考](config.yaml.example)
- [Python Plugin Hook 使用说明](docs/plugins.md)
- [飞书操作手册](飞书操作手册.md)
- [Skills 目录说明](skills/README.md)
- [Memory 目录说明](memories/README.md)

项目仍在持续开发中。若计划把它暴露为公网高权限服务，请先针对具体部署方案完成额外的安全审计。
