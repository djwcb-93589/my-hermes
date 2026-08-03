# P6/P7 工作流 Controller

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

P7.1 的 `current_interaction` 返回当前有效的真实 Claude Code Prompt、最多 64 项可见选项、`action_id`、`process_id`、owner、状态和 cursor 范围。Prompt/options 来自 Runtime 保留的有界临时原生视图：它已按终端语义规范化，但不替换密钥值；公开 Snapshot、Event、日志和归档结果仍只保留脱敏视图。安全/原生视图无法可靠映射或原生视图缺失时，它返回无交互，不从历史 PTY 拼接或回退。它不返回历史 Event、已回复 Prompt、interrupt 前提示、终态提示或 Controller 自身的 `stalled`。

`reply_to_interaction` 是所有真实 Prompt 的唯一回复入口。调用方必须传入 `user_confirmed=True`、当前 `action_id`、匹配的 `process_id`/owner 和用户明确给出的确切字符串；空字符串 `response=""` 表示一次明确的 Enter，并由 `Runtime.submit(data="")` 原样提交。`response` 缺失、为 `None` 或不是字符串仍是无效请求，不等同于空字符串 Enter。Controller 在同一任务锁内先做一次有界 observe，再校验进程仍 active、Prompt 仍当前且未消费，随后将回复原样 `Runtime.submit`。它不解释或映射 `y/n`、编号、选项、密码或 Token，也不按 `clarification`、`approval`、`authentication`、`destructive_action`、`external_access` 或 `unknown_prompt` 采取不同发送策略。

成功提交、任何新的 write 输入或送达未知后，旧 action id 与临时原生视图失效并观察真实输出；新 Prompt 替换为新视图和新身份。送达未知时不自动重发，旧动作不恢复为可自动消费状态。用户回复正文不写入 Event、Snapshot、Controller 长期状态、日志、错误 details 或持久文件。详细透明冒泡边界见 [approvals.md](approvals.md)。

## 中断、终止与终态

`request_interrupt` 只调用一次当前 Runtime 的协作式 interrupt，再进行有限次数观察。状态必须来自 Runtime/Detector 的真实证据；未收敛时返回 `interrupt_pending`，不得自动升级为 kill。

`terminate` 是调用方明确选择的强制收敛路径：只对当前 `process_id` 有界调用 Runtime `kill`，确认 ProcessStatus 已非 active，再执行有限 final drain。final drain 在 cursor 不再推进、达到 `final_drain_attempts` 或观察次数上限时停止，保留有界尾部事件和退出信息；终态 Snapshot 的当前 ActionRequired 必须为空。重复 `terminate` 或终态后 `poll` 返回已保存的稳定终态结果，终态后输入返回 `terminal_session`。

deadline 和取消只停止 Controller 继续等待，默认不发送 interrupt 或 kill。调用方必须另行明确选择 `request_interrupt` 或 `terminate`。

## 状态与并发边界

Controller 只保存 `process_id`、owner、cwd、固定 deadline、观察/空读/输出计数、最新 Snapshot、有界终态结果以及有界的 opaque action id 消费状态；它不保存用户回复正文。它不复制 Runtime `_sessions`、ProcessManager registry、Handle 或完整输出历史。每个任务使用独立轻量锁，允许不同 cwd 的工作流并发；同 cwd 互斥继续由默认 Runtime 负责。

Controller 尚未接入 AgentLoop，也没有自己的后台 polling worker。P7 只提供原生交互透明冒泡，不提供自动审批、自动认证、自动回答或任何 myHermes 自有权限决策。

## P7.5 完成通知 Watcher

P7.5 只在同一 myHermes runtime 内观察已经由当前 Runtime 启动、且已经登记给 Controller 的 Claude Code task：

```text
register_watch(process_id, session_owner, notification_target)
→ 内存 Watcher 定期调用一次 Controller.poll(terminal_observation=True)
→ Controller 已完成 final drain 并返回真实终态
→ 构造安全、限长通知
→ NotificationPort 接收
→ Gateway 持久 Outbox 投递
→ 注销 Watch
```

Watcher 只保存 process/owner/target 的最小绑定、有限状态和有限重试信息；Target 还带相同的 `session_owner` 绑定，Watcher 只比较该绑定而不解释平台、聊天、thread 或 reply 字段，因此不能把 Session A 的终态投给 Session B 的 target。同一 process id 在本 runtime 内只允许一个 Watch；可选显示名必须很短，不能传入完整任务 Prompt。Watcher 不读取原始 PTY、不重新实现 Detector/final drain、不拥有 ProcessManager，不会 interrupt、kill、cleanup、扫描或接管外部进程。

终态只以 Controller `result.terminal` 为事实，并使用其中已经 final drain 的公开安全 Snapshot。Watcher 的受限 `terminal_observation=True` 仅让 Controller 先复核真实进程状态：进程仍 active 时，它原样保留旧 ActionRequired 或 stalled；确认非 active 后才进入既有 observe/final drain。它不发送输入、不自动处理动作，普通 `poll` 的暂停语义保持不变。只有 `COMPLETED`、`FAILED`、`INTERRUPTED` 和 `LOST` 会通知；`READY`、`WORKING`、等待输入/审批、stalled、deadline、取消、观察次数限制和普通输出变化绝不生成“已完成”通知。缺少 Detector 完成证据的 `exited` 即使 exit code 为零也保守映射为 failed，不得声称 completed。

通知由确定性代码生成，不调用 LLM。它只包含稳定 notification id、watch/process/owner、cwd、终态、Controller outcome、ProcessStatus、exit code、完成时刻、limits 和再次脱敏、明确限长的公开 `safe_output_tail`；不使用 P7 临时原生 Prompt、ActionRequired 原文、用户回复、Token、密码、认证码、完整 PTY 输出或未被 Snapshot 支持的测试/commit/push 结论。无安全输出尾部时仍可通知状态。

同一 Watch 的同一终态用稳定 `watch_id + terminal_state → notification_id` 去重。通知入队重试始终复用该 ID，`delivery_unknown` 也不得立即生成新 ID；Outbox 已存在或可靠入库即视为 accepted 并注销 Watch。平台发送失败由既有 Outbox retry、分片和恢复处理，Watcher 不创建第二个消息队列。

默认 `get_claude_code_completion_watcher()` 是惰性、进程级单例，首次由 Gateway 组合根注入 NotificationPort，后续同一 runtime 复用。它使用一个受控 asyncio Task 扫描多个 Watch，并以有限并发通过线程桥接调用同步 Controller；策略限制 polling interval、并发 poll、输出尾部、入队尝试、重试间隔和 shutdown 等待，不能零间隔忙轮询。Gateway Adapter 只把平台无关 target 转为独立系统 source identity、确定性平台 payload 和现有持久 Outbox，复用 runtime fencing 与 Outbox worker，不调用平台 API，也不改变原入站消息或 reply 状态；`accepted=True` 仅表示入 Outbox，不表示平台已同步送达。

Watch 仅在内存中存在：runtime 在检测终态前重启时不恢复 Watch、不重新发现旧 CC；已经入 Outbox 的通知仍由 Gateway 的既有 Outbox 恢复。Gateway shutdown 先关闭 Watcher，再进行全局 Session/进程清理；Watcher shutdown 只停止接受和观察，不终止或等待 Claude Code，也不伪造终态通知。P8 将来只需在 Agent 明确启动受管 CC 后，从当前 Gateway 会话构造绑定 target 并调用 `register_watch(process_id, owner, target)`；P7.5 本身不做 Agent 选择、自然语言映射或自动交互/审批。
