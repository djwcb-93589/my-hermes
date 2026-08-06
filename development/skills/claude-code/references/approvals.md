# ActionRequired 与原生交互边界

当受管 Claude Code 显示澄清、目录信任、运行时权限、认证、破坏性操作、外部访问、未知 Prompt 或中断菜单时，Controller 生成当前 ActionRequired。myHermes 不把它变成模型可自由调用的 Tool action。

## 安全投影与透明冒泡

普通 `claude_code` Tool result 只包含安全 ActionRequired 投影：稳定 `action_id`、kind、摘要、脱敏 prompt/options、风险与 cursor 范围。它不包含原生 prompt/options、用户回复、owner、Token、密码、认证码或完整终端输出。

确定性 Conversation 协调层可以在当前交互仍有效时调用内部 `current_interaction`，向用户展示对应的有界原生 Prompt 与可见选项。该原生视图：

- 仅绑定当前 owner、process、round、action id 与已解析的 prompt 范围；
- 遵守现有终端规范化、echo masking、长度和选项数量限制；
- 不从历史 PTY 缓冲拼接，不因安全文本与原生文本宽松匹配而暴露无关输出；
- 在回复、新输入、送达未知、interrupt、新 Prompt 或终态后失效。

相同 Prompt 重绘保持相同 action identity；Prompt 或选项实质变化才产生新的 identity。`stalled` 是 Controller 合成状态，没有可展示或可回复的原生视图。

## 用户回复

模型不得自行回答或推荐选择。用户下一条明确回复由 Conversation 协调层绕过模型，并用当前 owner、process、round 和 `action_id` 重新校验后，调用内部 `reply_to_interaction`：

```text
用户明确回复
→ user_confirmed=true
→ 校验当前 ActionRequired identity
→ 原样提交一次
→ 失效旧 action / 原生视图
→ 一次有界 observe
```

`response=""` 只表示用户明确选择一次 Enter；缺失、`None` 或非字符串不是 Enter。系统不解释或转换 `y/n`、编号、选项、密码、Token、OAuth/device code，也不按 action kind 自动决定发送内容。

`delivery_unknown` 时，不假定回复成功或失败，不自动重发，也不让旧 action 恢复为可自动消费状态。用户回复正文只短暂存在于这次续接调用中，不写入普通日志、Event、Snapshot、Controller 长期状态、错误 details 或持久化。

## 禁止自动审批

Agent 和协调层都不得自动选择 `Allow once`、`Deny`、`Always allow`、`Don't ask again` 或 `Remember this choice`，不得自动输入任何凭据，也不得根据屏幕文本猜测默认选项。用户不回复时，系统不会向 Claude Code 输入内容；后续中断或终止仍必须来自用户当前明确控制请求。
