# 用户 Skill 目录

这个目录用于保存当前 `HERMES_HOME` 下的用户 Skill。用户直接创建的 Skill、background review 生成的 Skill、治理 sidecar、references、assets、scripts 和运行锁都属于本地状态，不提交到公共仓库。

随 MyHermes 发布并需要同步到仓库的内置 Skill 位于：

```text
hermes/skills/bundled/skills/<skill-name>/
```

创建本地 Skill 时使用下面的结构：

```text
skills/
└── <skill-name>/
    ├── SKILL.md
    ├── .myhermes.json
    └── references/
```

可复制 `example/SKILL.example.md` 作为起点，并将副本命名为目标目录中的 `SKILL.md`。示例文件自身不会被 SkillRepository 识别为可执行 Skill。
