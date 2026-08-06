# 受管工作流 Controller

Controller 是受管 Claude Code 的唯一 round、READY、ActionRequired 和终态收敛事实来源。Agent 通过 `claude_code` Tool/Adapter 间接使用它，不能直接构造 Controller 调用、owner 或 SessionRef。

## 启动与 READY

`start(cwd, task)` 会启动一个新的受管 Session，建立最小 Controller task，并有界观察启动状态。只有同时满足活跃进程、稳定 `READY`、没有当前 ActionRequired 时，Controller 才提交初始任务一次。

启动阶段出现目录信任、认证、模式选择或其他 Prompt 时，Controller 返回当前 ActionRequired 并保留受管 process；`initial_instruction_submitted=false`。它不会把初始任务当作 Prompt 回复，不保存任务明文等待重放，也不自动选择任何选项、kill 或重启。

启动阶段没有可引用终态 round 时，`send_instruction` 不能绕过该边界创建新 round。用户交互只能走确定性续接；后续任务创建仍需要公开合同规定的前一终态 round。

## 一次 `poll` 的边界

`poll` 执行一次有界 observation，不读取完整历史、不 sleep 到完成、不自动输入、不自动批准，也不自动中断或终止。结果中的 `events` 只属于本次 observation，`normalized_output` 是有界脱敏快照，`raw_cursor` 是内部单调管理的绝对 cursor。

Controller 保留固定窗口和有限计数，包括 observation、连续空读、deadline、final drain 和 cleanup 尝试。`output_used` 仅是累计观察统计，不是累计输出熔断；长会话仍按单次有界读取继续工作。达到 deadline、观察次数或空读限制时，Controller 返回对应 outcome，不默认 kill 进程。

## round 语义

一个 process 可以连续承载多个独立 round。每个 round 有独立 `round_id`、边界、观察计数、终态结果和 Gateway Watch identity。进程继续 `running` 不代表前一 round 仍在工作；round 终态也不等于进程终态。

`send_instruction` 严格为 new-round-only：

1. 调用方提供最新终态 round 的 `round_id` 和新的明确任务；
2. Controller 在同一 task 锁内重新验证 owner、process 活跃性、无未消费 ActionRequired、无活动 round、前一 round 一致和 READY；
3. 通过后单次提交，并生成一个新的、不同的 `round_id`；
4. 失败时不追加输入、不排队、不重放、不自动 interrupt/kill。

活动 round 只能通过它已有的观察、用户交互、中断或终止路径继续。普通 `send_instruction` 不接受补充 stdin；常见拒绝包括 `round_in_progress`、`round_mismatch`、`round_not_found` 和 `previous_round_required`。未消费 ActionRequired 优先于新任务提交。

## ActionRequired 的确定性续接

安全 Snapshot 可报告 `clarification`、`approval`、`authentication`、`destructive_action`、`external_access`、`unknown_prompt` 或 `stalled`。其中目录信任、运行时权限和中断菜单是由前几类原生 Prompt 表现出来的实际场景。

`stalled` 是 Controller 工作流状态，不是可回复的原生 Prompt。其他当前原生 Prompt 通过内部 `current_interaction` 取得临时原生视图，并只允许内部 `reply_to_interaction` 在 `user_confirmed=true`、owner/process/round/action id 仍一致时原样提交用户回复。该接口不在模型 Tool schema 中。

成功回复、输入送达未知、interrupt、新 Prompt 或终态都会使旧交互身份失效；Controller 不自动重发，不解释 `y/n`、编号、密码、Token 或选项含义。详细边界见 [approvals.md](approvals.md)。

## 中断、终止与终态

`request_interrupt` 对当前 round 最多发送一次 Ctrl+C，然后仅在有限次数内观察真实进程与 Detector 证据。未收敛时返回 `interrupt_pending`，不会伪造 `INTERRUPTED`、再次 Ctrl+C 或自动升级为 kill。

`terminate` 是用户明确选择的强制收敛路径。Controller 只作用于当前受管 process，确认非 active 后执行有限 final drain；终态 Snapshot 的当前 ActionRequired 必须为空。重复 poll/terminate 使用已保存的稳定结果，不重新通知或输入。

`COMPLETED`、`FAILED`、`INTERRUPTED` 和 `LOST` 都必须由 Controller/Detector/ProcessStatus 的组合事实收敛。静默、spinner、退出码、单条总结或某个子命令结束都不是成功任务的单独证据。

## Completion Watch

Gateway 只会在 `start` 或 `send_instruction` 已成功确认提交一个 round 后尝试注册 Watch。Watcher 通过 Controller 观察真实终态和既有 final drain，不直接访问 Runtime、PTY 或 ProcessManager，不发送输入，也不自动处理 ActionRequired。

每个 Watch 对同一终态最多构造一个通知；只通知 `COMPLETED`、`FAILED`、`INTERRUPTED` 或 `LOST`。`READY`、`WORKING`、ActionRequired、STALLED、deadline、取消和普通输出不会产生完成通知。Watch 注册或 Outbox accepted 不等于平台已送达；CLI 没有 Gateway Watch。
