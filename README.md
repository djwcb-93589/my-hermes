# MyHermes

MyHermes 是一个模块化的 Python Agent 项目。它以 OpenAI 兼容模型接口为核心，提供本地工具、持久会话、远程消息 Gateway、可靠定时任务、审批和文件投递能力。

项目当前适合以下场景：

- 在终端中运行带工具调用能力的对话 Agent；
- 通过飞书、个人微信或本地 CLI Adapter 接入远程会话；
- 读取和修改文件、执行命令、保存长期记忆、管理本地 Skill；
- 委派同步或后台子任务，并查询或取消后台任务；
- 创建具备时区、重试、权限和投递策略的定时 Agent 任务；
- 对高风险命令、文件操作、文件发送和 Cron 能力申请进行审批；
- 在进程退出或 Gateway 重启后恢复消息、工具调用、Cron 和待投递结果。

当前尚未内置通用 MCP Client、联网搜索工具或自动修改自身代码的自进化流程。这些能力可以在现有工具注册与审批边界上继续扩展。

## 主要能力

### Agent 会话

- 同步 CLI 会话与异步 Gateway 会话；
- 模型调用重试、fallback、continuation 和上下文压缩；
- 会话消息、工具调用和最终回复持久化；
- 支持取消正在等待的异步模型请求；
- 根据 `SOUL.md`、项目说明、记忆和 Skill 组装 system prompt。

### 工具系统

所有工具注册在统一的 `ToolRegistry` 中。每个工具声明自己的 toolset、运行环境、无人值守策略、可信上下文要求、审批方式、风险等级和崩溃恢复语义。

当前工具包括：

| 工具 | 能力 |
|---|---|
| `terminal` | 通过 Local、Docker 或 SSH backend 执行命令，保留会话工作目录 |
| `file` | 读取、分段读取、写入、追加、替换、列目录和查看文件信息 |
| `memory` | 读取和维护长期记忆、用户档案 |
| `skill_view` / `skills_list` | 查看经过名称与路径校验的本地 Skill |
| `skill_manage` | 创建、编辑、局部修改和删除 Skill |
| `delegate_task` | 创建隔离的子 Agent 任务 |
| `delegate_status` / `delegate_result` / `delegate_cancel` | 管理后台 Delegate |
| `cron` | 创建和管理定时任务 |
| `gateway_send_file` | 在普通 Gateway 会话中审批并发送文件 |

模型可见的工具定义与运行时可分发的工具名单来自同一次策略解析。全局注册并不等于当前会话自动获得执行权限。

### Gateway

Gateway 使用统一 Runner 接收平台事件、建立会话、调用 Agent，并通过持久 Outbox 发送结果。

目前提供：

- 本地 CLI Adapter；
- 飞书 Adapter，包括 Webhook、去重、限流、处理状态、文本和文件投递；
- 个人微信 Adapter；
- Console 和 Simulated Adapter，供本地集成调试使用；
- runtime lease 与 fencing，避免多个 Gateway 实例同时拥有正式运行资格；
- 消息队列、审批、Outbox 和文件投递恢复；
- 全局模型并发限制与有界重试。

### Cron

Cron 不是直接调用某个工具，而是为每次运行创建独立 Agent 会话，并复用 AgentLoop 和 ToolRegistry。

当前支持：

- 一次性延迟、固定间隔和五字段 Cron 表达式；
- IANA timezone，例如 `UTC`、`Asia/Shanghai`；
- `create`、`list`、`get`、`update`、`pause`、`resume`、`run`、`delete`、`history`；
- 独立的任务定义、运行记录、重试 attempt 和历史；
- `skip`、`queue`、`parallel` 重叠策略；
- `skip`、`run_once`、`catch_up` misfire 策略；
- 指数退避与 jitter 的 Agent 执行重试；
- 每次运行独立的 Skill 预加载和产物目录；
- 文本、产物文件、失败通知和静默完成等投递策略；
- 持久投递准备，Gateway 重启后能够继续准备和发送结果；
- 与任务版本、prompt、toolsets、路径、Terminal 约束和投递目标绑定的持久授权。

Cron 默认要求显式提供最小 toolsets。Cron 内禁止 Cron 管理工具自递归，也不会默认开放后台 Delegate。

### 安全与审批

- 文件路径统一经过拒绝规则、敏感文件规则和 allowed roots 检查；
- Local Terminal 会过滤项目自身的基础设施凭据；
- 高风险操作可在 CLI 交互确认，或在 Gateway 中创建持久远程审批请求；
- 审批绑定会话、工具调用、规范化参数和 fingerprint，参数变化后不能复用旧批准；
- Cron 使用独立的 `CronCapabilityGrant`，不会复用普通会话的一次性授权；
- Terminal 的 Cron 授权可限制 executable、工作目录、Shell 操作符、重定向、后台执行和网络访问；
- 工具执行 Journal 区分可安全重试和崩溃后结果未知的操作。

路径策略是应用层访问控制。它不能把 Local Terminal 变成操作系统级沙箱；需要强隔离时应使用 Docker、独立账号或其他系统级隔离机制。

### 持久化

项目使用 SQLite 保存会话、消息、Gateway 状态、审批、Outbox、文件投递、工具执行记录、CronJob、CronRun、授权和产物信息。

数据库使用 schema version 和顺序 migration 升级，并启用 WAL、busy timeout 和事务保护。正式运行状态不依赖旧的 `jobs.json`；旧任务只通过幂等迁移导入。

## 项目结构

下面列出仓库中全部 Python 文件。目录说明以当前代码职责为准；`__init__.py` 主要负责包边界、公开接口或模块装配。

```text
my-hermes/
├─ main.py                                      argv 分发与交互式 CLI 入口
├─ hermes/
│  ├─ __init__.py                               Hermes 包入口
│  ├─ _io_utils.py                              文件锁与原子文本写入工具
│  ├─ agent_loop.py                             通用 AgentLoop、轮次控制与结构化结果
│  ├─ approval.py                               远程审批 Tool Result 协议
│  ├─ approval_policy.py                        File、Terminal 风险评估与审批策略
│  ├─ config.py                                 .env、config.yaml 与运行常量
│  ├─ conversation.py                           同步和异步会话 AgentLoop 入口
│  ├─ db.py                                     旧调用方兼容的数据库导出层
│  ├─ delegate_jobs.py                          内存型后台 Delegate 任务管理
│  ├─ durable_tool_dispatcher.py                带执行 Journal 的工具分发包装
│  ├─ errors.py                                 模型错误分类、退避与 fallback
│  ├─ file_state.py                             文件状态快照和乐观并发检查
│  ├─ outbound_file.py                          出站文件路径验证与稳定快照
│  ├─ path_policy.py                            模型可控路径的统一拒绝策略
│  ├─ path_utils.py                             Windows 与 Git Bash 路径转换
│  ├─ prompt.py                                 system prompt 与项目上下文组装
│  ├─ redaction.py                              凭据和 Terminal 输出脱敏
│  ├─ security.py                               安全策略兼容导出层
│  ├─ skill_security.py                         Skill 内容风险扫描与信任状态
│  ├─ terminal_path_preflight.py                Local Terminal 命令路径预检查
│  ├─ tokens.py                                 token 估算与上下文压缩
│  ├─ tool_execution_recovery.py                Gateway、Cron 工具执行恢复策略
│  ├─ backends/
│  │  ├─ __init__.py                            执行环境接口、工厂与环境变量过滤
│  │  ├─ docker.py                              Docker 命令执行 backend
│  │  ├─ local.py                               本地 subprocess 执行 backend
│  │  └─ ssh.py                                 SSH ControlMaster 执行 backend
│  ├─ tools/
│  │  ├─ __init__.py                            ToolRegistry、工具元数据与统一解析器
│  │  ├─ delegate.py                            同步及后台 Delegate 工具
│  │  ├─ file.py                                文件读取、写入、替换和目录操作工具
│  │  ├─ gateway_send_file.py                   普通 Gateway 会话文件发送工具
│  │  ├─ memory.py                              长期记忆与用户档案工具
│  │  ├─ skill.py                               Skill 查看、列举和管理工具
│  │  └─ terminal.py                            Terminal 工具与 backend 调用入口
│  ├─ cron/
│  │  ├─ __init__.py                            Cron 子系统公开接口
│  │  ├─ artifacts.py                           系统管理的 Cron 产物路径规则
│  │  ├─ capability.py                          Cron 持久授权 scope 与运行时 Guard
│  │  ├─ executor.py                            独立 Cron Agent 会话执行器
│  │  ├─ gateway_scheduler.py                   lease 约束下的数据库 Cron 调度器
│  │  ├─ job.py                                 CronJob 与 CronRun 数据模型
│  │  ├─ parser.py                              one-shot、interval、Cron 与时区解析
│  │  ├─ store.py                               Cron 数据库 Store 与旧 jobs.json 导入
│  │  └─ tool.py                                Cron 生命周期管理工具
│  ├─ gateway/
│  │  ├─ __init__.py                            Gateway 包公开接口
│  │  ├─ cache.py                               图片、音频媒体缓存兼容入口
│  │  ├─ file_transfer.py                       Gateway 文件传输配置解析
│  │  ├─ observability.py                       日志使用的稳定脱敏标识
│  │  ├─ outbound_delivery.py                   统一出站文件验证与投递服务
│  │  ├─ persistence.py                         同步数据库 API 的异步调用边界
│  │  ├─ runner.py                              消息路由、Agent 调用与回复编排
│  │  ├─ runtime_lease.py                       Gateway runtime lease 生命周期
│  │  ├─ session_store.py                       按 route key 管理会话运行状态
│  │  ├─ text_utils.py                          UTF-16 截断、去重与文本批处理
│  │  ├─ types.py                               平台无关消息、附件和发送结果类型
│  │  ├─ adapters/
│  │  │  ├─ __init__.py                         平台 Adapter 抽象接口
│  │  │  ├─ cli.py                              使用 GatewayRunner 的本地 CLI Adapter
│  │  │  ├─ console.py                          stdin/stdout Console Adapter
│  │  │  ├─ feishu.py                           飞书 Webhook、消息收发和生命周期
│  │  │  ├─ feishu_files.py                     飞书资源下载与文件上传 HTTP 边界
│  │  │  ├─ simulated.py                        脚本化消息模拟 Adapter
│  │  │  ├─ webhook_security.py                 Webhook IP、代理与限流安全组件
│  │  │  └─ weixin.py                           个人微信 iLink Bot Adapter 与登录
│  │  └─ files/
│  │     ├─ __init__.py                         Gateway 文件边界公开接口
│  │     └─ cache.py                            平台无关的入站文件缓存与清理
│  ├─ persistence/
│  │  ├─ __init__.py                            SQLite 持久化统一公开接口
│  │  ├─ approval.py                            Gateway 审批、恢复和审计数据访问
│  │  ├─ core.py                                会话、消息和模型调用事件数据访问
│  │  ├─ cron.py                                CronJob、CronRun、Grant 和产物数据访问
│  │  ├─ database.py                            SQLite 连接、事务和结构化异常
│  │  ├─ delivery.py                            Outbox 与文件 Delivery 创建和查询
│  │  ├─ feishu.py                              飞书 Inbox、附件和重试数据访问
│  │  ├─ gateway.py                             Gateway 队列、lease 与 ownership 数据访问
│  │  ├─ gateway_delivery.py                    Outbox、消息及文件投递状态协调
│  │  ├─ schema.py                              schema version 与 migration 总调度
│  │  ├─ tool_execution.py                      工具执行 Journal 数据访问
│  │  ├─ schemas/
│  │  │  ├─ __init__.py                         最新版 DDL 装配
│  │  │  ├─ approval.py                         审批领域最新版 DDL
│  │  │  ├─ core.py                             会话与消息最新版 DDL
│  │  │  ├─ cron.py                             Cron 领域最新版 DDL
│  │  │  ├─ delivery.py                         Outbox 与 Delivery 最新版 DDL
│  │  │  ├─ feishu.py                           飞书 Inbox 与附件最新版 DDL
│  │  │  ├─ gateway.py                          Gateway 队列与 lease 最新版 DDL
│  │  │  └─ tool_execution.py                   工具执行 Journal 最新版 DDL
│  │  └─ migrations/
│  │     ├─ __init__.py                         历史 migration 公开与装配
│  │     ├─ approval.py                         审批领域历史 migration
│  │     ├─ core.py                             会话与核心表历史 migration
│  │     ├─ cron.py                             Cron 领域历史 migration
│  │     ├─ delivery.py                         Delivery 领域历史 migration
│  │     ├─ feishu.py                           飞书领域历史 migration
│  │     ├─ gateway.py                          Gateway 领域历史 migration
│  │     ├─ mixed.py                            跨领域原子 migration
│  │     └─ tool_execution.py                   工具执行 Journal 历史 migration
│  ├─ gateway_console.py                        Console Gateway 启动入口
│  ├─ gateway_entry.py                          多平台统一 Gateway 启动入口
│  ├─ gateway_simulated.py                      Simulated Gateway 启动入口
│  └─ gateway_weixin_login.py                   个人微信二维码登录入口
├─ .claude/
│  └─ skills/write-development-notes/scripts/
│     └─ validate_notes.py                      开发笔记结构校验脚本
├─ 开发说明/                                    面向后续开发的模块指南
├─ config.yaml.example                          配置示例
├─ pyproject.toml                               项目元数据与依赖
└─ requirements.txt                             pip 依赖清单
```

## 环境要求

- Python 3.13 或更高版本；
- 一个 OpenAI 兼容的模型服务；
- Local Terminal 需要可用的 Bash 环境；
- Docker 和 SSH backend 只有在选择相应 backend 时才需要额外环境。

## 安装

使用 pip：

```bash
python -m venv .venv
python -m pip install -r requirements.txt
```

也可以使用 uv：

```bash
uv sync
```

## 配置

复制示例配置：

```bash
cp config.yaml.example config.yaml
```

Windows PowerShell：

```powershell
Copy-Item config.yaml.example config.yaml
```

至少需要配置模型地址、模型名称和 API Key。API Key 建议放在项目 `.env` 或环境变量中，不要提交到版本库：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL=your-model
```

常用环境变量：

| 变量 | 作用 |
|---|---|
| `HERMES_HOME` | 配置、数据库、记忆和 Skill 的根目录 |
| `DB_PATH` | SQLite 数据库路径 |
| `OPENAI_API_KEY` | 主模型 API Key |
| `OPENAI_BASE_URL` | 主模型 API 地址 |
| `MODEL` | 主模型名称 |
| `FALLBACK_API_KEY` | fallback 模型 API Key |
| `FALLBACK_BASE_URL` | fallback 模型地址 |
| `FALLBACK_MODEL` | fallback 模型名称 |

完整字段请参考 `config.yaml.example`。加载优先级是环境变量高于 `config.yaml`。

## 运行

交互式 CLI：

```bash
python main.py
```

按 `config.yaml` 启动统一 Gateway：

```bash
python main.py --gateway
```

其他入口：

```bash
python main.py --gateway-console
python main.py --simulate
python main.py --weixin-login
```

统一 Gateway 会读取 `gateway.platforms`，只启动显式启用的平台。飞书和微信凭据应通过环境变量引用或私有配置提供。

## 扩展项目

新增普通工具时，通常只需要：

1. 在 `hermes/tools/` 中实现程序级能力、模型 handler 和 `register()`；
2. 在 `hermes/tools/__init__.py` 的 `register_all()` 中注册一次；
3. 声明 toolset、运行环境、无人值守、审批、风险和恢复策略；
4. 需要持久化时，在 `hermes/persistence/` 增加领域接口与 migration；
5. 只有功能需要远程消息、Outbox 或 Gateway 生命周期时才修改 Gateway。

详细规则见 [新功能接入指南](开发说明/新功能接入指南.md)。

## 当前边界

- 未实现通用 MCP Server 发现和动态工具目录；
- 未提供内置联网搜索工具；
- 未实现自动生成、验证并应用代码变更的自进化闭环；
- Local Terminal 的应用层路径规则不是强沙箱；
- Gateway 的部分历史编排仍集中在 `GatewayRunner`，与 Gateway 无关的新能力不应继续加入其中。

## 开发约定

- 代码注释使用中文，代码标识符和错误类型保持英文；
- 新工具必须经过 ToolRegistry 注册和会话策略解析；
- 程序内部不要通过 `json.loads()` 调用模型工具 handler，应提供结构化程序级接口；
- 不要把凭据、完整内部 route key、原始敏感命令参数或本地隐私内容写入日志和审批消息；
- 新增数据库状态时沿用现有 schema migration，不要求用户删除数据库；
- 与 Gateway 无关的功能不应修改 GatewayRunner。
