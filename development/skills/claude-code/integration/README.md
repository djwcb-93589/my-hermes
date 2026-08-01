# Claude Code Skill 本地用户接入

`development/skills/claude-code/` 是唯一开发源；`<HERMES_HOME>/skills/claude-code/` 是由辅助工具生成的本地安装副本。后续内容修改始终先发生在开发源，安装副本不应手工编辑或反向同步。

在 `integration/` 目录中使用：

```bash
python local_skill.py install
python local_skill.py status
python local_skill.py uninstall
```

需要为后续隔离验证指定其他用户 Skill 根目录时，在子命令后增加 `--skills-dir <path>`。默认根目录来自当前 my-hermes 配置的 `HERMES_HOME / "skills"`，安装目标是其直接子目录 `claude-code/`。

安装器与 `SkillRepository` 共用 `<skills-root>/.locks/claude-code.lock`。它在锁内校验 source 和目标、通过同一文件系统的 staging 原子发布，再将所有权记录原子写入 `<skills-root>/.installer-state/claude-code.json`；如果所有权记录无法落盘，只会在发布内容仍与本次 revision 一致时安全回滚。

安装前的 runtime package 校验允许 Markdown 使用 `../` 引用同一 package 内的文件，例如从 `references/` 引用 `../templates/...`。安装器以当前 Markdown 文件所在目录为基准解析目标，拒绝路径中的 symlink、junction 或 reparse point，并仅在 canonical 目标仍位于 package 根内且属于 runtime manifest 时接受。绝对路径、解析后逃出 package 的路径、包外目标、不存在或不在 manifest 中的文件，以及不支持的 URL scheme 仍会被拒绝。

`uninstall` 只删除所有权记录匹配、完整 package revision 未变化且没有治理 sidecar 或信任记录的安装副本。它固定按“Skill 操作锁 → trust store 锁”的顺序取得锁，并在 trust 锁内完成最后一次信任检查和目标目录删除；并发 trust 不能插入该临界区。目标由其他来源创建、内容被修改、所有权损坏，或 Skill 已被 adopt、pin、trust 等治理机制接管时都会拒绝删除；不提供强制卸载，也不会自动删除用户修改过的 Skill。

`status` 的 `ok` 只表示命令成功执行，不能单独表示 Skill 已安装或可用。`ready` 只表示安装副本结构完整、ownership 有效且当前内容与 `installed_revision` 一致；判断 Skill 能否使用应检查 `ready == true`。已 trust、adopt、pin 或存在合法治理 sidecar 不会单独令 `ready=false`。

治理与卸载权限通过 `managed`、`uninstall_allowed` 和 `uninstall_block_reason` 单独表达。判断安装器能否自动卸载应检查 `uninstall_allowed == true`；trust 状态无法可靠读取时该字段为 false。`in_sync` 只比较安装副本和当前开发源，判断版本同步应检查 `in_sync == true`；开发源更新后可以同时出现 `in_sync=false` 与 `ready=true`。`reason` 只解释安装副本为何未就绪，调用方不应把它与卸载阻塞原因混用。

`installed` 表示目标是可安全检查的普通目录，`managed_by_installer` 表示存在当前安装器身份的 ownership 记录，`ownership_valid` 表示该记录的 schema、installer、Skill 和目标路径均有效。`status` 通过前后两次只读观察复核目标 revision、ownership、治理与 trust 状态；安装完整性变化会通过 `reason=concurrent_change` 阻止就绪判断，仅治理或 trust 变化时则通过 `uninstall_block_reason=concurrent_change` 阻止卸载，不会错误降低 `ready`，也不会自动修改任何状态。

安装不会写入 `trusted_skills.json`、生成 `.myhermes.json` 或自动信任、pin、adopt Skill。风险扫描、交互确认和治理决策仍由 my-hermes 现有机制处理。

## P3 发现与选择接入

安装器只负责把开发源中的运行时 package 复制和管理到用户安装目录，不注册 Skill、不修改 Agent Prompt，也不通知运行中的 Agent。默认安装目录与现有发现根一致，链路为：

```text
HERMES_HOME
→ <HERMES_HOME>/skills/claude-code/SKILL.md
→ SkillRepository.discover()
→ SkillService.render_skills_section()
→ build_system_prompt()
→ Agent 可见的 Skill 名称和 description
```

`SkillRepository` 当前合并 bundled 与本地用户两个根目录：先扫描 bundled，再以同目录名的用户 Skill 稳定覆盖。当前没有独立的 workspace Skill 来源；项目上下文文件也不是第三个 Skill 根。P3 沿用这一通用规则，不增加 `claude-code` 特判、第二套 Loader 或 Claude Code 专用 Tool。

### Skill catalog

具备 `skill_read` 或 `skill_manage` 任一 toolset 的上下文都会获得现有 Skill catalog；未显式限定工具集的默认上下文也会获得。通用 discovery 记录保留 name、description、version、platforms 和 metadata 等现有字段，当前 system Prompt 摘要渲染每个 Skill 的名称和 description，用于初步匹配，不提前注入完整正文或全部 references。

### `skill_view`

只有具备 `skill_read` 的上下文才能调用 `skill_view`，并获得 Skill-First 指引。对于 `claude-code`，只有用户明确要求使用 Claude Code/CC，或明确要求继续、补充指示、停止或控制已有会话后，Agent 才调用 `skill_view(name="claude-code")` 读取完整 `SKILL.md`；需要细节时，再通过同一工具的 `relative_path` 按需读取 package 内的 `references/`、`templates/`、`scripts/` 或 `assets/` 文件。通用 Repository 继续拒绝绝对路径、包外逃逸和符号链接路径。

### `skill_manage`

仅具备 `skill_manage` 的上下文仍会看到 catalog 摘要，但这不等于拥有 `skill_view`，也不会获得依赖 `skill_read` 的 Skill-First 指引。P3 不为修正文档而改变任何实际工具权限。

### 用户显式启用

Claude Code Skill 不根据仓库规模、多文件修改、任务复杂度或预计耗时自动触发。Agent 也不主动推荐 Claude Code，或询问用户是否启用。用户未明确要求时继续使用 myHermes 自身工具，不读取本 Skill、不执行 Claude Code preflight，也不启动 Claude Code。Skill 已安装、catalog 可见或 `ready=true` 均不构成用户授权；用户明确要求后仍必须读取 Skill、执行 preflight，并继续遵守原任务范围和审批边界。

`local_skill.py install` 成功不会把 Skill 热注入已经构建并正在使用的 Agent 上下文。安装后应新建 Agent 对话/运行上下文；对于启动时缓存 Prompt 的入口，应重启相关 runtime。新上下文构建时才通过现有链路重新扫描。P3 不增加当前会话热重载、目录监听或专用缓存失效机制。

使用 `python local_skill.py status` 可以确认安装状态；`ready=true` 只表示安装副本结构、ownership 和 revision 完整，不表示当前 Agent 已发现、具备 `skill_read`、获得用户启用授权、调用过 `skill_view`、通过 preflight，或已经启动 Claude Code。确认这些状态必须留到新 Agent 上下文中的独立 T3；不能从安装器状态推断。

P3 只完成发现和选择接入。它不自动启动 `claude`，不调用 Terminal/Process，不处理真实 Claude Code 输出，也不代表 one-shot 或 supervised PTY 已通过真实任务验收；这些行为留给后续阶段和独立 T3。
