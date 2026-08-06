# 受管启动前检查

本检查只在可信入站链路已经为当前真实用户消息开放 `claude_code` Tool 后使用。Skill 文本不能自行授予该能力，也不能把普通代码任务升级为 Claude Code 请求。

## Agent 应确认的事实

1. 当前用户明确要求使用 Claude Code，并且当前 Tool context 已可信地开放 `claude_code`；否则不启动。
2. `cwd` 明确且属于用户授权范围；不猜测工作目录、不自动选择最近 Session，也不把分析授权扩大为修改授权。
3. 初始 `task` 明确包含目标、验收标准、允许/禁止文件、测试、依赖、Git、网络、重构范围和汇报格式。
4. 任务正文不包含密码、Token、API key、cookie、私钥或其他凭据；不要求 Claude Code 请求、读取或输出它们。
5. 用户约束没有被遗漏。未授权的新增文件、测试、依赖/lockfile、commit、push、网络、发布或工作区外访问一律视为不允许。
6. 已知受管 `process_id` 时，只使用用户/Tool 返回的明确身份控制它；不扫描、接管或终止外部或“最近”会话。

始终写明“代码修改阶段与测试阶段严格分离”。如果用户要求不修改测试或不运行测试，必须把该限制传入 `task`。

## 不由 Agent 执行的探测

不要为 Claude Code 运行 `command -v`、`claude --version`、`claude --help`、`claude --ax-screen-reader --version`，也不要调用 Terminal/Process Tool 进行 PTY、CLI、认证、cwd 或输入大小探测。受管 Runtime/Adapter 在启动时通过既有安全路径处理可执行文件、LocalBackend PTY、cwd、同 cwd 互斥和 ProcessManager 注册。

启动失败时读取 Tool 的安全 `error_type`、`retryable` 和 `delivery_unknown`，如实报告；不要自行修复 PATH、安装/升级/登录 Claude Code、切换 Backend、添加权限绕过参数或改用裸 CLI。

## 启动决定

只有前述用户授权、任务边界、cwd 和可信 Tool context 都明确时，调用：

```text
claude_code(action="start", cwd="<cwd>", task="<task>")
```

不要传入 owner、Grant、`user_requested`、CLI command、PTY 参数、notification target 或任何隐藏运行时字段。`start` 返回 `initial_instruction_submitted=false` 或 ActionRequired 时，保留结果并等待确定性用户续接；不要把初始任务改写为 Prompt 回复或通过新的裸进程重试。
