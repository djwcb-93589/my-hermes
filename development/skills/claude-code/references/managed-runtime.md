# P4 受管运行时

本文件描述真实 Claude Code 后台 PTY 的基础生命周期。只有用户当前请求明确指定使用 Claude Code/CC，或明确要求控制由当前 myHermes runtime 启动的已有 CC 会话后，才进入本流程。任务规模、复杂度、文件数量、运行时间、已安装状态或 Agent 偏好都不能代替该要求。

## 唯一进程链路

受管启动固定使用：

```text
ClaudeCodeRuntime
→ ClaudeCodeProcessPort
→ ProcessManager
→ LocalBackend.spawn_background(pty=true)
→ Windows ConPTY / POSIX PTY
→ claude --ax-screen-reader
```

`ProcessManager` 返回的 `process_id` 是唯一运行身份。不得直接使用 PID、Handle、`subprocess`、`pywinpty`、Windows Job Object、系统信号或 ProcessManager 私有字段。P4 Adapter 不实现 reader、日志缓冲、cursor、进程 registry、进程树终止或 cleanup。

Agent 面向工具的调用仍使用 Terminal Tool 和 Process Tool；P4 module 是可信运行时可组合的生产接口，不注册 `claude_code` Tool，也不修改 AgentLoop、Prompt、Skill Loader 或通用工具权限。

## 启动入口

启动调用必须显式提供：

```text
user_requested=true
session_owner=<当前可信 session>
cwd=<明确目标目录>
```

同时满足以下条件后才能启动：

1. `user_requested` 必须严格为 true；
2. cwd 通过当前 LocalBackend 的 PathPolicy，并且是可访问目录；
3. 配置的 Claude Code executable 能被当前环境解析；
4. 当前 Backend 是 LocalBackend，并提供后台 PTY；
5. 当前 ProcessManager 提供启动、读取、输入、等待和终止公共方法；
6. 同一 runtime 内该 canonical cwd 没有其他活跃的受管 CC；
7. 当前 session owner 可拥有新 ProcessManager 记录。

运行模块不自动安装、升级、认证或登录 Claude Code，不读取或输入凭据，不修改全局配置，也不放宽权限。CLI 命令只包含已解析 executable 和固定的 `--ax-screen-reader`，不把任务内容拼入 shell command。

如果 ProcessManager 已返回 process id，但 `ClaudeCodeSessionRef` 无法建立，立即通过同一 owner 调用 ProcessManager 终止刚登记的进程。即使清理不能确认，该进程仍由 ProcessManager 与既有 `SessionResource` 拥有，不建立替代清理路径。

## SessionRef 与所有权

`ClaudeCodeSessionRef` 只保存：

```text
process_id
session_owner
cwd
cursor
started_at
last_activity_at
```

它不保存 PID、Handle、完整日志、输入历史、凭据、环境变量、Claude Code 配置或私有 session 文件。

每次 read、write、submit、status、wait、interrupt 和 kill 都先验证：

```text
调用 session owner
== ClaudeCodeSessionRef.session_owner
== ProcessManager 记录 owner
```

Runtime 先检查 SessionRef；Adapter 再把同一 owner 传给 ProcessManager，由 ProcessManager 的公共 owner-scoped 方法完成最终验证。不得通过猜测 process id 操作其他 session。

## cwd 并发保护

同一个 `ClaudeCodeRuntime` 实例只允许一个 canonical cwd 对应一个活跃受管 CC。发现 `starting` 或 `running` 会话时返回 `cwd_session_active`，不复用、不覆盖、不自动终止。

该约束只覆盖当前 runtime 保存的受管 SessionRef。启动前不扫描系统 PID，不读取外部终端，不承诺发现用户手工启动或其他 runtime 启动的 Claude Code。Process 进入终态或既有 ProcessManager 记录已经消失后，Runtime 只释放 cwd 占用；进程历史和资源释放仍由 ProcessManager 管理。

## 基础操作

- `start`：通过 LocalBackend 后台 PTY 与 ProcessManager 启动 `claude --ax-screen-reader`，返回 cursor 为 0 的 SessionRef。
- `read`：把 SessionRef 保存的绝对 cursor 原样传给 ProcessManager，只采用其返回的 `next_cursor`；该原始兼容入口继续返回含 ANSI、`\r`、echo、spinner 和重绘的脱敏 PTY 追加文本。
- `observe`：在同一次有界调用中读取一页新增输出、规范化、生成事件、复核 ProcessStatus 并返回 P5 Snapshot；不循环、不等待、不输入也不终止。
- `write`：调用 `write_stdin` 原样发送文本，不附加 Enter，不保存输入。
- `submit`：调用 `submit_stdin`，由当前 PTY transport 提交 Enter，不保存输入。
- `status`：只返回 `starting`、`running`、`exited`、`killed`、`lost` 或 `failed_start` 等 ProcessStatus 事实。
- `wait`：只允许有上限的等待；超时结束本次 wait，不终止进程。
- `interrupt`：通过 owner-scoped 输入路径发送一次 Ctrl+C，只请求协作式中断；delivery unknown 时不自动重发。
- `kill`：调用 ProcessManager `kill`，由其负责协作式中断、grace period、必要的强制终止和进程树清理。

P4 的 `read`、`status` 等基础接口仍不推断语义。P5 只通过 `observe` 提供有界规范化和多证据状态识别，详见 [output-observation.md](output-observation.md)。P6 在独立 [workflow-controller.md](workflow-controller.md) 中串联这些公开接口；Runtime 本身仍不自动轮询、回复、批准或终止。

## cleanup 与外部会话

Runtime 丢弃 SessionRef 或释放 cwd 占用不等于进程 cleanup。session 结束时仍统一调用：

```text
SessionResource
→ ProcessManager.cleanup_session(session_owner)
```

不得为 Claude Code 新建 cleanup worker、持久化 registry、PTY host、handoff 或重附着服务。Gateway/runtime 重启后不重新附着，操作系统重启后不恢复。外部已经运行的 Claude Code 明确不可接管：不扫描 PID、不附着普通 Git Bash、不读取外部历史输出、不注入输入、不使用 `--resume` 收养运行中任务，也不终止或清理其他终端、runtime 或 session 的进程。
