# P5 输出观察

P5 在现有受管进程之上解释一页新增输出：

```text
ProcessManager 绝对 cursor 日志
→ ClaudeCodeOutputNormalizer
→ ClaudeCodeOutputDetector
→ ClaudeCodeSnapshot
```

该能力不注册新 Tool。Agent 面向工具时仍调用 Terminal/Process；可信生产调用方可以对由当前 Runtime 启动的 SessionRef 调用一次 `observe`。不得把 `observe` 包装成自动循环，也不得据此自动回答、批准或终止。

## 模块与状态

- `normalizer.py`：保存有界 ANSI/重绘/跨 chunk 解释状态，不识别业务状态；P7.1 对同一增量页并行生成脱敏安全视图和仅供当前交互提取的原生视图。
- `detector.py`：始终以安全视图组合文本、提示结构、任务送达事实、ProcessStatus 和 exit code，生成事件、公开 ActionRequired 与状态；仅在已有安全 Action 后提取临时原生交互视图。
- `snapshot.py`：组合本轮公开结果，不读取进程、不等待也不输入；它不保存原生交互文本。
- `runtime.py`：保留原始 `read`，新增一次性 `observe` 并记录不含正文的成功 submit/interrupt 事实。
- `process_port.py` 的公开 `output` 继续脱敏；同次读取的原生副本只通过私有 `observe` 调用链短暂传给交互视图，不属于公开日志对象，也不改变 ProcessManager、LocalBackend 或 PTY 的 cursor、reader、ownership 和 cleanup 职责。

P5 状态为：

```text
STARTING READY WORKING WAITING_INPUT WAITING_APPROVAL
COMPLETED FAILED INTERRUPTED LOST UNKNOWN
```

`READY` 需要活跃进程与可信接收任务证据；`WORKING` 需要任务已送达和进度证据；普通明确问题进入 `WAITING_INPUT`；权限、认证、破坏性或外部访问提示进入 `WAITING_APPROVAL`。只有退出码为 0、进程已退出且存在完成证据时才使用 `COMPLETED`。静默、总结、spinner 消失或仍存活的进程都不能单独完成。

`INTERRUPTED` 还需要 Runtime 记录过明确送达的 interrupt，且终态/退出码与中断一致。ProcessManager 记录或所有权无法确认时使用 `LOST`。其他证据不足情形使用 `UNKNOWN`。

## 结构化结果

`ClaudeCodeEvent` 只包含事件类型、process id、原始 cursor 区间、时间戳、脱敏文本和小型 metadata。事件类型包括：

```text
OUTPUT PROGRESS QUESTION APPROVAL_REQUEST AUTH_REQUIRED
COMPLETION_SIGNAL FAILURE_SIGNAL PROCESS_EXIT READ_ERROR
CURSOR_GAP UNKNOWN_PROMPT
```

公开 `ClaudeCodeActionRequired` 包含稳定 `action_id`、`process_id`、`session_owner`、`kind`、脱敏 prompt、脱敏 options、risk、原始 cursor 范围和创建时间。公开 action id 是不透明值，不直接包含原生文本；已验证原生 Prompt 的 identity 会使用仅存于当前 Detector 内存中的 HMAC 材料，因此原生可见值变化也会失效旧身份。P7.1 仅在当前有效交互对象中临时附加终端规范化但不替换密钥值的原生 prompt/options；该副本不进入公开 Snapshot、Event、归档结果或持久化。相同重绘沿用同一 action id；Prompt 或 options 实质变化才生成新身份。`kind` 支持 `clarification`、`approval`、`authentication`、`destructive_action`、`external_access`、`unknown_prompt` 和 `stalled`；其中 `stalled` 是 P6 Controller 合成状态，不是原生 Claude Code Prompt。

`ClaudeCodeSnapshot` 包含更新后的 SessionRef、状态、本轮新事件、当前安全 ActionRequired、原始 cursor、有界脱敏规范化输出、ProcessStatus、exit code 和最近活动时间。它不包含输入历史、凭据、临时原生 Prompt、Handle、PID 或完整原始日志。

## 规范化与预算

规范化器跨 read 保留当前行、cursor column、未完成 escape、UTF-8 增量 decoder、最近渲染签名和有界历史。它处理 SGR、常见水平 cursor movement、erase line/display、`\r` 覆写、`\b`、tab、OSC、重复绘制、无换行进度和跨 chunk prompt；垂直全屏移动只作为重绘信号，不伪造复杂 TUI 屏幕。

每个受管 Session 的硬上限为：

```text
raw tail: 32768 chars
normalized output: 32768 chars
current line: 4096 chars
single event text: 2048 chars
pending escape: 256 chars
recent event fingerprints: 128
current action prompt/options: bounded by the current normalized input view
temporary native interaction prompt: 8192 chars
temporary native interaction options: 64
```

命中上限时截断旧内容并在输出或事件 metadata 中标记；不得无限保存历史。此类滚动淘汰只移除当前视图中已经处理的最早内容，后续新输出仍持续增量读取和分析，不会因累计输出量停止会话。公开 ActionRequired 的 Prompt 和 options 来自脱敏有界视图；临时原生 Prompt/options 只在当前交互存活期间保留，分别受 8192 字符和 64 项限制，不另设审批过滤。未形成有效交互的原生解析上下文也受同一字符上限和短观察期限制；无法安全映射时直接不冒泡当前原生交互。重复 spinner、相同重绘、相同事件和相同 ActionRequired 通过最近规范化签名、事件 fingerprint 与 ActionRequired fingerprint 去重。Prompt、选项、错误原因、完成信号和进程退出发生实质变化时仍生成新事件。

## gap、错误与安全降级

原始 cursor 始终只采用 ProcessManager 返回值，规范化字符位置从不写回。只有请求 cursor 已落后于 ProcessManager 当前可用窗口、`output_truncated` 表明未读取的原始区间丢失，或原始 `read` 绕过了分析上下文时，`observe` 才生成 `CURSOR_GAP`、保留新的合法 cursor、清除不完整语义证据并返回 `UNKNOWN`；该页对外输出可以使用固定占位而不暴露无法确认的 PTY 文本。规范化和 Detector 的正常滚动淘汰不构成 cursor gap。在新的明确任务边界前，不得猜补缺失内容或据此分类审批、认证与完成。

读取或状态查询失败生成 `READ_ERROR`，保留 SessionRef，并根据仍可确认的生命周期事实返回 `UNKNOWN`、`LOST` 或 `FAILED`；P5 不自动重试或 kill。终态观察只分析本次可读剩余页并生成 `PROCESS_EXIT`，不执行 final-drain 循环。

所有普通输出在 Process Port 和 P5 组合上下文再次脱敏。Event、Snapshot、公开 ActionRequired 与 fingerprint 不保存 Token、password、Authorization header、OAuth/verification/device code 或用户输入凭据。只有当前临时原生交互视图可以保留 Claude Code 自己显示的密钥值，并在回复、替换、任何成功或未知送达的新输入、interrupt、终态或安全降级时删除。已匹配或可疑的 PTY 输入 echo 不作为语义证据，也不对外暴露输入正文；其行掩码同时应用于原生视图，绝不作为 Claude Code Prompt 冒泡，其 Event 和 Snapshot 仅使用固定占位标记。安全/原生双视图无法可靠映射，或原生视图缺失时，`current_interaction()` 返回无交互，不回退或拼接历史原始 PTY。认证提示产生 `authentication`，但不会自动填写或批准；用户明确提供的认证回复只在 P7 Controller 的单次提交调用中短暂存在。任何无法可靠分类的交互提示必须返回：

```text
UNKNOWN
+ ActionRequired(kind="unknown_prompt")
```

连续轮询、deadline、停滞计数、终态 drain/retry 和工作流收敛由独立的 P6 [workflow-controller.md](workflow-controller.md) 承担；Detector 与 `observe` 仍不循环、不自动响应，也不执行审批决策。
