# 受管运行时边界

本文件说明受管 Claude Code 的生产边界，不提供 Agent 直接调用 Runtime 的操作指南。

## 正式调用链

```text
当前真实用户明确请求
→ 可信请求识别与 Invocation Grant
→ 动态开放 claude_code Tool
→ ClaudeCodeAgentAdapter
→ ClaudeCodeController
→ ClaudeCodeRuntime / ClaudeCodeProcessPort
→ ProcessManager / LocalBackend PTY
→ Claude Code CLI
```

AgentLoop 仍使用通用 Tool 调度；它不直接嵌入 Controller 状态机。Skill 不能替代可信请求识别，也不能使 Tool 在没有当前 Grant 的轮次可见。

## Runtime 与进程所有权

受管启动使用当前配置的 Claude executable 和固定的 `--ax-screen-reader` PTY 形态。Runtime/ProcessPort 在后端锁内处理 cwd、环境快照和启动校验；Controller 只通过其公开接口管理任务 round。ProcessManager 继续拥有实际后台进程、输出窗口、输入送达、PTY、进程树终止和 session cleanup。

`process_id` 是受管 Session 的公开运行身份。Tool 结果还会返回 `cwd`、`process_active`、状态和 round identity，但不会暴露 owner、PID、Handle、后台 command、环境变量、Claude 私有 session 文件或 ProcessManager registry。

每个受管操作都会按当前 owner 验证 Session。调用方不得猜测 process id、从其他会话复制身份、按 PID 操作系统进程，或访问 Runtime/ProcessManager 私有字段。

## cwd 与 Session 边界

当前 myHermes runtime 的默认 Runtime 复用同一份 cwd 占用状态；同一 canonical cwd 不允许并发启动多个活跃受管 Claude Code Session。发生冲突时返回结构化错误，不复用、覆盖、终止或接管已有 Session。

该限制不扫描外部终端或跨 runtime 状态。myHermes 不能发现、重附着、恢复、终止或清理外部已运行的 Claude Code；Gateway、runtime 或 OS 重启后也不恢复内存 Session。

## Agent 可见行为

Agent 只通过 `claude_code` Tool 请求 `start`、`poll`、`send_instruction`、`request_interrupt` 或 `terminate`。它不调用 `Runtime.start/read/write/submit/status/wait/interrupt/kill`，不调用 Terminal/Process Tool 启动 Claude CLI，也不向 PTY 发送控制字符。

Runtime 的绝对 cursor、输出归一化、input echo 隔离和状态检测会被 Controller 投影为安全的 Tool result。Agent 不维护该 cursor，也不建立日志副本或 cleanup worker。

## 启动与失败边界

`start` 的 `cwd` 必须明确，`task` 必须非空；可信 Grant 和 owner 由宿主注入。Runtime 在启动阶段检查可执行文件、LocalBackend/PTy 能力、cwd、同 cwd 互斥和 ProcessManager 注册。它不会自动安装、升级、登录、读取凭据、修改全局权限或追加危险 CLI 参数。

如果 ProcessManager 已登记进程但启动后快照转换、PTY/cwd 校验或 SessionRef 建立失败，受管路径只清理本次 process id；不会清理同 session 的其他后台进程，也不会建立第二套 cleanup。启动或清理失败以安全结构化结果返回，不能触发裸 CLI fallback。

Runtime 的内部 `read`、`write`、`submit`、`wait`、`interrupt` 和 `kill` 保持 Controller/ProcessManager 的职责；它们不是本 Skill 的模型接口。关于公开结果和输入语义，见 [tool-contract.md](tool-contract.md)。
