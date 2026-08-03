# P7 原生交互透明冒泡

Claude Code 输出问题、审批、认证或其他交互提示时，myHermes 只负责把当前受管会话的内容原样冒泡给上层：

```text
ActionRequired
→ 当前 Prompt 和可见选项
→ 用户明确提供确切回复
→ 同一 process_id 的 Runtime.submit
→ 一次有界 observe
```

`clarification`、`approval`、`authentication`、`destructive_action`、`external_access` 和 `unknown_prompt` 只说明 Claude Code 大致在等待什么；它们不决定用户能否回复，也不会触发 myHermes 自有审批策略。

## 展示与选择

- `current_interaction` 只展示当前有效 Prompt 的临时原生 `prompt_text` 和 `options`：它们经过同一终端规范化流程，但不替换密码、Token、设备码或其他密钥值；最多保留 64 项选项，按 Claude Code 显示的文本、顺序、编号和重复项返回，超过上限时不扩展留存。
- 普通日志、Event、公开 Snapshot 和归档结果仍只使用脱敏 Prompt/options；临时原生视图不写入日志、错误 details、持久文件或历史 Snapshot。
- 不添加、删除、改写、重排或推荐选项；不把 `y/n`、编号或自然语言解释转换成终端输入。
- `Allow once`、`Deny`、`Always allow`、`Don't ask again`、`Remember this choice` 与密码、Token、OAuth code、API key、设备码均按 Claude Code 原文冒泡。
- 用户未提供回复时，不向 Claude Code 发送任何内容。

## 当前交互与原样回复

`current_interaction` 只返回当前有效的真实 Claude Code Prompt；`stalled` 是 Controller 自身的工作流状态，不是原生 Prompt，继续使用 `send_instruction`、`request_interrupt` 或 `terminate`。

每个真实 Prompt 都带有稳定的 `action_id`、`process_id`、`session_owner`、cursor 范围和创建时间。action id 不直接包含原生明文：对已验证的原生 Prompt，它结合安全结构与仅存于当前 Detector 内存中的 HMAC 身份材料构造不透明值。相同终端重绘沿用同一身份；Prompt、选项或原生可见值实际变化会生成新身份。成功回复、任何新输入、未知送达、明确 interrupt 和终态都会使旧身份和临时原生视图失效。

`reply_to_interaction` 只接受用户已经明确解析好的确切字符串；`response=""` 是用户明确选择的一次空 Enter，仍必须带有 `user_confirmed=True`，不得替换为换行、空格或默认值：

```text
校验 user_confirmed=True、owner、process_id 与 action_id
→ 在同一 Controller task 锁内标记消费中
→ 原样 Runtime.submit(response)
→ 使旧 Prompt 失效
→ 一次有界 observe
```

Controller 不解释回复含义，也不按 `kind`、`risk` 或选项内容阻止发送。每个 action id 最多成功提交一次；新 Prompt 不能使用旧 action id。

`response` 缺失、为 `None`、不是字符串或回复对象无效时仍是无效请求；它们不等同于 `response=""`。

## 输入送达未知与最小留存

提交返回 delivery unknown 时，不自动重发、不假定成功或失败，也不把旧 Prompt 恢复为可自动消费状态。用户只能在上层明确选择继续观察、提交新的明确回复、interrupt 或 terminate。

用户回复正文只在本次 `reply_to_interaction` 调用栈中短暂存在：不得写入普通日志、Event、Snapshot、Controller 长期状态、错误 details 或持久文件。P5.1 的短生命周期 fingerprint 匹配到 PTY echo 后，必须从 Event 和 Snapshot 的对外视图中移除正文，只留固定占位标记；同一 echo 行也不得重新进入临时原生 Prompt 视图。

透明冒泡仍只作用于当前 Runtime 通过同一 ProcessManager 启动且受同一 owner 管理的会话；它不转移 PTY、不扫描 PID、不接管外部 Claude Code，也不接入 AgentLoop 或通用 Approval Tool。
