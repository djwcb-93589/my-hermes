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

PTY 仅由 `LocalBackend` 支持。Terminal Tool 成功启动后返回 `process_id`；该值是后续 Process Tool 调用的唯一进程标识。Windows LocalBackend 命令遵循 Git Bash/POSIX 语义；不要生成 PowerShell、CMD 或 WSL 命令。

## Process Tool

本 Skill 可以使用以下公开 action：

- `list`：查看当前 session 拥有的已注册进程；只用于确认归属或清理结果。
- `poll`：非阻塞读取状态与一页新增日志。
- `log`：按绝对 cursor 读取追加日志。
- `wait`：等待状态变化或超时，并读取日志；超时只结束本次等待，不终止进程。
- `kill`：请求协作终止，必要时由既有实现强制终止。
- `write`：原样写入输入；仅在确实需要非行式控制输入时使用。
- `submit`：写入文本并按当前 transport 提交 Enter；所有普通指示都使用它。

PTY 不使用 `close`。当前 PTY stdin EOF 不支持，调用会被拒绝；不要把 `close` 当作退出或取消机制。

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

发送 `write` 或 `submit` 前必须通过最新的 `poll`/`log` 响应确认 `status=running`。不要向 `starting` 或任何终态发送输入。

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

## 输入送达

`submit` 成功只表示 Process Tool 确认了本次 transport 写入。若返回 `process_input_delivery_unknown`：

1. 不自动重发；
2. 不假设失败；
3. 不假设成功；
4. 使用当前 `next_cursor` 读取新增日志；
5. 只有输出明确证明未送达且获得新的人工决定时，才考虑新输入；
6. 无法确认时停止自动输入并报告。

日志没有立即 echo 不能作为重发依据。
