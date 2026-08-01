# 模式选择

为每个任务选择一个模式。不要同时启动 one-shot 与 supervised PTY，也不要为了“更保险”并行启动重复进程。

## One-shot 模式

同时满足以下条件时优先使用：

- 目标、范围和验收标准清晰；
- 启动后不需要补充要求或中途纠偏；
- 不预期权限确认、选择题或追问；
- 只需要最终输出与自然退出状态；
- Claude Code 的 print 模式能够完成工作。

启动形态：

```text
terminal(
    command="claude -p",
    background=true,
    pty=false
)
```

Terminal 公开返回 `status=running` 时，标准输入协议是：

```text
process(
    action="write",
    process_id=<process_id>,
    data=<完整任务文本>
)
→ write 明确成功
→ process(action="close", process_id=<process_id>)
→ next_cursor=0
→ process(action="log", process_id=<process_id>, cursor=<next_cursor>)
→ process(action="wait", process_id=<process_id>, cursor=<next_cursor>)
```

不要把任务文本放入 `command`，也不要手工构造 Bash 转义、heredoc、shell 变量或其他 shell 拼接。保存返回的 `process_id` 后按 Terminal 的公开状态分支：

- `running`：通过 `process write` 的 `data` 原样发送完整多行任务；只有 write 明确成功，才调用 `process close` 为 pipe stdin 发送真实 EOF。然后从 cursor `0` 开始使用 `log`/`wait`，后续始终传回 `next_cursor`。
- `exited`：Claude Code 在任务提交前已经退出；不要调用 `write` 或 `close`，读取最终日志并报告提前退出。
- `killed`、`lost` 或 `failed_start`：不要发送任务，也不要声称 one-shot 已开始；按 Process Tool 的公开状态和恢复语义处理。

任务内容只存在于本次 Process Tool `data` 传输中，不写入 command、logger 或输入历史。`write` 返回 delivery unknown 时不要重发，也不要调用 `close`；close 未确认时只能按公开 `retryable` 语义处理 close，绝不重新发送任务。

如果 one-shot 意外要求交互，不要假设输入格式，不要自动重启为 PTY，也不要重复执行同一任务。读取最终新增日志并把阻塞报告给用户。

## Supervised PTY 模式

当前 CLI 的公开帮助确认支持 `--ax-screen-reader`，并且存在以下任一具体需要时使用：

- 用户明确要求监控进度；
- 需要在运行中纠偏或传递用户新增要求；
- 可能出现权限、确认或问题提示；
- 任务要求与 Claude Code 多轮交互；
- 需要长时间持续监督并区分停滞与正常耗时。

启动形态：

```text
terminal(
    command="claude --ax-screen-reader",
    background=true,
    pty=true
)
```

保存 `process_id`。先用 `poll` 确认 ProcessStatus 为 `running`，并从新增日志判断 Claude Code 已可接收输入，再执行：

```text
process(
    action="submit",
    process_id="<process_id>",
    data="<initial task>"
)
```

`--ax-screen-reader` 只减少装饰边框和动画，不会把 PTY 输出变成结构化事件。输出仍可能包含 ANSI、`\r`、输入 echo、spinner 和重复重绘，且只是追加流，不是屏幕快照。不要承诺完整支持 `vim`、`top` 或其他复杂全屏 TUI。

如果公开启动调用返回 `pty_unsupported`，不要删除 `--ax-screen-reader` 后静默启动复杂 TUI，不要回退到 pipe 交互、切换 Backend 或访问私有运行时状态；报告 supervised PTY 当前不可用。one-shot pipe 能力必须单独判断，不能因 PTY 不支持而一并判定不可用。

## 选择原则

- 用完成任务所需的最小交互能力；可 one-shot 时不启用 PTY。
- 不要为了规避 pipe stdin 协议而默认把所有任务改成 PTY；简单任务仍优先 one-shot。
- “任务耗时长”本身不等于需要 PTY；“必须持续监督或可能交互”才是 PTY 理由。
- 模式属于当前 `process_id` 的启动属性，运行中不能原地切换。
- 模式选择错误造成失败时，先报告退出状态和已产生的工作树影响；未经用户或恢复策略授权，不自动重跑。
- supervised PTY 不使用 `process close`。需要结束时先提交一次 safe-stop 文字指示并有限观察，仍需终止时调用 `process kill`。
