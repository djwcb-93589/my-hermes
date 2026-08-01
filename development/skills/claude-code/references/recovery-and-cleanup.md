# 失败、恢复与清理

恢复目标是保留事实、避免重复副作用，并通过 Process Tool 与既有 session resource 生命周期收敛进程。不要直接操作 PID、Handle、私有 registry 或 Claude Code 私有 session 文件。

## Claude Code 自然失败

Process 自然进入 `exited` 但任务未完成或 exit code 表示失败时：

1. 使用当前 `next_cursor` 读取最终新增日志；
2. 记录最后一个有效进展、明确错误和工作树可能的部分修改；
3. 不自动重新启动，不自动重复任务；
4. 汇报退出状态、最后有效进展、未完成事项与建议的下一步决定。

一段错误文本不一定意味着进程已退出；以 ProcessStatus 为准。

## Process `lost`

- 不假设 Claude Code 已退出，也不把 `lost` 当作成功或普通失败。
- 使用 `process poll`、`process list` 和必要的 `process kill` 遵循现有公开语义。
- 不按返回的 PID 直接查询、发送信号或结束系统进程。
- 让 Process Tool/SessionResource 的既有清理流程拥有资源，不建立替代 cleanup。
- `kill` 无法确认清理时，报告 unresolved 状态并交由现有 session cleanup 继续处理。
- 保留最小状态历史用于汇报，不复制完整日志。

## 输入送达未知

收到 `process_input_delivery_unknown` 时：

1. 不重发相同输入；
2. 不假设送达或未送达；
3. 用已有 `next_cursor` 观察新增输出；
4. 输出可明确证明效果时再更新逻辑状态；
5. 无法确认时停止自动干预并报告。

没有立即看到输入 echo 不是失败证据。

## 长时间停滞

只有连续多个监督周期无有效进展，并排除正常思考、文件读取和获准耗时命令后，才按以下顺序恢复：

```text
读取新增日志
→ 请求一次文字状态
→ 观察状态变化
→ 必要时请求安全停止或发送 Ctrl+C
→ 最后才 process kill
```

文字状态请求只能发送一次，并遵守干预去重。安全停止使用模板并先给 Claude Code 完成当前不可中断最小操作的机会。只有进程仍为 `running`、协作停止无效且继续运行存在风险或用户已取消时，才使用 `write` 发送 Ctrl+C；随后继续观察。`kill` 是最后手段，不因短暂无输出或 spinner 消失而使用。

## 用户取消

收到用户取消后：

1. 若 Process 为 `running`，提交一次安全停止指示；
2. 观察 Claude Code 是否自然停止或进入可安全结束状态；
3. 必要时发送 Ctrl+C；仍未收敛时使用 `process kill`；
4. 用 `poll`/`list` 确认当前 session 不再有该活动进程，无法确认时明确报告；
5. 读取最后一段日志；
6. 汇报当前工作树可能留下的部分修改、未完成事项和风险。

取消进程不等于回滚文件。不要自动删除、reset 或覆盖部分修改。

## 正常完成后的清理

Claude Code 明确完成并自然退出时，使用 `process wait` 或最终状态确认收尾，并读取最后一段日志。Claude Code 明确等待下一任务且当前任务已完成时，发送安全停止，观察自然退出；必要时才升级为 Ctrl+C 或 `kill`。

最终核对 Claude Code 报告的文件和仓库 diff。只做获准的只读检查；不要在清理阶段新增修改、运行未授权测试或启动第二个 Claude Code。
