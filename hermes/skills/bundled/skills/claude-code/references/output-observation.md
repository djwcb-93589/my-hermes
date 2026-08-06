# 受管输出观察

Controller 通过 Runtime 的一次性 `observe` 取得新增 PTY 输出，并把它投影为安全的 Snapshot。模型只从 `claude_code` Tool result 读取该投影；不直接读取 Process 日志、PTY 或 Runtime。

```text
ProcessManager 绝对输出窗口
→ Runtime cursor 与双视图规范化
→ Detector
→ ClaudeCodeSnapshot
→ ControllerResult
→ claude_code Tool envelope
```

## 有界结果

每次 `poll`（以及会触发一次观察的其他 Tool action）返回当前 observation 的：

- `raw_cursor`：Controller/Runtime 管理的绝对 cursor；不是 Tool 入参，也不应由 Agent 另行保存或计算；
- `events`：本次 observation 的有界事件，不从历史日志重建；
- `normalized_output`：有界、脱敏的显示快照，而非完整日志或屏幕回放；
- `state`、`process_active`、`round_terminal`、`action_required` 与限制计数。

事件类型仅使用当前生产枚举：

```text
OUTPUT
PROGRESS
QUESTION
APPROVAL_REQUEST
AUTH_REQUIRED
COMPLETION_SIGNAL
FAILURE_SIGNAL
PROCESS_EXIT
READ_ERROR
CURSOR_GAP
UNKNOWN_PROMPT
```

Tool 只公开事件的 `type`、cursor 范围、脱敏文本和明确白名单中的标量 metadata。它不公开原始 PTY、完整缓冲、输入正文、凭据、Handle 或内部对象。

## 状态证据

Snapshot 状态使用：

```text
STARTING
READY
WORKING
WAITING_INPUT
WAITING_APPROVAL
COMPLETED
FAILED
INTERRUPTED
LOST
UNKNOWN
```

`READY` 需要活跃进程、可信可输入界面证据和没有当前 ActionRequired；`WORKING` 需要任务已提交后的真实非 echo 活动。输入 echo、spinner、重复重绘、孤立 `$`、`effort` UI 或短暂无输出不能单独成为 WORKING、完成、失败或审批证据。

完成、失败和中断都依赖多个事实与当前 round 边界。`STALLED` 不是 Snapshot state；它是 Controller 的 ActionRequired/outcome，表示有限观察中缺少足够活动，不能当作进程退出、完成或新的 READY 证据。

## 规范化与滚动窗口

运行时处理 ANSI、`\r`、退格、跨 chunk、输入 echo、spinner 与常见重绘，并在各层保留固定滚动窗口。旧内容被淘汰不会阻止后续增量观察，也不会因为 `output_used` 很大而停止会话。

真正的 `CURSOR_GAP` 只表示请求 cursor 已落后于 ProcessManager 仍可提供的原始窗口，或未读取的原始区间已丢失。正常的显示窗口淘汰不构成 cursor gap。发生 gap 时，Detector 安全降级到 `UNKNOWN`，不猜测缺失 Prompt、审批、完成或失败。

## 安全投影与原生交互

普通 Event、Snapshot、`normalized_output` 和 Tool 的 `action_required` 都使用脱敏安全视图。Runtime 可在当前有效交互存活期间保留有界原生 Prompt/options，以便确定性 Conversation 协调层向用户透明展示；该视图不进入模型 Tool result、日志、历史 Snapshot、错误 details 或持久化。

用户输入的 PTY echo 会被掩码并排除为高风险语义证据。原生和安全视图无法可靠映射时，系统不冒泡交互，也不回退拼接历史 PTY。用户回复的送达未知不会自动重发。

有关 Controller 如何对 observation 进行 round 收敛，见 [workflow-controller.md](workflow-controller.md)；有关用户可见交互，见 [approvals.md](approvals.md)。
