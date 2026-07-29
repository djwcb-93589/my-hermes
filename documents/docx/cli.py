"""独立 DOCX 创建、读取、搜索与安全编辑命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import DocxError
from .models import (
    AppliedEdit,
    AppendParagraph,
    AppendTableRow,
    BlockRemap,
    CreateDocumentRequest,
    DeleteParagraph,
    DeleteTableRow,
    DocumentMetadata,
    DocumentSnapshot,
    DocumentWarning,
    EditDocumentRequest,
    EditDocumentResult,
    EditOperation,
    FormatTextMatch,
    HeadingSpec,
    InspectDocumentRequest,
    InsertBulletListAfter,
    InsertHyperlinkAfter,
    InsertImageAfter,
    InsertNumberedListAfter,
    InsertParagraphAfter,
    InsertParagraphBefore,
    InsertTableAfter,
    PageBreakSpec,
    ParagraphSnapshot,
    ParagraphSpec,
    ReplaceParagraphText,
    ReplaceTableCellText,
    ReplaceTextMatch,
    SearchDocumentRequest,
    SearchDocumentResult,
    TableCellSnapshot,
    TableSnapshot,
    TableSpec,
    TextMatch,
    TextRunSnapshot,
    TextRunSpec,
    UpdateParagraphProperties,
    UpdateDocumentMetadata,
    UpdateFooterText,
    UpdateHeaderText,
    UpdatePageSetup,
)
from .renderer import (
    RenderDocumentRequest,
    RenderDocumentResult,
    RenderedPage,
)
from .runtime import DocxRuntimeStatus, RuntimeComponentStatus
from .service import DocxService
from .validation_models import (
    ValidateDocumentRequest,
    ValidateDocumentResult,
    ValidationIssue,
)


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
        "block_remap",
    ),
    AppliedEdit: ("operation_index", "operation_type", "block_id"),
    BlockRemap: ("old_block_id", "new_block_id"),
    SearchDocumentResult: (
        "source_path",
        "revision",
        "query",
        "matches",
        "total_matches",
    ),
    TextMatch: (
        "match_id",
        "block_id",
        "matched_text",
        "start",
        "end",
        "prefix",
        "suffix",
        "editable",
        "warnings",
    ),
    ValidateDocumentResult: (
        "source_path",
        "valid",
        "revision",
        "size_bytes",
        "issues",
        "checked_parts",
    ),
    ValidationIssue: (
        "code",
        "message",
        "part_name",
        "severity",
    ),
    RenderDocumentResult: (
        "source_path",
        "pdf_path",
        "pages",
        "renderer",
    ),
    RenderedPage: ("page_number", "image_path"),
    DocxRuntimeStatus: ("core_available", "components"),
    RuntimeComponentStatus: (
        "name",
        "available",
        "version",
        "detail",
    ),
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
        elif args.command == "search":
            payload = _run_search(args)
        elif args.command == "edit":
            payload = _run_edit(args)
        elif args.command == "validate":
            payload = _run_validate(args)
        elif args.command == "render":
            payload = _run_render(args)
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

    runtime_parser = subparsers.add_parser(
        "runtime-check",
        help="检查 DOCX 核心与可选运行组件",
    )
    runtime_parser.add_argument("--node-executable")
    runtime_parser.add_argument("--libreoffice-executable")

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

    search_parser = subparsers.add_parser("search", help="搜索现有 DOCX 的可见文字")
    search_parser.add_argument("--source", required=True, type=Path)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--ignore-case", action="store_true")
    search_parser.add_argument("--whole-word", action="store_true")
    search_parser.add_argument("--no-paragraphs", action="store_true")
    search_parser.add_argument("--no-table-cells", action="store_true")
    search_parser.add_argument("--max-matches", type=int, default=100)

    edit_parser = subparsers.add_parser("edit", help="基于 revision 安全修改现有 DOCX")
    edit_parser.add_argument("--source", required=True, type=Path)
    edit_parser.add_argument("--output", required=True, type=Path)
    edit_parser.add_argument("--expected-revision", required=True)
    edit_parser.add_argument("--operations", required=True, type=Path)
    edit_parser.add_argument("--overwrite", action="store_true")

    validate_parser = subparsers.add_parser(
        "validate",
        help="使用纯 Python 验证 DOCX 核心结构",
    )
    validate_parser.add_argument("source", type=Path)
    strict_group = validate_parser.add_mutually_exclusive_group()
    strict_group.add_argument(
        "--strict",
        dest="strict",
        action="store_true",
    )
    strict_group.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
    )
    validate_parser.set_defaults(strict=True)

    render_parser = subparsers.add_parser(
        "render",
        help="使用可选 LibreOffice 将 DOCX 渲染为 PDF",
    )
    render_parser.add_argument("source", type=Path)
    render_parser.add_argument("output_dir", type=Path)
    render_parser.add_argument("--overwrite", action="store_true")
    render_parser.add_argument(
        "--export-page-images",
        action="store_true",
    )
    render_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
    )
    render_parser.add_argument("--libreoffice-executable")
    return parser


def _run_runtime_check(args: argparse.Namespace) -> dict[str, Any]:
    result = DocxService(
        node_executable=args.node_executable,
        libreoffice_executable=args.libreoffice_executable,
    ).runtime_check()
    return {"ok": True, "result": _serialize_json_value(result)}


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


def _run_search(args: argparse.Namespace) -> dict[str, Any]:
    request = SearchDocumentRequest(
        source_path=args.source,
        query=args.query,
        case_sensitive=not args.ignore_case,
        whole_word=args.whole_word,
        include_paragraphs=not args.no_paragraphs,
        include_table_cells=not args.no_table_cells,
        max_matches=args.max_matches,
    )
    result = DocxService().search_document(request)
    serialized = _serialize_json_value(result)
    if not isinstance(serialized, dict):
        raise DocxError("io_error", "DOCX 搜索结果序列化失败。")
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


def _run_validate(args: argparse.Namespace) -> dict[str, Any]:
    result = DocxService().validate_document(
        ValidateDocumentRequest(
            source_path=args.source,
            strict=args.strict,
        )
    )
    serialized = _serialize_json_value(result)
    if not isinstance(serialized, dict):
        raise DocxError("io_error", "DOCX 验证结果序列化失败。")
    return {"ok": True, "result": serialized}


def _run_render(args: argparse.Namespace) -> dict[str, Any]:
    result = DocxService(
        libreoffice_executable=args.libreoffice_executable,
    ).render_document(
        RenderDocumentRequest(
            source_path=args.source,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
            export_page_images=args.export_page_images,
            timeout_seconds=args.timeout_seconds,
        )
    )
    serialized = _serialize_json_value(result)
    if not isinstance(serialized, dict):
        raise DocxError("io_error", "DOCX 渲染结果序列化失败。")
    return {"ok": True, "result": serialized}


def _read_edit_operations(
    path: Path,
) -> list[EditOperation]:
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
) -> EditOperation:
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
    if operation_type == "replace_text_match":
        _reject_unknown_keys(
            value,
            {
                "type",
                "match_id",
                "block_id",
                "expected_text",
                "replacement_text",
                "preserve_format",
            },
            f"第 {index} 个 replace_text_match",
            "invalid_edit_operation",
        )
        return ReplaceTextMatch(
            match_id=value.get("match_id"),
            block_id=value.get("block_id"),
            expected_text=value.get("expected_text"),
            replacement_text=value.get("replacement_text"),
            preserve_format=value.get("preserve_format", True),
        )
    if operation_type in {
        "insert_paragraph_before",
        "insert_paragraph_after",
    }:
        _reject_unknown_keys(
            value,
            {"type", "block_id", "runs", "style", "alignment"},
            f"第 {index} 个 {operation_type}",
            "invalid_edit_operation",
        )
        runs = _parse_edit_runs(value.get("runs"), index, operation_type)
        operation_class = (
            InsertParagraphBefore
            if operation_type == "insert_paragraph_before"
            else InsertParagraphAfter
        )
        return operation_class(
            block_id=value.get("block_id"),
            runs=runs,
            style=value.get("style"),
            alignment=value.get("alignment"),
        )
    if operation_type == "append_paragraph":
        _reject_unknown_keys(
            value,
            {"type", "runs", "style", "alignment"},
            f"第 {index} 个 append_paragraph",
            "invalid_edit_operation",
        )
        return AppendParagraph(
            runs=_parse_edit_runs(
                value.get("runs"),
                index,
                operation_type,
            ),
            style=value.get("style"),
            alignment=value.get("alignment"),
        )
    if operation_type == "delete_paragraph":
        _reject_unknown_keys(
            value,
            {"type", "block_id"},
            f"第 {index} 个 delete_paragraph",
            "invalid_edit_operation",
        )
        return DeleteParagraph(block_id=value.get("block_id"))
    if operation_type == "update_paragraph_properties":
        _reject_unknown_keys(
            value,
            {
                "type",
                "block_id",
                "style",
                "alignment",
                "heading_level",
            },
            f"第 {index} 个 update_paragraph_properties",
            "invalid_edit_operation",
        )
        property_values: dict[str, object] = {
            "block_id": value.get("block_id")
        }
        for field_name in ("style", "alignment", "heading_level"):
            if field_name in value:
                property_values[field_name] = value[field_name]
        return UpdateParagraphProperties(**property_values)
    if operation_type == "format_text_match":
        _reject_unknown_keys(
            value,
            {
                "type",
                "match_id",
                "block_id",
                "expected_text",
                "bold",
                "italic",
                "underline",
            },
            f"第 {index} 个 format_text_match",
            "invalid_edit_operation",
        )
        if (
            "expected_text" not in value
            or not isinstance(value["expected_text"], str)
            or not value["expected_text"]
        ):
            raise DocxError(
                "invalid_edit_operation",
                "format_text_match.expected_text 必须是非空字符串。",
            )
        return FormatTextMatch(
            match_id=value.get("match_id"),
            block_id=value.get("block_id"),
            expected_text=value["expected_text"],
            bold=value.get("bold"),
            italic=value.get("italic"),
            underline=value.get("underline"),
        )
    if operation_type == "insert_table_after":
        _reject_unknown_keys(
            value,
            {"type", "block_id", "rows", "header_row"},
            f"第 {index} 个 insert_table_after",
            "invalid_edit_operation",
        )
        return InsertTableAfter(
            block_id=value.get("block_id"),
            rows=value.get("rows"),
            header_row=value.get("header_row", False),
        )
    if operation_type == "append_table_row":
        _reject_unknown_keys(
            value,
            {"type", "table_block_id", "cells"},
            f"第 {index} 个 append_table_row",
            "invalid_edit_operation",
        )
        return AppendTableRow(
            table_block_id=value.get("table_block_id"),
            cells=value.get("cells"),
        )
    if operation_type == "delete_table_row":
        _reject_unknown_keys(
            value,
            {"type", "table_block_id", "row_index"},
            f"第 {index} 个 delete_table_row",
            "invalid_edit_operation",
        )
        return DeleteTableRow(
            table_block_id=value.get("table_block_id"),
            row_index=value.get("row_index"),
        )
    if operation_type == "insert_image_after":
        _reject_unknown_keys(
            value,
            {
                "type",
                "block_id",
                "image_path",
                "width_px",
                "height_px",
                "alt_text",
            },
            f"第 {index} 个 insert_image_after",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"block_id", "image_path"},
            index,
            operation_type,
        )
        if (
            not isinstance(value["image_path"], str)
            or not value["image_path"]
        ):
            raise DocxError(
                "invalid_edit_operation",
                "insert_image_after.image_path 必须是非空路径字符串。",
            )
        return InsertImageAfter(
            block_id=value["block_id"],
            image_path=Path(value["image_path"]),
            width_px=value.get("width_px"),
            height_px=value.get("height_px"),
            alt_text=value.get("alt_text"),
        )
    if operation_type == "insert_hyperlink_after":
        _reject_unknown_keys(
            value,
            {"type", "block_id", "text", "url"},
            f"第 {index} 个 insert_hyperlink_after",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"block_id", "text", "url"},
            index,
            operation_type,
        )
        return InsertHyperlinkAfter(
            block_id=value["block_id"],
            text=value["text"],
            url=value["url"],
        )
    if operation_type in {
        "insert_bullet_list_after",
        "insert_numbered_list_after",
    }:
        _reject_unknown_keys(
            value,
            {"type", "block_id", "items"},
            f"第 {index} 个 {operation_type}",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"block_id", "items"},
            index,
            operation_type,
        )
        operation_class = (
            InsertBulletListAfter
            if operation_type == "insert_bullet_list_after"
            else InsertNumberedListAfter
        )
        return operation_class(
            block_id=value["block_id"],
            items=value["items"],
        )
    if operation_type == "update_page_setup":
        _reject_unknown_keys(
            value,
            {
                "type",
                "section_index",
                "page_size",
                "orientation",
                "margin_top_twips",
                "margin_bottom_twips",
                "margin_left_twips",
                "margin_right_twips",
            },
            f"第 {index} 个 update_page_setup",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"section_index"},
            index,
            operation_type,
        )
        return UpdatePageSetup(
            section_index=value["section_index"],
            page_size=value.get("page_size"),
            orientation=value.get("orientation"),
            margin_top_twips=value.get("margin_top_twips"),
            margin_bottom_twips=value.get("margin_bottom_twips"),
            margin_left_twips=value.get("margin_left_twips"),
            margin_right_twips=value.get("margin_right_twips"),
        )
    if operation_type == "update_header_text":
        _reject_unknown_keys(
            value,
            {"type", "section_index", "text"},
            f"第 {index} 个 update_header_text",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"section_index", "text"},
            index,
            operation_type,
        )
        return UpdateHeaderText(
            section_index=value["section_index"],
            text=value["text"],
        )
    if operation_type == "update_footer_text":
        _reject_unknown_keys(
            value,
            {"type", "section_index", "text", "include_page_number"},
            f"第 {index} 个 update_footer_text",
            "invalid_edit_operation",
        )
        _require_operation_keys(
            value,
            {"section_index", "text"},
            index,
            operation_type,
        )
        return UpdateFooterText(
            section_index=value["section_index"],
            text=value["text"],
            include_page_number=value.get("include_page_number", False),
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


def _require_operation_keys(
    value: dict[str, Any],
    required_keys: set[str],
    operation_index: int,
    operation_type: str,
) -> None:
    missing = sorted(required_keys - set(value))
    if missing:
        raise DocxError(
            "invalid_edit_operation",
            f"第 {operation_index} 个 {operation_type} 缺少必填字段。",
        )


def _parse_edit_runs(
    value: object,
    operation_index: int,
    operation_type: str,
) -> list[TextRunSpec]:
    if not isinstance(value, list):
        raise DocxError(
            "invalid_edit_operation",
            f"第 {operation_index} 个 {operation_type} 的 runs 必须是列表。",
        )
    return [
        _parse_edit_run(run, operation_index, run_index, operation_type)
        for run_index, run in enumerate(value)
    ]


def _parse_edit_run(
    value: object,
    operation_index: int,
    run_index: int,
    operation_type: str,
) -> TextRunSpec:
    if not isinstance(value, dict):
        raise DocxError(
            "invalid_edit_operation",
            f"第 {operation_index} 个 {operation_type} 的第 {run_index} 个 run 必须是 JSON object。",
        )
    _reject_unknown_keys(
        value,
        {"text", "bold", "italic", "underline"},
        f"第 {operation_index} 个 {operation_type} 的第 {run_index} 个 run",
        "invalid_edit_operation",
    )
    return TextRunSpec(
        text=value.get("text"),
        bold=value.get("bold", False),
        italic=value.get("italic", False),
        underline=value.get("underline", False),
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
