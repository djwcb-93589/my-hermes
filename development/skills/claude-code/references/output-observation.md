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

- `normalizer.py`：保存有界 ANSI/重绘/跨 chunk 解释状态，不识别业务状态。
- `detector.py`：集中组合文本、提示结构、任务送达事实、ProcessStatus 和 exit code，生成事件、ActionRequired 与状态。
- `snapshot.py`：组合本轮结果，不读取进程、不等待也不输入。
- `runtime.py`：保留原始 `read`，新增一次性 `observe` 并记录不含正文的成功 submit/interrupt 事实。
- `process_port.py`、ProcessManager、LocalBackend 与 PTY 的 cursor、reader、ownership 和 cleanup 职责不变。

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

`ClaudeCodeActionRequired` 包含 `kind`、安全摘要、脱敏 prompt、有限 options、risk 和原始 cursor。`kind` 支持 `clarification`、`approval`、`authentication`、`destructive_action`、`external_access`、`unknown_prompt` 和 `stalled`；P5 只报告，不执行。

`ClaudeCodeSnapshot` 包含更新后的 SessionRef、状态、本轮新事件、当前 ActionRequired、原始 cursor、有界规范化输出、ProcessStatus、exit code 和最近活动时间。它不包含输入历史、凭据、Handle、PID 或完整原始日志。

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
action options: 8
```

命中上限时截断旧内容并在输出或事件 metadata 中标记；不得无限保存历史。此类滚动淘汰只移除当前视图中已经处理的最早内容，后续新输出仍持续增量读取和分析，不会因累计输出量停止会话。重复 spinner、相同重绘、相同事件和相同 ActionRequired 通过最近规范化签名、事件 fingerprint 与 ActionRequired fingerprint 去重。Prompt、选项、错误原因、完成信号和进程退出发生实质变化时仍生成新事件。

## gap、错误与安全降级

原始 cursor 始终只采用 ProcessManager 返回值，规范化字符位置从不写回。只有请求 cursor 已落后于 ProcessManager 当前可用窗口、`output_truncated` 表明未读取的原始区间丢失，或原始 `read` 绕过了分析上下文时，`observe` 才生成 `CURSOR_GAP`、保留新的合法 cursor、清除不完整语义证据并返回 `UNKNOWN`；规范化和 Detector 的正常滚动淘汰不构成 cursor gap。在新的明确任务边界前，不得猜补缺失内容或据此分类审批、认证与完成。

读取或状态查询失败生成 `READ_ERROR`，保留 SessionRef，并根据仍可确认的生命周期事实返回 `UNKNOWN`、`LOST` 或 `FAILED`；P5 不自动重试或 kill。终态观察只分析本次可读剩余页并生成 `PROCESS_EXIT`，不执行 final-drain 循环。

所有输出在 Process Port 和 P5 组合上下文再次脱敏。Event、Snapshot、ActionRequired 与 fingerprint 不保存 Token、password、Authorization header、OAuth/verification/device code 或用户输入凭据。认证提示只能产生 `authentication`，不能自动填写或批准。任何无法可靠分类的交互提示必须返回：

```text
UNKNOWN
+ ActionRequired(kind="unknown_prompt")
```

连续轮询、deadline、停滞计数、终态 drain/retry 和工作流收敛由独立的 P6 [workflow-controller.md](workflow-controller.md) 承担；Detector 与 `observe` 仍不循环、不自动响应，也不执行审批决策。
