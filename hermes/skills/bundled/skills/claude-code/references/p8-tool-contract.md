# 受管 `claude_code` Tool 合同

本文件描述 Agent 面向 Claude Code 的唯一正式受管接口。它不替代可信入站授权，也不公开 Runtime、Controller、ProcessManager、PTY、PID、Handle、owner、Grant 或 Claude CLI 参数。

## 动态开放与授权

`claude_code` 默认关闭。只有可信 CLI 或 Gateway 入站链路从当前真实用户消息识别到明确 Claude Code 请求后，才会在当前 Agent 轮次同时注入短生命周期 Grant、可信上下文和 `claude_code` toolset。

模型不能通过 Tool JSON 伪造 Grant、`user_requested`、session owner、route、notification target 或权限绕过参数。缺少可信 Grant 时，Tool 返回 `claude_code_tool_disabled`；Grant 不允许该 action 时返回 `grant_operation_not_authorized`。这些错误不应触发裸 CLI fallback。

## action 与参数

每次调用只接受下表对应字段；未列字段会被拒绝。

| action | 必填字段 | 可选字段 | 语义 |
| --- | --- | --- | --- |
| `start` | `cwd`、`task` | 无 | 启动新的受管 Claude Code Session。 |
| `poll` | `process_id` | `round_id` | 执行一次有界 Controller observation。 |
| `send_instruction` | `process_id`、`round_id`、`instruction` | 无 | 以最新终态 round 为前置条件创建新 round。 |
| `request_interrupt` | `process_id`、`round_id` | 无 | 对活动 round 请求一次协作式 Ctrl+C。 |
| `terminate` | `process_id` | 无 | 终止当前 owner 管理的受管 Session。 |

`cwd`、`task`、`process_id`、`round_id` 和 `instruction` 都是字符串；Tool schema 的最大长度分别为 4096、65535、512、512 和 65535 个字符。Tool 不公开 `wait`、`write`、`submit`、`close`、`log`、`list`、`current_interaction` 或 `reply_to_interaction` action。

## `start`

调用方只传明确 `cwd` 与任务正文。Adapter/Controller 在可信上下文中处理 owner、受管启动、READY 门禁和初始任务提交；模型不提供 `user_requested` 或 CLI command。

如果启动阶段先出现 ActionRequired，结果保留 process 身份并报告 `initial_instruction_submitted=false`，不会把任务文本当作 Prompt 回复，也不会自动重放。原生交互只能由确定性 Conversation 续接处理。

## `poll` 与结果 envelope

所有 action 返回同构的安全 envelope，至少包含：

```text
ok
operation
outcome
state
process_id
cwd
round_id
initial_instruction_submitted
process_active
round_terminal
raw_cursor
events
normalized_output
observation_count
consecutive_empty_reads
output_used
deadline_remaining
action_required
limits_hit
error_type
retryable
delivery_unknown
notification_watch
```

- `raw_cursor` 直接来自 Controller/Runtime 的绝对 cursor；Tool Handler 不维护、消费或回传 cursor。
- `events` 只来自本次 Controller observation，每项只包含 `type`、`cursor_start`、`cursor_end`、脱敏有界 `text` 与允许公开的标量 `metadata`。
- `normalized_output` 是当前有界、脱敏的显示快照，不是完整历史、原始 PTY 或新的日志分页接口。
- `action_required` 只包含安全投影：`action_id`、`kind`、`summary`、`prompt_text`、`options`、`risk`、cursor 范围和 `requires_user_input`。

后续调用方必须以返回的 process、round、cursor 和 action identity 为事实来源；不得通过读取 ProcessManager、PTY log 或历史文本建立第二套增量观察。

## `send_instruction`：严格 new-round-only

`round_id` 必须是调用方已知的、紧邻的最新终态 round。Controller 在 task 锁内重新验证 owner、process 活跃性、无未消费 ActionRequired、无活动 round、最新终态 round 一致性和 READY 门禁；只有全部通过才单次提交并创建不同的新 round id。

活动 round 会拒绝提交，通常返回 `round_in_progress`；若当前还有未消费的 `ActionRequired`，相关交互错误会优先。不会追加 stdin、重置活动状态、排队、自动 interrupt 或注册 Watch。已过期的终态 round 返回 `round_mismatch`，未知 round 返回 `round_not_found`；没有可引用前一终态 round 时，Controller 使用 `previous_round_required`，而 Tool 的前置引用校验也可能提前返回等价的 `round_not_found`。`instruction_delivery_unknown` 时不自动重发。

`instruction` 必须是用户当前明确给出的新任务，而非“继续”“完成了吗”或旧任务重放。不得重新 `start` 来模拟同一 Session 的新 round。

## 中断、终止与通知

`request_interrupt` 只发送一次协作式 Ctrl+C；若未确认终止，返回 `interrupt_pending`，不自动发送第二次 Ctrl+C 或 kill。`terminate` 只在用户明确选择时调用受管终止路径；两者都不扫描或操作外部 PID。

Gateway 仅在 `start` 或 `send_instruction` 已成功确认提交新 round 后尝试 Watch 注册。`notification_watch.status=registered` 或 Outbox `accepted` 都不表示平台通知已经送达；注册失败、未知或 target 冲突不改变已提交任务，也不触发重启、终止或重复提交。CLI 的 `notification_watch` 不适用。

## 内部用户续接

`current_interaction`、`reply_to_interaction`、`user_confirmed` 和原生 prompt/options 不在模型 Tool schema 中。确定性 Conversation 协调层以当前 owner、process、round 和 `action_id` 复核用户明确回复后，才调用 Controller 的内部续接路径。用户回复、密码、Token 和认证内容不进入普通 Tool result；送达未知时不自动重发。
