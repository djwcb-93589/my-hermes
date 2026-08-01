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

安装完成后，应重启 my-hermes 或重新建立 Agent 运行上下文，让现有 Skill 发现链路读取本地副本。P2.2 不修改 Agent、Prompt 或 Skill 加载器；后续应先在独立 T2 阶段验证双锁并发、失败回滚、部分清理状态和保守卸载，再进入 CCS-P3 验证本地发现、Skill 选择以及真实 one-shot 与 supervised 行为。
