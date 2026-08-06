# 受管失败、恢复与清理

恢复目标是保留事实、避免重复副作用，并让 Controller、Runtime 与 ProcessManager 按既有所有权和 cleanup 语义处理受管进程。Agent 不直接调用 PID、Handle、`taskkill`、系统信号、Terminal/Process 裸 CLI 或 Claude 私有 session 文件。

## Tool 错误与送达未知

Tool result 的 `error_type`、`retryable` 与 `delivery_unknown` 是唯一公开错误事实。出现 `delivery_unknown` 时：

1. 不重发 `start`、`send_instruction`、用户回复、interrupt 或 terminate；
2. 不假定已送达或未送达；
3. 仅在当前可信 Grant 允许且用户意图仍明确时，使用 `poll` 观察安全结果；
4. 无法确认时停止自动副作用并报告。

`round_in_progress`、`round_mismatch`、`round_not_found`、`previous_round_required`、`action_required`、`interaction_pending`、`session_terminal` 或 `process_not_found` 都是边界结果，不是通过重试、自动选择最近 Session 或裸 CLI 可以修复的问题。

## 状态异常

- `FAILED`：汇报最后安全事件、当前 round/process 状态、可能留下的工作树修改和未完成事项；不自动重启或重复任务。
- `LOST`：不假设进程已退出或成功；不要扫描 PID。按现有受管语义报告，并让 Session/ProcessManager cleanup 保留所有权。
- `STALLED`：不是完成或 kill 信号。不要用安全停止文本自动输入；等待用户明确查询、中断或终止决定。
- `CURSOR_GAP` 或 `UNKNOWN`：不猜测缺失输出或交互，不凭旧文本重新分类。

## 中断、终止与清理

用户明确请求中断时使用 `request_interrupt`，并接受 `interrupt_pending` 作为“已请求、尚未确认终态”的真实结果。不要自动升级为 `terminate`。用户明确选择强制终止时，使用 `terminate`；Controller 负责只清理该受管 process、确认非 active 和有限 final drain。

进程/round 终态后，Controller 和 ProcessManager 负责保存稳定结果及 session cleanup。Agent 可以在授权范围内做只读工作树核对，但不得 reset、删除、覆盖部分修改或启动另一个 Claude Code 来“修复”失败。

## Watch 注册异常

Gateway 的 Watch 注册失败、未知或 target 冲突不意味着已提交任务回滚。它不会触发自动重启、重复提交、interrupt 或 terminate；系统应提示用户在合适的可信轮次显式 `poll`。CLI 没有 Gateway Watch。
