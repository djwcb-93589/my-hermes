# Runtime 检查与降级

## 状态检查

执行：

```bash
python -m documents.docx.cli runtime-check
```

项目通过 uv 管理环境时执行：

```bash
uv run python -m documents.docx.cli runtime-check
```

命令返回五个独立组件：

| 组件 | 提供的能力 | 不可用时的降级 |
|---|---|---|
| `python_core` | inspect、search、edit、validate | 核心能力不可用，停止任务 |
| `node_runtime` | 运行 create bundle | create 不可用；已有文档的 Python 能力仍可用 |
| `node_docx_dependency` | 随包 `docx` bundle | create 不可用；不要执行 npm |
| `libreoffice_renderer` | DOCX 转 PDF | 结构验证仍可用，但不能完成 PDF/视觉闭环 |
| `pdf_page_renderer` | PDF 转逐页 PNG | 可生成 PDF，但不能执行逐页视觉检查 |

`node_docx_dependency.version` 是锁定的 bundle 版本；`detail="bundled_cache"` 表示依赖已从发布物校验并准备到用户缓存。不可用组件的 `detail` 是稳定错误类型，应原样报告。

## 依赖策略

- Node 20 或更高版本是 create 的外部运行条件。
- `docx` 依赖已随项目发布，不需要也不允许在任务中执行 npm。
- Runtime 会验证 bundle、锁文件与内容摘要，并写入用户缓存；它不会修改安装目录。
- LibreOffice 和 PyMuPDF 是可选渲染组件，不因 create、inspect、search、edit 或 validate 自动安装。

默认缓存位置按平台为：

- Windows：`%LOCALAPPDATA%/MyHermes/Cache/docx-node/<bundle-sha256>/`
- macOS：`~/Library/Caches/MyHermes/docx-node/<bundle-sha256>/`
- Linux：`$XDG_CACHE_HOME/myhermes/docx-node/<bundle-sha256>/`，未配置时使用 `~/.cache/myhermes/docx-node/`

部署方可用绝对路径变量 `MYHERMES_DOCX_RUNTIME_CACHE` 覆盖根目录。Runtime 会拒绝把缓存放进安装包目录或 `site-packages`。

## 失败处理

- `node_runtime_unavailable`：仅 create 停止；如任务是读取、搜索、编辑或验证，继续 Python 路径。
- `node_version_unsupported`：请用户提供 Node 20+，不要切换到不受控可执行文件。
- `node_dependencies_missing`：发布 bundle 缺失、损坏或缓存不可用；停止 create，报告安装包需要修复。
- `renderer_unavailable`：保留 strict validate 结果和 DOCX，报告未完成视觉验证。
- `pdf_renderer_unavailable`：可不带 `--export-page-images` 生成 PDF；报告需要人工查看 PDF。
- `validation_failed`：停止渲染，先处理 Validator 报告的问题。
- `output_exists`：确认覆盖权限或改用新路径，不删除未知文件。
- 超时或 `io_error`：保留原始输入；不要假定输出完整，不自动重复可能已产生副作用的操作。

任何降级都要在最终答复中说明缺少的组件、仍完成的检查和没有完成的检查。
