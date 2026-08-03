# 权限与安全

Claude Code 的原生交互不扩大 myHermes 的自动授权。myHermes 不会替用户选择、批准、拒绝、回答或填写任何 Prompt；但用户可以通过 P7 透明交互接口明确决定要原样发送的内容。

## 自动行为边界

- 不自动输入密码、Token、API key、OAuth code、设备码或其他凭据；
- 不从环境、配置、日志或历史消息提取秘密并转交给 Claude Code；
- 不自动选择 `Allow once`、`Deny`、`Always allow`、`Don't ask again` 或 `Remember this choice`；
- 不根据屏幕文字猜测 `y`、`n`、编号、默认项或版本特定快捷键；
- 不自动批准危险 Bash、工作区外路径、Git push、发布、部署或安全检查关闭；
- 不使用 `--dangerously-skip-permissions`、`bypassPermissions` 或等价绕过方式。

这些规则约束 myHermes 的自动行为，而不是替用户重写 Claude Code 的原生选项。当前 Prompt 与全部可见选项必须透明展示；用户明确提供的确切回复会原样提交给同一受管 Claude Code 进程，包括用户自行选择的长期授权选项或认证内容。

## 凭据与最小留存

是否输入密码、Token、OAuth code、API key、设备码或其他认证内容由用户决定。用户未明确提供时，myHermes 不请求、猜测、补全或发送它们；用户明确提供时，Controller 只在本次提交调用中使用原样字符串。

无论回复内容为何：

- 不写入普通日志、Event、Snapshot、Controller 长期状态、错误 details 或持久文件；
- 不通过异常正文或汇总回显用户回复；
- 继续使用现有 P5.1 PTY echo 隔离与输出脱敏；
- 输入送达未知时不自动重发。

## 范围与工作流

初始任务仍须列出允许和禁止的文件范围，以及新增文件、依赖、网络、Git、测试和重构授权。代码修改与测试保持严格分阶段；用户要求不修改或不运行测试时，仍必须传给 Claude Code。

这些任务范围约束不把 Claude Code 原生 Prompt 改造成另一套审批系统。遇到 Prompt 时先透明冒泡，等待用户明确回复；用户不回复时继续观察、请求 interrupt 或 terminate 均须由用户明确选择。
