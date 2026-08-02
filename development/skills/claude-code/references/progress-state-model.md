# 进度状态模型

这些状态是 Skill 在当前 Agent 上下文中的逻辑判断，用于解释 Claude Code 的活动。它们不写入 ProcessManager，也不替代 ProcessStatus：`starting`、`running`、`exited`、`killed`、`lost`、`failed_start`。

P5 `ClaudeCodeSnapshot.state` 使用较粗的运行时集合：`STARTING`、`READY`、`WORKING`、`WAITING_INPUT`、`WAITING_APPROVAL`、`COMPLETED`、`FAILED`、`INTERRUPTED`、`LOST`、`UNKNOWN`。其中 `WORKING` 可以由上层监督根据新增事件细分为下表的 `PLANNING`、`INSPECTING`、`EDITING`、`RUNNING_COMMAND` 或 `RUNNING_TESTS`；`WAITING_INPUT` 对应 `WAITING_USER`，`WAITING_APPROVAL` 对应 `WAITING_PERMISSION`。不要把上层细分写回 ProcessManager 或伪装成 P5 检测器已确认的事实。

## 逻辑状态

| 状态 | 判定含义 |
| --- | --- |
| `PRECHECK` | 正在检查 Backend、Tool、cwd、CLI、模式和用户约束。 |
| `STARTING` | Terminal Tool 已被请求启动，尚未确认可交互运行。 |
| `READY` | Process 为 `running`，Claude Code 已表现为可接收初始任务或下一条输入。 |
| `PLANNING` | 正在形成、说明或调整与目标相关的计划。 |
| `INSPECTING` | 正在读取或分析相关文件、diff、配置或错误上下文。 |
| `EDITING` | 正在创建或修改获准范围内的文件。 |
| `RUNNING_COMMAND` | 正在执行获准的非测试命令。 |
| `RUNNING_TESTS` | 正在执行用户明确允许的测试或检查。 |
| `WAITING_PERMISSION` | 明确等待权限或安全确认。 |
| `WAITING_USER` | 明确提出需要用户或上层 Agent 回答的问题。 |
| `BLOCKED` | 已识别具体阻塞，Claude Code 无法自行继续。 |
| `STALLED` | 连续多个监督周期无有效进展，且不能由正常思考、读取或允许的耗时命令解释。 |
| `COMPLETED` | 满足 Skill 的组合完成条件，正在或已经收尾。 |
| `FAILED` | 自然失败、启动失败或已确认无法完成。 |
| `UNKNOWN` | 新增输出不足以可靠分类；暂时保持观察。 |

同一轮可以观察到多种活动，但只保存最能决定下一步的当前状态。ProcessStatus 优先决定能否输入和是否需要收尾；逻辑状态只决定如何理解进展与是否考虑干预。

## 允许保存的最小状态

当前 Agent 上下文只维护：

```text
process_id
next_cursor
logical_state
last_meaningful_progress
last_progress_signature
last_instruction
last_intervention_reason
intervention_count
unknown_count
```

不要保存 Process Handle、完整重复日志副本、输入历史、用户凭证或 Claude Code 私有 session 文件。`last_instruction` 只保留最近一条必要内容，用于语义去重，不是输入历史。

## 有效进展

以下事件可以更新 `last_meaningful_progress`：

- 开始读取新的相关文件；
- 形成或调整与目标相关的计划；
- 开始修改目标文件；
- 完成一个明确修改；
- 执行允许的命令；
- 得到新的错误并开始处理；
- 报告测试或检查结果；
- 主动总结当前阶段；
- 明确提出需要上层决策的问题。

`last_progress_signature` 应是简短语义摘要，例如“`EDITING` + 目标文件 + 新完成项”或“`BLOCKED` + 新错误类型”，用于识别是否真的变化。不要包含凭证或大段日志。

以下现象不能单独更新有效进展：

- spinner；
- 重复状态行；
- ANSI 重绘；
- 输入 echo；
- 相同错误重复出现；
- 同一文件反复读取但没有新结论；
- 短暂无输出。

## 状态变化规则

- 启动前保持 `PRECHECK`；Terminal Tool 接受启动后进入 `STARTING`。
- Process 为 `running` 且出现可输入提示时进入 `READY`。
- 新的计划、检查、编辑、命令或测试证据驱动对应活动状态。
- 明确权限提示使用 `WAITING_PERMISSION`；明确提问使用 `WAITING_USER`；已知无法继续的原因使用 `BLOCKED`。
- 只有多周期无进展且排除正常耗时后才使用 `STALLED`，不要设固定秒数作为唯一依据。
- 无法分类时进入 `UNKNOWN` 并增加 `unknown_count`；一旦出现可分类证据就重置或降低连续未知判断。
- 只有组合完成条件满足才进入 `COMPLETED`；Process 终态但没有完成证据时根据日志进入 `FAILED` 或 `UNKNOWN`，不要推断成功。
