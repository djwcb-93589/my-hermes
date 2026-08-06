# 用户控制与非干预策略

默认行为是观察和如实汇报。受管 Claude Code 的普通任务输入不再通过 Agent 直接 `submit` 到活动 PTY。

## 允许的受管控制

只有当前真实用户消息被可信入站链路分类为相应显式请求时，当前 Grant 才会允许对应 Tool action：

- 新任务：在最新终态 round 后使用 `send_instruction` 创建新 round；
- 状态查询：使用 `poll`；
- 协作式中断：使用 `request_interrupt`；
- 强制终止：使用 `terminate`。

Agent 不得因“进度如何”“继续”“完成了吗”、任务偏离、短暂无输出、spinner、未知状态、历史消息或自身建议而自行发送普通指令、中断或终止。用户在活动 round 中补充范围、纠偏或风险信息时，不向当前 round 追加 stdin；应说明当前输入边界，并等待用户明确选择继续等待、中断、终止，或在 round 终态后提交新的具体任务。

## ActionRequired 优先

当前 ActionRequired 高于新的普通任务输入。Agent 不得替用户回答澄清、批准目录信任/运行时权限、认证、破坏性操作、外部访问、未知 Prompt 或中断菜单；也不得发送 `progress-request.md`、`corrective-instruction.md` 或 `safe-stop.md` 作为自动 PTY 输入。

确定性 Conversation 续接会将用户明确回复原样提交。用户未回复、交互已过期或送达未知时，停止自动输入；不要以相同或改写的消息反复尝试。

## 中断与终止

`request_interrupt` 只请求一次 Ctrl+C，未收敛时返回 `interrupt_pending`，不自动第二次中断或 kill。只有用户明确要求强制终止时才调用 `terminate`。两种操作都不得通过 Terminal、Process、PID、控制字符或裸 CLI 替代。

## 去重与节奏

不要高频 `poll`、重复发送相同 Tool action、重复注册 Watch 或把 `raw_cursor` 当作自行管理的进度游标。以最新 Tool result 的 process、round、action identity、`delivery_unknown` 和限制字段为准；失败或未知时报告，而非自动重试副作用。
