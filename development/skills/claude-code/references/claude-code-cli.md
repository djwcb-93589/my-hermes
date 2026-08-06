# Claude Code CLI 边界

受管 Agent 不直接运行 Claude CLI。正常流程只调用 `claude_code` Tool；Runtime/ProcessPort 在其受控启动路径中解析当前配置的 executable，并以固定的 `--ax-screen-reader` 启动受管 PTY Session。

`--ax-screen-reader` 用于减少装饰和动画，但不会承诺消除所有 ANSI、`\r`、输入 echo、spinner、重绘或复杂全屏 TUI。Runtime/Normalizer 负责将这些细节投影为有界安全结果；Agent 不直接探测、拼接或控制终端界面。

本 Skill 不把 `claude -p`、`claude --help`、`command -v claude`、版本 probe、shell 转义、heredoc、stdin close 或其他 CLI 参数作为模型操作步骤。它们既不是 Tool schema，也不能在 Tool 失败、Grant 缺失、ActionRequired、送达未知或 Watch 异常时作为 fallback。

Runtime 不自动安装、升级、登录 Claude Code，也不添加危险权限绕过参数。CLI 能力或版本问题由受管 `start` 的安全错误结果报告；Agent 不应据此修改 PATH、全局配置或权限。
