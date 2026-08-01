# Claude Code Skill 本地用户接入

`development/skills/claude-code/` 是唯一开发源；`<HERMES_HOME>/skills/claude-code/` 是由辅助工具生成的本地安装副本。后续内容修改始终先发生在开发源，安装副本不应手工编辑或反向同步。

在 `integration/` 目录中使用：

```bash
python local_skill.py install
python local_skill.py status
python local_skill.py uninstall
```

需要为后续隔离验证指定其他用户 Skill 根目录时，在子命令后增加 `--skills-dir <path>`。默认根目录来自当前 my-hermes 配置的 `HERMES_HOME / "skills"`，安装目标是其直接子目录 `claude-code/`。

安装不会写入 `trusted_skills.json`、生成 `.myhermes.json` 或自动信任、pin、adopt Skill。风险扫描、交互确认和治理决策仍由 my-hermes 现有机制处理。

安装完成后，应重启 my-hermes 或重新建立 Agent 运行上下文，让现有 Skill 发现链路读取本地副本。P2 不修改 Agent、Prompt 或 Skill 加载器；下一阶段 CCS-P3 将单独验证本地发现、Skill 选择以及真实 one-shot 与 supervised 行为。
