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
    command="claude -p \"<task>\"",
    background=true,
    pty=false
)
```

保存返回的 `process_id`，从 cursor `0` 开始读取首个日志页，此后始终使用返回的 `next_cursor`。使用 `process log` 或 `process wait` 观察自然完成。不要仅为获取更多过程文字改用 PTY。

如果 one-shot 意外要求交互，不要假设输入格式，不要自动重启为 PTY，也不要重复执行同一任务。读取最终新增日志并把阻塞报告给用户。

## Supervised PTY 模式

存在以下任一具体需要时使用：

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

## 选择原则

- 用完成任务所需的最小交互能力；可 one-shot 时不启用 PTY。
- “任务耗时长”本身不等于需要 PTY；“必须持续监督或可能交互”才是 PTY 理由。
- 模式属于当前 `process_id` 的启动属性，运行中不能原地切换。
- 模式选择错误造成失败时，先报告退出状态和已产生的工作树影响；未经用户或恢复策略授权，不自动重跑。
- supervised PTY 不使用 `process close`。需要结束时先安全停止，最后才 `kill`。
