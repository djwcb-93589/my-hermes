# 启动前检查

在启动任何 Claude Code 进程前逐项检查。任一必要条件不满足时停止启动并报告，不要通过安装、换后端或放宽权限自行修复。

## 运行条件

1. **Backend 能力**：supervised PTY 需要 `LocalBackend`。可信运行时上下文已经公开给出 Backend 类型时可以预先判断；否则不要读取 Backend 实例、session registry、ToolRegistry 或 runtime 私有字段，直接通过 Terminal Tool 的公开调用尝试启动。
2. **Tool 可用性**：只依据当前 Agent 已公开、可调用的 Tool 确认同一可信 session 拥有 Terminal Tool 和 Process Tool；不要探查私有注册表。
3. **工作目录**：明确 Claude Code 的目标工作目录。先确认当前 Terminal cwd 与目标一致；不要依赖提示文本让 Claude Code自行猜测目录。
4. **CLI 可用性**：通过 Git Bash/POSIX 语义执行公开检查：

   ```text
   command -v claude
   claude --version
   claude --help
   ```

   `command -v` 失败表示命令不可用；version 或 help 失败时按其公开结果报告。不要自动安装、升级、降级或改写 PATH。
5. **PTY 必要性**：根据任务是否需要交互、纠偏或持续监督选择模式。print 模式足够时使用 one-shot。

## 模式能力检查

- **one-shot**：从当前 `claude --help` 的公开输出单独确认 `-p` 能力，并通过 Terminal 的公开结果判断普通后台 pipe。不要因为 PTY 不支持而判定 one-shot 不可用。
- **supervised PTY**：从当前 `claude --help` 的公开输出确认存在 `--ax-screen-reader`，不要把可能过时的固定版本号作为唯一依据。参数存在时才允许进入 supervised PTY。
- 帮助输出不支持 `--ax-screen-reader` 时，报告“supervised PTY prerequisites 不满足”；不要误报为 Claude Code 未安装，不要自动升级，也不要静默删除该参数后启动复杂 TUI。
- Terminal Tool 返回 `pty_unsupported` 时，不回退 pipe 交互模式、不切换 Backend、不访问私有状态；报告当前环境不支持 supervised PTY，并在任务确实适合时建议独立评估 one-shot pipe。

## 任务约束清单

从用户请求中提取并在启动前形成明确值：

- 任务目标与验收标准；
- 允许修改的文件或目录；
- 禁止修改的文件或目录；
- 是否允许新增文件；
- 是否允许运行测试，以及允许的测试范围；
- 是否允许修改依赖或锁文件；
- 是否允许创建 Git commit；
- 是否允许访问网络；
- 是否允许扩大重构范围；
- 完成后的汇报格式；
- 其他时间、平台、编码或命令约束。

缺失项不能被解释为开放授权。会影响结果且无法从上下文安全推断时，先向用户确认；否则采用最保守、不会扩大修改面的解释。始终写明“代码修改阶段与测试阶段严格分离”。

如果用户明确要求“不要修改测试”或“不要运行测试”，在初始任务中逐字保留等价约束，不要只依赖上层 Agent记忆。

## 凭证与权限检查

- 在提交给 Claude Code 前移除密码、token、API key、cookie、私钥和其他凭证。
- 不把环境变量的秘密值展开进任务或日志摘要。
- 不自动添加 `--dangerously-skip-permissions`、`bypassPermissions` 或任何等价绕过设置。
- 预期存在高风险操作时，不要预先批准；继续使用 Hermes 现有审批机制，并由用户明确决定超出原授权的事项。
- 任务要求访问工作区外路径、发布、部署、Git push 或输入凭证时，将其标为需上层决策，不要把它伪装成普通实现步骤。

## 启动决定

只有公开 Tool、cwd、所选模式的 CLI/transport 能力和用户约束均已确认，且任务提示不含凭证时，才进入 `STARTING`。不要为能力判断访问私有状态、另建状态字段或持久化 registry。
