"""独立 DOCX 创建、只读检查与安全编辑命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import DocxError
from .models import (
    AppliedEdit,
    CreateDocumentRequest,
    DocumentMetadata,
    DocumentSnapshot,
    DocumentWarning,
    EditDocumentRequest,
    EditDocumentResult,
    HeadingSpec,
    InspectDocumentRequest,
    PageBreakSpec,
    ParagraphSnapshot,
    ParagraphSpec,
    ReplaceParagraphText,
    ReplaceTableCellText,
    TableCellSnapshot,
    TableSnapshot,
    TableSpec,
    TextRunSnapshot,
    TextRunSpec,
    UpdateDocumentMetadata,
)
from .runtime import NodeRuntime
from .service import DocxService


_JSON_MODEL_FIELDS: dict[type[object], tuple[str, ...]] = {
    DocumentSnapshot: (
        "source_path",
        "revision",
        "size_bytes",
        "metadata",
        "blocks",
        "warnings",
        "paragraph_count",
        "table_count",
        "image_count",
        "section_count",
    ),
    DocumentMetadata: (
        "title",
        "creator",
        "subject",
        "description",
        "created",
        "modified",
        "last_modified_by",
    ),
    ParagraphSnapshot: (
        "block_id",
        "text",
        "style",
        "alignment",
        "runs",
        "editable",
        "warnings",
    ),
    TextRunSnapshot: ("text", "bold", "italic", "underline"),
    TableSnapshot: (
        "block_id",
        "rows",
        "row_count",
        "column_count",
        "editable",
        "warnings",
    ),
    TableCellSnapshot: (
        "block_id",
        "text",
        "paragraphs",
        "editable",
        "warnings",
    ),
    DocumentWarning: ("warning_type", "message", "part", "block_id"),
    EditDocumentResult: (
        "source_path",
        "output_path",
        "old_revision",
        "new_revision",
        "size_bytes",
        "sha256",
        "changed",
        "applied_edits",
    ),
    AppliedEdit: ("operation_index", "operation_type", "block_id"),
}
_JSON_BLOCK_TYPES: dict[type[object], str] = {
    ParagraphSnapshot: "paragraph",
    TableSnapshot: "table",
}


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DocxError("invalid_request", f"命令行参数无效：{message}。")


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令并始终以单行 JSON 报告执行结果。"""

    try:
        args = _build_parser().parse_args(argv)
        if args.command == "runtime-check":
            payload = _run_runtime_check(args)
        elif args.command == "inspect":
            payload = _run_inspect(args)
        elif args.command == "edit":
            payload = _run_edit(args)
        else:
            payload = _run_create(args)
        _print_json(payload)
        return 0
    except DocxError as exc:
        _print_json(
            {
                "ok": False,
                "error_type": exc.error_type,
                "message": str(exc),
            }
        )
        return 1
    except Exception:
        _print_json(
            {
                "ok": False,
                "error_type": "io_error",
                "message": "DOCX 命令执行失败。",
            }
        )
        return 1


def _build_parser() -> _JsonArgumentParser:
    parser = _JsonArgumentParser(prog="python -m documents.docx.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    runtime_parser = subparsers.add_parser("runtime-check", help="检查固定 Node runtime")
    runtime_parser.add_argument("--node-executable")

    create_parser = subparsers.add_parser("create", help="根据 JSON 规格创建新 DOCX")
    create_parser.add_argument("--spec", required=True, type=Path)
    create_parser.add_argument("--output", required=True, type=Path)
    create_parser.add_argument("--overwrite", action="store_true")
    create_parser.add_argument("--node-executable")
    create_parser.add_argument("--timeout-seconds", type=float, default=60.0)

    inspect_parser = subparsers.add_parser("inspect", help="读取现有 DOCX 的结构化快照")
    inspect_parser.add_argument("--source", required=True, type=Path)
    inspect_parser.add_argument("--no-runs", action="store_true")
    inspect_parser.add_argument("--no-tables", action="store_true")
    inspect_parser.add_argument("--max-blocks", type=int)
    inspect_parser.add_argument("--max-text-chars", type=int)

    edit_parser = subparsers.add_parser("edit", help="基于 revision 安全修改现有 DOCX")
    edit_parser.add_argument("--source", required=True, type=Path)
    edit_parser.add_argument("--output", required=True, type=Path)
    edit_parser.add_argument("--expected-revision", required=True)
    edit_parser.add_argument("--operations", required=True, type=Path)
    edit_parser.add_argument("--overwrite", action="store_true")
    return parser


def _run_runtime_check(args: argparse.Namespace) -> dict[str, Any]:
    runtime = NodeRuntime(node_executable=args.node_executable)
    runtime.check()
    return {
        "ok": True,
        "node_version": runtime.node_version,
        "dependencies_ready": True,
    }


def _run_create(args: argparse.Namespace) -> dict[str, Any]:
    specification = _read_specification(args.spec)
    request = _request_from_specification(
        specification,
        output_path=args.output,
        overwrite=args.overwrite,
    )
    result = DocxService(node_executable=args.node_executable).create_document(
        request,
        timeout_seconds=args.timeout_seconds,
    )
    return {
        "ok": True,
        "output_path": str(result.output_path),
        "size_bytes": result.size_bytes,
        "sha256": result.sha256,
        "block_count": result.block_count,
    }


def _run_inspect(args: argparse.Namespace) -> dict[str, Any]:
    request = InspectDocumentRequest(
        source_path=args.source,
        include_runs=not args.no_runs,
        include_tables=not args.no_tables,
        max_blocks=args.max_blocks,
        max_text_chars=args.max_text_chars,
    )
    snapshot = DocxService().inspect_document(request)
    serialized = _serialize_json_value(snapshot)
    if not isinstance(serialized, dict):
        raise DocxError("io_error", "DOCX 快照序列化失败。")
    return {"ok": True, **serialized}


def _run_edit(args: argparse.Namespace) -> dict[str, Any]:
    operations = _read_edit_operations(args.operations)
    request = EditDocumentRequest(
        source_path=args.source,
        output_path=args.output,
        expected_revision=args.expected_revision,
        operations=operations,
        overwrite=args.overwrite,
    )
    result = DocxService().edit_document(request)
    serialized = _serialize_json_value(result)
    if not isinstance(serialized, dict):
        raise DocxError("io_error", "DOCX 编辑结果序列化失败。")
    return {"ok": True, **serialized}


def _read_edit_operations(
    path: Path,
) -> list[ReplaceParagraphText | ReplaceTableCellText | UpdateDocumentMetadata]:
    try:
        with path.expanduser().open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DocxError(
            "invalid_edit_operation",
            "operations 文件不是有效的 UTF-8 JSON。",
        ) from exc
    except OSError as exc:
        raise DocxError("io_error", "无法读取 operations 文件。") from exc
    if not isinstance(value, dict):
        raise DocxError(
            "invalid_edit_operation",
            "operations 文件顶层必须是 JSON object。",
        )
    _reject_unknown_keys(
        value,
        {"operations"},
        "operations 文件",
        "invalid_edit_operation",
    )
    raw_operations = value.get("operations")
    if not isinstance(raw_operations, list):
        raise DocxError(
            "invalid_edit_operation",
            "operations 字段必须是列表。",
        )
    return [
        _parse_edit_operation(operation, index)
        for index, operation in enumerate(raw_operations)
    ]


def _parse_edit_operation(
    value: object,
    index: int,
) -> ReplaceParagraphText | ReplaceTableCellText | UpdateDocumentMetadata:
    if not isinstance(value, dict):
        raise DocxError(
            "invalid_edit_operation",
            f"第 {index} 个编辑操作必须是 JSON object。",
        )
    operation_type = value.get("type")
    if operation_type == "replace_paragraph_text":
        _reject_unknown_keys(
            value,
            {"type", "block_id", "text", "preserve_first_run_format"},
            f"第 {index} 个 replace_paragraph_text",
            "invalid_edit_operation",
        )
        return ReplaceParagraphText(
            block_id=value.get("block_id"),
            text=value.get("text"),
            preserve_first_run_format=value.get(
                "preserve_first_run_format",
                True,
            ),
        )
    if operation_type == "replace_table_cell_text":
        _reject_unknown_keys(
            value,
            {"type", "block_id", "text", "preserve_first_run_format"},
            f"第 {index} 个 replace_table_cell_text",
            "invalid_edit_operation",
        )
        return ReplaceTableCellText(
            block_id=value.get("block_id"),
            text=value.get("text"),
            preserve_first_run_format=value.get(
                "preserve_first_run_format",
                True,
            ),
        )
    if operation_type == "update_document_metadata":
        _reject_unknown_keys(
            value,
            {"type", "fields"},
            f"第 {index} 个 update_document_metadata",
            "invalid_edit_operation",
        )
        return UpdateDocumentMetadata(fields=value.get("fields"))
    raise DocxError(
        "invalid_edit_operation",
        f"第 {index} 个编辑操作 type 不受支持。",
    )


def _read_specification(path: Path) -> dict[str, Any]:
    try:
        with path.expanduser().open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DocxError("invalid_request", "spec 文件不是有效的 UTF-8 JSON。") from exc
    except OSError as exc:
        raise DocxError("io_error", "无法读取 spec 文件。") from exc
    if not isinstance(value, dict):
        raise DocxError("invalid_request", "spec 顶层必须是 JSON object。")
    _reject_unknown_keys(value, {"title", "creator", "blocks"}, "spec", "invalid_request")
    return value


def _request_from_specification(
    specification: dict[str, Any],
    *,
    output_path: Path,
    overwrite: bool,
) -> CreateDocumentRequest:
    raw_blocks = specification.get("blocks")
    if not isinstance(raw_blocks, list):
        raise DocxError("invalid_request", "spec.blocks 必须是列表。")
    blocks = [_parse_block(block, index) for index, block in enumerate(raw_blocks)]
    return CreateDocumentRequest(
        output_path=output_path,
        blocks=blocks,
        overwrite=overwrite,
        title=specification.get("title"),
        creator=specification.get("creator"),
    )


def _parse_block(value: object, index: int) -> object:
    if not isinstance(value, dict):
        raise DocxError("invalid_block", f"第 {index} 个内容块必须是 JSON object。")
    block_type = value.get("type")

    if block_type == "paragraph":
        _reject_unknown_keys(
            value,
            {"type", "runs", "style", "alignment"},
            f"第 {index} 个 paragraph",
            "invalid_block",
        )
        raw_runs = value.get("runs")
        if not isinstance(raw_runs, list):
            raise DocxError("invalid_block", f"第 {index} 个 paragraph.runs 必须是列表。")
        runs = [_parse_run(run, index, run_index) for run_index, run in enumerate(raw_runs)]
        return ParagraphSpec(
            runs=runs,
            style=value.get("style"),
            alignment=value.get("alignment"),
        )

    if block_type == "heading":
        _reject_unknown_keys(
            value,
            {"type", "text", "level"},
            f"第 {index} 个 heading",
            "invalid_block",
        )
        return HeadingSpec(text=value.get("text"), level=value.get("level"))

    if block_type == "table":
        _reject_unknown_keys(
            value,
            {"type", "rows", "header_row"},
            f"第 {index} 个 table",
            "invalid_block",
        )
        return TableSpec(
            rows=value.get("rows"),
            header_row=value.get("header_row", False),
        )

    if block_type == "page_break":
        _reject_unknown_keys(
            value,
            {"type"},
            f"第 {index} 个 page_break",
            "invalid_block",
        )
        return PageBreakSpec()

    raise DocxError("invalid_block", f"第 {index} 个内容块 type 不受支持。")


def _parse_run(value: object, block_index: int, run_index: int) -> TextRunSpec:
    if not isinstance(value, dict):
        raise DocxError(
            "invalid_block",
            f"第 {block_index} 个内容块的第 {run_index} 个 run 必须是 JSON object。",
        )
    _reject_unknown_keys(
        value,
        {"text", "bold", "italic", "underline"},
        f"第 {block_index} 个内容块的第 {run_index} 个 run",
        "invalid_block",
    )
    return TextRunSpec(
        text=value.get("text"),
        bold=value.get("bold", False),
        italic=value.get("italic", False),
        underline=value.get("underline", False),
    )


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
    error_type: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DocxError(error_type, f"{label} 包含不支持的字段。")


def _serialize_json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serialize_json_value(item) for item in value]

    model_type = type(value)
    model_fields = _JSON_MODEL_FIELDS.get(model_type)
    if model_fields is None:
        raise TypeError(f"Unsupported JSON model: {model_type.__name__}")
    payload: dict[str, Any] = {}
    block_type = _JSON_BLOCK_TYPES.get(model_type)
    if block_type is not None:
        payload["type"] = block_type
    for field_name in model_fields:
        payload[field_name] = _serialize_json_value(getattr(value, field_name))
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
