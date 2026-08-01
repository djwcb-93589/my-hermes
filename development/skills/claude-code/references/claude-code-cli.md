# Claude Code CLI 参考

本 Skill 只依赖以下稳定、实际使用的入口。CLI 参数仍须通过 Terminal Tool 的安全策略与审批，不因写在 Skill 中而获得额外授权。

## `claude -p`

用于单次非交互任务。Terminal Tool 的 command 只包含 `claude -p`；完整任务与约束通过普通 pipe 的 Process Tool `write` 原样传入 stdin，write 明确成功后再用 `close` 发送真实 EOF。完整任务在启动前必须满足 Process stdin 的 UTF-8 64 KiB 上限；超限时不启动、不发送，也不自动分块。

不要把完整任务放入命令行参数，不要构造 shell 转义、插值、heredoc、临时变量或 command substitution。任务在 64 KiB 上限内可以包含任意多行文本，且不得被额外写入 logger 或输入历史。该模式适合无需中途输入、确认或纠偏的 one-shot 任务；若运行中出现交互需求，不要猜测输入或自动用另一模式重跑。

## `claude --ax-screen-reader`

用于 supervised PTY 会话。该参数减少装饰边框和动画，使追加日志更容易理解，但不保证移除所有 ANSI、`\r`、输入 echo、spinner 或重复重绘，也不把输出变成屏幕快照。

启动开发会话前，使用以下安全 flag probe 验证当前 CLI 是否接受参数：

```text
claude --ax-screen-reader --version
```

probe 只查询版本，不得启动 Claude Code 开发会话。成功退出表示参数可用；只有输出明确表示 `unknown option`、`unrecognized option`、`unexpected argument` 或等价参数错误时，才能判定不支持。认证、配置、安装、环境、依赖或未知非零错误应报告 `Claude Code capability probe failed`，不得解释成 flag 不支持，也不得在报告中泄漏凭证、用户目录、配置正文或完整环境变量。

启动后通过 Process Tool 的 `submit` 发送初始任务和必要的后续行式指示。不要依赖完整全屏 TUI 控制。

`claude --help` 只作辅助：出现 flag 可以作为支持信号，缺少 flag 不能作为不支持证据。probe 未成功时不要自动升级，不要静默删除参数后启动复杂 TUI。`claude -p` 的 one-shot 可用性应独立检查，不能把 screen-reader 不可用误报为 Claude Code 未安装或整体不可用。

## 版本与权限边界

- 不自动增加危险权限参数，不使用 `--dangerously-skip-permissions` 或 `bypassPermissions`。
- 不自动安装、升级或降级 Claude Code。
- 不假设所有 Claude Code 版本支持相同实验功能。
- 按 `command -v claude`、`claude --version`、安全 flag probe 的顺序检查；`claude --help` 仅提供辅助诊断。固定版本字符串不能替代 probe。
- 不把本 Skill 未使用的 CLI 参数扩展成隐式能力。
