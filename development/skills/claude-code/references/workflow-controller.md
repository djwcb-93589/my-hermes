# P6 工作流 Controller

P6 在 `ClaudeCodeRuntime` 的公开接口上为 supervised PTY 提供同步、有界的工作流编排；one-shot 仍使用既有 pipe 流程。生产调用方必须复用惰性单例 `get_claude_code_controller()`；`create_claude_code_controller()` 只用于显式隔离或依赖注入。Controller 不直接访问 Backend、PTY、ProcessManager registry 或 OS PID，也不注册新 Tool、不接入 AgentLoop。

## 启动与身份

`start_task` 必须同时收到 `user_requested=True`、明确 `cwd`、`session_owner` 和非空任务。缺少当前用户对 Claude Code 的明确要求时返回 `explicit_user_request_required`，不得根据任务复杂度、安装状态或历史使用情况自动启动。

启动顺序固定为：

```text
Runtime.start
→ 以 process_id 登记最小 Controller 状态
→ Runtime.submit 提交初始任务
→ 一次 Runtime.observe
→ 返回 ClaudeCodeControllerResult
```

Controller 不保存初始任务或后续输入的明文副本。首次提交失败时只对本次 `process_id` 有界执行 `kill` 和非 active 复核；cleanup 失败返回 `cleanup_failed`。正常会话资源仍由 SessionResource 和 ProcessManager 的现有 cleanup 语义回收。

## 有界观察

`poll` 每次只执行一个观察轮次，不 sleep、不循环到任务结束。它复用 Runtime 的绝对 cursor，并只按相邻 Snapshot 的 cursor 增量累计 `output_used`；该字段仅用于当前 Controller task 的历史统计和诊断，不是输出配额，不得修改 Runtime cursor 或建立第二套日志分页。

策略由不可变的 `ClaudeCodeControllerPolicy` 集中验证：

- `poll_interval`、`total_deadline` 和 `single_wait_limit` 限制等待时间；
- `max_consecutive_empty_reads` 和 `max_observation_count` 限制观察次数；
- `interrupt_observation_attempts` 限制协作式中断后的复核；
- `final_drain_attempts` 限制终态尾部读取；
- `cleanup_attempts` 与 `cleanup_retry_interval` 限制单进程 cleanup。

空读必须同时满足没有 cursor 推进、状态变化、ProcessStatus 变化、新 ActionRequired、活动时间推进或新事件。连续空读达到上限时返回 `state=UNKNOWN` 和 `action_required.kind=stalled`；stalled 不是完成，也不会自动 kill。达到 deadline 或观察次数时返回对应结构化 outcome，保留最后可得 Snapshot，不默认终止进程。累计输出再多也继续按单次读取上限增量观察；各层只保留各自的固定滚动窗口，淘汰最早内容不会停止会话。

`wait_for_action` 与 `wait_for_terminal_state` 只组合有界 `wait` 和单轮 `poll`。两者都受固定任务 deadline 和观察次数限制；后者遇到任何 ActionRequired 也必须提前返回。累计输出统计不构成停止条件。

## ActionRequired 与输入

以下 ActionRequired 一出现就暂停并返回上层：

```text
clarification
approval
authentication
destructive_action
external_access
unknown_prompt
stalled
```

`send_instruction` 只通过 Runtime `submit` 发送用户明确给出的普通指令。它可以清除已由该指令处理的 stalled，但不得覆盖其他未解决 ActionRequired。

`answer_question` 只接受当前 `kind=clarification` 的回答，并只作用于对应 `process_id` 和 owner。approval、authentication、destructive_action、external_access 与 unknown_prompt 均返回 `invalid_action_response`；P6 不提供 approve、deny、authenticate 或 always-allow，不自动输入选项、`y` 或凭据。

## 中断、终止与终态

`request_interrupt` 只调用一次当前 Runtime 的协作式 interrupt，再进行有限次数观察。状态必须来自 Runtime/Detector 的真实证据；未收敛时返回 `interrupt_pending`，不得自动升级为 kill。

`terminate` 是调用方明确选择的强制收敛路径：只对当前 `process_id` 有界调用 Runtime `kill`，确认 ProcessStatus 已非 active，再执行有限 final drain。final drain 在 cursor 不再推进、达到 `final_drain_attempts` 或观察次数上限时停止，保留有界尾部事件和退出信息；终态 Snapshot 的当前 ActionRequired 必须为空。重复 `terminate` 或终态后 `poll` 返回已保存的稳定终态结果，终态后输入返回 `terminal_session`。

deadline 和取消只停止 Controller 继续等待，默认不发送 interrupt 或 kill。调用方必须另行明确选择 `request_interrupt` 或 `terminate`。

## 状态与并发边界

Controller 只保存 `process_id`、owner、cwd、固定 deadline、观察/空读/输出计数、最新 Snapshot 和有界终态结果。它不复制 Runtime `_sessions`、ProcessManager registry、Handle 或完整输出历史。每个任务使用独立轻量锁，允许不同 cwd 的工作流并发；同 cwd 互斥继续由默认 Runtime 负责。

P6 Controller 尚未接入 AgentLoop，也没有后台 polling worker。Agent 工具侧继续遵守本 Skill 的 Terminal/Process 协议；自动审批、认证、未知 Prompt 回答和更细粒度权限决策留到 P7。
