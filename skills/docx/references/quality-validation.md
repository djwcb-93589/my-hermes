# 输出质量验证编排

## 必须执行的流水线

```text
create 或 edit
→ validate --strict
→ render PDF --export-page-images
→ 核对页面数量和文件清单
→ media_analyze 分批检查全部页面
→ 必要时修正并从 strict validate 重跑
```

这只是 Skill 编排：不要向 `DocxService`、validator 或 renderer 添加视觉判断。

## 渲染

为最终 DOCX 创建独立 QA 目录，然后执行：

```bash
python -m documents.docx.cli validate output/report.docx --strict
python -m documents.docx.cli render output/report.docx output/report-qa --export-page-images --overwrite
```

先检查 render JSON 中的 `pdf_path` 和 `pages`，再用 `file` 列出 QA 目录。页码必须从 1 连续到返回页数，且每个路径都存在。不要把“生成了 PDF”当作已经完成视觉检查。

视觉检查前也要对可见文字分别搜索 `{{`、`${`、`[TODO]`、`TBD` 和 `Lorem ipsum` 等常见占位标记。命中后结合上下文判断是否为用户有意保留的文字；不要使用正则或直接扫描 OOXML。

## 逐页视觉检查

使用 `media_analyze` 检查所有 `page-*.png`。每次最多 20 页；超过 20 页时按连续页码分批，批次之间保留页码范围。提示词至少要求逐页报告：

1. 表格是否越过页边距、列宽是否异常、文字是否被截断或重叠；
2. 分页是否异常，包括孤立标题、段落被不合理拆分、意外分页和多余空白页；
3. 图片是否缺失、拉伸、压缩、裁切、遮挡或超出页面；
4. 字体是否异常，包括乱码、缺字方框、明显替代字体、字号或行距突变；
5. 是否存在残留占位符，例如 `{{...}}`、`${...}`、`[TODO]`、`TBD`、`Lorem ipsum` 或未替换的示例文本；
6. 页眉、页脚、页码、边距和整体对齐是否跨页一致。

同时比较相邻页，检查表格跨页断裂、标题与正文分离、重复内容和缺页。空白页只有在用户明确要求或页面布局合理需要时才可接受。

## 发现问题后的处理

- 先记录页码、对象和可见症状，再选择现有 create/edit 能力修正。
- 不支持的复杂结构不直接改 XML；保留文件并报告限制。
- 每次修正后重新执行 strict validate、render 和全部页面检查，不能只看发生变化的页面。
- 使用同一 QA 目录覆盖时，renderer 只管理严格命名的页面图片；不要删除目录中的其他文件。
- 多次修正仍无法稳定排版时，交付前明确列出未解决问题和受影响页码，不声称验证通过。

## 降级

- LibreOffice 不可用：只完成 strict validate，无法生成 PDF；请求用户在有 LibreOffice 的环境复检。
- PDF 页面 renderer 不可用：生成 PDF 后停止自动视觉流程，提供 PDF 路径并请求人工逐页查看。
- `media_analyze` 不可用、未获授权或外部分析失败：保留 PDF 和页面 PNG，报告未完成视觉分析；不要把文件发送到其他服务。
- 任何 strict validation 失败：不得进入上述降级，必须先停止并修复结构问题。

最终报告必须区分“结构验证通过”“PDF 生成成功”“逐页视觉检查通过”三个独立状态。
