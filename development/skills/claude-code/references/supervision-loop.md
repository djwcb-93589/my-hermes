# 监督循环

仅在 supervised PTY 模式执行主动监督循环。one-shot 按 pipe 协议在 `status=running` 后 `write` 完整任务，并仅在 write 明确成功后 `close` stdin；此后只观察日志与自然退出，不发送常规进度追问。

## 初始化

Terminal Tool 启动成功后，在当前 Agent 上下文初始化：

```text
process_id = Terminal Tool 返回值
next_cursor = 0
logical_state = STARTING
last_meaningful_progress = 启动观察点
last_progress_signature = 空
last_instruction = 初始任务或空
last_intervention_reason = 空
intervention_count = 0
unknown_count = 0
```

不要持久化这些值，不要复制完整日志。初始任务通过 `submit` 成功送达后，把它记录为 `last_instruction`，但不把它计作纠偏干预。

## 每轮步骤

严格按以下顺序执行：

1. 使用 `process poll` 或 `process log` 获取新增输出。
2. 请求中传入上次返回的 `next_cursor`。
3. 只分析本轮新增输出；旧结论可保留为小型状态字段，不重建完整日志。
4. 将 Process Tool 本轮返回的 `next_cursor` 原样保存。
5. 读取并判断 ProcessStatus。
6. 根据新增输出判断 Claude Code 的逻辑状态。
7. 判断是否出现有效进展，并在有进展时更新 `last_meaningful_progress` 与 `last_progress_signature`。
8. 判断是否满足允许的干预条件和去重条件。
9. 必要时先确认 `status=running`，再通过 `process submit` 发送一次最小指示。
10. 不需要干预时继续观察；根据当前活动选择合理等待，不以高频轮询制造噪声。
11. 达到完成条件后使用 `process wait` 收尾，或确认已有最终 ProcessStatus。
12. 读取最后一段日志，汇总 Claude Code 结果和仓库状态。

`next_cursor` 只能使用 Process Tool 返回值。不得按脱敏文本、ANSI 清理文本或显示宽度计算，也不得反复从 `0` 读取或另建日志分页。

## 状态与进展判断

- 先判断 ProcessStatus，再判断逻辑状态。Process 已进入终态时不再发送输入。
- 对 ANSI、`\r`、spinner、输入 echo 和重复重绘只做理解层过滤，原始 cursor 不变。
- 新文件读取、新计划、目标修改、新错误处理、获准命令结果、测试结果、阶段总结或明确问题可以构成有效进展。
- 重复状态、相同错误、无新结论的反复读取和短暂无输出不能单独构成有效进展。
- 正在运行获准的耗时命令时，即使暂时没有输出，也不要仅凭固定秒数标记 `STALLED`。

## 观察节奏

观察间隔应与可见活动匹配：正在输出时及时消费新增页；运行耗时命令时降低干预频率；等待明确提示时再输入。`wait` 超时只表示本次等待结束，不代表进程失败、停滞或需要 kill。

连续多个监督周期没有有效进展时，结合最后动作、命令性质、重复模式和是否等待输入判断 `STALLED`。状态暂时无法分类时使用 `UNKNOWN` 并增加 `unknown_count`；`UNKNOWN` 本身不是干预理由。

## 收尾

自然退出后仍要以最新 `next_cursor` 读取最终新增输出。只有明确完成总结与自然退出同时成立，才直接判定 `COMPLETED`。如果 Claude Code 保持运行并明确等待下一任务，先确认当前修改和获准检查完成，再执行安全结束流程。

单独出现一次 `Stop`、一段总结、暂时无输出、某个子任务完成、测试命令结束或 spinner 消失，都不能判定整体完成。

最终可使用 Terminal/File Tool 做只读仓库核对，例如检查 Git diff；不要把核对阶段扩大成未获准的测试或修复阶段。
