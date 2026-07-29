# MyHermes

MyHermes 是一个参考 [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) 方法架构开发的模块化 Python Agent。

项目以统一 AgentLoop 为核心，将终端 CLI、消息 Gateway、Cron 定时任务、Delegate 子 Agent、工具调用、审批、持久化、长期记忆、Skills、Browser 和后台 Review 连接到同一套运行框架中。

> [!IMPORTANT]
> MyHermes 是独立学习与实现项目，并非 Nous Research 官方 Hermes Agent，也不保证与官方版本的配置、插件或数据格式完全兼容。

## 当前能力

MyHermes 当前适合以下场景：

* 在终端中运行支持工具调用、流式输出和持久会话的 Agent；
* 通过飞书、个人微信或本地 CLI Adapter 使用远程 Agent；
* 读取和修改文件、执行命令并保持会话工作目录；
* 使用浏览器完成页面访问、交互、结构化提取、截图和下载；
* 分析本地图片或音频；
* 保存长期记忆与用户资料；
* 创建、读取和持续改进本地 Skills；
* 将任务委派给同步或后台子 Agent；
* 创建具备时区、最小权限、重试和投递策略的定时 Agent 任务；
* 对高风险命令、文件、浏览器、媒体分析、文件发送和 Cron 授权进行审批；
* 在 Gateway 重启后恢复消息队列、审批、工具执行、Cron 和待投递结果；
* 在前台任务完成后，后台审视是否需要更新 Memory 或 Skill；
* 通过 Hooks 在模型调用和工具调用前后执行控制或观察逻辑。

当前没有内置通用 MCP Client、联网搜索工具、完整 Plugin 发现器或自动修改项目代码的自进化闭环。

---


## 核心子系统

### 1. AgentLoop

`hermes/agent_loop.py` 提供同步和异步 Agent 循环的公共骨架，负责：

* 模型调用；
* 流式响应累积；
* Assistant 消息解析；
* 工具调用参数校验；
* 工具分发；
* 循环次数限制；
* 取消状态检查；
* 结构化错误结果；
* Hook 事件分发；
* 工具批次与调用数量统计。

主会话通过 `ConversationAgentLoop` 加入：

* SQLite 会话历史；
* 上下文压缩；
* 模型错误分类；
* jittered backoff 重试；
* fallback 模型切换；
  -输出截断后的 continuation；
* Assistant 与 Tool 消息的原子持久化；
* 审批暂停和恢复；
* 后台 Review 触发。

Delegate 只复用公共循环骨架，不会自动获得主会话的数据库、长期记忆、压缩、fallback 或 continuation。

Gateway 使用异步 AgentLoop，因此 `/stop`、`/new` 或新消息可以取消当前异步模型请求。

---

### 2. Prompt 组装

系统提示可以由以下内容组成：

* `<HERMES_HOME>/SOUL.md`；
* `<HERMES_HOME>/memories/MEMORY.md`；
* `<HERMES_HOME>/memories/USER.md`；
* 当前可用 Skills 的摘要；
* 当前时间与工作目录；
* 工具能力与权限说明；
* 项目上下文文件。

项目上下文按以下优先级查找：

1. `.hermes.md`
2. `HERMES.md`
3. `AGENTS.md`
4. `CLAUDE.md`
5. `.cursorrules`

只加载找到的第一个文件，内容最多读取 20,000 个字符。

Agent 创建的临时辅助脚本统一保存到：

```text
<HERMES_HOME>/scripts/
```

避免在用户项目目录中散落框架生成的脚本。

---

### 3. ToolRegistry

所有工具注册到统一的 `ToolRegistry`。

每个工具会声明：

* 工具名称；
* 所属 toolset；
* JSON Schema；
* 支持的运行环境；
* 是否允许无人值守；
* 所需可信运行上下文；
* 审批方式；
* 风险等级；
* 是否默认启用；
* 崩溃后能否安全重试；
* 是否支持取消；
* 是否存在状态查询方式。

模型看到的工具 Schema 与运行时允许分发的工具名称来自同一次策略解析。

因此：

```text
工具已注册
≠ 当前会话拥有该工具
≠ 当前调用一定会被执行
```

工具还需要通过环境、toolset、风险、审批和可信上下文检查。

---

## 工具列表

| Toolset        | 主要工具                                                  | 运行环境                                     | 说明                   |
| -------------- | ----------------------------------------------------- | ---------------------------------------- | -------------------- |
| `terminal`     | `terminal`                                            | CLI / Gateway / Cron / Delegate          | 执行 Shell 命令并保持会话 cwd |
| `file`         | `file`                                                | CLI / Gateway / Cron / Delegate          | 文件读取、写入、替换、目录与元数据操作  |
| `memory`       | `memory`                                              | CLI / Gateway / Cron / Review            | 管理长期记忆和用户资料          |
| `skill_read`   | `skills_list`、`skill_view`                            | CLI / Gateway / Cron / Delegate / Review | 发现和读取 Skill          |
| `skill_manage` | `skill_manage`                                        | CLI / Gateway / Cron / Review            | 创建、修改和维护 Skill       |
| `delegate`     | `delegate_task`                                       | CLI / Gateway / Cron                     | 启动隔离的叶子子 Agent       |
| `delegate`     | `delegate_status`、`delegate_result`、`delegate_cancel` | CLI / Gateway                            | 管理后台 Delegate        |
| `cron`         | `cron`                                                | CLI / Gateway                            | 管理定时 Agent 任务        |
| `browser`      | `browser_*`                                           | CLI / Gateway                            | 浏览器访问、交互、提取和产物管理     |
| `media`        | `media_analyze`                                       | CLI / Gateway                            | 使用外部多模态模型分析本地图片或音频   |
| `messaging`    | `gateway_send_file`                                   | Gateway                                  | 向当前远程会话发送本地文件        |

CLI 默认启用本地核心工具。Gateway 按平台配置显式开放 toolset。

Browser 默认关闭，只有 `browser.enabled: true` 时才会进入 CLI 或 Gateway 的工具集合。

---

## File 工具

File 工具支持：

* `read`
* `read_range`
* `write`
* `append`
* `replace`
* `list`
* `stat`
* `pwd`
* `context`

主要行为：

* 相对路径以当前会话 backend 的 cwd 为起点；
* Terminal 执行 `cd` 后，File 会使用更新后的相同 cwd；
* 单次读取上限为 100 KB，超限后可以通过 `offset` 继续读取；
* `write` 默认不覆盖已有文件；
* 覆盖写通过临时文件和 `os.replace` 原子完成；
* `replace` 仅处理不超过 100 KB 的 UTF-8 文件；
* 统一拒绝 `security.filesystem.denied_paths`；
* 敏感文件按 critical 风险直接拒绝；
* 覆盖、追加和替换审批会绑定目标文件状态；
* 审批后文件发生变化时返回 `approval_stale`，不会继续写入。

File 的路径规则属于结构化强制检查，比从 Terminal 命令字符串中猜测路径更可靠。

当前 File 的实际 I/O 只支持 LocalBackend。Docker 和 SSH backend 下会返回 `unsupported_backend`。

---

## Terminal 与 Backend

Terminal 支持三类 backend：

| Backend  | 说明               |
| -------- | ---------------- |
| `local`  | 在本机启动 Shell 子进程  |
| `docker` | 在 Docker 容器中执行命令 |
| `ssh`    | 通过 SSH 在远程主机执行命令 |

每个会话使用独立 backend，以下状态不会跨会话共享：

* 当前工作目录；
* 导出的环境变量；
* 运行中的进程；
* 审批上下文。

本地 Shell 规则：

* Windows 使用 Git Bash / MSYS；
* Linux 和 macOS 使用 `/bin/bash`；
* Windows 下应生成 Bash 语法；
* Windows 绝对路径建议使用 `/d/project` 形式，而不是 PowerShell 命令。

Terminal 可以：

* 保持 cwd；
* 保持普通导出环境变量；
* 接收协作式取消；
* 在取消时中断当前进程组；
* 对输出进行凭据脱敏；
* 对明显路径执行审批前预检查。

> [!WARNING]
> Local Terminal 不是操作系统沙箱。
>
> 路径预检查只能识别结构清晰的命令，动态脚本、解释器代码或间接路径可能绕过字符串分析。需要强隔离时，应使用 Docker、独立系统账号、虚拟机或其他系统级安全边界。

Docker 也不会因为被称为“容器”就自动获得信任。宿主机挂载、Docker Socket 和远程 SSH 都会提高审批风险。

---

## Browser

Browser 是独立模块，通过 `hermes/tools/browser.py` 接入 ToolRegistry。

开启方式：

```yaml
browser:
  enabled: true
  headless: true
  channel: chrome
  idle_timeout_seconds: 1800
  startup_timeout_seconds: 30
  operation_timeout_seconds: 60
```

主要能力包括：

### 页面操作

* `browser_navigate`
* `browser_back`
* `browser_forward`
* `browser_reload`
* `browser_click`
* `browser_type`
* `browser_press`
* `browser_select`
* `browser_scroll`

### 页面等待

* `browser_wait_for_url`
* `browser_wait_for_text`
* `browser_wait_for_ref`
* `browser_wait_for_load_state`

### 页面读取

* `browser_snapshot`
* `browser_get_text`
* `browser_find_in_page`
* `browser_extract_links`
* `browser_extract_tables`
* `browser_extract_forms`
* `browser_extract_metadata`
* `browser_collect_paginated`

### 页面与产物管理

* `browser_list_pages`
* `browser_switch_page`
* `browser_close_page`
* `browser_screenshot`
* `browser_screenshot_element`
* `browser_download`
* `browser_list_artifacts`
* `browser_get_artifact`
* `browser_delete_artifact`
* `browser_cleanup_artifacts`

### 高风险能力

* `browser_upload_files`
* `browser_console`
* `browser_analyze_page`

Browser 使用 `snapshot_id` 和元素 `ref` 绑定页面状态。页面发生变化后应使用工具返回的新 `snapshot_id`，避免继续操作旧页面结构。

文件上传只能读取当前 Browser 固定工作区内的普通文件，并拒绝：

* 绝对路径；
* `..` 路径逃逸；
* 符号链接；
* 工作区外文件；
* 审批后发生变化的文件。

JavaScript 执行、文件上传、外部视觉模型分析和产物删除需要额外审批。

Browser 当前只在 CLI 和 Gateway 中使用，不开放给 Cron 或 Delegate。

---

## Media 分析

`media_analyze` 可以分析当前 LocalBackend 工作目录中的本地媒体文件。

支持：

* PNG
* JPEG
* WEBP
* MP3
* WAV
* AAC
* M4A

一次调用最多处理 20 个文件。

所有路径必须：

* 使用相对路径；
* 位于当前会话 cwd；
* 是普通文件；
* 不经过符号链接；
* 通过统一路径策略；
* 不属于敏感文件。

媒体内容会被发送给配置的外部多模态模型，因此每次调用都需要审批，并可能产生外部 API 费用。

审批会绑定文件大小、修改时间和内容摘要。文件变化后旧审批失效。

---

## Memory

长期记忆保存在：

```text
<HERMES_HOME>/memories/MEMORY.md
<HERMES_HOME>/memories/USER.md
```

其中：

* `MEMORY.md` 保存长期上下文；
* `USER.md` 保存稳定的用户资料和偏好；
* 条目之间使用 `§` 分隔。

Memory 工具支持：

* `read`
* `add`
* `remove`
* `replace`

写入流程为：

```text
获取文件锁
  → 重新读取最新条目
  → 校验内容
  → 写入临时文件
  → fsync
  → os.replace
  → 释放文件锁
```

因此并发写入不会简单覆盖对方的结果，写入失败时旧文件保持不变。

其他规则：

* 完全相同的条目不会重复写入；
* `remove` 和 `replace` 要求唯一子串匹配；
* 多条命中时返回候选项，不会猜测目标；
* 超过字符上限时拒绝整个写入；
* 拒绝 `§` 分隔符注入；
* 拒绝不可见 Unicode 控制字符；
* 对明显的凭据和 prompt injection 文本进行拦截。

---

## Skills

Skills 保存在：

```text
<HERMES_HOME>/skills/<skill-name>/SKILL.md
```

Skill 名称只能包含：

```text
A-Z a-z 0-9 _ -
```

一个 Skill 可以包含：

```text
skills/<name>/
├─ SKILL.md
├─ references/
├─ templates/
├─ scripts/
└─ assets/
```

工具包括：

* `skills_list`
* `skill_view`
* `skill_manage`

`skill_manage` 支持：

* `create`
* `edit`
* `patch`
* `delete`
* `write_file`
* `remove_file`

Skill 写入具备：

* 名称校验；
* 路径逃逸检查；
* 允许目录限制；
* 内容风险扫描；
* 单 Skill 操作锁；
* 原子写入；
* `revision` 乐观并发检查；
* `governance_revision` 治理状态检查；
* 用户管理、系统管理和后台管理来源区分。

后台 Review 修改 Skill 时必须先读取最新 revision，过期修改会被拒绝，避免后台任务覆盖用户刚刚完成的编辑。

Skills 采用渐进式加载：

1. System Prompt 只注入 Skill 摘要；
2. Agent 判断 Skill 与任务相关后调用 `skill_view`；
3. 需要时再读取其 support files。

---

## Delegate

`delegate_task` 会创建一个独立的叶子子 Agent。

每次调用都会生成唯一的：

```text
child_session_key
```

子 Agent 拥有独立的：

* backend；
* cwd；
* Terminal 状态；
* File 运行上下文；
* AgentLoop；
* 工具能力集合。

子 Agent不会获得：

* 主会话数据库历史；
* 长期 Memory；
* `skill_manage`；
* Cron 管理；
* 再次 Delegate 的能力；
* 主 Agent 的 compression；
* 主 Agent 的 fallback 和 retry；
* 主 Agent 的 continuation。

可授权的子 Agent toolset 只有：

* `terminal`
* `file`
* `skill_read`

默认使用：

```json
["terminal", "file"]
```

未知或不允许的 toolset 会直接返回 `invalid_args`，不会静默过滤。

### 同步 Delegate

```text
delegate_task(background=false)
  → 等待子 Agent 完成
  → 返回完整结果
```

### 后台 Delegate

```text
delegate_task(background=true)
  → 立即返回 job_id
  → delegate_status 查询状态
  → delegate_result 获取结果
  → delegate_cancel 请求取消
```

后台 Delegate 当前保存在进程内存中，进程退出后不能恢复。

Cron 中允许同步 Delegate，但禁止后台 Delegate，避免 Cron 主任务结束后遗留无所有者的后台线程。

远程会话不能将需要审批的 File 或 Terminal 操作交给 Delegate。此类操作必须由主 Agent 直接调用，确保审批后仍使用原会话 backend 和原始 cwd。

---

## Cron

Cron 是独立 Agent 任务，而不是单纯的 Shell 定时命令。

每次运行都会：

1. 领取一个持久化 CronRun；
2. 创建独立 Agent 会话；
3. 加载任务 Prompt；
4. 注入指定 Skills；
5. 按最小 toolsets 解析工具；
6. 应用 CronCapabilityGuard；
7. 执行 AgentLoop；
8. 保存运行结果和产物；
9. 按投递策略发送结果；
10. 更新下一次运行时间。

Cron 管理工具支持：

* `create`
* `list`
* `get`
* `update`
* `pause`
* `resume`
* `run`
* `delete`
* `history`

### 调度格式

一次性延迟：

```text
5m
2h
1d
```

固定间隔：

```text
every 5m
every 2h
```

五字段 Cron：

```text
0 9 * * 1-5
```

五字段表达式一定是重复任务，创建时必须显式设置：

```yaml
recurring: true
```

支持 IANA 时区，例如：

```text
UTC
Asia/Shanghai
America/Los_Angeles
```

### 运行策略

重叠策略：

* `skip`
* `queue`
* `parallel`

错过执行时间后的 misfire 策略：

* `skip`
* `run_once`
* `catch_up`

重试策略支持：

* 最大尝试次数；
* 基础退避时间；
* 最大退避时间；
* jitter；
* 可重试错误类型白名单。

### 最小权限

创建 Cron 时必须显式提供最小 toolsets。

例如，只需读取文件：

```yaml
toolsets:
  - file
```

需要执行命令时才加入：

```yaml
toolsets:
  - file
  - terminal
```

Cron 内不能再次调用 Cron 管理工具。

使用 Terminal 时还必须设置可执行文件白名单：

```yaml
capability_spec:
  terminal_allowed_executables:
    - python
    - git
  terminal_allow_shell_operators: false
  terminal_allow_redirection: false
  terminal_allow_background: false
  terminal_allow_network: false
```

Cron 的 `workdir` 同时定义文件访问边界。Prompt 中出现的绝对路径必须位于该目录内。

文件写入默认关闭：

```yaml
capability_spec:
  allow_file_write: false
```

### 投递策略

支持：

* `text`
* `text_and_files`
* `failure_only`
* `silent`

需要向远程会话投递产物时：

```yaml
delivery_policy: text_and_files

capability_spec:
  allow_file_write: true
```

产物只能写入系统管理的 Cron artifact 目录，并受单文件与总大小限制。

---

## Gateway

统一 Gateway 通过平台 Adapter 接收消息，再交给 `GatewayRunner`。

当前 Adapter 包括：

* 本地 CLI Adapter；
* Console Adapter；
* Simulated Adapter；
* 飞书 / Lark Adapter；
* 个人微信 iLink Bot Adapter。

Gateway 具备：

* 按 route key 隔离会话；
* 同一路由串行处理；
* 不同路由并发处理；
* 单路由有界 pending 队列；
* 全局模型并发限制；
* 新消息取消当前模型 Task；
* SQLite 持久化消息队列；
* Gateway runtime lease；
* 多实例 fencing；
* 持久审批；
* 持久 Outbox；
* 文件下载和上传任务；
* Cron 调度和投递；
* 中断工具恢复；
* 重启后的状态协调；
* 终态记录定期清理。

统一 Gateway 启动命令：

```bash
python main.py --gateway
```

等价兼容入口：

```bash
python main.py --gateway-unified
```

其他入口：

```bash
python main.py --gateway-console
python main.py --simulate
python main.py --weixin-login
```

Gateway 只启动 `config.yaml` 中明确启用的平台。

### Gateway 上下文隔离

可以分别控制私聊、群聊和话题会话是否读取：

* SOUL；
* Memory；
* USER；
* 项目上下文。

默认策略是：

| 会话类型  | SOUL | MEMORY | USER | 项目上下文 |
| ----- | ---: | -----: | ---: | ----: |
| 未识别类型 |    是 |      否 |    否 |     否 |
| 私聊    |    是 |      是 |    是 |     否 |
| 群聊    |    是 |      否 |    否 |     否 |
| Topic |    是 |      否 |    否 |     否 |

这可以防止私人 Memory 和 USER 信息默认进入群聊。

### Gateway 命令

常用远程命令：

```text
/new
/stop
/approve
/approve session
/deny
```

审批只能由原请求者、在原路由和请求有效期内处理。

### 飞书

飞书 Adapter 支持：

* Webhook 校验；
* Verification Token；
* Encrypt Key；
  -可信代理配置；
* 来源 IP 限流；
* 请求体大小与读取超时限制；
* 用户与群聊白名单；
* 群聊 @机器人要求；
* Inbox 去重；
* Inbox 重试；
* 消息发送限流；
* 文本分段；
* 附件下载；
* 持久文件上传；
* 处理中状态和最终回复。

正式部署前建议：

```yaml
allow_all: false
allowed_users:
  - your-user-id
allowed_chats:
  - your-chat-id
```

不要在公开服务中保留无限制的 `allow_all: true`。

---

## Hooks

当前 Hook Runtime 支持固定事件：

* `pre_llm_call`
* `pre_tool_call`
* `post_llm_call`
* `post_tool_call`
* `run_end`

其中：

* `pre_llm_call` 可以阻止模型调用或向当前轮增加上下文；
* `pre_tool_call` 可以阻止工具调用；
* `post_llm_call` 用于观察模型调用结果；
* `post_tool_call` 用于观察工具执行结果；
* `run_end` 在 AgentLoop 结束时触发。

控制结果包括：

* `Allow`
* `Block`
* `AddContext`

Hook Registry 提供同步和异步版本。

主要规则：

* 回调按注册顺序执行；
* Hook Context 顶层数据只读；
* 同一事件不允许重复 hook ID；
* 单个 Hook 失败不会直接让整个 Registry 崩溃；
* 异步 Hook 可以设置独立超时；
* Delegate 可以继承经过桥接的运行期 Hook；
* 后台 Delegate 会显式管理 Hook Bridge 生命周期。

当前 `PluginContext` 只提供固定 Hook 的注册上下文，不负责：

* Plugin 目录发现；
* `plugin.yaml` 加载；
* Plugin 启停；
* 工具注册；
* CLI 命令注册；
* Plugin 依赖与版本管理。

因此当前属于 Hook 与 PluginContext 基础设施阶段，还不是完整的 Hermes Plugin 系统。

---

## Background Review

后台 Review 默认关闭：

```yaml
background_review:
  enabled: false
```

开启后，它会在前台会话完成后判断是否需要提交后台任务。

```yaml
background_review:
  enabled: true
  memory_interval: 3
  skill_tool_batch_interval: 4
  claim_ttl_seconds: 1800
  retry_cooldown_seconds: 60
  max_iterations: 8
  max_concurrent_jobs: 1
  max_pending_jobs: 32
```

### Memory Review

Memory Review 每累计指定数量的已完成前台任务触发一次。

它只检查上一次 Review 之后新增的消息窗口，并且：

* 将用户消息视为用户意图的主要证据；
* 将工具结果视为实际观察；
* 将 Assistant 决策和总结标记为未验证；
* 不把网页、文件或工具输出中的指令当作用户要求；
* 不因 Assistant 自己的描述直接写入 Memory；
* 修改前重新读取当前 Memory；
* 避免语义重复；
* 用户纠正旧信息时优先更新或删除冲突条目；
* 把稳定偏好、长期要求和可跨会话事实写入 Memory；
* 不保存一次性结果、临时进度和工具操作流程。

Memory Review 只能使用 `memory` toolset。

### Skill Review

Skill Review 根据前台工具批次数触发。

它会构造固定证据窗口，区分：

* `USER_MESSAGE`
* `TOOL_OBSERVATION`
* `TOOL_ERROR`
* `ASSISTANT_DECISION — UNVERIFIED`
* `ASSISTANT_REPORT — UNVERIFIED`

它重点寻找：

```text
失败
  → 改变策略
  → 再次执行
  → 得到可验证结果
```

只有形成稳定、可复用并且有工具证据支持的方法时，才会创建或修改 Skill。

Skill Review 会：

* 优先改进当前任务实际加载的 Skill；
* 优先使用已有上位 Skill；
* 将具体记录写入 support file；
* 只有确实不存在合适 Skill 时才创建新 Skill；
* 不保存用户偏好、项目状态和一次性事实；
* 修改前检查 revision 与 governance revision；
* 不覆盖用户管理、系统管理、外部或 pinned Skill。

---

## 审批与安全策略

审批不是简单的“弹窗后执行”，而是与具体操作绑定的授权对象。

审批会绑定：

* session key；
* tool call ID；
* 完整工具参数；
* 规范化命令或路径；
* 当前工作目录；
* operation fingerprint；
* 文件状态；
* Browser 页面或产物状态；
* 媒体文件状态；
* 风险等级；
* 请求者身份；
* 有效期；
* 允许的授权范围。

支持：

```text
once
session
```

`session` 授权也不是无条件放行，只会匹配结构化命令、路径规则、当前会话和风险上限。

### 风险处理

一般策略为：

| 风险       | 处理             |
| -------- | -------------- |
| Low      | 可直接执行或按策略记录    |
| Medium   | 可执行、审批或受会话授权约束 |
| High     | 通常要求一次性审批      |
| Critical | 直接拒绝，不能审批      |

内置 hardline 规则会拒绝明显的高危操作，例如：

* 根目录或磁盘根递归删除；
* 文件系统格式化；
* 原始块设备写入；
* Fork Bomb；
* 破坏关键安全服务；
* 修改 MyHermes 自身审批配置；
* Docker Socket 等可直接控制宿主机的能力。

### 路径策略

```yaml
security:
  filesystem:
    denied_paths:
      - /path/to/private-data
```

`denied_paths` 同时拒绝目标路径和全部子路径。

该规则会应用于：

* File；
* Local Terminal 的尽力预检查；
* Browser 文件上传；
* Media；
* Gateway 文件发送；
* Cron 能力范围；
* Docker mount 配置验证。

但它仍然是应用层策略，不等同于操作系统级沙箱。

### 凭据保护

项目会尽量避免将以下内容进入模型上下文、日志或审批消息：

* API Key；
* Authorization Header；
* Access Token；
* Password；
* Verification Code；
  -基础设施环境变量；
* 完整本地敏感文件内容；
* 大段 Browser 输入；
* Skill 和 File 写入正文；
* 完整 traceback。

脱敏只能减少泄漏面，不能阻止已经获准运行的本地进程主动读取和发送数据。

---

## 持久化

MyHermes 使用 SQLite 保存：

* 会话；
  -消息；
* 模型调用事件；
* Gateway 路由状态；
* Gateway pending 队列；
* 消息 ownership；
* Gateway runtime lease；
* 审批请求；
* 审批恢复状态；
* 审批审计记录；
* Outbox；
* 文件投递任务；
* 工具执行 Journal；
* CronJob；
* CronRun；
* Cron Capability Grant；
* Cron 产物；
* 飞书 Inbox；
* 飞书附件；
* Memory Review 进度；
* Skill Review 进度。

数据库具备：

* schema version；
* 顺序 migration；
* foreign key；
* index；
* WAL；
* busy timeout；
  -显式事务；
  -结构化数据库错误；
  -跨表原子写入；
  -重启恢复与状态协调。

正式 Cron 状态保存在 SQLite。旧版 `jobs.json` 只用于幂等迁移，不再是运行时事实来源。

---

## CLI 状态机

默认 CLI 不再使用主线程中的简单阻塞循环。

当前结构为：

```text
输入线程
  → CLIEventQueue
  → CLIController
  → 单一 CLIWorker
  → AgentLoop
  → Worker Result
  → CLIController
  → UI
```

主要特性：

* 用户输入和流式事件统一事件化；
* 单个 Worker 串行执行数据库工作；
* 每个 Worker 任务独占自己的 SQLite 连接；
* Agent 运行期间可以继续输入普通消息；
* 普通消息进入有界队列；
* 会话切换命令不会和运行中任务并发；
* Ctrl+C 或 `/stop` 设置当前取消事件；
* 关闭时会清空尚未提交的消息，并等待 Worker 安全收尾。

CLI 普通消息队列默认最多保存 20 条。

### CLI 命令

```text
/new
/sessions
/resume
/resume <编号或会话 ID>
/stop
/approve
/approve once
/approve session
/deny
/quit
/exit
```

当确实需要向模型发送以 `/` 开头的普通文本时，可以：

```text
//literal-message
```

或者在文本前加入空格。

---

## 环境要求

* Python 3.13 或更高版本；
* 一个 OpenAI 兼容的模型接口；
* Local Terminal 需要 Bash；
* Windows Local Terminal 需要 Git Bash；
* Browser 开启时需要可用的 Chrome；
* Docker backend 需要 Docker；
* SSH backend 需要可连接的 SSH 服务；
* 飞书 Gateway 需要对应应用凭据；
  -微信 Gateway 需要可用的 iLink Bot 凭据。

---

## 安装

克隆仓库：

```bash
git clone https://github.com/djwcb-93589/my-hermes.git
cd my-hermes
```

使用 `uv`：

```bash
uv sync
```

或者使用 `pip`：

```bash
python -m venv .venv
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

不要将真实 `.env`、`config.yaml`、数据库、Memory 或本地 Skill 提交到版本库。

---

## 配置

复制示例配置：

```bash
cp config.yaml.example config.yaml
```

Windows PowerShell：

```powershell
Copy-Item config.yaml.example config.yaml
```

推荐将凭据保存在 `.env`：

```dotenv
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://your-openai-compatible-endpoint/v1
MODEL=your-model

FALLBACK_API_KEY=
FALLBACK_BASE_URL=
FALLBACK_MODEL=
```

基础配置示例：

```yaml
model: ${MODEL}
max_output_tokens: 8192
base_url: ${OPENAI_BASE_URL}
api_key: ${OPENAI_API_KEY}

fallback:
  model: ${FALLBACK_MODEL}
  max_output_tokens: 8192
  base_url: ${FALLBACK_BASE_URL}
  api_key: ${FALLBACK_API_KEY}

limits:
  max_iterations: 40
  max_child_iterations: 20
  max_retries: 3
  max_continuations: 3
  model_timeout_seconds: 120

compression:
  threshold: 180000
  protect_first: 4
  keep_recent_tool_results: 4
  tail_token_budget: 60000

memory:
  memory_char_limit: 4000
  user_char_limit: 2000

background_review:
  enabled: false
  memory_interval: 3
  skill_tool_batch_interval: 0
  claim_ttl_seconds: 1800
  retry_cooldown_seconds: 60
  max_iterations: 8
  max_concurrent_jobs: 1
  max_pending_jobs: 32

db_path: database/hermes.db

browser:
  enabled: false
  headless: true
  channel: chrome
  idle_timeout_seconds: 1800
  startup_timeout_seconds: 30
  operation_timeout_seconds: 60

terminal:
  backend: local
  docker_image: python:3.11-slim
  docker_mounts: []
  ssh_host: ''
  ssh_user: ''
  ssh_key: ''
  env_passthrough: []

security:
  filesystem:
    denied_paths: []

  approval:
    denied_command_patterns: []
    denied_executables: []
    protected_paths: []
    denied_file_rules: []
    remote_default_allow: true
    approval_file_rules: []
    request_ttl_seconds: 600

gateway:
  agent_name: main
  max_pending_messages: 2
  max_concurrent_llm_requests: 1

  platforms:
    cli:
      enabled: false

    feishu:
      enabled: false
      toolsets:
        - file
        - terminal
        - memory
        - skill_read
        - delegate
        - cron
        - messaging

    weixin:
      enabled: false
```

完整配置和字段注释见：

```text
config.yaml.example
```

加载优先级为：

```text
环境变量
  > config.yaml
  > 程序默认值
```

### HERMES_HOME

默认情况下，项目根目录作为 `HERMES_HOME`。

也可以显式设置：

```bash
export HERMES_HOME=/path/to/my-hermes-home
```

PowerShell：

```powershell
$env:HERMES_HOME = "D:\my-hermes-home"
```

`HERMES_HOME` 下通常包含：

```text
<HERMES_HOME>/
├─ .env
├─ config.yaml
├─ SOUL.md
├─ database/
│  └─ hermes.db
├─ memories/
│  ├─ MEMORY.md
│  └─ USER.md
├─ skills/
├─ scripts/
├─ cache/
├─ browser/
└─ cron artifacts/
```

---

## 运行

### 交互式 CLI

```bash
python main.py
```

### 统一 Gateway

```bash
python main.py --gateway
```

### Console Gateway

```bash
python main.py --gateway-console
```

### 模拟 Gateway

```bash
python main.py --simulate
```

### 微信登录

```bash
python main.py --weixin-login
```

---

## 项目结构

```text
my-hermes/
├─ main.py
├─ config.yaml.example
├─ requirements.txt
├─ pyproject.toml
├─ browser/
│  ├─ runtime/
│  ├─ session/
│  ├─ artifacts/
│  └─ multimodal/
├─ hermes/
│  ├─ agent_loop.py
│  ├─ conversation.py
│  ├─ cli.py
│  ├─ cli_state_machine.py
│  ├─ cli_approval.py
│  ├─ config.py
│  ├─ errors.py
│  ├─ prompt.py
│  ├─ redaction.py
│  ├─ path_policy.py
│  ├─ durable_tool_dispatcher.py
│  ├─ tool_execution_recovery.py
│  ├─ backends/
│  ├─ tools/
│  ├─ skills/
│  ├─ hooks/
│  ├─ plugins/
│  ├─ review/
│  ├─ cron/
│  ├─ gateway/
│  └─ persistence/
├─ skills/
├─ 开发说明/
└─ tests and e2e scripts
```

主要职责：

| 路径                            | 职责                               |
| ----------------------------- | -------------------------------- |
| `browser/`                    | 独立 Browser Runtime、页面会话、产物和多模态分析 |
| `hermes/agent_loop.py`        | 同步/异步 AgentLoop 公共骨架             |
| `hermes/conversation.py`      | 主会话压缩、重试、fallback、持久化和 Review 触发 |
| `hermes/cli_state_machine.py` | CLI Controller、事件队列和 Worker      |
| `hermes/backends/`            | Local、Docker 和 SSH 执行环境          |
| `hermes/tools/`               | ToolRegistry 适配层与工具审批            |
| `hermes/skills/`              | Skill 领域模型、存储、信任和管理服务            |
| `hermes/hooks/`               | Hook 契约、Registry、控制结果和同步桥接       |
| `hermes/plugins/`             | 当前最小 Plugin Hook 注册上下文           |
| `hermes/review/`              | Memory/Skill 后台 Review、证据和进度管理   |
| `hermes/cron/`                | Cron 调度、执行、授权和产物                 |
| `hermes/gateway/`             | 平台 Adapter、路由、投递和运行租约            |
| `hermes/persistence/`         | SQLite schema、migration 和领域数据访问  |

---

## 扩展方式

### 新增工具

通常需要：

1. 在 `hermes/tools/` 中实现 handler；
2. 提供严格 JSON Schema；
3. 在 `register()` 中声明工具元数据；
4. 在 `register_all()` 中装配；
5. 选择允许的运行环境；
6. 声明 unattended、approval、risk 和 retry 语义；
7. 需要审批时注册审批 handler；
8. 需要崩溃恢复时接入 DurableToolDispatcher；
9. 需要持久化时在 `hermes/persistence/` 增加领域接口和 migration。

不要通过修改 GatewayRunner 来实现与消息平台无关的普通工具能力。

### 新增 Gateway Adapter

Adapter 应负责：

* 平台认证；
* 平台事件转成 `MessageEvent`；
* 来源授权；
* 文本和附件发送；
* 平台错误分类；
* 平台级快速重试。

Runner 负责：

* 会话路由；
* Agent 调用；
* 持久队列；
  -审批；
* Outbox；
  -恢复；
* Cron；
  -通用投递编排。

### 新增 Hook

通过 `SyncHookRegistry` 或 `AsyncHookRegistry` 注册固定事件。

Plugin 侧应使用 `PluginContext.register_hook()`，而不是直接修改 AgentLoop。

---

## 当前边界

当前版本仍存在以下明确边界：

* 不是 Nous Research 官方 Hermes；
* 不具备官方 Hermes 的全部 Provider、Backend、Gateway 平台和工具；
* 没有通用 MCP Client；
* 没有内置联网搜索工具；
* PluginContext 尚不负责 Plugin 发现和生命周期；
* 没有 Gateway Hook 目录发现或 Shell Hook 配置层；
* Background Delegate 是进程内任务，重启后不会恢复；
* Local Terminal 不是强沙箱；
* File I/O 当前只支持 LocalBackend；
* Browser 只开放给 CLI 和 Gateway；
* Media 当前绑定特定外部多模态服务；
* 后台 Review 默认关闭，开启后会产生额外模型调用；
* 自动改进仅限受治理的 Memory 和 Skill，不会自动修改项目代码；
* GatewayRunner 仍然较大，后续应继续拆分路由、审批和恢复编排；
* 当前仍处于持续开发阶段，不建议未经额外审计直接作为公网高权限 Agent 服务使用。

---

## 与真实 Hermes 的关系

MyHermes 主要参考真实 Hermes 的以下设计方向：

* 多入口共享 Agent 运行核心；
* Prompt 由 SOUL、Memory、Skills 和项目上下文组成；
* 中央工具注册与环境级能力解析；
* Local、Docker、SSH 等执行 backend；
* 持久会话和 SQLite 状态；
* 消息 Gateway 与平台 Adapter；
* Cron 作为独立 Agent 任务；
* Delegate 子 Agent；
* Memory 和 Skills 的持续学习闭环；
* 模型调用和工具调用生命周期 Hooks；
* 风险审批与强隔离安全边界区分。

MyHermes 不追求逐文件复制真实 Hermes，而是先理解其方法架构，再结合当前项目目标逐步实现相同职责边界。

参考：

* [MyHermes Repository](https://github.com/djwcb-93589/my-hermes)
* [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs/)
* [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent)

---

## 开发约定

* 注释和开发文档以中文为主；
* 标识符、状态值和 `error_type` 使用英文；
* 修改任务与测试任务分阶段完成；
* 新工具必须通过 ToolRegistry；
* 程序内部不得通过拼装模型参数调用工具 handler；
* 内部可信字段不能暴露为模型可控制参数；
* 不在日志中保存凭据、完整 route key 或敏感文件内容；
* 数据库变更必须使用 migration；
* 不要求用户通过删除数据库完成升级；
* 后台任务必须有并发上限、队列上限和明确所有权；
* 副作用操作必须定义取消、重试和崩溃恢复语义；
* 与 Gateway 无关的领域能力不应继续堆入 GatewayRunner；
* README 中只描述当前代码已经实现的能力。
