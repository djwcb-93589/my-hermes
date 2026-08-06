# 权限与安全边界

Claude Code 的受管运行不扩大 myHermes 的自动授权。Skill 指导不等于 Grant；可信入站链路、ToolPolicy、Adapter 和 Controller 仍分别执行授权、owner 与状态校验。

## 禁止自动行为

- 不自动输入或从环境/配置/日志提取密码、Token、API key、OAuth code、设备码、cookie、私钥或其他凭据；
- 不自动选择 `Allow once`、`Deny`、`Always allow`、`Don't ask again` 或 `Remember this choice`；
- 不根据 Prompt 文本猜测 `y`、`n`、编号、默认项或版本快捷键；
- 不自动批准危险 Bash、工作区外路径、Git push、发布、部署或关闭安全检查；
- 不使用 `--dangerously-skip-permissions`、`bypassPermissions` 或等价绕过；
- 不自动启动、继续、中断或终止 Claude Code，也不在 Tool 失败后回退裸 CLI。

目录信任、认证和权限 Prompt 通过确定性 Conversation 透明展示。用户明确提供的确切回复可原样交给内部续接路径，但回复内容不进入普通日志、Event、Snapshot、错误 details、Tool result 或持久化；送达未知时不自动重发。

## 任务范围

`start.task` 与新的 `send_instruction.instruction` 必须保留用户给出的文件范围、测试、依赖、Git、网络和重构限制。代码修改与测试严格分阶段。任何缺失授权都不能被 Agent 或 Claude Code 假定为允许；遇到高风险、范围不明或工作区外动作时停止并由用户决定。

## 身份与数据边界

只使用受管 Tool 返回的 `process_id`、`round_id`、状态和安全 ActionRequired 投影。不要暴露或构造 owner、Grant、route、notification target、PID、Handle、原始 PTY、Claude 私有 session 数据或完整终端缓冲。不要接管外部 Claude Code。
