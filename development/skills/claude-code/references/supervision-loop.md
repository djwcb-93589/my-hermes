# 受管观察循环

Controller 已拥有进程 cursor、输出窗口、状态识别、限额和终态收敛。Agent 不建立旧的 Terminal/Process 监督循环，不保存 `next_cursor`，不读取 PTY log，也不按文本长度、ANSI 清理结果或显示宽度计算位置。

## 单次观察

当当前可信 Grant 允许 `poll` 时，调用：

```text
claude_code(action="poll", process_id="<process_id>", round_id="<round_id>")
```

随后只解释本次 Tool result：

1. 读取 `state`、`process_active`、`round_terminal`、`round_id` 与 `outcome`；
2. 读取本次 `events` 和有界、脱敏的 `normalized_output`；
3. 把 `raw_cursor` 当作 Controller 返回的只读绝对事实，不回传也不另建分页；
4. 如果有 `action_required`，停止模型侧输入，等待确定性用户续接；
5. 如果 round 已终态，汇报该 round 的结果，不把仍运行的 process 误认为任务仍未收敛；
6. 如果出现 `error_type`、`delivery_unknown` 或限制 outcome，停止自动副作用并报告实际状态。

`poll` 是一次有界 Controller observation，不是后台服务、长轮询或授权 Agent 高频催促 Claude Code 的理由。Tool 不公开 `wait`、`log`、`read`、`write`、`submit` 或 `close`。

## 有效证据

新 `PROGRESS`、`OUTPUT`、完成/失败信号、进程状态变化或新 ActionRequired 可以补充当前理解。输入 echo、spinner、重复重绘、`effort` UI、孤立提示符、同一错误重现和短暂无输出不单独构成工作进展、完成、失败或可回复 Prompt。

Controller 已将重复观察、空读和状态变化计入 `observation_count`、`consecutive_empty_reads`、`limits_hit` 与 `deadline_remaining`。Agent 不维护重复版本，也不因 `output_used` 累计值停止观察；它只是统计，不是输出预算熔断。

## 收尾

只以 `round_terminal=true` 和 Controller 返回的稳定 `state`/`outcome` 收敛当前任务。完成后可在用户授权范围内做只读工作树核对；不要因一段总结、空读、spinner 消失、子命令结束或 exit code 单独宣布成功。

Gateway Watch 只负责后续正式终态的单次通知；它不替代当前 Tool result，也不允许 Agent 停止按事实汇报。详见 [workflow-controller.md](workflow-controller.md)。
