# 受管模式选择

当前 Claude Code Skill 只有一个正式 Agent 工作流：受管 `claude_code` Tool。Tool 不公开 one-shot、pipe、PTY、CLI command、stdin close 或 screen-reader 参数的选择；这些是 Runtime/ProcessManager 的内部实现细节。

## 何时使用受管流程

在当前真实用户明确要求 Claude Code，并且需要以下任意能力时，使用受管 Tool：

- 启动后台 Claude Code Session；
- 读取有界状态、事件或输出快照；
- 等待或处理 ActionRequired；
- 在同一 process 中创建新的任务 round；
- 协作式中断或明确终止；
- Gateway 终态通知。

Agent 不因任务简单、复杂、耗时、可打印结果或 PTY 是否方便而改选裸 CLI。`start` 的受管启动形态、READY 门禁、PTY 支持和输入提交由 Adapter/Controller 负责，模型只能传 `cwd` 和 `task`。

## 裸 CLI 不是 fallback

本 Skill 不定义 `claude -p`、`terminal(command="claude ...")`、`process write/submit/close/log/wait` 作为正常或失败恢复路径。受管 Tool 失败、Grant 缺失、状态未知、ActionRequired、输入送达未知或 Watch 注册失败时，均不得自动改用 Terminal/Process 裸 CLI。

若未来产品单独提供用户明确请求的裸 CLI/one-shot 能力，它必须拥有独立合同、明确能力差异和非受管身份；当前 `claude_code.poll`、`send_instruction`、ActionRequired 续接和 Watch 都不能接管那类进程。本 Skill 不承诺或实现该能力。
