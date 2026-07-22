# skill 模块开发指南

本指南面向后续升级 skill 模块的协同开发者。只描述当前实际实现、对外接口、以及加新功能时需要改哪些文件。

## 模块定位

skill 模块负责管理本地技能（skill）。一个 skill 是 `~/.hermes/skills/<name>/SKILL.md` 文件，包含 YAML frontmatter（元数据）+ Markdown 正文（技能内容）。Agent 可以查看、列出、创建、编辑、删除技能。

skill 模块**不负责**：技能内容的执行（当前未实现脚本执行）、技能市场或远程同步（当前未实现）、跨用户共享（当前未实现）。

## 当前已实现的功能

### skill 文件格式

每个 skill 是一个目录下的 `SKILL.md`：

```text
~/.hermes/skills/
├── my-skill/
│   └── SKILL.md
└── another-skill/
    └── SKILL.md
```

`SKILL.md` 格式：

```markdown
---
name: my-skill
description: 一句话描述这个技能做什么
version: "1.0"
platforms: ["cli", "gateway"]
metadata:
  key: value
---

# 技能正文

这里是 Markdown 格式的技能内容...
```

frontmatter 字段白名单：`name`、`description`、`version`、`platforms`、`metadata`。其它字段会被过滤掉。

### 名称校验与路径安全

skill 名称必须匹配 `^[A-Za-z0-9_-]+$`。`_resolve_skill_dir` 做两步校验：

1. 名称通过正则
2. resolve 后必须仍在 `SKILLS_DIR` 子树内，且是直接子目录

这保证 `../`、符号链接、绝对路径、嵌套子目录都被拒绝。

### 4 个工具入口

通过 `register()` 注册到工具注册表：

| 工具名 | toolset | 动作 | 风险等级 | 执行环境 |
|---|---|---|---|---|
| `skill_view` | `skill_read` | 按 name 加载完整正文 + 风险报告 + 信任状态 | low | cli/gateway/cron/delegate |
| `skills_list` | `skill_read` | 列出所有 skill 摘要 | low | cli/gateway/cron/delegate |
| `skill_manage` | `skill_manage` | create / edit / delete / patch | medium | cli/gateway/cron |

`skill_manage` 的 4 个子动作：

- `create`：新建 skill，已存在则拒绝
- `edit`：全量替换正文，保留 frontmatter 可选字段
- `delete`：删除整个 skill 目录
- `patch`：用 `old_text` 定位唯一子串，替换为 `new_text`（类似 search-and-replace）

### 无人值守风险拦截

`handle_skill_view` 接收 `interactive_approval` 参数。当 `interactive_approval=False`（Gateway/Cron 等无人值守场景）时：

- `high` 风险 skill：直接拒绝，返回 `safety_blocked`
- `medium` 风险且未受信任：拒绝，返回 `permission_denied` + `requires_confirmation`
- `low` 风险或已受信任：正常返回正文

CLI 默认 `interactive_approval=True`，完整展示正文。

### 内容风险扫描

`hermes/skill_security.py` 的 `scan_skill_content(text)` 扫描正文，返回 `SkillRiskReport`，包含 `risk_level`（none/low/medium/high）和 `findings` 列表。

扫描项：

- **指令劫持**：`ignore previous instructions`、`disregard system prompt` 等（high）
- **凭据访问**：`.env`、`~/.ssh`、`id_rsa`、`credentials.json`、`private key` 等（medium 如果是指令性，low 如果是引用）
- **环境变量访问**：`os.environ`、`env`、`printenv` 指令（medium）
- **网络工具**：`curl`、`wget`（medium）
- **本地文件路径**：绝对路径、相对路径引用（medium 如果是指令性）
- **隐藏指令**：HTML 注释里的中高风险指令（medium/high）
- **不可见字符**：零宽空格、双向控制字符（low/medium）

扫描器只报告，不修改正文。

### 信任记录

`hermes/skill_security.py` 维护 `~/.hermes/trusted_skills.json`，记录每个 skill 的 SHA-256 内容摘要和信任时间。

- `get_skill_trust_state(name, content)`：判断当前内容版本是否受信任，以及是否存在过期信任（内容变了但旧信任还在）
- `trust_skill_content(name, content)`：把当前内容版本写入信任记录

信任记录只存摘要，不存正文。skill 内容变化后，旧信任标记为 `trust_stale=True`，需要重新确认。

### 提示词注入

`render_skills_section()` 是 skill 模块对提示词组装层的唯一公开接口。返回拼好的纯文本段落，无 skill 时返回 `None`：

```text
# Available Skills
- **my-skill**: 一句话描述
- **another-skill**: 另一句描述
```

调用方不感知字段名、渲染格式。

## 对外接口

模块对外暴露 3 个层次：

### 1. 工具入口（给模型用）

```python
from hermes.tools.skill import handle_skill_view, handle_skill_list, handle_skill_manage
# 返回 JSON 字符串，给模型作工具结果
result_json = handle_skill_view({"name": "my-skill"})
result_json = handle_skill_list({})
result_json = handle_skill_manage({"action": "create", "name": "new", "body": "..."})
```

`handle_skill_view` 支持关键字参数 `interactive_approval`（默认 True）。Gateway/Cron 调用时传 `False`。

### 2. 渲染接口（给提示词组装用）

```python
from hermes.tools.skill import render_skills_section
section = render_skills_section()
if section is not None:
    parts.append(section)
```

`hermes/prompt.py` 用这个接口。调用方不感知 skill 列表字段名。

### 3. 程序级加载接口（给需要 skill 正文的外部模块用）

```python
from hermes.tools.skill import load_skill_body
# 返回结构化字典，不是 JSON 字符串
payload = load_skill_body("my-skill")
if payload["ok"]:
    body = payload["body"]       # Markdown 正文
    name = payload["name"]       # frontmatter 里的 name
    risk = payload["risk"]       # {"level": "low", "findings": [...]}
else:
    err = payload["error_type"]  # "not_found" / "invalid_name" / "parse_error"
```

`hermes/cron/executor.py` 用这个接口预加载任务允许的 skill。返回字典，不需要 `json.loads`。

`load_skill_body` 和 `handle_skill_view` 共享同一个内部函数 `_load_skill_payload`，所以返回的字段完全一致。区别只是 `handle_skill_view` 在外面套了一层 JSON 序列化和风险拦截，`load_skill_body` 直接返回字典且不做风险拦截（调用方自己决定怎么用 `risk` 字段）。

### 4. 程序级写接口（当前未实现）

skill 模块**当前没有程序级写接口**。`handle_skill_manage` 返回 JSON 字符串，是给模型用的工具入口。当前没有外部模块需要程序级创建/编辑/删除 skill，所以这个接口缺位是故意的（遵循 YAGNI），不是遗漏。

**当前没有需求时不要预先实现**。但如果有新功能需要程序级写 skill（典型场景：skill 导入、批量版本回滚、外部脚本批量创建），**不要直接在外部模块拼 `_render_skill` + `atomic_write_text` + `file_lock` 这些原语**。这些原语绕过了 `handle_skill_manage` 的名称校验、锁内二次检查、frontmatter 渲染逻辑，容易产生不一致状态。

**正确的接入路径**（等有需求时做）：

1. 把 `handle_skill_manage` 的 4 个 `_do_*` 内部函数（`_do_create` / `_do_edit` / `_do_delete` / `_do_patch`）的核心逻辑抽成返回字典的版本，例如 `create_skill(name, *, body, description=None, ...)` -> `dict`。抽的时候让 `_do_create` 改成调这个新函数再包一层 JSON 序列化，保持工具入口行为不变。
2. 新功能模块直接调这些程序级写接口，拿到字典做后续判断。

这和 memory 模块的 `mutate_memory_entries` 是同一个思路：工具入口和程序接口共享同一套校验和锁逻辑，程序接口返回字典而不是 JSON 字符串。

**什么时候应该触发这一步**：当你发现新功能需要"不经模型工具调用直接创建/编辑/删除 skill"时。在那之前，保持 `handle_skill_manage` 作为唯一写入口。

## 配置

skill 模块当前没有独立配置段。相关配置：

- `HERMES_HOME`（环境变量或 config）：决定 `skills/` 目录位置
- `gateway.context.*.include_*`：决定哪些会话类型注入 skill 列表到提示词（通过 `prompt.py` 的 `skill_enabled` 逻辑控制，见 `prompt.py:99-107`）

工具的 `execution_environments` / `default_enabled_environments` / `risk_level` / `approval_mode` 在 `register()` 里硬编码，不在配置文件里。

## 文件清单

| 文件 | 职责 |
|---|---|
| `hermes/tools/skill.py` | skill 工具入口 + 渲染接口 + 程序级加载接口 |
| `hermes/skill_security.py` | 内容风险扫描 + 信任记录 |
| `hermes/_io_utils.py` | 公共文件锁 + 原子写入（与 memory、skill_security 共用） |
| `hermes/prompt.py:10,99-107` | 调用 `render_skills_section` 注入提示词 |
| `hermes/cron/executor.py:49,164-175` | 调用 `load_skill_body` 预加载 skill |
| `hermes/tools/__init__.py:305` | 注册 skill 工具 |
| `~/.hermes/skills/<name>/SKILL.md` | skill 存储文件 |
| `~/.hermes/trusted_skills.json` | 信任记录文件 |

## 后续升级如何接入

### 场景 A：加脚本执行能力

假设要让每个 skill 可以带一个可执行脚本（如 `run.sh` 或 `run.py`），模型能查看脚本路径并调用它。

**需要改的文件**：

1. **`hermes/tools/skill.py`**
   - `_load_skill_payload`（内部加载函数）：加脚本路径检测逻辑。检查 `skill_dir` 下是否有 `run.sh` / `run.py`，有则在返回字典里加 `"script_path"` 和 `"script_type"` 字段。
   - `render_skills_section`：决定是否在列表里显示"有脚本"标记（可选，不改也行）。
   - 新增工具入口 `handle_skill_run(args)`：接收 `name` 参数，执行对应脚本。在 `register()` 里注册新工具 `skill_run`，`toolset="skill_execute"`（新 toolset），`risk_level="high"`，`approval_mode="once"`（每次执行都要审批）。
   - 新增 `_execute_skill_script(name)` 内部函数：负责实际执行，走 `hermes/backends` 的 terminal backend，受审批策略约束。

2. **`hermes/skill_security.py`**
   - `scan_skill_content` 扩展或新增 `scan_skill_script` 函数：扫描脚本内容，检测危险操作（`rm -rf`、`curl | bash` 等）。
   - 信任记录里加脚本内容摘要：`trust_skill_content` 同时记录 SKILL.md 和脚本的摘要。

3. **`hermes/approval_policy.py`**
   - 加 `assess_skill_script_execution` 函数：评估脚本执行的风险等级。可能复用 terminal 的审批逻辑，但绑定到 skill 身份。
   - 在审批黑名单里加脚本执行相关的模式（如果需要）。

4. **`hermes/tools/__init__.py`**
   - 如果新增了 `skill_execute` toolset，在 `register_all` 里确保 skill 模块注册时包含它。

5. **`config.yaml`**
   - `gateway.platforms.feishu.toolsets` 加 `skill_execute`（如果要让飞书会话能用）。
   - 可能加 `security.approval.approval_command_patterns` 的新模式，或者加专门的 `skill_execution` 配置段。

6. **`hermes/prompt.py`**（可选）
   - 如果要让模型知道哪些 skill 有脚本可执行，`render_skills_section` 的输出里加标记。但这不改 prompt.py 本身，只改 `render_skills_section` 内部。

**不需要改的文件**：`hermes/prompt.py`（只调 `render_skills_section`）、`hermes/cron/executor.py`（只调 `load_skill_body`，新字段 `script_path` 会自动透传，cron 如果要执行脚本才需要改）、`hermes/_io_utils.py`、`hermes/tools/memory.py`。

### 场景 B：加 skill 版本管理

假设要让 skill 支持多版本，模型能查看历史版本、回滚。

**需要改的文件**：

1. **`hermes/tools/skill.py`**
   - `_load_skill_payload`：返回字典加 `versions` 字段（历史版本列表）。这一处改动让 `handle_skill_view` 和 `load_skill_body` 都自动返回新字段。
   - `handle_skill_manage`：加 `rollback` 动作，接收 `version` 参数。
   - 新增 `_save_version_snapshot` 内部函数：每次 edit/patch 时把旧版本存到 `~/.hermes/skills/<name>/versions/`。
   - **如果版本管理需要被外部模块程序级调用**（比如 cron 任务里自动回滚到稳定版本）：按"程序级写接口（当前未实现）"段的接入路径，先把 `_do_edit` / `_do_patch` 的核心抽成返回字典的程序级函数，再加 `rollback_skill(name, version)` 程序级接口。如果只在模型工具调用里用，不需要抽程序级接口。

2. **`hermes/skill_security.py`**
   - 信任记录可能要扩展：每个版本一个摘要，或者只信任当前版本。

3. **`hermes/tools/skill.py` 的 `register()`**
   - `skill_manage` 的 schema 里 `action` enum 加 `rollback`，加 `version` 参数。

**不需要改的文件**：`prompt.py`、`cron/executor.py`、`_io_utils.py`、`memory.py`。

### 场景 C：加 skill 导入/导出

假设要支持从 URL 或本地文件导入 skill，或把 skill 导出为 zip。

导入是典型的"程序级写"场景：外部模块拿到 zip 文件后，需要不经模型工具调用直接创建 skill。**这个场景必须先做程序级写接口**。

**需要改的文件**：

1. **`hermes/tools/skill.py`**
   - **先按"程序级写接口（当前未实现）"段的接入路径**，把 `_do_create` 的核心抽成 `create_skill(name, *, body, description=None, ...) -> dict` 程序级接口。让 `_do_create` 改成调这个新函数再包一层 JSON 序列化。
   - 新增 `import_skill_from_zip(path) -> dict` 程序级接口：内部解压、校验、调 `create_skill` 写入。返回字典，不返回 JSON。
   - `handle_skill_manage` 加 `import` / `export` 动作：如果要让模型也能通过工具导入，再加；如果只在外部脚本用，不需要加。
   - 新增 `_export_skill_to_zip(name, path)` 内部函数。

2. **`hermes/skill_security.py`**
   - 导入时强制扫描，高风险直接拒绝。
   - 导入的 skill 默认未信任，需要用户确认。

3. **`hermes/approval_policy.py`**
   - 如果导入涉及网络下载（从 URL 导入），加 URL 白名单或审批规则。

**不需要改的文件**：`prompt.py`、`cron/executor.py`、`_io_utils.py`、`memory.py`。

**关键点**：导入功能不应该在 `handle_skill_manage` 里塞一个 `import` action 然后让外部模块 `json.loads(handle_skill_manage({"action":"import",...}))`。应该先有程序级 `import_skill_from_zip`，`handle_skill_manage` 的 `import` action（如果有）只是薄包装。

### 场景 D：加新的风险扫描规则

**需要改的文件**：

1. **`hermes/skill_security.py`**
   - 在对应的正则列表里加规则。例如加一条 SQL 注入检测，在 `_scan_lines` 里加检测逻辑。
   - 如果是新类别，加到 `SkillFinding` 的 `category` 取值里。

**不需要改的文件**：其它任何文件。风险扫描是 skill_security 内部逻辑。

### 场景 E：加 skill marketplace（远程同步）

这是大改，会影响存储层。

**需要改的文件**：

1. **`hermes/tools/skill.py`**
   - 新增 `sync` 动作：从远程拉取 skill 列表，下载到本地。
   - `_load_skill_payload` 可能要区分本地 skill 和远程 skill。

2. **新增 `hermes/skill_sync.py`**
   - 负责远程 API 调用、下载、校验签名、本地写入。

3. **`hermes/skill_security.py`**
   - 远程 skill 默认未信任，强制扫描。
   - 可能要加签名验证逻辑。

4. **`hermes/approval_policy.py`**
   - 远程同步属于网络操作，加审批规则。

5. **`hermes/config.py`** + **`config.yaml`**
   - 加 marketplace URL、认证 token、签名公钥等配置。

6. **`pyproject.toml`**
   - 加 HTTP 客户端依赖（如果 `httpx` 还没在依赖里）。

**不需要改的文件**：`prompt.py`、`cron/executor.py`、`_io_utils.py`、`memory.py`。

## 接入新功能时的检查清单

加完功能后，按这个清单确认边界没破：

- [ ] `hermes/prompt.py` 是否需要改？如果新功能只是"skill 多一个字段"或"多一个工具"，prompt.py 应该零改动（`render_skills_section` 内部处理）。如果 prompt.py 被迫改了，说明渲染接口抽象不够。
- [ ] `hermes/cron/executor.py` 是否需要改？cron 只调 `load_skill_body`，新字段会自动透传。只有 cron 要主动利用新能力时才改。
- [ ] `hermes/_io_utils.py` 是否需要改？加 skill 功能不需要动公共 io 工具。
- [ ] `hermes/tools/memory.py` 是否需要改？skill 和 memory 是独立模块，不应该互相影响。
- [ ] 新工具的 `toolset` 是否已注册？新增 toolset 要在 `config.yaml` 的 `gateway.platforms.feishu.toolsets` 里加，否则飞书会话看不到。
- [ ] 新工具的 `risk_level` 和 `approval_mode` 设置对吗？写操作至少 medium，执行外部副作用至少 high。
- [ ] 新工具的 `execution_environments` 是否覆盖所有应该用的场景？默认 cli/gateway/cron，delegate 要单独评估。
- [ ] 新功能需要程序级写 skill 吗？如果需要，是否先抽了程序级写接口（`create_skill` / `edit_skill` 等）？不要让外部模块直接拼 `_render_skill` + `atomic_write_text` 原语。参考"程序级写接口（当前未实现）"段。

## 常见陷阱

1. **不要在 skill.py 里直接 `import hermes.prompt` 或 `import hermes.cron.executor`**。这两个是上层模块，反向 import 会产生循环依赖。上层调用 skill 的接口，skill 不调用上层。
2. **不要绕过 `file_lock` 直接写 SKILL.md**。原子写入保证不了跨进程安全，必须先拿锁。
3. **不要在 `handle_skill_view` 里做写操作**。view 是只读工具，写操作走 `handle_skill_manage`。
4. **不要让 `load_skill_body` 做风险拦截**。它是程序级接口，调用方自己决定怎么用 `risk` 字段。风险拦截只在 `handle_skill_view`（工具入口）里做，因为那是给模型用的。
5. **新增工具必须同时在 `register()` 里注册**。忘了注册的工具不会出现在模型可用工具列表里，且不会报错，只会静默缺失。
6. **`scan_skill_content` 只扫描正文，不扫描 frontmatter**。如果 frontmatter 里要扫描，需要单独加逻辑。当前 frontmatter 字段白名单已经过滤了未知字段，是另一层防护。
7. **信任记录的 key 是 skill 名称，不是内容摘要**。同名 skill 内容变了，旧信任标记为 stale，不会自动删除。如果 skill 被删除，信任记录会留下孤儿条目（当前未清理，低优先级）。
8. **不要让外部模块 `json.loads(handle_skill_manage({...}))` 来做程序级写**。`handle_skill_manage` 是给模型用的工具入口，返回 JSON 字符串。程序级写应该走封装好的程序级写接口；如果该接口还不存在，先按"程序级写接口（当前未实现）"段的接入路径抽出来，再做新功能。
9. **新 skill 字段加在 `_load_skill_payload` 里**。这是 skill 加载的单一入口，改这一处让 `handle_skill_view` 和 `load_skill_body` 都返回新字段。不要在两个入口各加一遍。
