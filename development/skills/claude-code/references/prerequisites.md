# 启动前检查

本文件只在用户已明确要求使用或控制 Claude Code/CC，且 Agent 已通过 `skill_view` 读取正文后使用。它只决定当前能否启动或控制 Claude Code，不是调用 `skill_view` 前的选择门禁。

在启动任何 Claude Code 进程前逐项检查。任一必要条件不满足时，保留 Skill 已正确选择和读取的事实，停止启动并报告具体原因。不要自动安装或认证 Claude Code，不要输入凭证，不要终止同一 cwd 的已有会话，也不要通过换后端、绕过 Terminal/Process/PTY 或放宽权限自行修复。

## 运行条件

1. **Backend 能力**：supervised PTY 需要 `LocalBackend`。可信运行时上下文已经公开给出 Backend 类型时可以预先判断；否则不要读取 Backend 实例、session registry、ToolRegistry 或 runtime 私有字段，直接通过 Terminal Tool 的公开调用尝试启动。
2. **Tool 可用性**：只依据当前 Agent 已公开、可调用的 Tool 确认同一可信 session 拥有 Terminal Tool 和 Process Tool；不要探查私有注册表。
3. **工作目录与授权**：明确 Claude Code 的目标工作目录。先确认当前 Terminal cwd 与目标一致；对于修改任务，还要确认用户已授权目标和修改范围。不要依赖提示文本让 Claude Code 自行猜测目录，也不要把纯分析授权扩展为修改授权。
4. **CLI 可用性**：通过 Git Bash/POSIX 语义执行公开检查：

   ```text
   command -v claude
   claude --version
   ```

   `command -v` 失败表示命令不可用；`claude --version` 只用于诊断、报告和辅助识别明显过旧版本，不以固定版本字符串作为唯一能力依据。`claude --help` 可以辅助诊断：出现 flag 是支持信号，未出现不能判定不支持。不要自动安装、升级、降级或改写 PATH。
5. **认证就绪**：Claude Code 必须已经完成认证。`claude --version` 成功不代表认证就绪；不要自动启动登录流程、输入凭证或读取 Claude Code 私有配置。无法从用户确认或安全的公开结果确认认证可用时，停止启动并报告；运行中出现登录或凭证提示时同样停止自动操作。
6. **cwd 会话互斥**：依据当前 Agent 已保存的 `process_id` 和 Process Tool 的公开 `list`/状态结果，确认同一 cwd 没有已知的活跃 Claude Code 会话。发现活跃会话时停止启动并报告，不自动接管或终止。公开结果不足以排除重复会话时，不要根据 PID、Handle、command 私有字段或系统进程猜测，应向用户报告并确认后再继续。
7. **PTY 必要性**：根据任务是否需要交互、纠偏或持续监督选择模式。print 模式足够时使用 one-shot。

## 模式能力检查

- **one-shot**：单独确认 `claude` 命令存在，并通过实际 Terminal/Process 公开结果判断 `claude -p` 与普通后台 pipe stdin。screen-reader probe 的结果不能代表 one-shot 可用性。
- **supervised PTY**：命令存在且任务确实需要监督时，先执行安全、非交互、不会启动开发会话的 flag probe：

  ```text
  claude --ax-screen-reader --version
  ```

  - probe 成功退出：参数被当前 CLI 接受，screen-reader prerequisite 满足。
  - stdout/stderr 明确出现 `unknown option`、`unrecognized option`、`unexpected argument` 或当前 CLI 的等价参数错误：判定当前版本不支持 supervised PTY 所需的 screen-reader 模式，但不要误报 Claude Code 未安装。
  - 认证问题、配置错误、安装损坏、环境异常、依赖加载失败或其他未知非零退出：不得判定 flag 不支持；报告 `Claude Code capability probe failed`，并停止 supervised PTY 自动启动。

probe 报告不得泄漏 token、API key、用户目录、配置文件正文或完整环境变量。不要自动升级，也不要静默删除参数后启动复杂 TUI。probe 成功后仍需通过公开 Terminal 结果确认 PTY；返回 `pty_unsupported` 时，不回退 pipe 交互模式、不切换 Backend、不访问私有状态，并单独评估 one-shot。

## 输入大小预检查

- 当前 Process `write`/`submit` 的单次 stdin 上限为 64 KiB。依据必须是完整 `data` UTF-8 编码后的字节数，不是 Python 字符数、显示字符数或 Markdown 行数；中文和 emoji 可能占多个字节。
- one-shot 在启动 `claude -p` 前确认完整 prompt 符合上限。超限或无法可靠确认时，不启动、不 `write`、不 `close`，也不改用命令行参数、heredoc、echo pipe、临时 shell 变量或多次 write；报告 `The Claude Code task exceeds the current process input limit and was not sent.`，且不回显完整任务。
- 第一版不实现 prompt 分块。Process Tool 仍是最终权威校验；运行时拒绝输入时不重试、不切换 shell 传输、不自动分块，并报告任务未送达。
- supervised 初始任务和每条后续 `submit`/`write` 也独立遵守上限。保持指示简短，只发送新增目标、具体偏差、硬约束和下一步动作；不要发送巨大日志、完整文件或重复全部历史。超限时停止并报告，不自动拆分。

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

只有公开 Tool、cwd、所选模式的 CLI/transport 能力、输入大小和用户约束均已确认，且任务提示不含凭证时，才进入 `STARTING`。不要为能力判断访问私有状态、另建状态字段或持久化 registry。
