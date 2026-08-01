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

## One-shot 输入与 EOF 失败

one-shot 只在 Terminal 公开返回 `status=running` 后执行一次任务 `write`。如果 Terminal 已返回 `exited`，不要调用 `write` 或 `close`；读取最终日志并报告任务提交前提前退出。`killed`、`lost` 或 `failed_start` 时同样不得发送任务。

`write` 返回 `process_input_delivery_unknown` 时：

1. 不重发完整任务，不发送第二份 prompt；
2. 不假设完全送达或完全未送达；
3. 不调用 `close`；
4. 用已有 `next_cursor` 执行 `poll`/`log`；
5. 不根据完整或部分 echo 猜测 prompt 是否送达；
6. 无法确认时停止自动输入并报告；
7. 为避免 Claude Code 执行不完整提示而需要终止时，调用 `process kill`。

`write` 明确失败且公开结果确认没有送达时，不进行无限重试，也不把自动重试写成统一策略。只有公开 `retryable` 语义允许，且能确认不会重复副作用时，才可以决定是否做一次受控处理。

`close` 未确认时，不声称 EOF 已发送或 Claude Code 已开始处理。根据公开 `retryable` 语义只能重试 `close`，绝不能重新 `write` prompt；无法收敛时停止自动操作并报告。

## Supervised 输入送达未知

supervised `submit` 返回 `process_input_delivery_unknown` 时，不重发、不假设送达结果，并用已有 `next_cursor` 观察新增输出。输出明确证明效果时再更新逻辑状态；无法确认时停止自动干预并报告。没有立即看到输入 echo 不是失败证据。

## 长时间停滞

只有连续多个监督周期无有效进展，并排除正常思考、文件读取和获准耗时命令后，才按以下顺序恢复：

```text
读取新增日志
→ 请求一次文字状态
→ 观察状态变化
→ process submit 发送一次 safe-stop
→ 读取新增日志并进行一次有界观察，判断是否出现有效状态变化
→ 自然结束则 process wait
→ 仍需终止则 process kill
→ 读取最终日志并确认状态
```

文字状态请求和 safe-stop 都只能发送一次，并遵守干预去重。Claude Code 明确正在完成不可中断的最小操作、仍有正常进展且未违反安全约束时，可以短暂继续观察，但观察必须有界，不能无限等待。未自然结束或继续运行存在风险时调用 `process kill`；不要通过 `write` 发送控制字符，也不因短暂无输出或 spinner 消失而终止。

## `process kill` 的职责

终止统一调用 `process(action="kill")`。ProcessManager 负责尝试协作式终止、等待 grace period、必要时强制终止整个 Job/PGID、记录终止来源和真实信号、收尾 Handle 与最终输出，并保留 `lost`、`failed_start` 等状态历史。

Skill 不调用 PID、系统信号、`taskkill`、`os.kill`，不操作 Windows Job 或直接关闭 Handle，也不在 kill 失败后自行连续重试。kill 无法确认时，读取其公开结果，报告 unresolved 状态，并让既有 SessionResource cleanup 继续拥有清理责任。

## 用户取消

收到用户取消后：

1. 若 Process 为 `running` 且继续输入安全、stdin 可用，提交一次 safe-stop 文字指示；
2. 读取新增日志并进行短暂且有限的状态观察；
3. 没有自然结束或用户要求立即停止时，调用 `process kill`；
4. kill 后读取最终日志，并用 `poll`/`list` 确认当前 session 不再有该活动进程；无法确认时明确报告；
5. 检查工作树可能留下的部分修改；
6. 汇报未完成事项和风险。

正在执行高风险或越权操作、明确违反用户硬约束、进程失控、用户要求立即终止、继续输入可能产生不可控副作用，或之前输入处于 delivery unknown 时，跳过 safe-stop，直接调用 `process kill`。

取消进程不等于回滚文件。不要自动删除、reset 或覆盖部分修改。

## 正常完成后的清理

Claude Code 明确完成并自然退出时，使用 `process wait` 或最终状态确认收尾，并读取最后一段日志。Claude Code 明确等待下一任务且当前任务已完成时，在输入安全且可用的 supervised PTY 中提交一次 safe-stop，有限观察自然退出；仍需终止时调用 `process kill`。one-shot pipe stdin 已关闭时直接依赖自然退出或 `kill`，不再发送输入。

最终核对 Claude Code 报告的文件和仓库 diff。只做获准的只读检查；不要在清理阶段新增修改、运行未授权测试或启动第二个 Claude Code。
