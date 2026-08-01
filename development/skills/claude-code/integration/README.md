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

`SkillRepository` 当前合并 bundled 与本地用户两个根目录：先扫描 bundled，再以同目录名的用户 Skill 稳定覆盖。当前没有独立的 workspace Skill 来源；项目上下文文件也不是第三个 Skill 根。P3 沿用这一通用规则，不增加 `claude-code` 特判、第二套 Loader 或 Claude Code 专用 Tool。只有具备现有 Skill catalog/read 能力的 Agent 上下文才会获得目录摘要和 `skill_view`。

初始 Prompt 只注入每个已发现 Skill 的名称和 description，不提前注入完整正文或全部 references。Agent 判断任务适合后，使用现有 `skill_view` 读取 `claude-code` 的完整 `SKILL.md`；需要细节时，再以 package 内的 `references/...` 或 `templates/...` 相对路径读取单个 support file。通用 Repository 只允许这些受支持目录下的安全相对路径，并拒绝绝对路径、`..` 和符号链接逃逸。

`local_skill.py install` 成功不会把 Skill 热注入已经构建并正在使用的 Agent 上下文。安装后应新建 Agent 对话/运行上下文；对于启动时缓存 Prompt 的入口，应重启相关 runtime。新上下文构建时才通过现有链路重新扫描。P3 不增加当前会话热重载、目录监听或专用缓存失效机制。

使用 `python local_skill.py status` 可以确认安装状态；`ready=true` 只表示安装副本结构、ownership 和 revision 完整，不表示某个 Agent 已发现或选择该 Skill。确认发现必须留到新 Agent 上下文中的独立 T3；不能从安装器状态推断。

P3 只完成发现和选择接入。它不自动启动 `claude`，不调用 Terminal/Process，不处理真实 Claude Code 输出，也不代表 one-shot 或 supervised PTY 已通过真实任务验收；这些行为留给后续阶段和独立 T3。
