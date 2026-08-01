# Claude Code CLI 参考

本 Skill 只依赖以下稳定、实际使用的入口。CLI 参数仍须通过 Terminal Tool 的安全策略与审批，不因写在 Skill 中而获得额外授权。

## `claude -p`

用于单次非交互任务。把完整任务与约束作为 print 模式输入，使用后台 pipe 启动并通过 Process Tool 读取日志和等待自然退出。

适合无需中途输入、确认或纠偏的 one-shot 任务。若运行中出现交互需求，不要猜测输入或自动用另一模式重跑。

## `claude --ax-screen-reader`

用于 supervised PTY 会话。该参数减少装饰边框和动画，使追加日志更容易理解，但不保证移除所有 ANSI、`\r`、输入 echo、spinner 或重复重绘，也不把输出变成屏幕快照。

启动后通过 Process Tool 的 `submit` 发送初始任务和必要的后续行式指示。不要依赖完整全屏 TUI 控制。

## 版本与权限边界

- 不自动增加危险权限参数，不使用 `--dangerously-skip-permissions` 或 `bypassPermissions`。
- 不自动安装、升级或降级 Claude Code。
- 不假设所有 Claude Code 版本支持相同实验功能。
- 发现参数不受支持时读取最终日志并报告，不自动尝试大量替代参数。
- 不把本 Skill 未使用的 CLI 参数扩展成隐式能力。
