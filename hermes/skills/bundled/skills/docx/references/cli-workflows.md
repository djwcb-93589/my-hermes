# DOCX CLI 工作流

所有命令均从 MyHermes 项目环境执行。以下示例使用 `python -m documents.docx.cli`；使用 uv 的项目可统一加 `uv run` 前缀。

## Create

先用 `file` 写入 UTF-8 JSON spec，再执行：

```bash
python -m documents.docx.cli create --spec report-spec.json --output output/report.docx
```

spec 顶层包含 `title`、`creator` 和 `blocks`。只使用当前 create 模型支持的 paragraph、heading、table 和 page break；不要尝试传入 XML 或未公开字段。父目录必须预先存在。

创建成功只代表 package 已生成；随后仍要 strict validate 和视觉验证。

## Inspect

```bash
python -m documents.docx.cli inspect --source output/report.docx
```

保存返回的：

- `revision`：下一次 edit 的并发令牌；
- `block_id`：段落、表格和单元格的稳定定位；
- `editable` 与 `warnings`：当前结构是否允许安全修改；
- metadata、sections、images：编辑前后复核依据。

不要从数组位置自行推导 block_id。

## Search

```bash
python -m documents.docx.cli search --source output/report.docx --query "旧内容"
```

需要时增加 `--ignore-case`、`--whole-word`、`--no-paragraphs`、`--no-table-cells` 或 `--max-matches`。保存搜索结果的 `revision`、`match_id`、`block_id`、`matched_text` 和 `editable`。

跨 run 搜索由模块处理；不要重写整段来模拟局部搜索。

## Edit

operations 文件格式：

```json
{
  "operations": [
    {
      "type": "replace_text_match",
      "match_id": "match:...",
      "block_id": "body:p:2",
      "expected_text": "旧内容",
      "replacement_text": "新内容",
      "preserve_format": true
    }
  ]
}
```

执行：

```bash
python -m documents.docx.cli edit --source output/report.docx --output output/report-edited.docx --expected-revision "<inspect-or-search-revision>" --operations report-operations.json
```

同一请求内的全部操作以旧 revision 和旧 block_id 计划。结构编辑完成后，以返回的 `block_remap` 和新 revision 为准。编辑同一路径时只有在确认属于本次任务且允许覆盖后增加 `--overwrite`。

支持的标准流程包括：

- 全段或简单表格单元格替换；
- 基于 match_id 的局部替换和单 run 基础格式；
- 简单段落插入、追加、删除与属性更新；
- 规则表格插入、追加行、删除普通非表头行；
- 当前公共操作模型支持的图片、超链接、列表、页面设置、页眉页脚和元数据修改。

如果 snapshot 标记不可编辑，或 CLI 返回 `block_not_editable`、`match_not_editable`，停止该修改并说明复杂结构限制，不直接改 OOXML。

## Validate

```bash
python -m documents.docx.cli validate output/report.docx --strict
```

只有退出成功且 JSON 中 `result.valid` 为 true 才进入 render。warning 需要保留在报告中；外部资源 warning 可能仍会使 renderer 拒绝。

## Render

render 的输出目录必须使用当前会话 backend cwd 内的相对路径，并确认解析结果仍位于 cwd 内。即使 source 或最终 DOCX 位于 cwd 外，也要使用 cwd 内的 QA 目录；不得传入绝对 QA 路径，也不得使用 `..` 越过 cwd。

```bash
python -m documents.docx.cli render output/report.docx output/report-qa --export-page-images
```

覆盖本次已有 QA 产物时增加 `--overwrite`。Renderer 内部会重新 strict validate；Skill 仍须在调用前显式执行 validate，以便清晰报告失败阶段。
