# memory 模块开发指南

本指南面向后续升级 memory 模块的协同开发者。只描述当前实际实现、对外接口、以及加新功能时需要改哪些文件。

## 模块定位

memory 模块负责 Agent 的长期持久记忆。它把用户偏好、长期事实存成纯文本文件，在每次构建系统提示词时注入，让 Agent 跨会话记住事情。

memory 模块**不负责**：会话内短期记忆（那是对话历史的事）、向量检索（当前未实现）、跨用户共享（当前未实现）。

## 当前已实现的功能

### 两类记忆

| 类型 | 文件路径 | 用途 | 字符上限 |
|---|---|---|---|
| 长期记忆 | `~/.hermes/memories/MEMORY.md` | Agent 跨会话需要记住的事实 | `memory_char_limit`（默认 4000） |
| 用户档案 | `~/.hermes/memories/USER.md` | 用户身份、偏好、角色 | `user_char_limit`（默认 2000） |

路径中的 `~/.hermes` 是 `HERMES_HOME`，由 `hermes.config` 决定，可通过环境变量 `HERMES_HOME` 覆盖。

### 存储格式

两个文件都是纯文本，条目之间用 `§`（两个换行 + § + 两个换行）分隔：

```text
第一条记忆内容

§

第二条记忆内容
```

选 `§` 作为分隔符是因为它不会出现在正常记忆文本里，且不是正则元字符。

### 读写语义

所有写操作走"文件锁 + 原子替换"完整事务：

```text
获取文件锁 -> 重读最新内容 -> 校验 -> 写临时文件 -> fsync -> os.replace -> 释放锁
```

校验失败或写入失败时，旧文件保持不变。读操作不加锁（写走原子替换，读旧版或新版都可接受）。

### 4 种动作

通过 `handle_memory(args)` 入口分发：

| action | 必填参数 | 行为 |
|---|---|---|
| `read` | 无 | 返回所有条目 + 容量信息 |
| `add` | `content` | 追加一条；strip 后与现有条目重复则拒绝 |
| `remove` | `content` | 子串匹配删除；匹配多条则拒绝并返回候选 |
| `replace` | `old_text` + `content` | 子串定位后替换为新内容；重复检测排除被替换项 |

`remove` 和 `replace` 的匹配是大小写不敏感的子串匹配。匹配多条时返回 `ambiguous_match` 错误，并把前 5 条候选放进 `matches` 字段，让调用方补更具体的匹配文本。

### 安全扫描

写入前对内容做轻量检查，命中则拒绝：

- 不可见 Unicode 控制字符（零宽空格、双向控制、BOM 等）
- prompt injection 模式（"ignore previous instructions" 等）
- 凭据泄漏模式（api_key、secret_key、access_token、bearer_token、private_key、password）

这是浅层防御，不是完整安全系统。不要往里加复杂规则，复杂规则属于审批策略层。

### 提示词注入

`render_memory_section()` 是 memory 模块对提示词组装层的唯一公开接口。它返回拼好的纯文本段落，无内容时返回 `None`：

```text
# Memory (3 entries, 1200/4000 chars)
第一条记忆

§

第二条记忆

§

第三条记忆

# User Profile (2 entries, 80/2000 chars)
用户信息
```

调用方不感知文件路径、分隔符、字符限额。

## 对外接口

模块对外只暴露 3 个层次，调用方按需选择：

### 1. 工具入口（给模型用）

```python
from hermes.tools.memory import handle_memory
# 返回 JSON 字符串，给模型作工具结果
result_json = handle_memory({"action": "read", "target": "memory"})
```

模型通过 `memory` 工具调用，参数 schema 见 `register()`。返回值是 JSON 字符串，不是字典。

### 2. 渲染接口（给提示词组装用）

```python
from hermes.tools.memory import render_memory_section
# 返回拼好的纯文本段落，无内容返回 None
section = render_memory_section(include_long=True, include_user=True)
if section is not None:
    parts.append(section)
```

`hermes/prompt.py` 用这个接口。调用方不感知存储细节。

### 3. 程序级读写（给需要直接操作记忆的外部模块用）

供需要不经模型工具直接读写记忆的外部模块使用（如会话结束自动压缩、外部脚本批量写入）。返回结构化字典，不是 JSON 字符串，调用方不需要 `json.loads`。

**读取**：

```python
from hermes.tools.memory import read_memory_entries

result = read_memory_entries("memory")  # 或 "user"
if result["ok"]:
    entries = result["entries"]         # list[str]
    count = result["entry_count"]
    used = result["used_chars"]
    limit = result["limit_chars"]
```

**写入**（add / remove / replace）：

```python
from hermes.tools.memory import mutate_memory_entries

# 新增一条
r = mutate_memory_entries("add", target="memory", content="新条目内容")
# 删除匹配子串的唯一条目
r = mutate_memory_entries("remove", target="memory", content="要删除的子串")
# 替换：用 old_text 定位，换成 content
r = mutate_memory_entries("replace", target="memory",
                          old_text="旧内容子串", content="新内容")

if r["ok"]:
    print(r["action"], r["entry_count"], r["size"])
else:
    print(r["error_type"], r["error"])
    # error_type: invalid_target / unknown_action / invalid_args /
    #             invalid_content / blocked_content / duplicate /
    #             no_match / ambiguous_match / limit_exceeded /
    #             lock_timeout / io_error
```

**参数语义与 `handle_memory` 完全一致**：`content` 在 add 时是新文本、remove 时是子串、replace 时是新文本；`old_text` 仅 replace 需要。返回字典的键名也与 `handle_memory` 的 JSON 返回一致。

**复用同一套校验和锁逻辑**：分隔符拦截、安全扫描、重复检测、子串匹配规则、文件锁、原子写入，都与工具入口走同一条路径（共享 `_build_mutate` 和 `_do_write` 内部函数）。程序级接口不会绕过任何校验，也不会产生工具入口看不到的状态。

**何时用程序级接口 vs 工具入口**：

| 场景 | 用哪个 |
|---|---|
| 模型在对话中主动记忆 | `handle_memory`（工具入口，返回 JSON 给模型） |
| 会话结束自动压缩摘要写入 | `mutate_memory_entries`（程序级，返回字典给代码） |
| 外部脚本批量导入记忆 | `mutate_memory_entries` |
| 提示词组装读记忆 | `render_memory_section`（渲染接口，返回纯文本） |
| 代码里读记忆做判断 | `read_memory_entries`（程序级，返回字典） |

**不要直接用 `load_memory` / `render_entries` / `atomic_write_text` 这些原语拼写入逻辑**。这些是内部实现细节，未来可能调整。如果程序级接口不够用（比如需要批量原子写入多条），应该在 memory 模块里加新的程序级接口，而不是让外部模块自己拼锁。

## 配置

`config.yaml` 的 `memory` 段：

```yaml
memory:
  memory_char_limit: 4000    # MEMORY.md 最大字符数
  user_char_limit: 2000      # USER.md 最大字符数
```

`hermes/config.py` 加载为 `MEMORY_CHAR_LIMIT` / `USER_CHAR_LIMIT` 常量。改限额不需要改代码，改配置即可。

## 文件清单

| 文件 | 职责 |
|---|---|
| `hermes/tools/memory.py` | 全部 memory 逻辑：存储原语、工具入口、渲染接口、程序级读写接口 |
| `hermes/_io_utils.py` | 公共文件锁 + 原子写入（与 skill、skill_security 共用） |
| `hermes/config.py:470-471` | 读取 `memory_char_limit` / `user_char_limit` |
| `hermes/prompt.py:9,91-97` | 调用 `render_memory_section` 注入提示词 |
| `~/.hermes/memories/MEMORY.md` | 长期记忆存储文件 |
| `~/.hermes/memories/USER.md` | 用户档案存储文件 |

## 后续升级如何接入

### 场景 A：加一种新的记忆类型（例如"短期记忆"）

假设要加一种只属于当前会话、会话结束就清空的短期记忆。

**需要改的文件**：

1. **`hermes/tools/memory.py`**
   - 新增 `SHORTTERM_FILE = MEMORY_DIR / "SHORTTERM.md"` 常量
   - 在 `_resolve_target` 里加 `"short"` 分支，绑定到 `SHORTTERM_FILE` 和对应的 char_limit（这一处改动同时让 `handle_memory`、`read_memory_entries`、`mutate_memory_entries` 三个接口都支持新 target）
   - 在 `register()` 的 schema 里把 `target` enum 改成 `["memory", "user", "short"]`
   - 在 `render_memory_section` 加 `include_short: bool = False` 参数，内部调 `_render_single_section` 渲染新文件

2. **`hermes/config.py`**
   - 加 `SHORTTERM_CHAR_LIMIT = _config["memory"]["short_char_limit"]`

3. **`config.yaml`**
   - `memory` 段加 `short_char_limit: 2000`

4. **`hermes/prompt.py`**（可选）
   - 如果要让短期记忆默认注入，在 `build_system_prompt` 加 `include_short_memory` 参数，传给 `render_memory_section`
   - 如果只在特定会话类型注入，改 `config.yaml` 的 `gateway.context.*` 段加 `include_short_memory` 开关，再改 `hermes/gateway/runner.py` 的 context 表（见 `runner.py:144-169`）

**不需要改的文件**：`_io_utils.py`、`skill_security.py`、`skill.py`、审批策略、agent_loop、conversation、gateway 投递链路。

### 场景 B：加记忆自动压缩（会话结束时把短期记忆压缩成长期记忆）

**关键约束：不要改 `agent_loop.py`**。`AgentLoop` 是主会话、子 Agent（delegate）、Cron 任务共用的公共骨架。在它里面加"会话结束钩子"会让 delegate 子 Agent 和 Cron 任务也触发压缩--delegate 子 Agent 明确被禁止写 memory（`delegate.py:51` 的系统提示"Do not call delegate_task, memory, skill_manage, or cron"，且 `DELEGATE_BLOCKED_TOOLS` 会拦截 memory 工具），Cron 任务则可能并发写同一份 memory 产生竞态。正确做法是在**会话生命周期入口**加钩子，不是在 AgentLoop 里加。

**需要改的文件**：

1. **`hermes/tools/memory.py`**
   - 新增 `compress_short_to_long(*, summarizer)` 函数。内部用程序级接口：`read_memory_entries("short")` 读短期记忆，`summarizer(entries)` 回调生成摘要，`mutate_memory_entries("add", target="memory", content=摘要)` 写入长期记忆。`summarizer` 由调用方传入，避免 memory 模块直接依赖模型客户端。
   - 压缩函数本身不调模型、不开数据库连接、不感知会话身份。它只做"读短期 -> 调回调 -> 写长期"。

2. **`hermes/conversation.py`** 的 `run_conversation` / `run_conversation_async`（CLI 和 Gateway 主会话入口）
   - 这两个函数是主会话的顶层入口，只在主会话被调用，delegate 和 cron 不走这里。在函数返回前（正常返回或异常返回）调 `compress_short_to_long`。
   - `summarizer` 回调由这两个入口传入，内部调主模型客户端。
   - 用 try/finally 保证异常路径也触发压缩。

3. **`hermes/gateway/runner.py`**（如果 gateway 会话的 idle timeout 也要触发压缩）
   - 在 session idle timeout 的清理逻辑里调 `compress_short_to_long`。Gateway 的 session 清理不走 `run_conversation_async`，是独立路径。
   - 同样传入 `summarizer` 回调。

**不需要改的文件**：
- `hermes/agent_loop.py` -- 共享骨架，不能加会话生命周期钩子
- `hermes/tools/delegate.py` -- 子 Agent 不应触发主会话的压缩
- `hermes/cron/executor.py` -- Cron 任务的会话身份和主会话不同，不应共享压缩逻辑；如果 Cron 需要压缩，单独设计
- `hermes/prompt.py` -- 压缩是写操作，不涉及提示词注入
- `hermes/_io_utils.py`、`hermes/skill*.py`

**判断"钩子加在哪里"的规则**：问自己"这个钩子会在哪些入口触发"。如果答案是"只主会话"，加在 `run_conversation` / `run_conversation_async`；如果答案是"所有 AgentLoop 子类"，才考虑加在 `AgentLoop`。memory 压缩只属于主会话。

### 场景 C：加向量检索（语义记忆）

**关键约束：`build_system_prompt` 当前不知道 user input**。prompt 组装发生在模型调用之前，此时 user input 还没进入 messages。如果把 user input 传进 prompt 组装，会改变 `build_system_prompt` 的签名，影响所有调用方（CLI、Gateway、Cron、Delegate）。而且 `AgentLoop` 的 messages 是 `[{"role":"user","content":...}]`，prompt 是单独的 `system` 消息，两者在 `call_model` 才拼合。正确做法是让检索发生在 prompt 组装内部，且 query 来源不依赖调用方传 user input。

**需要改的文件**：

1. **`hermes/tools/memory.py`**
   - `render_memory_section` 的内部从"读文件"改成"按 query 检索相关片段"。但 **query 不能来自函数参数**（否则 `prompt.py` 就要知道 user input）。两个替代方案：
     - **方案 A（按会话主题检索）**：`render_memory_section` 接收一个可选的 `session_topic: str | None`（会话的稳定主题，不是单条 user input），按主题检索。主题来源在 `prompt.py` 里从 `SessionContext` 取，不从单条消息取。
     - **方案 B（全量注入 + 按相关性排序截断）**：不检索，把所有记忆按"最近使用"排序，截断到字符限额。这是当前行为的小改，不需要 query。
   - 新增向量存储初始化、embedding 调用、检索逻辑（如果走方案 A）。
   - 写入路径也要改：`mutate_memory_entries("add", ...)` 时计算 embedding 并存入向量存储。

2. **`hermes/prompt.py`**
   - 如果走方案 A，`build_system_prompt` 加 `session_topic: str | None = None` 参数，传给 `render_memory_section`。**这个参数对所有调用方都是可选的**，不传就退化成当前行为（全量注入），不破坏 CLI/Gateway/Cron/Delegate 现有调用。
   - 如果走方案 B，`prompt.py` 零改动。

3. **`hermes/config.py`** + **`config.yaml`**
   - 加 embedding 模型配置、向量存储路径配置。

4. **`pyproject.toml`**
   - 加 embedding 依赖（如 `sentence-transformers` 或调外部 API 的 SDK）。

**不需要改的文件**：
- `hermes/agent_loop.py` -- 不要为了把 user input 传给 prompt 而改 AgentLoop。AgentLoop 的 `system_prompt` 在 `__init__` 时就固定了（见 `agent_loop.py:634`），每轮对话不会重组 prompt。如果要"每轮按当前 user input 检索"，那是 AgentLoop 的行为变更，会影响 delegate 和 cron，必须单独评估。
- `hermes/tools/delegate.py` -- delegate 子 Agent 不应该看到主会话的长期记忆检索结果（隔离原则）。
- `hermes/_io_utils.py`、`hermes/skill*.py`
- 工具入口 `handle_memory`（写操作还是写纯文本，检索是读侧的事）

**判断"要不要改 AgentLoop"的规则**：问自己"改了之后 delegate 子 Agent 和 Cron 任务的行为会不会变"。如果会变，且这个变不是你有意为之的，就不要改 AgentLoop，改更上层的入口。

### 场景 D：加新的安全扫描规则

**需要改的文件**：

1. **`hermes/tools/memory.py:163-176`**（`_DANGEROUS_PATTERNS` 列表）
   - 加一条 `re.compile(...)` 即可。

**不需要改的文件**：其它任何文件。安全扫描是 memory 模块内部逻辑，不影响接口。

## 接入新功能时的检查清单

加完功能后，按这个清单确认边界没破：

- [ ] `hermes/prompt.py` 是否需要改？如果新功能只是"多一种记忆类型"，prompt.py 应该零改动（只在 `render_memory_section` 内部加分支）。如果 prompt.py 被迫改了，说明渲染接口抽象不够，应该先扩 `render_memory_section` 的签名。
- [ ] `hermes/cron/executor.py` 是否需要改？cron 只调 `load_skill_body`，不碰 memory。如果 cron 被迫改了，说明记忆注入走了 cron 不该知道的路径。
- [ ] `hermes/_io_utils.py` 是否需要改？加新记忆类型不需要动公共 io 工具。如果改了，说明三个模块（memory/skill/skill_security）有新的共享 io 需求，应该先确认是通用需求还是 memory 特有需求。
- [ ] 配置项是否加在 `config.yaml` 的 `memory` 段？不要散落到其它段。
- [ ] 新接口的返回值是字典还是 JSON 字符串？给程序用的返回字典，给模型用的返回 JSON 字符串。不要让程序调用方 `json.loads` 工具结果。
- [ ] 新记忆类型是否三个接口都支持？`_resolve_target` 是 target 解析的单一入口，改这一处应该让 `handle_memory`、`read_memory_entries`、`mutate_memory_entries` 同时支持新 target。如果某个接口需要单独改 target 解析，说明解析逻辑没收敛到 `_resolve_target`。
- [ ] 外部模块写记忆是否走了 `mutate_memory_entries`？如果看到外部模块自己拼 `load_memory` + `atomic_write_text`，说明程序级接口不够用或没被使用，应该补接口而不是放任原语泄漏。
- [ ] 是否改了 `hermes/agent_loop.py`？如果改了，确认这个改动对 delegate 子 Agent（`DelegateAgentLoop`）和 Cron 任务（`ConversationAgentLoop` 在 cron executor 里复用）也是有意为之的。如果不是，把钩子挪到 `run_conversation` / `run_conversation_async` 或 `gateway/runner.py` 的 session 生命周期路径。

## 常见陷阱

1. **不要在 memory.py 里直接 `import hermes.prompt`**。prompt 层在 memory 层之上，反向 import 会产生循环依赖。
2. **不要绕过 `file_lock` 直接写记忆文件**。原子写入保证不了跨进程安全，必须先拿锁。
3. **不要在 `_DANGEROUS_PATTERNS` 里加复杂规则**。复杂安全规则属于 `hermes/approval_policy.py` 的审批策略层，memory 只做浅层防御。
4. **不要改 `§` 分隔符**。已有记忆文件用 `§` 存储了，改分隔符会让旧文件解析出错。如果必须改，要同时写迁移逻辑。
5. **`handle_memory` 返回 JSON 字符串，不是字典**。模型工具调用走 `handle_memory`；程序内部读写走 `read_memory_entries` / `mutate_memory_entries`（返回字典）；提示词注入走 `render_memory_section`（返回纯文本）。不要混用。
6. **不要直接用 `load_memory` / `render_entries` / `atomic_write_text` 这些原语在外部模块拼写入逻辑**。这些是内部实现细节。程序级写记忆一律走 `mutate_memory_entries`，它复用工具入口的全部校验和锁逻辑。如果程序级接口不够用，在 memory 模块里加新接口，而不是让外部模块自己拼。
7. **不要为了加 memory 功能而改 `hermes/agent_loop.py`**。`AgentLoop` 是主会话、delegate 子 Agent、Cron 任务的共享骨架。在它里面加"会话结束钩子""每轮注入钩子"会让 delegate 和 cron 也触发，破坏隔离。会话生命周期相关的钩子加在 `run_conversation` / `run_conversation_async`（主会话入口）或 `gateway/runner.py` 的 session 清理路径，不要加在 `AgentLoop`。判断规则：改之前问自己"delegate 子 Agent 和 Cron 任务会不会也被触发"，如果会且不是你有意的，就改更上层。
