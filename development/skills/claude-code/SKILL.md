---
name: claude-code
description: >-
  Use only when the user explicitly asks to use Claude Code/CC, or explicitly
  asks to continue, steer, stop, or otherwise control an existing Claude Code
  session. Do not select, recommend, or launch Claude Code based only on task
  size or complexity. Without an explicit user request, use normal myHermes
  tools. After explicit activation, read this Skill and complete the required
  preflight before launch or session control.
version: 0.4.0
platforms:
  - windows
  - linux
  - darwin
metadata:
  development_stage: managed_runtime
  agent_integration: discovery_and_selection
  managed_runtime: true
  supports_one_shot: true
  supports_supervised_pty: true
  requires_process_input: true
---

# Claude Code 主动监督

## 显式启用与启动边界

只有当前用户请求明确要求使用 Claude Code/CC，或明确要求继续、补充指示、停止或控制已有 Claude Code 会话时，才选择本 Skill 并调用 `skill_view(name="claude-code")`。

不得用以下情况代替用户的明确要求：

- 任务复杂、涉及整个仓库、修改多个文件或运行时间较长；
- Agent 判断 Claude Code 更合适，或用户让 Agent 自行选择工具；
- Claude Code 已安装、已认证，或用户以前使用过 Claude Code。

用户没有明确要求 Claude Code/CC 时：

- 不选择或读取本 Skill；
- 不主动推荐 Claude Code，也不询问是否启用；
- 不执行 Claude Code preflight，不启动或控制 Claude Code；
- 使用正常的 myHermes File、Terminal、Process 等现有能力完成任务。

```text
用户明确要求 Claude Code / CC
→ skill_view
→ 阅读正文及当前任务需要的 references
→ 执行安装、认证、cwd、工具、所选 transport 和活跃会话 preflight
→ 全部通过后才进入后续启动或控制流程
```

用户明确启用只授权选择并读取本 Skill，不会扩大任务范围、文件权限或危险操作授权。读取后，按 [prerequisites.md](references/prerequisites.md) 完成 preflight；任一检查失败时停止后续操作并报告具体阻断原因。不要自动安装、认证、输入凭据、终止已有会话、放宽权限、commit 或 push。

## 职责边界

通过现有 Terminal Tool 和 Process Tool 启动、观察、纠偏和结束 Claude Code。P4 受管运行模块只封装同一条 ProcessManager 公共调用链，不是新 Tool，也不拥有进程、PTY、日志或 cleanup 基础设施。

必须遵守以下边界：

- 只通过 Terminal Tool 启动进程，只通过 Process Tool 管理已注册进程。
- 保存 Terminal Tool 返回的 `process_id`；不要保存 Handle，不要按 PID 操作系统进程，不要访问 `ProcessManager` 私有字段。
- 使用 Process Tool 返回的 `next_cursor` 继续读取；不要计算、修正或另建 cursor。
- P4 runtime 只保存 `process_id`、`session_owner`、`cwd`、cursor 和时间戳组成的 `ClaudeCodeSessionRef`；ProcessManager process id 仍是唯一运行身份。
- 不建立第二套日志副本、分页器、进程 registry、reader、cleanup 或后台监控服务。
- 只管理由当前 myHermes runtime、当前 ProcessManager 和当前 owner 启动的会话；不扫描、接管、恢复、终止或清理外部终端中的 Claude Code。
- 不创建 Claude Code 专用 Tool、Agent 唤醒机制或 Claude hooks。
- 不自动安装 Claude Code，不依赖任何 `scripts/` 或 `assets/`。

## 加载指引

选择本 Skill 后、启动前读取：

- [prerequisites.md](references/prerequisites.md)：前置检查与任务约束提取。
- [mode-selection.md](references/mode-selection.md)：one-shot 与 supervised PTY 的选择。
- [p8-tool-contract.md](references/p8-tool-contract.md)：Terminal/Process 的公开契约。
- [managed-runtime.md](references/managed-runtime.md)：P4 真实后台 PTY、SessionRef、owner/cwd 互斥和基础生命周期边界。
- [permissions-and-safety.md](references/permissions-and-safety.md)：权限、凭证和危险操作边界。
- [claude-code-cli.md](references/claude-code-cli.md)：本 Skill 使用的 CLI 能力。

启动后出现输入失败、取消、停滞或终止需要时，读取 [recovery-and-cleanup.md](references/recovery-and-cleanup.md)。使用 supervised PTY 时还要读取：

- [supervision-loop.md](references/supervision-loop.md)：增量监督循环。
- [progress-state-model.md](references/progress-state-model.md)：逻辑状态与有效进展。
- [intervention-policy.md](references/intervention-policy.md)：干预条件和去重。

构造初始输入时，按任务类型使用 [implementation-task.md](templates/implementation-task.md) 或 [review-task.md](templates/review-task.md)。仅在相应条件成立时使用 [progress-request.md](templates/progress-request.md)、[corrective-instruction.md](templates/corrective-instruction.md) 或 [safe-stop.md](templates/safe-stop.md)。提交前替换全部占位符。

## 选择运行模式

### One-shot

用于目标清晰、无需中途补充要求、无需交互确认且只需要最终结果的任务。

启动前确认完整任务的 UTF-8 编码大小不超过当前 Process stdin 的 64 KiB 上限。超限或无法可靠确认符合上限时停止并报告；不要启动进程、自动分块或改用 shell 拼接。

```text
terminal(
    command="claude -p",
    background=true,
    pty=false
)
→ 保存 process_id
→ 检查 Terminal 返回的真实 status
→ status=running 时 process write 完整任务文本
→ write 明确成功后 process close
→ process log / wait
```

完整任务只通过 Process Tool 的 `data` 原样写入 pipe stdin，可以包含多行和 Git Bash 元字符；不要把任务拼入 `command`，不要手工构造 Bash 转义，也不要把任务写入 logger 或输入历史。`close` 为普通 pipe 发送真实 EOF，只能在 `write` 明确成功后调用。只要该流程能满足任务，就不要无理由启用 PTY。

### Supervised PTY

用于用户明确要求监控、需要中途纠偏、可能出现权限或问题提示、需要多轮交互，或需要长时间持续监督的任务。启动前使用安全、非交互的 `claude --ax-screen-reader --version` probe 确认参数被接受；`claude --help` 只作辅助，缺少 flag 不能单独判定不支持。

```text
terminal(
    command="claude --ax-screen-reader",
    background=true,
    pty=true
)
→ 保存 process_id
→ 等待进程为 running 且 Claude Code 可接收输入
→ process(action="submit", process_id="<process_id>", data="<initial task>")
```

`--ax-screen-reader` 用于减少装饰边框和动画。PTY 日志仍是包含 ANSI、`\r`、输入 echo、spinner 和重复重绘的追加流，不是当前屏幕快照；不要假设能可靠控制 `vim`、`top` 或复杂全屏 TUI。PTY stdin close 当前不支持；需要结束时执行安全停止流程，必要时使用 `kill`。

## 核心工作流

1. 明确目标工作目录，完整提取用户目标、验收标准和硬约束。
2. 执行前置检查并分别判断 one-shot 与 supervised PTY 能力；候选模式为 supervised PTY 时执行安全 flag probe，检查失败时不要启动。
3. 使用任务模板生成初始任务，删除所有凭证，明确允许与禁止的范围，并按 UTF-8 字节数检查本次 Process 输入不超过 64 KiB。
4. 通过 Terminal Tool 启动一次 Claude Code，保存 `process_id` 并检查返回的真实 ProcessStatus；不要仅凭取得标识就假定进程正在等待输入。
5. 仅在当前 Agent 上下文保存协议允许的最小监督状态；不要复制完整日志。
6. one-shot 模式仅在 `status=running` 时用 `write` 原样发送完整任务，成功后用 `close` 发送 EOF，再用 `log`/`wait` 收集结果；supervised PTY 模式先确认 `status=running`，再用 `submit` 发送行式初始任务并进入监督循环。
7. 每轮只读取 `next_cursor` 之后的新增输出，分别判断 ProcessStatus、Claude Code 逻辑状态和有效进展。
8. 只有干预条件成立且未命中去重规则时，发送一次最小必要指示；否则继续观察。
9. 按完成判定或恢复策略收尾，不把总结、短暂无输出或单个子任务完成误判为整体完成。
10. 最终使用 `wait` 或确认终态，读取最后一段日志，核对报告的修改和仓库状态，再向用户汇总。

## 任务约束传播

初始任务必须明确：

- 允许修改哪些文件；
- 禁止修改哪些文件；
- 是否允许运行测试；
- 是否允许新增文件；
- 是否允许修改依赖；
- 是否允许提交 Git；
- 是否允许访问网络；
- 是否允许扩大重构范围；
- 完成后的汇报格式。

始终保留“代码修改阶段与测试阶段严格分离”。如果用户要求“不要修改测试”或“不要运行测试”，必须把原意明确写入初始任务，并在监督中检查即将发生或已经发生的违反行为。

## 不可违反的运行规则

- 发送任何输入前确认公开返回值报告 `status=running`。one-shot 完整任务使用一次 `process(action="write")`；supervised PTY 的普通行式指示使用 `process(action="submit")`。
- one-shot `write` 返回 `process_input_delivery_unknown` 后不得重发任务、不得调用 `close`；先用 `poll`/`log` 检查，仍无法确认时停止自动输入并报告，必要时用 `process kill` 避免执行不完整提示。
- `close` 只用于 one-shot pipe，且只在 `write` 明确成功后调用；close 重试不得伴随第二次 `write`。supervised PTY 不使用 `close`。
- supervised 输入出现 `process_input_delivery_unknown` 时同样不得自动重发，也不得假设成功或失败；先读新增日志，仍无法确认时停止自动输入并报告。
- Process `write`/`submit` 的单次输入受 UTF-8 64 KiB 上限约束，Process Tool 是最终校验权威。one-shot 超限时不启动、不发送、不 close、不改用 shell，也不自动分块。
- supervised 的初始任务和每条后续指示都必须保持在输入上限内；只发送简短目标、具体偏差、硬约束和下一步动作，不重复完整任务、历史、巨大日志或完整文件，超限时停止并报告。
- `next_cursor` 只能采用 Process Tool 返回值。ANSI 清理、脱敏和文本理解不得改变 cursor。
- 不自动使用 `--dangerously-skip-permissions`、`bypassPermissions` 或其他权限绕过方式。
- 不自动批准高风险、范围不明、涉及凭证、工作区外路径、危险 Bash、Git push、发布或部署的操作。
- 不因思考、读文件、允许的耗时命令、短暂无输出、spinner 或暂时 `UNKNOWN` 而频繁打断。
- 相同干预原因只发送一次；发送后至少等待一次有效状态变化，才允许相关的下一次干预。
- 终止时不要通过 `write` 自行发送控制字符；在输入安全且可用时先 `submit` 一次 safe-stop，有限观察后仍需终止则调用 `process kill`，由 ProcessManager 负责中断与强制终止。

## 完成与汇报

只有以下任一条件成立才进入完成收尾：

1. Claude Code 给出明确完成总结，并且 Process 已自然退出；或
2. Claude Code 明确等待下一任务，当前任务的修改与获准检查已经完成，并且上层 Agent 决定安全结束会话。

完成后说明 Claude Code 做了什么、是否遵守约束、是否有未完成事项或风险，以及是否需要独立测试阶段。不要声称执行了未获准或未实际执行的测试。
