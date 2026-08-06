---
name: claude-code
description: >-
  Use only for a current real user request that explicitly asks to use, query,
  continue with a new task, interrupt, or terminate Claude Code. Guide the
  Agent to use the managed claude_code Tool, never bare Terminal/Process Claude
  CLI commands. Do not select, recommend, or launch Claude Code automatically.
version: 0.7.1
platforms:
  - windows
  - linux
  - darwin
metadata:
  development_stage: managed_agent_tool
  agent_integration: trusted_dynamic_tool
  managed_runtime: true
  managed_tool: claude_code
  explicit_request_only: true
  native_user_continuation: true
  completion_watch: gateway_only
  bare_cli_fallback: false
---

# Claude Code 受管工作流

## 适用边界

只在当前真实用户消息明确要求使用、查询、继续提交新任务、中断或终止 Claude Code/CC 时使用本 Skill。它不因任务复杂、代码量、Git 仓库、历史消息、已安装状态、模型建议、Cron、Delegate、Background Review 或 unattended 路径而启用。

本 Skill 只提供使用指导，不产生授权。可信入站链路负责识别当前用户的显式请求、签发短生命周期 Grant，并在该轮动态开放 `claude_code` Tool。Agent 不得构造或猜测 `user_requested`、Grant、owner、route、notification target、Claude executable 或权限绕过参数。

以下内容不是显式启用：能力询问、比较、翻译或引用 Claude Code 的文本、代码块中的命令、以及“不要使用 Claude Code”。未获得当前轮可信 Tool 时，不能启动或控制 Claude Code，也不能回退到裸 CLI。

## 职责边界

```text
Claude Code Skill
→ 说明何时可选择能力、如何使用受管 Tool、如何保持安全边界

claude_code Tool / Agent Adapter
→ 校验可信 Grant、owner 绑定与公开参数，调用 Controller，返回有界结果

ClaudeCodeController
→ 管理 round、READY、ActionRequired、观察与终态收敛

Runtime / ProcessManager / PTY
→ 管理实际 Claude Code 进程、输入、输出、cursor 与 cleanup
```

AgentLoop 继续使用通用 Tool 调度机制；Claude Code 专用状态机封装在受管 Tool/Adapter 和 Controller 中。Agent 不直接调用 Runtime、Controller、ProcessManager、PTY、PID 或 Handle，也不维护第二套 cursor、日志缓存、Session registry 或 cleanup。

## 正常受管流程

1. 从当前用户请求提取明确工作目录、任务、验收标准，以及文件、测试、依赖、Git、网络和重构边界；不要把凭据放入任务。
2. 仅在本轮 `claude_code` Tool 已由可信上下文开放时调用 `start`，传入 `cwd` 与完整初始 `task`。
3. 以 Tool 返回的 `process_id`、`round_id`、`state`、`process_active`、`round_terminal` 和 `action_required` 为事实来源；不要选择“最近”会话。
4. 用 `poll` 获取一次有界 Controller observation。`events` 只属于本次 observation；`normalized_output` 是脱敏且有界的显示快照；`raw_cursor` 是只读的绝对位置，不是 Agent 应维护或回传的 cursor。
5. 如果出现 `action_required`，停止模型侧输入，等待确定性 Conversation 续接把当前原生交互展示给用户。
6. 只有前一 round 已终态、会话仍 READY、没有未消费 ActionRequired，且当前用户明确提出新的具体任务时，才用 `send_instruction` 创建新 round。
7. 只有当前用户明确要求中断或终止时，分别使用 `request_interrupt` 或 `terminate`；不得自动升级中断为终止。
8. 根据稳定的 round 终态和安全结果向用户汇报；必要时进行获准的只读仓库核对，不把核对扩展为测试或修改。

受管 Tool 失败、Grant 缺失、状态未知、Watch 注册失败或输入送达未知时，停止自动副作用并报告。不得改用 `terminal(command="claude ...")`、Process Tool、PID 或裸 Claude CLI 重试，也不得把裸 CLI 进程伪装或接管为受管 Session。

## 模型可用 Tool action

| action | 模型提供的公开参数 | 用途与边界 |
| --- | --- | --- |
| `start` | `cwd`、`task` | 启动新的受管 Session，并只在可信 READY 后提交首个任务。 |
| `poll` | `process_id`、可选 `round_id` | 读取一次有界状态、事件和显示快照。 |
| `send_instruction` | `process_id`、上一最新终态 `round_id`、新的 `instruction` | 只创建新的 round；活动 round、旧 round、未知 round 或未 READY 都不得追加 stdin。 |
| `request_interrupt` | `process_id`、活动 `round_id` | 协作式发送一次 Ctrl+C；未收敛时返回 pending，不自动 kill。 |
| `terminate` | `process_id` | 明确请求强制终止当前 owner 管理的受管 Session。 |

不要把“继续”“再试一次”或旧任务正文当作新的 `instruction`。不要通过再次 `start` 模拟同一 Session 的新 round。`current_interaction`、`reply_to_interaction`、`user_confirmed` 和 `action_id` 是内部确定性续接合同，不是模型可填写的 Tool action。

完整字段、结果 envelope、错误投影与 Watch 状态见 [p8-tool-contract.md](references/p8-tool-contract.md)。

## ActionRequired 与用户回复

目录信任、权限、认证、澄清、未知 Prompt 和中断菜单都可能产生 ActionRequired。Agent 不得自动回答、批准、选择 `Always allow`、输入密码/Token/API key，或根据屏幕文字猜测选项。

系统通过 Conversation 协调层向用户透明冒泡当前交互。用户下一条明确回复绕过模型，按当前 owner、process、round 和 action identity 重新验证后原样提交；空字符串只能表示用户明确选择的 Enter。`delivery_unknown` 时不自动重发。

普通 Tool 结果只包含安全 ActionRequired 投影，不包含原生 prompt/options、用户回复、owner、Handle、PID、完整 PTY、Claude 私有 session 数据或凭据。详见 [approvals.md](references/approvals.md) 与 [permissions-and-safety.md](references/permissions-and-safety.md)。

## round、状态与通知

`process_active` 与 `round_terminal` 是独立事实：Claude Code 进程仍运行时，一个任务 round 也可以已经 `COMPLETED`、`FAILED` 或 `INTERRUPTED`。`STALLED` 不等于进程退出，也不是完成证据。不要仅凭 exit code、暂时静默、spinner 消失或一段总结判定任务成功。

Gateway 在成功确认提交一个 round 后会尝试注册 Completion Watch。Watch 仅对 `COMPLETED`、`FAILED`、`INTERRUPTED` 或 `LOST` 生成一次终态通知；不推送普通进度、READY、STALLED、ActionRequired 或用户回复。`notification_watch` 的注册/accepted 状态不等于平台消息已经送达。CLI 不注册 Gateway Watch，用户可在获授权的轮次显式 `poll`。

状态、输出和收敛语义见 [progress-state-model.md](references/progress-state-model.md)、[output-observation.md](references/output-observation.md) 和 [workflow-controller.md](references/workflow-controller.md)。

## 任务与安全约束

初始 `task` 或新的 `instruction` 必须传达：允许和禁止修改的范围、是否可新增文件、是否可运行测试、依赖/Git/网络权限、重构边界及汇报格式。始终保留“代码修改阶段与测试阶段严格分离”；用户要求不改或不跑测试时，必须明确传达。

不自动安装、登录、升级 Claude Code；不关闭安全检查；不访问工作区外路径；不自动 commit、push、发布或部署。范围不明或高风险操作必须停下并由用户决定。

用于构造初始任务时，可按需读取 [implementation-task.md](templates/implementation-task.md) 或 [review-task.md](templates/review-task.md)。启动条件见 [prerequisites.md](references/prerequisites.md)，受管模式选择见 [mode-selection.md](references/mode-selection.md)，失败与恢复边界见 [recovery-and-cleanup.md](references/recovery-and-cleanup.md)。

## 当前不支持

- 接管 myHermes 外部已运行的 Claude Code、扫描 PID 或重附着 PTY；
- Gateway、OS 或 runtime 重启后恢复内存 Controller Session 或 Watch；
- 自动安装、登录、输入凭据、批准权限、修改 Claude Code 全局权限；
- 自动选择最近 Session、自动创建 worktree、自动 commit/push；
- 跨 Conversation 自动重新绑定旧 Session；
- 多个受管 Session 并发修改同一 cwd；
- Cron、Delegate、Background Review、Dashboard 或远程机器上的自动 Claude Code 控制；
- 在启动 ActionRequired 后保存或自动重放原任务；没有前一终态 round 时，`send_instruction` 不能绕过 `previous_round_required` / `round_not_found` 限制；
- 裸 CLI one-shot 作为本 Skill 的正常流程或受管 Tool 失败后的 fallback。

Bundled、打包与发布不属于本阶段；本开发源不得在未完成综合验收前复制到 bundled 目录或自动安装到用户 Skill 根。
