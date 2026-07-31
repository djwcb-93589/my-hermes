# 本地记忆目录

这个目录保存 MyHermes 的自进化记忆，属于当前用户的本地运行时状态：

```text
memories/
├── MEMORY.md
└── USER.md
```

- `MEMORY.md` 保存跨会话长期知识。
- `USER.md` 保存用户偏好和用户档案。
- 多个条目使用单独一行的 `§` 分隔。

真实的 `MEMORY.md` 和 `USER.md` 不提交到公共仓库。仓库只保留 `MEMORY.example.md` 与 `USER.example.md`，用于说明格式；使用时应复制为不带 `.example` 的本地文件。
