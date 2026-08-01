# P8 Tool Contract

本 Skill 只依赖 Terminal Tool 和 Process Tool 的公开返回值，并遵守现有 P8/P7 输入送达语义。不要访问 `ProcessManager`、后台 Handle、PID 或 session registry 的内部实现。

## Terminal Tool

one-shot 使用后台 pipe：

```text
background=true
pty=false
```

只有 supervised PTY 使用：

```text
background=true
pty=true
```

Terminal Tool 成功注册后台进程后返回 `process_id` 和公开 `status`；该标识是后续 Process Tool 调用的唯一进程标识。不要从 Backend 实例、session registry、ToolRegistry 或其他私有运行时状态判断能力。

PTY 只受 `LocalBackend` 支持。可信运行时上下文已公开给出 Backend 类型时可以预判；否则直接依赖 Terminal Tool 的公开启动结果。返回 `pty_unsupported` 时不要回退 pipe 交互、切换 Backend 或探查私有状态，应报告 supervised PTY 不可用；这不代表 one-shot pipe 也不可用。Windows LocalBackend 命令遵循 Git Bash/POSIX 语义；不要生成 PowerShell、CMD 或 WSL 命令。

## Process Tool

本 Skill 可以使用以下公开 action：

- `list`：查看当前 session 拥有的已注册进程；只用于确认归属或清理结果。
- `poll`：非阻塞读取状态与一页新增日志。
- `log`：按绝对 cursor 读取追加日志。
- `wait`：等待状态变化或超时，并读取日志；超时只结束本次等待，不终止进程。
- `kill`：把终止职责交给 ProcessManager，由其尝试协作式终止并在需要时强制终止。
- `write`：把 `data` 原样写入 stdin，不附加 Enter。
- `submit`：写入文本并按当前 transport 提交 Enter；所有普通指示都使用它。
- `close`：只为普通 pipe stdin 发送真实 EOF；PTY 不支持。

## Pipe one-shot

one-shot 的 command 固定为 `claude -p`，完整任务不得出现在 command 中。输入和生命周期 action 为：

```text
write
close
log
wait
kill
```

`poll` 可用于公开状态检查。标准顺序是：

```text
terminal(command="claude -p", background=true, pty=false)
→ status=running
→ process write：data 为完整多行任务
→ write 明确成功
→ process close：发送真实 pipe EOF
→ process log：持续传回 next_cursor
→ process wait：等待自然退出
```

任务文本只放入 `write.data`，不做 shell 转义或字符串替换，不写入 command、logger 或输入历史。`close` 只结束 stdin，不终止进程，也不适用于 supervised PTY。

Terminal 返回后按公开状态处理：

- `running`：允许执行一次任务 `write`，成功后 `close`。
- `exited`：不调用 `write` 或 `close`；读取最终日志并报告任务提交前提前退出。
- `killed`、`lost`、`failed_start`：不发送任务，不声称 one-shot 已开始，进入对应恢复流程。

## PTY supervised

supervised PTY 可使用：

```text
write
submit
poll
log
wait
kill
```

行式初始任务、纠偏和 safe-stop 使用 `submit`。`write` 只保留给已有公开协议明确要求的非行式数据，不得用于自行实现进程中断。PTY 不使用 `close`；当前 PTY stdin EOF 不支持，调用会被拒绝。

## ProcessStatus

Process Tool 的生命周期状态是：

```text
starting
running
exited
killed
lost
failed_start
```

这些状态由 ProcessManager 拥有。Skill 的 `PLANNING`、`EDITING`、`STALLED` 等逻辑状态只能解释 Claude Code 的活动，不得覆盖或替换 ProcessStatus。

发送 `write` 或 `submit` 前必须根据 Terminal 返回值或最新的 `poll`/`log` 响应确认 `status=running`。不要仅凭取得 `process_id` 推断正在等待 stdin，也不要向 `starting` 或任何终态发送输入。

## Cursor

第一次读取可以使用 `cursor=0`。之后每次都执行：

```text
下一次 cursor = 本次 Process Tool 返回的 next_cursor
```

必须原样保存和传回 `next_cursor`。不得：

- 按脱敏后文本长度计算 cursor；
- 按 ANSI 或 `\r` 清理后的长度计算 cursor；
- 根据显示字符数、行数或输入 echo 推算 cursor；
- 从 `0` 反复读取全部日志；
- 在 Skill 中建立第二套分页或补偿算法。

如果响应提示输出截断或当前可用起点变化，仍以 Process Tool 返回的 `next_cursor` 和公开字段继续，不自行修复原始位置。

## PTY 输出

PTY `output` 是原始追加流，可能包含：

```text
ANSI
\r
echo
spinner
重复重绘
```

这些内容可以在语义判断时忽略，但不能用于改写 cursor。Process 日志不是当前屏幕快照，也不提供可靠的 terminal resize 或复杂全屏 TUI 重建。

## 输入与 EOF 失败语义

所有 `write`/`submit` 输入都遵守：`process_input_delivery_unknown` 后不自动重发，不假设成功或失败，并先使用 `poll`/`log` 读取新增输出。日志没有立即 echo，或只 echo 了部分文本，都不能证明完整输入是否送达。

one-shot `write` 返回 delivery unknown 时还必须：

1. 不发送第二份任务；
2. 不立即调用 `close`；
3. 不根据 echo 猜测完整 prompt；
4. 无法确认时停止自动输入并报告；
5. 为避免 Claude Code 执行不完整提示而需要终止时，调用 `process kill`。

`write` 明确失败且公开结果确认未送达时，不统一自动重试。只有返回值明确允许 `retryable`，并且一次受控处理不会重复副作用时，才可以决定是否处理一次。

pipe `close` 未确认时，不声称 EOF 已发送或 Claude Code 已开始处理。只根据公开 `retryable` 语义决定是否重试 `close`；重试 close 时绝不再次 `write` prompt。无法收敛时停止自动操作并报告。

## 终止职责

supervised PTY 在输入安全且进程仍为 `running` 时，先通过 `submit` 发送一次 safe-stop 文字指示，读取新增日志并进行一次有界观察，判断是否出现有效状态变化；自然结束时用 `wait` 收尾，仍需终止时调用 `process(action="kill", process_id=<process_id>)`。one-shot pipe 已关闭 stdin、输入送达未知或继续输入不安全时，跳过 safe-stop，直接使用 `kill`。

`process kill` 由 ProcessManager 负责：

- 尝试协作式终止；
- 等待 grace period；
- 必要时强制终止整个 Job/PGID；
- 记录终止来源和真实信号；
- 收尾 Handle 与最终输出；
- 保持 `lost`、`failed_start` 等状态历史。

Skill 不直接发送 Ctrl+C 字节或可视文本，不操作 PID、系统信号、Windows Job 或 Handle，也不在 kill 失败后自行连续重试。
