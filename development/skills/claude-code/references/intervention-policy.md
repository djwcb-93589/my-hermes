# 干预策略

默认行为是观察。主动输入必须解决一个具体、当前且可由上层 Agent 安全处理的问题；不要把聊天式催促当作监督。

## 允许干预的条件

只有以下情况允许主动发送指示：

1. Claude Code 明确等待输入；
2. Claude Code 违反用户硬约束；
3. Claude Code 明显偏离任务目标；
4. Claude Code 开始无关的大规模重构；
5. Claude Code 遇到需要用户或上层 Agent 决策的阻塞；
6. 连续多个监督周期没有有效进展，且已排除正常耗时活动；
7. 用户在运行过程中补充了新要求；
8. Claude Code 即将执行被明确禁止的测试、提交或依赖修改。

如果违反行为已经发生，指示其立即停止继续扩大影响并报告现状；不要擅自要求回滚用户工作或执行破坏性恢复。

## 不允许作为单独干预理由

- 正在思考；
- 正在读取相关文件；
- 正在运行允许的耗时命令；
- 短时间无输出；
- spinner 重复；
- 当前状态暂时无法分类。

不得不断发送“进度如何？”、“继续。”或“完成了吗？”。这类输入会打断 Claude Code，且没有携带可执行的新信息。

## 干预去重

发送前比较：

```text
last_instruction
last_intervention_reason
last_progress_signature
```

执行以下规则：

1. 为候选干预生成简短、稳定的语义原因，例如“即将运行用户禁止的测试”。
2. 如果原因与 `last_intervention_reason` 相同，且 `last_progress_signature` 自上次发送后没有有效变化，不发送。
3. 如果新指示与 `last_instruction` 实质相同，不发送，即使措辞不同。
4. 同一原因只发送一次。发送后必须等待至少一个有效状态变化，才允许下一次相关干预。
5. 用户新增要求只有在内容实质变化时才构成新原因；重复转述不解除去重。
6. 每次实际干预后更新 `last_instruction`、`last_intervention_reason` 和 `intervention_count`。

干预后仍无有效变化时进入恢复策略，不通过改写同一句话反复尝试。

## 选择指示

- 状态长期不明且满足停滞条件：填充并发送 [progress-request.md](../templates/progress-request.md)，只发送一次，然后等待文字状态或其他有效变化。
- 目标、范围或硬约束偏离：填充并发送 [corrective-instruction.md](../templates/corrective-instruction.md)。
- 用户取消、风险升级或需要结束：在输入安全且可用时发送一次 [safe-stop.md](../templates/safe-stop.md)，按恢复协议进行有界观察；仍需终止时调用 `process kill`。
- 明确问题需要用户决定：不要替用户猜测；停止自动输入并把问题、选项与影响报告给用户。

## 输入步骤

以下步骤只用于 supervised PTY 的行式指示；one-shot 完整任务必须遵循 pipe `write`/`close` 协议。每次发送前：

1. 用最新 Process Tool 响应确认 `status=running`。
2. 确认内容不含密码、token、API key 或其他凭证。
3. 确认指示没有扩大用户授权。
4. 使用 `process(action="submit", process_id="<process_id>", data="<instruction>")` 发送一条行式指示。
5. 发送后进入观察，不因日志未立即 echo 而重发。

若返回 `process_input_delivery_unknown`，本次干预结果保持未知：不要重发，不要增加新的自动输入；先读取新增日志，无法确认时报告并停止自动干预。

不要通过 `process write` 自行发送控制字符。正在执行高风险或越权操作、用户要求立即终止、进程失控、继续输入可能产生不可控副作用，或之前输入 delivery unknown 时，跳过 safe-stop 并直接调用 `process kill`。
