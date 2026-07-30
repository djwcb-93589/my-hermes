---
name: docx
description: 使用 MyHermes 独立 DOCX CLI 安全创建、检查、搜索、编辑、验证和渲染 Word 文档，并编排逐页视觉质量检查。
version: 1.0.0
platforms:
- linux
- macos
- windows
metadata:
  category: productivity
  tags:
  - docx
  - word
  - documents
  - quality-assurance
---

# DOCX 标准工作流

## 何时使用

用户要求创建、读取、搜索、修改、验证或渲染 `.docx` 时使用本 Skill。仅调用 `documents.docx` 已公开的 Python CLI；不要直接解压、改写 OOXML，不要新增 DOCX Tool，也不要把这里的编排逻辑移入 `DocxService`、validator 或 renderer。

当前能力不覆盖 tracked changes、comments、TOC、模板和其他高级 Word 功能。遇到这些结构时保留原文件并明确报告限制。

## 渐进加载

按任务只读取需要的 supporting file：

- create、inspect、search、edit 的参数和 revision 流程：`skill_view(name="docx", relative_path="references/cli-workflows.md")`
- Runtime 状态、依赖与失败降级：`skill_view(name="docx", relative_path="references/runtime-and-failures.md")`
- strict validate、PDF、逐页图片和视觉复检：`skill_view(name="docx", relative_path="references/quality-validation.md")`

编辑任务必须读取 CLI 工作流和质量验证两份说明。创建任务必须读取质量验证说明；Runtime 不完整时再读取 Runtime 说明。

## 固定执行顺序

1. 确认源文件、最终输出路径和是否允许覆盖。没有明确路径时，在当前工作目录下选择语义清晰的 `.docx` 文件名，并先创建其父目录。
2. 执行 `python -m documents.docx.cli runtime-check`。项目使用 uv 时可加 `uv run` 前缀；不要自动安装系统组件或 Node 包。
3. create 使用 JSON spec；inspect/search/edit 只使用 CLI 返回的 `revision`、`block_id` 和 `match_id`，不让调用方提供 XPath、XML 或 run 索引。
4. 创建或编辑成功后立即执行 strict validate。验证失败时停止，不渲染、不交付。
5. strict validate 通过后渲染 PDF 并导出逐页 PNG，再检查每一页。
6. 发现排版问题时，只用当前模块支持的操作修正，然后从 strict validate 开始完整重跑。
7. 最终报告 DOCX 路径、验证状态、PDF/页面图片路径、已检查页数、发现并修复的问题以及任何降级。

## 输出约定

- 最终 DOCX 写入用户指定路径；没有指定时写入当前工作目录的 `output/`。
- 质量验证产物必须生成在当前会话 backend cwd 范围内；默认使用 cwd 相对目录 `output/<文件名>-qa/`。即使最终 DOCX 位于 cwd 外，也不得把 QA 目录放到最终 DOCX 同级。
- spec 和 operations JSON 放在上述 cwd 内 QA 目录的 `requests/`，不要与最终文件混在一起。
- 只在用户明确允许覆盖，或目标是本次流程创建的中间产物时使用 `--overwrite`。

## 不可省略的安全规则

- 编辑前先 inspect；局部文本操作先 search。始终把同一次读取产生的 revision 传给 edit。
- `revision_conflict`、`match_conflict` 或 `match_not_found` 后重新 inspect/search，不复用旧标识。
- edit 成功后使用返回的新 revision 和 `block_remap`；旧 block_id 不再视为稳定。
- strict validate 的 `valid` 不为 true 时禁止交付。
- Renderer 拒绝外部资源或失败时，不尝试下载资源，也不绕过验证。
- 传给 `media_analyze` 的页面路径必须是当前 backend cwd 内的普通相对路径；禁止绝对路径、包含 `..` 的越界路径或任何解析后位于 cwd 外的路径。
- 视觉检查不可用时，明确标记“仅完成结构验证，未完成视觉验证”，不能声称质量闭环已经完成。
