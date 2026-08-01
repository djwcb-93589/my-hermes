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

`uninstall` 只删除所有权记录匹配、完整 package revision 未变化且没有治理 sidecar 或信任记录的安装副本。目标由其他来源创建、内容被修改、所有权损坏，或 Skill 已被 adopt、pin、trust 等治理机制接管时都会拒绝删除；不提供强制卸载。

`status` 的 `ok` 只表示命令成功执行，不能单独表示 Skill 可用。调用方应按需要同时检查 `installed`、`managed_by_installer`、`ownership_valid`、`ready` 和 `in_sync`，并在 `ready` 为 false 时读取结构化 `reason`。

安装不会写入 `trusted_skills.json`、生成 `.myhermes.json` 或自动信任、pin、adopt Skill。风险扫描、交互确认和治理决策仍由 my-hermes 现有机制处理。

安装完成后，应重启 my-hermes 或重新建立 Agent 运行上下文，让现有 Skill 发现链路读取本地副本。P2.1 不修改 Agent、Prompt 或 Skill 加载器；后续应先在独立 T2 阶段验证并发锁、失败回滚和保守卸载，再进入 CCS-P3 验证本地发现、Skill 选择以及真实 one-shot 与 supervised 行为。
