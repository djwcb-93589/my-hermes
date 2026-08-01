# Claude Code CLI 参考

本 Skill 只依赖以下稳定、实际使用的入口。CLI 参数仍须通过 Terminal Tool 的安全策略与审批，不因写在 Skill 中而获得额外授权。

## `claude -p`

用于单次非交互任务。Terminal Tool 的 command 只包含 `claude -p`；完整任务与约束通过普通 pipe 的 Process Tool `write` 原样传入 stdin，write 明确成功后再用 `close` 发送真实 EOF。

不要把完整任务放入命令行参数，不要构造 shell 转义、插值、heredoc、临时变量或 command substitution。任务可以是任意多行文本，且不得被额外写入 logger 或输入历史。该模式适合无需中途输入、确认或纠偏的 one-shot 任务；若运行中出现交互需求，不要猜测输入或自动用另一模式重跑。

## `claude --ax-screen-reader`

用于 supervised PTY 会话。启动前通过当前 CLI 的 `claude --help` 公开输出确认参数存在，不要只依赖固定版本号。该参数减少装饰边框和动画，使追加日志更容易理解，但不保证移除所有 ANSI、`\r`、输入 echo、spinner 或重复重绘，也不把输出变成屏幕快照。

启动后通过 Process Tool 的 `submit` 发送初始任务和必要的后续行式指示。不要依赖完整全屏 TUI 控制。

如果帮助输出不支持该参数，明确报告 supervised PTY prerequisites 不满足；不要自动升级，不要静默删除参数后启动复杂 TUI。`claude -p` 的 one-shot 可用性应独立检查，不能把 screen-reader 参数缺失误报为 Claude Code 未安装。

## 版本与权限边界

- 不自动增加危险权限参数，不使用 `--dangerously-skip-permissions` 或 `bypassPermissions`。
- 不自动安装、升级或降级 Claude Code。
- 不假设所有 Claude Code 版本支持相同实验功能。
- 使用 `command -v claude`、`claude --version` 和 `claude --help` 的公开结果判断安装与所需能力；发现参数不受支持时明确区分“CLI 存在”和“supervised 参数缺失”。
- 不把本 Skill 未使用的 CLI 参数扩展成隐式能力。
