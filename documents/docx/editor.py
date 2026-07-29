"""基于 revision 与 block_id 的独立 DOCX 安全编辑器。"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from .atomic import (
    best_effort_unlink,
    commit_with_overwrite,
    commit_without_overwrite,
    create_temporary_output_path,
)
from .errors import DocxError
from .locator import (
    BodyChildLocation,
    EditTargetLocation,
    TableCellLocation,
    build_edit_target_index,
    get_single_cell_paragraph,
    is_strictly_editable_table,
    is_paragraph_block_id,
    is_strictly_editable_paragraph,
    is_strictly_editable_table_cell,
    is_table_block_id,
    is_table_cell_block_id,
    iter_body_children,
    iter_table_cells,
    visible_row_cells,
    visible_table_rows,
)
from .models import (
    AppliedEdit,
    AppendParagraph,
    AppendTableRow,
    BlockRemap,
    DeleteParagraph,
    DeleteTableRow,
    DocumentSnapshot,
    EditDocumentRequest,
    EditDocumentResult,
    FormatTextMatch,
    InspectDocumentRequest,
    InsertParagraphAfter,
    InsertParagraphBefore,
    InsertTableAfter,
    ParagraphSnapshot,
    ReplaceParagraphText,
    ReplaceTableCellText,
    ReplaceTextMatch,
    TableCellSnapshot,
    TableSnapshot,
    TextRunSpec,
    UNSET,
    UpdateParagraphProperties,
    UpdateDocumentMetadata,
)
from .package import DocxPackage
from .package_mutation import (
    PackageMutation,
    validate_package_mutation,
    verify_package_mutation,
)
from .parts import ContentTypesManager
from .reader import DocxReader
from .rich_content import (
    EMPTY_RICH_CONTENT_PLAN,
    PlannedRichBodyInsertion,
    RICH_OPERATION_TYPES,
    RichContentPlan,
    ValidatedRichOperation,
    prepare_rich_content_plan,
    rich_operation_block_id,
    validate_rich_operation,
    validate_rich_operation_conflicts,
    verify_rich_content_output,
)
from .search import build_match_id, iter_literal_matches
from .textmap import (
    VisibleTextMap,
    VisibleTextFormatting,
    VisibleTextReplacement,
    append_text_content,
    build_visible_text_map,
    format_visible_text_ranges,
    read_run_direct_format,
    replace_visible_text_ranges,
)
from .writer import (
    parse_xml_preserving_misc,
    serialize_xml,
    write_original_package,
    write_package,
)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_W_BODY = f"{{{_W_NS}}}body"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_RUN = f"{{{_W_NS}}}r"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_PARAGRAPH_STYLE = f"{{{_W_NS}}}pStyle"
_W_JUSTIFICATION = f"{{{_W_NS}}}jc"
_W_SECTION_PROPERTIES = f"{{{_W_NS}}}sectPr"
_W_TABLE = f"{{{_W_NS}}}tbl"
_W_TABLE_PROPERTIES = f"{{{_W_NS}}}tblPr"
_W_TABLE_GRID = f"{{{_W_NS}}}tblGrid"
_W_GRID_COLUMN = f"{{{_W_NS}}}gridCol"
_W_ROW = f"{{{_W_NS}}}tr"
_W_ROW_PROPERTIES = f"{{{_W_NS}}}trPr"
_W_TABLE_HEADER = f"{{{_W_NS}}}tblHeader"
_W_CELL = f"{{{_W_NS}}}tc"
_W_CELL_PROPERTIES = f"{{{_W_NS}}}tcPr"
_W_BOLD = f"{{{_W_NS}}}b"
_W_ITALIC = f"{{{_W_NS}}}i"
_W_UNDERLINE = f"{{{_W_NS}}}u"
_W_VAL = f"{{{_W_NS}}}val"

_CORE_PROPERTIES = f"{{{_CP_NS}}}coreProperties"
_RELATIONSHIPS = f"{{{_PACKAGE_REL_NS}}}Relationships"
_RELATIONSHIP = f"{{{_PACKAGE_REL_NS}}}Relationship"

_CORE_PART = "docProps/core.xml"
_DOCUMENT_PART = "word/document.xml"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELATIONSHIPS_PART = "_rels/.rels"
_CORE_CONTENT_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"
_CORE_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/package/2006/relationships/"
    "metadata/core-properties"
)
_CORE_RELATIONSHIP_TARGET = "docProps/core.xml"
_METADATA_QNAMES = {
    "title": f"{{{_DC_NS}}}title",
    "creator": f"{{{_DC_NS}}}creator",
    "subject": f"{{{_DC_NS}}}subject",
    "description": f"{{{_DC_NS}}}description",
}
_METADATA_FIELDS = frozenset(_METADATA_QNAMES)
_ALL_METADATA_FIELDS = (
    "title",
    "creator",
    "subject",
    "description",
    "created",
    "modified",
    "last_modified_by",
)
_REVISION_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MATCH_ID_PATTERN = re.compile(r"match:[0-9a-f]{64}\Z")
_TABLE_CELL_ROW_PATTERN = re.compile(
    r"body:table:(?:0|[1-9]\d*):row:(0|[1-9]\d*):cell:"
    r"(?:0|[1-9]\d*)\Z"
)
_ALLOWED_ALIGNMENTS = frozenset({"left", "center", "right", "justify"})
_ALIGNMENT_TO_WML = {
    "left": "left",
    "center": "center",
    "right": "right",
    "justify": "both",
}


@dataclass(frozen=True)
class _ValidatedTextOperation:
    operation_index: int
    operation_type: str
    block_id: str
    text: str
    preserve_first_run_format: bool
    target_kind: str


@dataclass(frozen=True)
class _ValidatedMetadataOperation:
    operation_index: int
    operation_type: str
    fields: dict[str, str | None]


@dataclass(frozen=True)
class _ValidatedMatchOperation:
    operation_index: int
    operation_type: str
    match_id: str
    block_id: str
    expected_text: str
    replacement_text: str
    preserve_format: bool
    target_kind: str


@dataclass(frozen=True)
class _ValidatedRun:
    text: str
    bold: bool
    italic: bool
    underline: bool


@dataclass(frozen=True)
class _ValidatedParagraphInsertion:
    operation_index: int
    operation_type: str
    block_id: str | None
    position: Literal["before", "after", "append"]
    runs: tuple[_ValidatedRun, ...]
    style: str | None
    alignment: str | None


@dataclass(frozen=True)
class _ValidatedDeleteParagraph:
    operation_index: int
    operation_type: str
    block_id: str


@dataclass(frozen=True)
class _ValidatedParagraphProperties:
    operation_index: int
    operation_type: str
    block_id: str
    style_is_set: bool
    style: str | None
    alignment_is_set: bool
    alignment: str | None
    heading_level_is_set: bool
    heading_level: int | None


@dataclass(frozen=True)
class _ValidatedFormatOperation:
    operation_index: int
    operation_type: str
    match_id: str
    block_id: str
    expected_text: str
    bold: bool | None
    italic: bool | None
    underline: bool | None
    target_kind: str


@dataclass(frozen=True)
class _ValidatedInsertTable:
    operation_index: int
    operation_type: str
    block_id: str
    rows: tuple[tuple[str, ...], ...]
    header_row: bool


@dataclass(frozen=True)
class _ValidatedAppendTableRow:
    operation_index: int
    operation_type: str
    table_block_id: str
    cells: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedDeleteTableRow:
    operation_index: int
    operation_type: str
    table_block_id: str
    row_index: int


_ValidatedOperation = (
    _ValidatedTextOperation
    | _ValidatedMatchOperation
    | _ValidatedParagraphInsertion
    | _ValidatedDeleteParagraph
    | _ValidatedParagraphProperties
    | _ValidatedFormatOperation
    | _ValidatedInsertTable
    | _ValidatedAppendTableRow
    | _ValidatedDeleteTableRow
    | _ValidatedMetadataOperation
    | ValidatedRichOperation
)


@dataclass(frozen=True)
class _PlannedTextEdit:
    operation: _ValidatedTextOperation
    paragraph: ElementTree.Element
    current_text: str


@dataclass(frozen=True)
class _ResolvedMatchEdit:
    operation: _ValidatedMatchOperation
    start: int
    end: int


@dataclass(frozen=True)
class _PlannedMatchGroup:
    block_id: str
    text_map: VisibleTextMap
    edits: tuple[_ResolvedMatchEdit, ...]
    expected_text: str


@dataclass(frozen=True)
class _ResolvedFormatEdit:
    operation: _ValidatedFormatOperation
    start: int
    end: int


@dataclass(frozen=True)
class _PlannedFormatGroup:
    block_id: str
    text_map: VisibleTextMap
    edits: tuple[_ResolvedFormatEdit, ...]


@dataclass(frozen=True)
class _PlannedParagraphInsertion:
    operation: _ValidatedParagraphInsertion
    anchor: ElementTree.Element | None
    paragraph: ElementTree.Element


@dataclass(frozen=True)
class _PlannedParagraphProperties:
    operation: _ValidatedParagraphProperties
    paragraph: ElementTree.Element


@dataclass(frozen=True)
class _PlannedTableInsertion:
    operation: _ValidatedInsertTable
    anchor: ElementTree.Element
    table: ElementTree.Element


@dataclass(frozen=True)
class _PlannedTableMutation:
    table_block_id: str
    table: ElementTree.Element
    deleted_rows: tuple[ElementTree.Element, ...]
    appended_rows: tuple[ElementTree.Element, ...]


@dataclass(frozen=True)
class _ExtendedEditPlan:
    paragraph_insertions: tuple[_PlannedParagraphInsertion, ...]
    deleted_paragraphs: tuple[ElementTree.Element, ...]
    paragraph_properties: tuple[_PlannedParagraphProperties, ...]
    format_groups: tuple[_PlannedFormatGroup, ...]
    table_insertions: tuple[_PlannedTableInsertion, ...]
    table_mutations: tuple[_PlannedTableMutation, ...]
    rich_insertions: tuple[PlannedRichBodyInsertion, ...]
    structural_changed: bool


@dataclass(frozen=True)
class _ExpectedFormatRange:
    block_id: str
    start: int
    end: int
    bold: bool | None
    italic: bool | None
    underline: bool | None


@dataclass(frozen=True)
class _ExpectedDocumentState:
    block_remap: tuple[BlockRemap, ...]
    top_level_blocks: tuple[tuple[str, str], ...]
    block_ids: frozenset[str]
    target_texts: dict[str, str]
    paragraph_properties: dict[str, tuple[str | None, str | None]]
    table_shapes: dict[str, tuple[int, ...]]
    editable_block_ids: frozenset[str]
    strict_table_block_ids: frozenset[str]
    format_ranges: tuple[_ExpectedFormatRange, ...]
    paragraph_count: int
    table_count: int


@dataclass(frozen=True)
class _SnapshotTableCellTarget:
    """保留单元格快照及其所属表格的内部编辑上下文。"""

    cell: TableCellSnapshot
    table: TableSnapshot


_SnapshotTextTarget = ParagraphSnapshot | _SnapshotTableCellTarget


@dataclass
class _MetadataContext:
    operation: _ValidatedMetadataOperation
    core_root: ElementTree.Element
    core_original: bytes | None
    content_types: ContentTypesManager
    relationships_root: ElementTree.Element
    relationships_original: bytes
    core_relationship: ElementTree.Element | None


class DocxEditor:
    """执行全有或全无的简单 DOCX 内容与结构编辑。"""

    def __init__(self, reader: DocxReader | None = None) -> None:
        self._reader = reader or DocxReader()

    def edit(self, request: EditDocumentRequest) -> EditDocumentResult:
        """校验、修改、重读验证并原子提交新的 DOCX。"""

        (
            source_path,
            output_path,
            expected_revision,
            overwrite,
            operations,
        ) = _validate_request(request)
        initial_snapshot = self._reader.inspect(
            InspectDocumentRequest(
                source_path=source_path,
                include_runs=False,
                include_tables=True,
            )
        )
        _require_revision(initial_snapshot.revision, expected_revision)

        replacements: dict[str, bytes] = {}
        additions: dict[str, bytes] = {}
        rich_plan = EMPTY_RICH_CONTENT_PLAN
        applied_edits = [
            AppliedEdit(
                operation_index=operation.operation_index,
                operation_type=operation.operation_type,
                block_id=_validated_operation_block_id(operation),
            )
            for operation in operations
        ]

        with DocxPackage.open(source_path) as package:
            _require_revision(package.revision, expected_revision)
            document_original = package.read_xml_bytes(_DOCUMENT_PART)
            document_root = parse_xml_preserving_misc(
                document_original,
                _DOCUMENT_PART,
            )
            body = document_root.find(_W_BODY)
            if body is None:
                raise DocxError(
                    "invalid_docx_package",
                    "word/document.xml 缺少 w:body。",
                )
            original_block_elements = _document_block_element_index(body)
            if set(original_block_elements) != _snapshot_block_ids(
                initial_snapshot
            ):
                raise DocxError(
                    "edit_verification_failed",
                    "Reader 与结构 locator 的初始 block_id 不一致。",
                )

            (
                planned_text_edits,
                planned_match_edits,
                expected_target_texts,
            ) = _plan_text_edits(
                operations,
                body=body,
                snapshot=initial_snapshot,
            )
            rich_operations = [
                operation
                for operation in operations
                if isinstance(operation, RICH_OPERATION_TYPES)
            ]
            needs_content_types = bool(rich_operations) or any(
                isinstance(operation, _ValidatedMetadataOperation)
                for operation in operations
            )
            content_types_original: bytes | None = None
            content_types: ContentTypesManager | None = None
            if needs_content_types:
                content_types_original = package.read_xml_bytes(
                    _CONTENT_TYPES_PART
                )
                content_types = ContentTypesManager(
                    parse_xml_preserving_misc(
                        content_types_original,
                        _CONTENT_TYPES_PART,
                    ),
                    validate_order=False,
                )
            metadata_context = _prepare_metadata_context(
                package,
                operations,
                content_types=content_types,
            )
            if metadata_context is not None:
                for part_name, payload in _apply_metadata_edit(
                    metadata_context
                ).items():
                    target = replacements if package.has_part(part_name) else additions
                    target[part_name] = payload
            rich_plan = prepare_rich_content_plan(
                package=package,
                operations=rich_operations,
                document_root=document_root,
                body=body,
                snapshot=initial_snapshot,
                content_types=content_types,
            )
            if content_types is not None:
                rich_plan = replace(
                    rich_plan,
                    content_types_changed=content_types.changed,
                )
            _merge_rich_package_changes(
                replacements,
                additions,
                rich_plan,
            )
            if content_types is not None and content_types.changed:
                if content_types_original is None:
                    raise DocxError(
                        "edit_verification_failed",
                        "Content Types 修改计划缺少原始 payload。",
                    )
                content_types.validate_canonical_order()
                replacements[_CONTENT_TYPES_PART] = serialize_xml(
                    content_types.root,
                    original_payload=content_types_original,
                )
            extended_plan = _plan_extended_edits(
                operations,
                body=body,
                snapshot=initial_snapshot,
                rich_insertions=rich_plan.body_insertions,
            )

            document_changed = rich_plan.document_changed
            for plan in planned_text_edits:
                if _apply_text_edit(plan):
                    document_changed = True
            for plan in planned_match_edits:
                if _apply_match_group(plan):
                    document_changed = True
            for plan in extended_plan.paragraph_properties:
                if _apply_paragraph_properties(plan):
                    document_changed = True
            for plan in extended_plan.format_groups:
                if _apply_format_group(plan):
                    document_changed = True
            for plan in extended_plan.table_mutations:
                if _apply_table_mutation(plan):
                    document_changed = True
            if _apply_body_mutations(body, extended_plan):
                document_changed = True
            if extended_plan.structural_changed:
                document_changed = True

            expected_state = _build_expected_document_state(
                body=body,
                initial_snapshot=initial_snapshot,
                original_block_elements=original_block_elements,
                expected_target_texts=expected_target_texts,
                operations=operations,
                extended_plan=extended_plan,
            )
            if document_changed:
                replacements[_DOCUMENT_PART] = serialize_xml(
                    document_root,
                    original_payload=document_original,
                )

            mutation = PackageMutation(
                replacements=replacements,
                additions=additions,
            )
            validate_package_mutation(package, mutation)

        changed = bool(
            mutation.replacements
            or mutation.additions
            or mutation.deletions
        )
        temporary_output: Path | None = None
        try:
            with DocxPackage.open(source_path) as current_package:
                _require_revision(current_package.revision, expected_revision)
                temporary_output = create_temporary_output_path(output_path)
                if changed:
                    write_package(current_package, temporary_output, mutation)
                else:
                    write_original_package(current_package, temporary_output)

            output_snapshot = _inspect_temporary_output(
                self._reader,
                temporary_output,
            )
            output_body = _read_temporary_output_body(
                temporary_output,
                expected_revision=output_snapshot.revision,
            )
            _verify_output(
                initial_snapshot=initial_snapshot,
                output_snapshot=output_snapshot,
                operations=operations,
                expected_state=expected_state,
                output_body=output_body,
                changed=changed,
                expected_image_count=(
                    initial_snapshot.image_count
                    + rich_plan.added_image_count
                ),
            )
            _verify_temporary_package(
                source_path=source_path,
                temporary_output=temporary_output,
                expected_revision=expected_revision,
                output_snapshot=output_snapshot,
                initial_snapshot=initial_snapshot,
                mutation=mutation,
                rich_plan=rich_plan,
                verify_rich=bool(rich_operations),
            )
            if overwrite:
                commit_with_overwrite(temporary_output, output_path)
            else:
                commit_without_overwrite(temporary_output, output_path)

            return EditDocumentResult(
                source_path=source_path,
                output_path=output_path,
                old_revision=initial_snapshot.revision,
                new_revision=output_snapshot.revision,
                size_bytes=output_snapshot.size_bytes,
                sha256=output_snapshot.revision.removeprefix("sha256:"),
                changed=changed,
                applied_edits=applied_edits,
                block_remap=list(expected_state.block_remap),
            )
        finally:
            best_effort_unlink(temporary_output)


def edit_document(request: EditDocumentRequest) -> EditDocumentResult:
    """使用一次性 Editor 实例修改现有 DOCX。"""

    return DocxEditor().edit(request)


def _merge_rich_package_changes(
    replacements: dict[str, bytes],
    additions: dict[str, bytes],
    plan: RichContentPlan,
) -> None:
    """合并富内容 part，并只允许基于 metadata 结果继续更新 Content Types。"""

    for part_name, payload in plan.replacements.items():
        if part_name in additions:
            raise DocxError(
                "package_mutation_conflict",
                "富内容 replacement 与已有 addition 冲突。",
            )
        if part_name in replacements and part_name != _CONTENT_TYPES_PART:
            raise DocxError(
                "package_mutation_conflict",
                "多个编辑计划尝试替换同一个 DOCX part。",
            )
        replacements[part_name] = payload
    for part_name, payload in plan.additions.items():
        if part_name in replacements or part_name in additions:
            raise DocxError(
                "package_mutation_conflict",
                "多个编辑计划尝试添加同一个 DOCX part。",
            )
        additions[part_name] = payload


def _validate_request(
    request: EditDocumentRequest,
) -> tuple[Path, Path, str, bool, list[_ValidatedOperation]]:
    if not isinstance(request, EditDocumentRequest):
        raise DocxError("invalid_request", "request 必须是 EditDocumentRequest。")
    if not isinstance(request.source_path, (str, os.PathLike)):
        raise DocxError("invalid_request", "source_path 必须是文件系统路径。")
    if not isinstance(request.output_path, (str, os.PathLike)):
        raise DocxError("invalid_output_path", "output_path 必须是文件系统路径。")
    if not isinstance(request.expected_revision, str) or not _REVISION_PATTERN.fullmatch(
        request.expected_revision
    ):
        raise DocxError(
            "invalid_request",
            "expected_revision 必须是 sha256:<64 位小写十六进制摘要>。",
        )
    if not isinstance(request.overwrite, bool):
        raise DocxError("invalid_request", "overwrite 必须是布尔值。")
    if not isinstance(request.operations, list) or not request.operations:
        raise DocxError("invalid_request", "operations 必须是非空列表。")

    source_path = _normalize_path(request.source_path, "source_path", "invalid_request")
    output_path = _normalize_path(
        request.output_path,
        "output_path",
        "invalid_output_path",
    )
    if output_path.suffix.lower() != ".docx":
        raise DocxError("unsupported_extension", "输出文件扩展名必须是 .docx。")
    if _paths_refer_to_same_file(source_path, output_path):
        raise DocxError("source_output_same", "源 DOCX 和输出 DOCX 不能是同一文件。")
    if not output_path.parent.exists() or not output_path.parent.is_dir():
        raise DocxError("invalid_output_path", "输出 DOCX 的父目录必须已经存在。")
    if os.path.lexists(output_path):
        if output_path.is_dir():
            raise DocxError("invalid_output_path", "输出路径不能是目录。")
        if not request.overwrite:
            raise DocxError("output_exists", "目标 DOCX 已存在。")

    operations = _validate_operations(request.operations)
    return (
        source_path,
        output_path,
        request.expected_revision,
        request.overwrite,
        operations,
    )


def _validate_operations(operations: list[object]) -> list[_ValidatedOperation]:
    validated: list[_ValidatedOperation] = []
    metadata_seen = False

    for operation_index, operation in enumerate(operations):
        if isinstance(operation, ReplaceParagraphText):
            validated_operation = _validate_text_operation(
                operation,
                operation_index=operation_index,
                operation_type="replace_paragraph_text",
                target_kind="paragraph",
            )
        elif isinstance(operation, ReplaceTableCellText):
            validated_operation = _validate_text_operation(
                operation,
                operation_index=operation_index,
                operation_type="replace_table_cell_text",
                target_kind="table_cell",
            )
        elif isinstance(operation, ReplaceTextMatch):
            validated_operation = _validate_match_operation(
                operation,
                operation_index,
            )
        elif isinstance(operation, InsertParagraphBefore):
            validated_operation = _validate_paragraph_insertion(
                operation,
                operation_index=operation_index,
                operation_type="insert_paragraph_before",
                position="before",
            )
        elif isinstance(operation, InsertParagraphAfter):
            validated_operation = _validate_paragraph_insertion(
                operation,
                operation_index=operation_index,
                operation_type="insert_paragraph_after",
                position="after",
            )
        elif isinstance(operation, AppendParagraph):
            validated_operation = _validate_paragraph_insertion(
                operation,
                operation_index=operation_index,
                operation_type="append_paragraph",
                position="append",
            )
        elif isinstance(operation, DeleteParagraph):
            validated_operation = _validate_delete_paragraph(
                operation,
                operation_index,
            )
        elif isinstance(operation, UpdateParagraphProperties):
            validated_operation = _validate_paragraph_properties(
                operation,
                operation_index,
            )
        elif isinstance(operation, FormatTextMatch):
            validated_operation = _validate_format_operation(
                operation,
                operation_index,
            )
        elif isinstance(operation, InsertTableAfter):
            validated_operation = _validate_insert_table(
                operation,
                operation_index,
            )
        elif isinstance(operation, AppendTableRow):
            validated_operation = _validate_append_table_row(
                operation,
                operation_index,
            )
        elif isinstance(operation, DeleteTableRow):
            validated_operation = _validate_delete_table_row(
                operation,
                operation_index,
            )
        elif isinstance(operation, UpdateDocumentMetadata):
            if metadata_seen:
                raise DocxError(
                    "edit_operation_conflict",
                    "一个请求最多包含一个 metadata 更新操作。",
                )
            metadata_seen = True
            validated_operation = _validate_metadata_operation(
                operation,
                operation_index,
            )
        else:
            rich_operation = validate_rich_operation(
                operation,
                operation_index,
            )
            if rich_operation is None:
                raise DocxError(
                    "invalid_edit_operation",
                    f"第 {operation_index} 个编辑操作类型不受支持。",
                )
            validated_operation = rich_operation

        validated.append(validated_operation)
    validate_rich_operation_conflicts(
        [
            operation
            for operation in validated
            if isinstance(operation, RICH_OPERATION_TYPES)
        ]
    )
    _validate_operation_conflicts(validated)
    return validated


def _validated_operation_block_id(
    operation: _ValidatedOperation,
) -> str | None:
    if isinstance(
        operation,
        (
            _ValidatedTextOperation,
            _ValidatedMatchOperation,
            _ValidatedDeleteParagraph,
            _ValidatedParagraphProperties,
            _ValidatedFormatOperation,
            _ValidatedInsertTable,
        ),
    ):
        return operation.block_id
    if isinstance(operation, _ValidatedParagraphInsertion):
        return operation.block_id
    if isinstance(
        operation,
        (_ValidatedAppendTableRow, _ValidatedDeleteTableRow),
    ):
        return operation.table_block_id
    if isinstance(operation, RICH_OPERATION_TYPES):
        return rich_operation_block_id(operation)
    return None


def _validate_operation_conflicts(
    operations: list[_ValidatedOperation],
) -> None:
    whole_targets: set[str] = set()
    match_targets: set[str] = set()
    format_targets: set[str] = set()
    seen_match_ids: set[str] = set()
    deleted_paragraphs: set[str] = set()
    property_targets: set[str] = set()
    insertion_anchors: set[str] = set()
    deleted_rows: set[tuple[str, int]] = set()

    for operation in operations:
        if isinstance(operation, _ValidatedTextOperation):
            if operation.block_id in whole_targets:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个 block_id 在一次请求中只能执行一次整块替换。",
                )
            whole_targets.add(operation.block_id)
        elif isinstance(
            operation,
            (_ValidatedMatchOperation, _ValidatedFormatOperation),
        ):
            if operation.match_id in seen_match_ids:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个 match_id 在一次请求中只能修改一次。",
                )
            seen_match_ids.add(operation.match_id)
            if isinstance(operation, _ValidatedMatchOperation):
                match_targets.add(operation.block_id)
            else:
                format_targets.add(operation.block_id)
        elif isinstance(operation, _ValidatedDeleteParagraph):
            if operation.block_id in deleted_paragraphs:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个段落在一次请求中只能删除一次。",
                )
            deleted_paragraphs.add(operation.block_id)
        elif isinstance(operation, _ValidatedParagraphProperties):
            if operation.block_id in property_targets:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个段落在一次请求中只能更新一次属性。",
                )
            property_targets.add(operation.block_id)
        elif isinstance(operation, _ValidatedParagraphInsertion):
            if operation.block_id is not None:
                insertion_anchors.add(operation.block_id)
        elif isinstance(operation, _ValidatedInsertTable):
            insertion_anchors.add(operation.block_id)
        elif isinstance(operation, RICH_OPERATION_TYPES):
            block_id = rich_operation_block_id(operation)
            if block_id is not None:
                insertion_anchors.add(block_id)
        elif isinstance(operation, _ValidatedDeleteTableRow):
            row_target = (operation.table_block_id, operation.row_index)
            if row_target in deleted_rows:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个表格行在一次请求中只能删除一次。",
                )
            deleted_rows.add(row_target)

    if whole_targets.intersection(match_targets | format_targets):
        raise DocxError(
            "edit_operation_conflict",
            "同一个 block 不能同时执行整块替换与局部文字或格式修改。",
        )
    if match_targets.intersection(format_targets):
        raise DocxError(
            "edit_operation_conflict",
            "同一个 block 不能同时执行局部替换和局部格式修改。",
        )
    if deleted_paragraphs.intersection(
        whole_targets
        | match_targets
        | format_targets
        | property_targets
        | insertion_anchors
    ):
        raise DocxError(
            "edit_operation_conflict",
            "被删除的段落不能同时作为其他编辑操作的目标或锚点。",
        )
    if property_targets.intersection(whole_targets):
        raise DocxError(
            "edit_operation_conflict",
            "同一个段落不能同时执行整段替换和段落属性更新。",
        )

    for table_block_id, row_index in deleted_rows:
        for block_id in whole_targets | match_targets | format_targets:
            if (
                block_id.startswith(f"{table_block_id}:row:")
                and _table_cell_row_index(block_id) == row_index
            ):
                raise DocxError(
                    "edit_operation_conflict",
                    "被删除的表格行不能同时包含文字或格式编辑操作。",
                )


def _validate_text_operation(
    operation: ReplaceParagraphText | ReplaceTableCellText,
    *,
    operation_index: int,
    operation_type: str,
    target_kind: str,
) -> _ValidatedTextOperation:
    if not isinstance(operation.block_id, str):
        raise DocxError("invalid_edit_operation", "block_id 必须是字符串。")
    if target_kind == "paragraph":
        valid_block_id = is_paragraph_block_id(operation.block_id)
    else:
        valid_block_id = is_table_cell_block_id(operation.block_id)
    if not valid_block_id:
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 block_id 格式无效。",
        )
    if (
        not isinstance(operation.text, str)
        or "\r" in operation.text
        or not _is_valid_xml_text(operation.text)
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 text 不是受支持的 XML 文本；换行请使用 \\n。",
        )
    if not isinstance(operation.preserve_first_run_format, bool):
        raise DocxError(
            "invalid_edit_operation",
            "preserve_first_run_format 必须是布尔值。",
        )
    return _ValidatedTextOperation(
        operation_index=operation_index,
        operation_type=operation_type,
        block_id=operation.block_id,
        text=operation.text,
        preserve_first_run_format=operation.preserve_first_run_format,
        target_kind=target_kind,
    )


def _validate_metadata_operation(
    operation: UpdateDocumentMetadata,
    operation_index: int,
) -> _ValidatedMetadataOperation:
    if not isinstance(operation.fields, dict) or not operation.fields:
        raise DocxError(
            "invalid_edit_operation",
            "metadata fields 必须是非空 object。",
        )
    unknown_fields = set(operation.fields) - _METADATA_FIELDS
    if unknown_fields or not all(isinstance(key, str) for key in operation.fields):
        raise DocxError(
            "invalid_edit_operation",
            "metadata fields 包含不受支持的字段。",
        )
    copied_fields: dict[str, str | None] = {}
    for field_name, value in operation.fields.items():
        if value is not None and (
            not isinstance(value, str)
            or "\r" in value
            or not _is_valid_xml_text(value)
        ):
            raise DocxError(
                "invalid_edit_operation",
                f"metadata 字段 {field_name} 必须是受支持的 XML 字符串或 null。",
            )
        copied_fields[field_name] = value
    return _ValidatedMetadataOperation(
        operation_index=operation_index,
        operation_type="update_document_metadata",
        fields=copied_fields,
    )


def _validate_match_operation(
    operation: ReplaceTextMatch,
    operation_index: int,
) -> _ValidatedMatchOperation:
    if (
        not isinstance(operation.match_id, str)
        or not _MATCH_ID_PATTERN.fullmatch(operation.match_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "replace_text_match 的 match_id 格式无效。",
        )
    if not isinstance(operation.block_id, str):
        raise DocxError("invalid_edit_operation", "block_id 必须是字符串。")
    if is_paragraph_block_id(operation.block_id):
        target_kind = "paragraph"
    elif is_table_cell_block_id(operation.block_id):
        target_kind = "table_cell"
    else:
        raise DocxError(
            "invalid_edit_operation",
            "replace_text_match 的 block_id 格式无效。",
        )
    if not isinstance(operation.expected_text, str) or not operation.expected_text:
        raise DocxError(
            "invalid_edit_operation",
            "replace_text_match 的 expected_text 必须是非空字符串。",
        )
    if (
        not isinstance(operation.replacement_text, str)
        or "\r" in operation.replacement_text
        or not _is_valid_xml_text(operation.replacement_text)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "replacement_text 不是受支持的 XML 文本；换行请使用 \\n。",
        )
    if not isinstance(operation.preserve_format, bool):
        raise DocxError(
            "invalid_edit_operation",
            "preserve_format 必须是布尔值。",
        )
    return _ValidatedMatchOperation(
        operation_index=operation_index,
        operation_type="replace_text_match",
        match_id=operation.match_id,
        block_id=operation.block_id,
        expected_text=operation.expected_text,
        replacement_text=operation.replacement_text,
        preserve_format=operation.preserve_format,
        target_kind=target_kind,
    )


def _validate_paragraph_insertion(
    operation: InsertParagraphBefore | InsertParagraphAfter | AppendParagraph,
    *,
    operation_index: int,
    operation_type: str,
    position: Literal["before", "after", "append"],
) -> _ValidatedParagraphInsertion:
    block_id = getattr(operation, "block_id", None)
    if position != "append" and (
        not isinstance(block_id, str) or not is_paragraph_block_id(block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 block_id 格式无效。",
        )
    runs = _validate_runs(operation.runs, operation_type)
    style = _validate_optional_style(operation.style, operation_type)
    alignment = _validate_optional_alignment(
        operation.alignment,
        operation_type,
    )
    return _ValidatedParagraphInsertion(
        operation_index=operation_index,
        operation_type=operation_type,
        block_id=block_id,
        position=position,
        runs=runs,
        style=style,
        alignment=alignment,
    )


def _validate_delete_paragraph(
    operation: DeleteParagraph,
    operation_index: int,
) -> _ValidatedDeleteParagraph:
    if (
        not isinstance(operation.block_id, str)
        or not is_paragraph_block_id(operation.block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "delete_paragraph 的 block_id 格式无效。",
        )
    return _ValidatedDeleteParagraph(
        operation_index=operation_index,
        operation_type="delete_paragraph",
        block_id=operation.block_id,
    )


def _validate_paragraph_properties(
    operation: UpdateParagraphProperties,
    operation_index: int,
) -> _ValidatedParagraphProperties:
    if (
        not isinstance(operation.block_id, str)
        or not is_paragraph_block_id(operation.block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "update_paragraph_properties 的 block_id 格式无效。",
        )
    style_is_set = operation.style is not UNSET
    alignment_is_set = operation.alignment is not UNSET
    heading_level_is_set = operation.heading_level is not UNSET
    if not (style_is_set or alignment_is_set or heading_level_is_set):
        raise DocxError(
            "invalid_edit_operation",
            "update_paragraph_properties 至少需要提供一个属性字段。",
        )
    if style_is_set and heading_level_is_set:
        raise DocxError(
            "invalid_edit_operation",
            "style 和 heading_level 不能在同一个操作中同时提供。",
        )

    style: str | None = None
    if style_is_set:
        style = _validate_optional_style(
            operation.style,
            "update_paragraph_properties",
        )
    alignment: str | None = None
    if alignment_is_set:
        alignment = _validate_optional_alignment(
            operation.alignment,
            "update_paragraph_properties",
        )
    heading_level: int | None = None
    if heading_level_is_set:
        if operation.heading_level is not None and (
            isinstance(operation.heading_level, bool)
            or not isinstance(operation.heading_level, int)
            or not 1 <= operation.heading_level <= 6
        ):
            raise DocxError(
                "invalid_edit_operation",
                "heading_level 必须是 1 到 6 之间的整数或 null。",
            )
        heading_level = operation.heading_level
    return _ValidatedParagraphProperties(
        operation_index=operation_index,
        operation_type="update_paragraph_properties",
        block_id=operation.block_id,
        style_is_set=style_is_set,
        style=style,
        alignment_is_set=alignment_is_set,
        alignment=alignment,
        heading_level_is_set=heading_level_is_set,
        heading_level=heading_level,
    )


def _validate_format_operation(
    operation: FormatTextMatch,
    operation_index: int,
) -> _ValidatedFormatOperation:
    if (
        not isinstance(operation.match_id, str)
        or not _MATCH_ID_PATTERN.fullmatch(operation.match_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "format_text_match 的 match_id 格式无效。",
        )
    if not isinstance(operation.block_id, str):
        raise DocxError("invalid_edit_operation", "block_id 必须是字符串。")
    if is_paragraph_block_id(operation.block_id):
        target_kind = "paragraph"
    elif is_table_cell_block_id(operation.block_id):
        target_kind = "table_cell"
    else:
        raise DocxError(
            "invalid_edit_operation",
            "format_text_match 的 block_id 格式无效。",
        )
    values = (operation.bold, operation.italic, operation.underline)
    if all(value is None for value in values):
        raise DocxError(
            "invalid_edit_operation",
            "format_text_match 至少需要提供一个格式字段。",
        )
    if not all(value is None or isinstance(value, bool) for value in values):
        raise DocxError(
            "invalid_edit_operation",
            "bold、italic 和 underline 必须是布尔值或 null。",
        )
    if (
        not isinstance(operation.expected_text, str)
        or not operation.expected_text
    ):
        raise DocxError(
            "invalid_edit_operation",
            "format_text_match.expected_text 必须是非空字符串。",
        )
    return _ValidatedFormatOperation(
        operation_index=operation_index,
        operation_type="format_text_match",
        match_id=operation.match_id,
        block_id=operation.block_id,
        expected_text=_validate_plain_text(
            operation.expected_text,
            "format_text_match.expected_text",
        ),
        bold=operation.bold,
        italic=operation.italic,
        underline=operation.underline,
        target_kind=target_kind,
    )


def _validate_insert_table(
    operation: InsertTableAfter,
    operation_index: int,
) -> _ValidatedInsertTable:
    if not isinstance(operation.block_id, str) or not (
        is_paragraph_block_id(operation.block_id)
        or is_table_block_id(operation.block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "insert_table_after 的 block_id 格式无效。",
        )
    rows = _validate_table_rows(operation.rows, "insert_table_after")
    if not isinstance(operation.header_row, bool):
        raise DocxError(
            "invalid_edit_operation",
            "header_row 必须是布尔值。",
        )
    return _ValidatedInsertTable(
        operation_index=operation_index,
        operation_type="insert_table_after",
        block_id=operation.block_id,
        rows=rows,
        header_row=operation.header_row,
    )


def _validate_append_table_row(
    operation: AppendTableRow,
    operation_index: int,
) -> _ValidatedAppendTableRow:
    if (
        not isinstance(operation.table_block_id, str)
        or not is_table_block_id(operation.table_block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "append_table_row 的 table_block_id 格式无效。",
        )
    if not isinstance(operation.cells, list) or not operation.cells:
        raise DocxError(
            "invalid_edit_operation",
            "append_table_row 的 cells 必须是非空列表。",
        )
    cells = tuple(
        _validate_plain_text(cell, "append_table_row 单元格")
        for cell in operation.cells
    )
    return _ValidatedAppendTableRow(
        operation_index=operation_index,
        operation_type="append_table_row",
        table_block_id=operation.table_block_id,
        cells=cells,
    )


def _validate_delete_table_row(
    operation: DeleteTableRow,
    operation_index: int,
) -> _ValidatedDeleteTableRow:
    if (
        not isinstance(operation.table_block_id, str)
        or not is_table_block_id(operation.table_block_id)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "delete_table_row 的 table_block_id 格式无效。",
        )
    if (
        isinstance(operation.row_index, bool)
        or not isinstance(operation.row_index, int)
        or operation.row_index < 0
    ):
        raise DocxError(
            "invalid_edit_operation",
            "delete_table_row 的 row_index 必须是非负整数。",
        )
    return _ValidatedDeleteTableRow(
        operation_index=operation_index,
        operation_type="delete_table_row",
        table_block_id=operation.table_block_id,
        row_index=operation.row_index,
    )


def _validate_runs(
    runs: object,
    operation_type: str,
) -> tuple[_ValidatedRun, ...]:
    if not isinstance(runs, list):
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 runs 必须是列表。",
        )
    validated: list[_ValidatedRun] = []
    for run_index, run in enumerate(runs):
        if not isinstance(run, TextRunSpec):
            raise DocxError(
                "invalid_edit_operation",
                f"{operation_type} 的第 {run_index} 个 run 类型无效。",
            )
        text = _validate_plain_text(run.text, f"第 {run_index} 个 run.text")
        if not all(
            isinstance(value, bool)
            for value in (run.bold, run.italic, run.underline)
        ):
            raise DocxError(
                "invalid_edit_operation",
                f"{operation_type} 的第 {run_index} 个 run 格式必须是布尔值。",
            )
        validated.append(
            _ValidatedRun(
                text=text,
                bold=run.bold,
                italic=run.italic,
                underline=run.underline,
            )
        )
    return tuple(validated)


def _validate_table_rows(
    rows: object,
    operation_type: str,
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(rows, list) or not rows:
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 rows 必须是非空列表。",
        )
    column_count: int | None = None
    validated_rows: list[tuple[str, ...]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or not row:
            raise DocxError(
                "invalid_edit_operation",
                f"{operation_type} 的第 {row_index} 行必须是非空列表。",
            )
        if column_count is None:
            column_count = len(row)
        elif len(row) != column_count:
            raise DocxError(
                "invalid_edit_operation",
                f"{operation_type} 必须使用规则矩形行列。",
            )
        validated_rows.append(
            tuple(
                _validate_plain_text(cell, f"第 {row_index} 行单元格")
                for cell in row
            )
        )
    return tuple(validated_rows)


def _validate_optional_style(
    value: object,
    operation_type: str,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or not _is_valid_xml_text(value)
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 style 必须是非空字符串或 null。",
        )
    return value


def _validate_optional_alignment(
    value: object,
    operation_type: str,
) -> str | None:
    if value is not None and (
        not isinstance(value, str) or value not in _ALLOWED_ALIGNMENTS
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{operation_type} 的 alignment 不受支持。",
        )
    return value


def _validate_plain_text(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or "\r" in value
        or not _is_valid_xml_text(value)
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{label} 不是受支持的 XML 文本；换行请使用 \\n。",
        )
    return value


def _table_cell_row_index(block_id: str) -> int | None:
    match = _TABLE_CELL_ROW_PATTERN.fullmatch(block_id)
    return int(match.group(1)) if match is not None else None


def _plan_text_edits(
    operations: list[_ValidatedOperation],
    *,
    body: ElementTree.Element,
    snapshot: DocumentSnapshot,
) -> tuple[
    list[_PlannedTextEdit],
    list[_PlannedMatchGroup],
    dict[str, str],
]:
    targets = build_edit_target_index(body)
    snapshot_targets = _snapshot_text_targets(snapshot)
    text_plans: list[_PlannedTextEdit] = []
    match_plans: list[_PlannedMatchGroup] = []
    expected_target_texts: dict[str, str] = {}
    match_operations_by_block: dict[str, list[_ValidatedMatchOperation]] = {}

    for operation in operations:
        if isinstance(operation, _ValidatedMatchOperation):
            match_operations_by_block.setdefault(operation.block_id, []).append(
                operation
            )
            continue
        if not isinstance(operation, _ValidatedTextOperation):
            continue
        paragraph, current_text = _resolve_text_edit_target(
            operation,
            targets=targets,
            snapshot_targets=snapshot_targets,
        )
        text_plans.append(
            _PlannedTextEdit(
                operation=operation,
                paragraph=paragraph,
                current_text=current_text,
            )
        )
        expected_target_texts[operation.block_id] = operation.text

    for block_id, match_operations in match_operations_by_block.items():
        representative = match_operations[0]
        paragraph, current_text = _resolve_text_edit_target(
            representative,
            targets=targets,
            snapshot_targets=snapshot_targets,
        )
        text_map = build_visible_text_map(paragraph)
        if text_map.text != current_text:
            raise DocxError(
                "edit_verification_failed",
                "Reader 与文字映射的可见内容不一致。",
            )
        resolved_edits: list[_ResolvedMatchEdit] = []
        for operation in match_operations:
            start, end = _locate_match_span(
                operation,
                text=text_map.text,
                revision=snapshot.revision,
            )
            if not text_map.is_searchable_range(start, end):
                raise DocxError(
                    "match_not_editable",
                    "匹配跨越显式分页符或分栏符，当前阶段不允许局部编辑。",
                )
            try:
                text_range = text_map.resolve_range(start, end)
            except ValueError as exc:
                raise DocxError(
                    "match_conflict",
                    "匹配范围无法映射到当前可见文字。",
                ) from exc
            if operation.preserve_format and not text_range.uniform_format:
                raise DocxError(
                    "match_not_editable",
                    "匹配跨越多个格式不同的 run，无法安全保留格式。",
                )
            resolved_edits.append(
                _ResolvedMatchEdit(
                    operation=operation,
                    start=start,
                    end=end,
                )
            )

        ordered_edits = tuple(
            sorted(resolved_edits, key=lambda item: (item.start, item.end))
        )
        for previous, current in zip(ordered_edits, ordered_edits[1:]):
            if current.start < previous.end:
                raise DocxError(
                    "edit_operation_conflict",
                    "同一个 block 中的局部替换范围不能重叠。",
                )

        expected_text = text_map.text
        for edit in reversed(ordered_edits):
            expected_text = (
                expected_text[: edit.start]
                + edit.operation.replacement_text
                + expected_text[edit.end :]
            )
        match_plan = _PlannedMatchGroup(
            block_id=block_id,
            text_map=text_map,
            edits=ordered_edits,
            expected_text=expected_text,
        )
        match_plans.append(match_plan)
        expected_target_texts[block_id] = match_plan.expected_text
    return text_plans, match_plans, expected_target_texts


def _plan_extended_edits(
    operations: list[_ValidatedOperation],
    *,
    body: ElementTree.Element,
    snapshot: DocumentSnapshot,
    rich_insertions: tuple[PlannedRichBodyInsertion, ...],
) -> _ExtendedEditPlan:
    body_locations = {
        location.block_id: location
        for location in iter_body_children(body)
        if location.block_id is not None
    }
    snapshot_blocks = {block.block_id: block for block in snapshot.blocks}
    paragraph_insertions: list[_PlannedParagraphInsertion] = []
    deleted_paragraphs: list[ElementTree.Element] = []
    paragraph_properties: list[_PlannedParagraphProperties] = []
    table_insertions: list[_PlannedTableInsertion] = []
    table_operations: dict[
        str,
        list[_ValidatedAppendTableRow | _ValidatedDeleteTableRow],
    ] = {}

    for operation in operations:
        if isinstance(operation, _ValidatedParagraphInsertion):
            anchor: ElementTree.Element | None = None
            if operation.block_id is not None:
                location = _require_body_location(
                    operation.block_id,
                    body_locations=body_locations,
                    snapshot_blocks=snapshot_blocks,
                    expected_kind="paragraph",
                )
                anchor = location.element
            paragraph_insertions.append(
                _PlannedParagraphInsertion(
                    operation=operation,
                    anchor=anchor,
                    paragraph=_create_simple_paragraph(
                        operation.runs,
                        style=operation.style,
                        alignment=operation.alignment,
                    ),
                )
            )
        elif isinstance(operation, _ValidatedDeleteParagraph):
            location = _require_body_location(
                operation.block_id,
                body_locations=body_locations,
                snapshot_blocks=snapshot_blocks,
                expected_kind="paragraph",
            )
            snapshot_block = snapshot_blocks[operation.block_id]
            if (
                not isinstance(snapshot_block, ParagraphSnapshot)
                or not snapshot_block.editable
                or not is_strictly_editable_paragraph(location.element)
                or _paragraph_contains_section_properties(location.element)
            ):
                raise DocxError(
                    "block_not_editable",
                    "指定段落包含复杂结构或承载 section properties，不能删除。",
                )
            deleted_paragraphs.append(location.element)
        elif isinstance(operation, _ValidatedParagraphProperties):
            location = _require_body_location(
                operation.block_id,
                body_locations=body_locations,
                snapshot_blocks=snapshot_blocks,
                expected_kind="paragraph",
            )
            snapshot_block = snapshot_blocks[operation.block_id]
            if (
                not isinstance(snapshot_block, ParagraphSnapshot)
                or not snapshot_block.editable
                or not is_strictly_editable_paragraph(location.element)
                or not _paragraph_properties_are_stable(location.element)
            ):
                raise DocxError(
                    "block_not_editable",
                    "指定段落的属性结构复杂，当前阶段不允许修改。",
                )
            paragraph_properties.append(
                _PlannedParagraphProperties(
                    operation=operation,
                    paragraph=location.element,
                )
            )
        elif isinstance(operation, _ValidatedInsertTable):
            location = _require_body_location(
                operation.block_id,
                body_locations=body_locations,
                snapshot_blocks=snapshot_blocks,
                expected_kind=None,
            )
            table_insertions.append(
                _PlannedTableInsertion(
                    operation=operation,
                    anchor=location.element,
                    table=_create_simple_table(
                        operation.rows,
                        header_row=operation.header_row,
                    ),
                )
            )
        elif isinstance(
            operation,
            (_ValidatedAppendTableRow, _ValidatedDeleteTableRow),
        ):
            table_operations.setdefault(
                operation.table_block_id,
                [],
            ).append(operation)

    final_paragraph_count = (
        snapshot.paragraph_count
        - len(deleted_paragraphs)
        + len(paragraph_insertions)
        + sum(
            len(insertion.elements)
            for insertion in rich_insertions
            if all(element.tag == _W_PARAGRAPH for element in insertion.elements)
        )
    )
    if deleted_paragraphs and final_paragraph_count < 1:
        raise DocxError(
            "edit_operation_conflict",
            "结构编辑完成后文档必须至少保留一个正文段落。",
        )

    format_groups = _plan_format_edits(
        operations,
        body=body,
        snapshot=snapshot,
    )
    table_mutations = _plan_table_mutations(
        table_operations,
        body_locations=body_locations,
        snapshot_blocks=snapshot_blocks,
    )
    structural_changed = bool(
        paragraph_insertions
        or deleted_paragraphs
        or table_insertions
        or table_mutations
        or rich_insertions
    )
    return _ExtendedEditPlan(
        paragraph_insertions=tuple(paragraph_insertions),
        deleted_paragraphs=tuple(deleted_paragraphs),
        paragraph_properties=tuple(paragraph_properties),
        format_groups=tuple(format_groups),
        table_insertions=tuple(table_insertions),
        table_mutations=tuple(table_mutations),
        rich_insertions=rich_insertions,
        structural_changed=structural_changed,
    )


def _plan_format_edits(
    operations: list[_ValidatedOperation],
    *,
    body: ElementTree.Element,
    snapshot: DocumentSnapshot,
) -> list[_PlannedFormatGroup]:
    grouped: dict[str, list[_ValidatedFormatOperation]] = {}
    for operation in operations:
        if isinstance(operation, _ValidatedFormatOperation):
            grouped.setdefault(operation.block_id, []).append(operation)
    if not grouped:
        return []

    targets = build_edit_target_index(body)
    snapshot_targets = _snapshot_text_targets(snapshot)
    plans: list[_PlannedFormatGroup] = []
    for block_id, format_operations in grouped.items():
        paragraph, current_text = _resolve_text_edit_target(
            format_operations[0],
            targets=targets,
            snapshot_targets=snapshot_targets,
        )
        text_map = build_visible_text_map(paragraph)
        if text_map.text != current_text:
            raise DocxError(
                "edit_verification_failed",
                "Reader 与格式编辑文字映射的可见内容不一致。",
            )

        resolved_edits: list[_ResolvedFormatEdit] = []
        for operation in format_operations:
            start, end = _locate_format_match_span(
                operation,
                text=text_map.text,
                revision=snapshot.revision,
            )
            if not text_map.is_searchable_range(start, end):
                raise DocxError(
                    "match_not_editable",
                    "格式范围跨越显式分页符或分栏符。",
                )
            try:
                text_range = text_map.resolve_range(start, end)
                read_run_direct_format(text_range.start_run.element)
            except ValueError as exc:
                raise DocxError(
                    "match_not_editable",
                    "格式范围无法稳定映射到普通 run。",
                ) from exc
            if len(text_range.affected_run_indexes) != 1:
                raise DocxError(
                    "match_not_editable",
                    "第一版文字格式修改不允许跨 run。",
                )
            resolved_edits.append(
                _ResolvedFormatEdit(
                    operation=operation,
                    start=start,
                    end=end,
                )
            )

        ordered_edits = tuple(
            sorted(resolved_edits, key=lambda item: (item.start, item.end))
        )
        for previous, current in zip(ordered_edits, ordered_edits[1:]):
            if current.start < previous.end:
                raise DocxError(
                    "edit_operation_conflict",
                    "同一个 block 中的格式修改范围不能重叠。",
                )
        plans.append(
            _PlannedFormatGroup(
                block_id=block_id,
                text_map=text_map,
                edits=ordered_edits,
            )
        )
    return plans


def _plan_table_mutations(
    table_operations: dict[
        str,
        list[_ValidatedAppendTableRow | _ValidatedDeleteTableRow],
    ],
    *,
    body_locations: dict[str, BodyChildLocation],
    snapshot_blocks: dict[str, ParagraphSnapshot | TableSnapshot],
) -> list[_PlannedTableMutation]:
    plans: list[_PlannedTableMutation] = []
    for table_block_id, operations in table_operations.items():
        location = _require_body_location(
            table_block_id,
            body_locations=body_locations,
            snapshot_blocks=snapshot_blocks,
            expected_kind="table",
        )
        snapshot_block = snapshot_blocks[table_block_id]
        if (
            not isinstance(snapshot_block, TableSnapshot)
            or not snapshot_block.editable
            or not is_strictly_editable_table(location.element)
        ):
            raise DocxError(
                "block_not_editable",
                "指定表格不是当前阶段允许修改的简单规则表格。",
            )
        rows, wrapped = visible_table_rows(location.element)
        if wrapped or len(rows) != snapshot_block.row_count:
            raise DocxError(
                "edit_verification_failed",
                "Reader 与 locator 的表格行定位结果不一致。",
            )

        deleted_rows: list[ElementTree.Element] = []
        appended_rows: list[ElementTree.Element] = []
        for operation in operations:
            if isinstance(operation, _ValidatedDeleteTableRow):
                if operation.row_index >= len(rows):
                    raise DocxError(
                        "block_not_found",
                        "指定的旧表格行不存在。",
                    )
                if _row_is_header(rows[operation.row_index]):
                    raise DocxError(
                        "block_not_editable",
                        "当前阶段不允许删除表格表头行。",
                    )
                deleted_rows.append(rows[operation.row_index])
            else:
                if len(operation.cells) != snapshot_block.column_count:
                    raise DocxError(
                        "edit_operation_conflict",
                        "追加行的单元格数量必须与规则表格列数一致。",
                    )
                appended_rows.append(
                    _create_simple_table_row(
                        operation.cells,
                        header=False,
                    )
                )
        if len(rows) - len(deleted_rows) + len(appended_rows) < 1:
            raise DocxError(
                "edit_operation_conflict",
                "删除表格行后必须至少保留一行。",
            )
        plans.append(
            _PlannedTableMutation(
                table_block_id=table_block_id,
                table=location.element,
                deleted_rows=tuple(deleted_rows),
                appended_rows=tuple(appended_rows),
            )
        )
    return plans


def _require_body_location(
    block_id: str,
    *,
    body_locations: dict[str, BodyChildLocation],
    snapshot_blocks: dict[str, ParagraphSnapshot | TableSnapshot],
    expected_kind: Literal["paragraph", "table"] | None,
) -> BodyChildLocation:
    location = body_locations.get(block_id)
    snapshot_block = snapshot_blocks.get(block_id)
    if location is None and snapshot_block is None:
        raise DocxError("block_not_found", "指定的正文 block_id 不存在。")
    if location is None or snapshot_block is None:
        raise DocxError(
            "edit_verification_failed",
            "Reader 与 locator 的正文 block_id 不一致。",
        )
    if expected_kind is not None and location.kind != expected_kind:
        raise DocxError(
            "edit_verification_failed",
            "正文 block 类型与编辑操作不一致。",
        )
    if (
        location.kind == "paragraph"
        and not isinstance(snapshot_block, ParagraphSnapshot)
    ) or (
        location.kind == "table"
        and not isinstance(snapshot_block, TableSnapshot)
    ):
        raise DocxError(
            "edit_verification_failed",
            "Reader 与 locator 的正文 block 类型不一致。",
        )
    return location


def _resolve_text_edit_target(
    operation: (
        _ValidatedTextOperation
        | _ValidatedMatchOperation
        | _ValidatedFormatOperation
    ),
    *,
    targets: dict[str, EditTargetLocation],
    snapshot_targets: dict[str, _SnapshotTextTarget],
) -> tuple[ElementTree.Element, str]:
    location = targets.get(operation.block_id)
    snapshot_target = snapshot_targets.get(operation.block_id)
    if location is None and snapshot_target is None:
        error_type = (
            "match_not_found"
            if isinstance(
                operation,
                (_ValidatedMatchOperation, _ValidatedFormatOperation),
            )
            else "block_not_found"
        )
        raise DocxError(error_type, "指定的 block_id 不存在。")
    if location is None or snapshot_target is None:
        raise DocxError(
            "edit_verification_failed",
            "Reader 与 locator 的 block_id 定位结果不一致。",
        )

    paragraph: ElementTree.Element | None
    if operation.target_kind == "paragraph":
        if (
            not isinstance(location, BodyChildLocation)
            or location.kind != "paragraph"
            or not isinstance(snapshot_target, ParagraphSnapshot)
        ):
            raise DocxError(
                "edit_verification_failed",
                "Reader 与 locator 的段落定位结果不一致。",
            )
        paragraph = location.element
        editable = snapshot_target.editable and is_strictly_editable_paragraph(
            paragraph
        )
        current_text = snapshot_target.text
    else:
        if (
            not isinstance(location, TableCellLocation)
            or not isinstance(snapshot_target, _SnapshotTableCellTarget)
            or snapshot_target.cell.block_id != location.block_id
            or not location.block_id.startswith(
                f"{snapshot_target.table.block_id}:row:"
            )
        ):
            raise DocxError(
                "edit_verification_failed",
                "Reader 与 locator 的表格单元格定位结果不一致。",
            )
        paragraph = get_single_cell_paragraph(location)
        editable = (
            snapshot_target.table.editable
            and snapshot_target.cell.editable
            and paragraph is not None
            and is_strictly_editable_table_cell(location)
        )
        current_text = snapshot_target.cell.text

    if not editable or paragraph is None:
        if isinstance(
            operation,
            (_ValidatedMatchOperation, _ValidatedFormatOperation),
        ):
            raise DocxError(
                "match_not_editable",
                "匹配内容或其所属表格包含复杂结构，当前阶段不允许局部编辑。",
            )
        raise DocxError(
            "block_not_editable",
            "指定内容块或其所属表格包含复杂结构，当前阶段不允许编辑。",
        )
    return paragraph, current_text


def _locate_match_span(
    operation: _ValidatedMatchOperation,
    *,
    text: str,
    revision: str,
) -> tuple[int, int]:
    expected_text_found = False
    for start, end in iter_literal_matches(
        text,
        operation.expected_text,
        case_sensitive=True,
        whole_word=False,
    ):
        expected_text_found = True
        if (
            build_match_id(
                revision,
                operation.block_id,
                start,
                end,
                text[start:end],
            )
            == operation.match_id
        ):
            return start, end
    if not expected_text_found:
        raise DocxError(
            "match_conflict",
            "当前 block 中已不存在 expected_text。",
        )
    raise DocxError(
        "match_not_found",
        "match_id 在当前 revision 和 block 中不存在。",
    )


def _locate_format_match_span(
    operation: _ValidatedFormatOperation,
    *,
    text: str,
    revision: str,
) -> tuple[int, int]:
    expected_text_found = False
    for start, end in iter_literal_matches(
        text,
        operation.expected_text,
        case_sensitive=True,
        whole_word=False,
    ):
        expected_text_found = True
        if (
            build_match_id(
                revision,
                operation.block_id,
                start,
                end,
                text[start:end],
            )
            == operation.match_id
        ):
            return start, end
    if not expected_text_found:
        raise DocxError(
            "match_conflict",
            "当前 block 中已不存在 format_text_match.expected_text。",
        )
    raise DocxError(
        "match_not_found",
        "format_text_match 的 match_id 在当前 revision 和 block 中不存在。",
    )


def _prepare_metadata_context(
    package: DocxPackage,
    operations: list[_ValidatedOperation],
    *,
    content_types: ContentTypesManager | None,
) -> _MetadataContext | None:
    metadata_operation = next(
        (
            operation
            for operation in operations
            if isinstance(operation, _ValidatedMetadataOperation)
        ),
        None,
    )
    if metadata_operation is None:
        return None
    if content_types is None:
        raise DocxError(
            "edit_verification_failed",
            "metadata 计划缺少统一 Content Types 管理器。",
        )

    core_exists = package.has_part(_CORE_PART)
    if core_exists:
        core_original = package.read_xml_bytes(_CORE_PART)
        core_root = parse_xml_preserving_misc(core_original, _CORE_PART)
        if core_root.tag != _CORE_PROPERTIES:
            raise DocxError(
                "edit_operation_conflict",
                "现有 core properties part 的根节点无效。",
            )
    else:
        core_original = None
        core_root = ElementTree.Element(_CORE_PROPERTIES)

    for field_name in metadata_operation.fields:
        matching = [
            child
            for child in core_root
            if child.tag == _METADATA_QNAMES[field_name]
        ]
        if len(matching) > 1:
            raise DocxError(
                "edit_operation_conflict",
                f"core properties 包含重复字段：{field_name}。",
            )

    core_content_type = content_types.override_content_type_for(_CORE_PART)
    if core_content_type is not None and core_content_type != _CORE_CONTENT_TYPE:
        raise DocxError(
            "package_mutation_conflict",
            "core properties content type 与标准定义冲突。",
        )

    relationships_original = package.read_xml_bytes(_ROOT_RELATIONSHIPS_PART)
    relationships_root = parse_xml_preserving_misc(
        relationships_original,
        _ROOT_RELATIONSHIPS_PART,
    )
    if relationships_root.tag != _RELATIONSHIPS:
        raise DocxError(
            "edit_operation_conflict",
            "package relationships 根节点无效。",
        )
    core_relationships = [
        child
        for child in relationships_root
        if child.tag == _RELATIONSHIP
        and (
            child.attrib.get("Type") == _CORE_RELATIONSHIP_TYPE
            or _normalize_relationship_target(child.attrib.get("Target", ""))
            == _CORE_RELATIONSHIP_TARGET
        )
    ]
    if len(core_relationships) > 1:
        raise DocxError(
            "edit_operation_conflict",
            "core properties relationship 存在重复或冲突定义。",
        )
    core_relationship = core_relationships[0] if core_relationships else None
    if core_relationship is not None and (
        core_relationship.attrib.get("Type") != _CORE_RELATIONSHIP_TYPE
        or _normalize_relationship_target(
            core_relationship.attrib.get("Target", "")
        )
        != _CORE_RELATIONSHIP_TARGET
        or core_relationship.attrib.get("TargetMode", "").lower() == "external"
    ):
        raise DocxError(
            "edit_operation_conflict",
            "core properties relationship 与标准定义冲突。",
        )

    return _MetadataContext(
        operation=metadata_operation,
        core_root=core_root,
        core_original=core_original,
        content_types=content_types,
        relationships_root=relationships_root,
        relationships_original=relationships_original,
        core_relationship=core_relationship,
    )


def _apply_text_edit(plan: _PlannedTextEdit) -> bool:
    operation = plan.operation
    if operation.preserve_first_run_format and plan.current_text == operation.text:
        return False

    before = ElementTree.tostring(plan.paragraph, encoding="utf-8")
    first_run_properties: ElementTree.Element | None = None
    if operation.preserve_first_run_format:
        for child in plan.paragraph:
            if child.tag != _W_RUN:
                continue
            properties = child.find(_W_RUN_PROPERTIES)
            if properties is not None:
                first_run_properties = copy.deepcopy(properties)
            break

    for child in list(plan.paragraph):
        if child.tag != _W_PARAGRAPH_PROPERTIES:
            plan.paragraph.remove(child)

    run = ElementTree.SubElement(plan.paragraph, _W_RUN)
    if first_run_properties is not None:
        run.append(first_run_properties)
    append_text_content(run, operation.text)
    after = ElementTree.tostring(plan.paragraph, encoding="utf-8")
    return before != after


def _apply_match_group(plan: _PlannedMatchGroup) -> bool:
    try:
        replacements = tuple(
            VisibleTextReplacement(
                start=edit.start,
                end=edit.end,
                replacement=edit.operation.replacement_text,
                preserve_format=edit.operation.preserve_format,
            )
            for edit in reversed(plan.edits)
        )
        return replace_visible_text_ranges(
            plan.text_map,
            replacements,
        )
    except ValueError as exc:
        raise DocxError(
            "edit_verification_failed",
            "已规划的局部文字范围无法安全写回。",
        ) from exc


def _apply_paragraph_properties(
    plan: _PlannedParagraphProperties,
) -> bool:
    before = ElementTree.tostring(plan.paragraph, encoding="utf-8")
    operation = plan.operation
    properties = plan.paragraph.find(_W_PARAGRAPH_PROPERTIES)

    if operation.style_is_set:
        properties = _set_paragraph_property(
            plan.paragraph,
            properties,
            _W_PARAGRAPH_STYLE,
            operation.style,
        )
    elif operation.heading_level_is_set:
        if operation.heading_level is not None:
            properties = _set_paragraph_property(
                plan.paragraph,
                properties,
                _W_PARAGRAPH_STYLE,
                f"Heading{operation.heading_level}",
            )
        elif properties is not None:
            style = properties.find(_W_PARAGRAPH_STYLE)
            style_value = _property_value(style)
            if style_value in {f"Heading{level}" for level in range(1, 7)}:
                properties.remove(style)

    if operation.alignment_is_set:
        alignment = (
            _ALIGNMENT_TO_WML[operation.alignment]
            if operation.alignment is not None
            else None
        )
        properties = _set_paragraph_property(
            plan.paragraph,
            properties,
            _W_JUSTIFICATION,
            alignment,
        )
    if properties is not None and len(properties) == 0:
        plan.paragraph.remove(properties)
    after = ElementTree.tostring(plan.paragraph, encoding="utf-8")
    return before != after


def _apply_format_group(plan: _PlannedFormatGroup) -> bool:
    formatting_ranges = tuple(
        VisibleTextFormatting(
            start=edit.start,
            end=edit.end,
            bold=edit.operation.bold,
            italic=edit.operation.italic,
            underline=edit.operation.underline,
        )
        for edit in plan.edits
    )
    try:
        return format_visible_text_ranges(plan.text_map, formatting_ranges)
    except ValueError as exc:
        raise DocxError(
            "edit_verification_failed",
            "已规划的文字格式范围无法安全写回。",
        ) from exc


def _apply_table_mutation(plan: _PlannedTableMutation) -> bool:
    before = ElementTree.tostring(plan.table, encoding="utf-8")
    for row in plan.deleted_rows:
        plan.table.remove(row)
    for row in plan.appended_rows:
        plan.table.append(row)
    after = ElementTree.tostring(plan.table, encoding="utf-8")
    return before != after


def _apply_body_mutations(
    body: ElementTree.Element,
    plan: _ExtendedEditPlan,
) -> bool:
    if not plan.structural_changed:
        return False

    before = ElementTree.tostring(body, encoding="utf-8")
    before_by_anchor: dict[int, list[tuple[int, ElementTree.Element]]] = {}
    after_by_anchor: dict[int, list[tuple[int, ElementTree.Element]]] = {}
    appended_paragraphs: list[tuple[int, ElementTree.Element]] = []
    for insertion in plan.paragraph_insertions:
        item = (insertion.operation.operation_index, insertion.paragraph)
        if insertion.operation.position == "append":
            appended_paragraphs.append(item)
        elif insertion.operation.position == "before":
            if insertion.anchor is None:
                raise DocxError(
                    "edit_verification_failed",
                    "段落插入计划缺少锚点。",
                )
            before_by_anchor.setdefault(id(insertion.anchor), []).append(item)
        else:
            if insertion.anchor is None:
                raise DocxError(
                    "edit_verification_failed",
                    "段落插入计划缺少锚点。",
                )
            after_by_anchor.setdefault(id(insertion.anchor), []).append(item)
    for insertion in plan.table_insertions:
        after_by_anchor.setdefault(id(insertion.anchor), []).append(
            (insertion.operation.operation_index, insertion.table)
        )
    for insertion in plan.rich_insertions:
        after_by_anchor.setdefault(id(insertion.anchor), []).extend(
            (insertion.operation_index, element)
            for element in insertion.elements
        )

    deleted_ids = {id(paragraph) for paragraph in plan.deleted_paragraphs}
    new_children: list[ElementTree.Element] = []
    for child in list(body):
        new_children.extend(
            element
            for _, element in sorted(
                before_by_anchor.get(id(child), []),
                key=lambda item: item[0],
            )
        )
        if id(child) not in deleted_ids:
            new_children.append(child)
        new_children.extend(
            element
            for _, element in sorted(
                after_by_anchor.get(id(child), []),
                key=lambda item: item[0],
            )
        )

    appended = [
        element
        for _, element in sorted(
            appended_paragraphs,
            key=lambda item: item[0],
        )
    ]
    section_index = next(
        (
            index
            for index in range(len(new_children) - 1, -1, -1)
            if new_children[index].tag == _W_SECTION_PROPERTIES
        ),
        len(new_children),
    )
    new_children[section_index:section_index] = appended

    for child in list(body):
        body.remove(child)
    body.extend(new_children)
    after = ElementTree.tostring(body, encoding="utf-8")
    return before != after


def _create_simple_paragraph(
    runs: tuple[_ValidatedRun, ...],
    *,
    style: str | None,
    alignment: str | None,
) -> ElementTree.Element:
    paragraph = ElementTree.Element(_W_PARAGRAPH)
    if style is not None or alignment is not None:
        properties = ElementTree.SubElement(
            paragraph,
            _W_PARAGRAPH_PROPERTIES,
        )
        if style is not None:
            ElementTree.SubElement(
                properties,
                _W_PARAGRAPH_STYLE,
                {_W_VAL: style},
            )
        if alignment is not None:
            ElementTree.SubElement(
                properties,
                _W_JUSTIFICATION,
                {_W_VAL: _ALIGNMENT_TO_WML[alignment]},
            )
    for run_spec in runs:
        run = ElementTree.SubElement(paragraph, _W_RUN)
        if run_spec.bold or run_spec.italic or run_spec.underline:
            run_properties = ElementTree.SubElement(
                run,
                _W_RUN_PROPERTIES,
            )
            if run_spec.bold:
                ElementTree.SubElement(run_properties, _W_BOLD)
            if run_spec.italic:
                ElementTree.SubElement(run_properties, _W_ITALIC)
            if run_spec.underline:
                ElementTree.SubElement(run_properties, _W_UNDERLINE)
        append_text_content(run, run_spec.text)
    return paragraph


def _create_simple_table(
    rows: tuple[tuple[str, ...], ...],
    *,
    header_row: bool,
) -> ElementTree.Element:
    table = ElementTree.Element(_W_TABLE)
    ElementTree.SubElement(table, _W_TABLE_PROPERTIES)
    grid = ElementTree.SubElement(table, _W_TABLE_GRID)
    for _ in rows[0]:
        ElementTree.SubElement(grid, _W_GRID_COLUMN)
    for row_index, cells in enumerate(rows):
        table.append(
            _create_simple_table_row(
                cells,
                header=header_row and row_index == 0,
            )
        )
    return table


def _create_simple_table_row(
    cells: tuple[str, ...],
    *,
    header: bool,
) -> ElementTree.Element:
    row = ElementTree.Element(_W_ROW)
    if header:
        row_properties = ElementTree.SubElement(row, _W_ROW_PROPERTIES)
        ElementTree.SubElement(row_properties, _W_TABLE_HEADER)
    for cell_text in cells:
        cell = ElementTree.SubElement(row, _W_CELL)
        ElementTree.SubElement(cell, _W_CELL_PROPERTIES)
        paragraph = ElementTree.SubElement(cell, _W_PARAGRAPH)
        run = ElementTree.SubElement(paragraph, _W_RUN)
        append_text_content(run, cell_text)
    return row


def _set_paragraph_property(
    paragraph: ElementTree.Element,
    properties: ElementTree.Element | None,
    tag: str,
    value: str | None,
) -> ElementTree.Element | None:
    if properties is None:
        if value is None:
            return None
        properties = ElementTree.Element(_W_PARAGRAPH_PROPERTIES)
        paragraph.insert(0, properties)
    matching = [child for child in properties if child.tag == tag]
    if len(matching) > 1:
        raise DocxError(
            "edit_verification_failed",
            "段落属性在应用阶段出现重复节点。",
        )
    if value is None:
        if matching:
            properties.remove(matching[0])
        return properties

    element = matching[0] if matching else ElementTree.Element(tag)
    element.attrib.pop("val", None)
    element.set(_W_VAL, value)
    if not matching:
        if tag == _W_PARAGRAPH_STYLE:
            properties.insert(0, element)
        else:
            section = properties.find(_W_SECTION_PROPERTIES)
            if section is None:
                properties.append(element)
            else:
                properties.insert(list(properties).index(section), element)
    return properties


def _property_value(element: ElementTree.Element | None) -> str | None:
    if element is None:
        return None
    return element.attrib.get(_W_VAL, element.attrib.get("val"))


def _paragraph_contains_section_properties(
    paragraph: ElementTree.Element,
) -> bool:
    properties = paragraph.find(_W_PARAGRAPH_PROPERTIES)
    return (
        properties is not None
        and properties.find(_W_SECTION_PROPERTIES) is not None
    )


def _paragraph_properties_are_stable(
    paragraph: ElementTree.Element,
) -> bool:
    properties = paragraph.find(_W_PARAGRAPH_PROPERTIES)
    if properties is None:
        return True
    return all(
        sum(1 for child in properties if child.tag == tag) <= 1
        for tag in (_W_PARAGRAPH_STYLE, _W_JUSTIFICATION)
    )


def _row_is_header(row: ElementTree.Element) -> bool:
    properties = row.find(_W_ROW_PROPERTIES)
    return (
        properties is not None
        and properties.find(_W_TABLE_HEADER) is not None
    )


def _apply_metadata_edit(context: _MetadataContext) -> dict[str, bytes]:
    replacements: dict[str, bytes] = {}
    core_changed = False

    for field_name, value in context.operation.fields.items():
        qname = _METADATA_QNAMES[field_name]
        existing = next(
            (child for child in context.core_root if child.tag == qname),
            None,
        )
        if value is None:
            if existing is not None:
                context.core_root.remove(existing)
                core_changed = True
            continue
        if existing is None:
            existing = ElementTree.SubElement(context.core_root, qname)
            existing.text = value
            core_changed = True
        elif (existing.text or "") != value:
            existing.text = value
            core_changed = True

    if core_changed:
        ElementTree.register_namespace("cp", _CP_NS)
        ElementTree.register_namespace("dc", _DC_NS)
        replacements[_CORE_PART] = serialize_xml(
            context.core_root,
            original_payload=context.core_original,
        )
    else:
        return replacements

    context.content_types.ensure_override(
        _CORE_PART,
        _CORE_CONTENT_TYPE,
    )

    if context.core_relationship is None:
        ElementTree.SubElement(
            context.relationships_root,
            _RELATIONSHIP,
            {
                "Id": _next_relationship_id(context.relationships_root),
                "Type": _CORE_RELATIONSHIP_TYPE,
                "Target": _CORE_RELATIONSHIP_TARGET,
            },
        )
        replacements[_ROOT_RELATIONSHIPS_PART] = serialize_xml(
            context.relationships_root,
            original_payload=context.relationships_original,
        )
    return replacements


def _build_expected_document_state(
    *,
    body: ElementTree.Element,
    initial_snapshot: DocumentSnapshot,
    original_block_elements: dict[str, ElementTree.Element],
    expected_target_texts: dict[str, str],
    operations: list[_ValidatedOperation],
    extended_plan: _ExtendedEditPlan,
) -> _ExpectedDocumentState:
    current_block_elements = _document_block_element_index(body)
    current_id_by_identity = {
        id(element): block_id
        for block_id, element in current_block_elements.items()
    }
    full_remap = tuple(
        BlockRemap(
            old_block_id=old_block_id,
            new_block_id=current_id_by_identity.get(
                id(original_block_elements[old_block_id])
            ),
        )
        for old_block_id in _ordered_snapshot_block_ids(initial_snapshot)
    )
    remap_lookup = {
        item.old_block_id: item.new_block_id for item in full_remap
    }

    expected_texts: dict[str, str] = {}
    initial_targets = _snapshot_text_targets(initial_snapshot)
    for old_block_id, initial_target in initial_targets.items():
        new_block_id = remap_lookup.get(old_block_id)
        if new_block_id is None:
            continue
        expected_texts[new_block_id] = expected_target_texts.get(
            old_block_id,
            _snapshot_target_text(initial_target),
        )

    current_targets = build_edit_target_index(body)
    for block_id, location in current_targets.items():
        if block_id in expected_texts:
            continue
        if isinstance(location, BodyChildLocation):
            expected_texts[block_id] = build_visible_text_map(
                location.element
            ).text
        else:
            expected_texts[block_id] = _read_cell_visible_text(location.cell)

    top_level_blocks: list[tuple[str, str]] = []
    paragraph_properties: dict[str, tuple[str | None, str | None]] = {}
    table_shapes: dict[str, tuple[int, ...]] = {}
    paragraph_count = 0
    table_count = 0
    for location in iter_body_children(body):
        if location.block_id is None:
            continue
        if location.kind == "paragraph":
            paragraph_count += 1
            top_level_blocks.append(("paragraph", location.block_id))
        elif location.kind == "table":
            table_count += 1
            top_level_blocks.append(("table", location.block_id))
            rows, _ = visible_table_rows(location.element)
            table_shapes[location.block_id] = tuple(
                len(visible_row_cells(row)[0]) for row in rows
            )

    property_elements = [
        plan.paragraph for plan in extended_plan.paragraph_properties
    ] + [
        plan.paragraph for plan in extended_plan.paragraph_insertions
    ]
    for paragraph in property_elements:
        block_id = current_id_by_identity.get(id(paragraph))
        if block_id is None:
            raise DocxError(
                "edit_verification_failed",
                "段落属性验证目标未出现在修改后结构中。",
            )
        paragraph_properties[block_id] = (
            _read_paragraph_property(paragraph, _W_PARAGRAPH_STYLE),
            _read_paragraph_property(paragraph, _W_JUSTIFICATION),
        )

    editable_block_ids: set[str] = set()
    for operation in operations:
        old_block_id = _validated_operation_block_id(operation)
        if old_block_id is None:
            continue
        if isinstance(
            operation,
            (
                _ValidatedTextOperation,
                _ValidatedMatchOperation,
                _ValidatedFormatOperation,
                _ValidatedParagraphProperties,
                _ValidatedAppendTableRow,
                _ValidatedDeleteTableRow,
            ),
        ):
            new_block_id = remap_lookup.get(old_block_id)
            if new_block_id is not None:
                editable_block_ids.add(new_block_id)

    old_element_identities = {
        id(element) for element in original_block_elements.values()
    }
    complex_inserted_element_ids = frozenset(
        element_id
        for insertion in extended_plan.rich_insertions
        for element_id in insertion.complex_element_ids
    )
    for block_id, element in current_block_elements.items():
        if (
            id(element) not in old_element_identities
            and id(element) not in complex_inserted_element_ids
        ):
            editable_block_ids.add(block_id)
    for table_plan in extended_plan.table_mutations:
        new_table_id = remap_lookup.get(table_plan.table_block_id)
        if new_table_id is not None:
            editable_block_ids.add(new_table_id)
            editable_block_ids.update(
                block_id
                for block_id in current_block_elements
                if block_id.startswith(f"{new_table_id}:row:")
            )

    strict_table_block_ids: set[str] = set()
    for table_plan in extended_plan.table_insertions:
        new_table_id = current_id_by_identity.get(id(table_plan.table))
        if new_table_id is None:
            raise DocxError(
                "edit_verification_failed",
                "新插入表格未出现在修改后结构中。",
            )
        strict_table_block_ids.add(new_table_id)
    for table_plan in extended_plan.table_mutations:
        new_table_id = remap_lookup.get(table_plan.table_block_id)
        if new_table_id is None:
            raise DocxError(
                "edit_verification_failed",
                "表格行编辑目标在结构计划中被意外删除。",
            )
        strict_table_block_ids.add(new_table_id)

    format_ranges: list[_ExpectedFormatRange] = []
    for group in extended_plan.format_groups:
        new_block_id = remap_lookup.get(group.block_id)
        if new_block_id is None:
            raise DocxError(
                "edit_verification_failed",
                "格式编辑目标在结构计划中被意外删除。",
            )
        for edit in group.edits:
            format_ranges.append(
                _ExpectedFormatRange(
                    block_id=new_block_id,
                    start=edit.start,
                    end=edit.end,
                    bold=edit.operation.bold,
                    italic=edit.operation.italic,
                    underline=edit.operation.underline,
                )
            )

    return _ExpectedDocumentState(
        block_remap=full_remap if extended_plan.structural_changed else (),
        top_level_blocks=tuple(top_level_blocks),
        block_ids=frozenset(current_block_elements),
        target_texts=expected_texts,
        paragraph_properties=paragraph_properties,
        table_shapes=table_shapes,
        editable_block_ids=frozenset(editable_block_ids),
        strict_table_block_ids=frozenset(strict_table_block_ids),
        format_ranges=tuple(format_ranges),
        paragraph_count=paragraph_count,
        table_count=table_count,
    )


def _document_block_element_index(
    body: ElementTree.Element,
) -> dict[str, ElementTree.Element]:
    elements: dict[str, ElementTree.Element] = {}
    for location in iter_body_children(body):
        if location.block_id is None:
            continue
        if location.kind == "paragraph":
            elements[location.block_id] = location.element
        elif location.kind == "table":
            elements[location.block_id] = location.element
            for cell_location in iter_table_cells(location):
                elements[cell_location.block_id] = cell_location.cell
    return elements


def _read_cell_visible_text(cell: ElementTree.Element) -> str:
    paragraphs: list[ElementTree.Element] = []

    def visit(element: ElementTree.Element) -> None:
        for child in element:
            if child.tag == _W_PARAGRAPH:
                paragraphs.append(child)
            elif child.tag == _W_TABLE:
                visit(child)
            elif child.tag != _W_CELL_PROPERTIES:
                visit(child)

    visit(cell)
    return "\n".join(
        build_visible_text_map(paragraph).text for paragraph in paragraphs
    )


def _read_paragraph_property(
    paragraph: ElementTree.Element,
    tag: str,
) -> str | None:
    properties = paragraph.find(_W_PARAGRAPH_PROPERTIES)
    if properties is None:
        return None
    matching = [child for child in properties if child.tag == tag]
    if len(matching) > 1:
        raise DocxError(
            "edit_verification_failed",
            "修改后的段落包含重复基础属性。",
        )
    return _property_value(matching[0]) if matching else None


def _inspect_temporary_output(
    reader: DocxReader,
    temporary_output: Path,
) -> DocumentSnapshot:
    try:
        return reader.inspect(
            InspectDocumentRequest(
                source_path=temporary_output,
                include_runs=False,
                include_tables=True,
            )
        )
    except DocxError as exc:
        raise DocxError(
            "edit_verification_failed",
            "修改后的临时 DOCX 无法通过安全读取验证。",
        ) from exc


def _verify_temporary_package(
    *,
    source_path: Path,
    temporary_output: Path,
    expected_revision: str,
    output_snapshot: DocumentSnapshot,
    initial_snapshot: DocumentSnapshot,
    mutation: PackageMutation,
    rich_plan: RichContentPlan,
    verify_rich: bool,
) -> None:
    """复检 package mutation 字节保真及 P4.3 富内容关系。"""

    with (
        DocxPackage.open(source_path) as source_package,
        DocxPackage.open(temporary_output) as output_package,
    ):
        _require_revision(source_package.revision, expected_revision)
        if output_package.revision != output_snapshot.revision:
            raise DocxError(
                "edit_verification_failed",
                "临时输出 package revision 与 Reader 结果不一致。",
            )
        verify_package_mutation(
            source_package,
            output_package,
            mutation,
        )
        if rich_plan.content_types_changed and not verify_rich:
            ContentTypesManager(
                output_package.read_xml(_CONTENT_TYPES_PART),
                error_type="edit_verification_failed",
            )
        if verify_rich:
            verify_rich_content_output(
                package=output_package,
                plan=rich_plan,
                initial_snapshot=initial_snapshot,
                output_snapshot=output_snapshot,
            )


def _read_temporary_output_body(
    temporary_output: Path,
    *,
    expected_revision: str,
) -> ElementTree.Element:
    with DocxPackage.open(temporary_output) as package:
        if package.revision != expected_revision:
            raise DocxError(
                "edit_verification_failed",
                "临时输出的 package revision 与 Reader 结果不一致。",
            )
        document_root = package.read_xml(_DOCUMENT_PART)
        body = document_root.find(_W_BODY)
        if body is None:
            raise DocxError(
                "edit_verification_failed",
                "修改后的 word/document.xml 缺少 w:body。",
            )
        return body


def _verify_output(
    *,
    initial_snapshot: DocumentSnapshot,
    output_snapshot: DocumentSnapshot,
    operations: list[_ValidatedOperation],
    expected_state: _ExpectedDocumentState,
    output_body: ElementTree.Element,
    changed: bool,
    expected_image_count: int,
) -> None:
    if changed and output_snapshot.revision == initial_snapshot.revision:
        raise DocxError(
            "output_revision_unchanged",
            "编辑产生了内容变化，但输出 revision 未改变。",
        )
    if not changed and output_snapshot.revision != initial_snapshot.revision:
        raise DocxError(
            "edit_verification_failed",
            "幂等编辑不应改变输出 revision。",
        )
    if (
        output_snapshot.paragraph_count != expected_state.paragraph_count
        or output_snapshot.table_count != expected_state.table_count
        or output_snapshot.image_count != expected_image_count
        or output_snapshot.section_count != initial_snapshot.section_count
        or _snapshot_block_ids(output_snapshot) != expected_state.block_ids
        or _snapshot_top_level_sequence(output_snapshot)
        != expected_state.top_level_blocks
    ):
        raise DocxError(
            "edit_verification_failed",
            "修改后的文档 block 结构与统一计划不一致。",
        )

    output_targets = _snapshot_text_targets(output_snapshot)
    if set(output_targets) != set(expected_state.target_texts):
        raise DocxError(
            "edit_verification_failed",
            "修改后的文字目标集合与统一计划不一致。",
        )
    for block_id, expected_text in expected_state.target_texts.items():
        target = output_targets.get(block_id)
        if target is None or _snapshot_target_text(target) != expected_text:
            raise DocxError(
                "edit_verification_failed",
                "修改后的目标文字与统一计划不一致。",
            )

    output_blocks = {block.block_id: block for block in output_snapshot.blocks}
    for block_id, expected_properties in (
        expected_state.paragraph_properties.items()
    ):
        paragraph = output_blocks.get(block_id)
        if not isinstance(paragraph, ParagraphSnapshot) or (
            paragraph.style,
            paragraph.alignment,
        ) != expected_properties:
            raise DocxError(
                "edit_verification_failed",
                "修改后的段落属性与统一计划不一致。",
            )
    for block_id, expected_shape in expected_state.table_shapes.items():
        table = output_blocks.get(block_id)
        if not isinstance(table, TableSnapshot) or tuple(
            len(row) for row in table.rows
        ) != expected_shape:
            raise DocxError(
                "edit_verification_failed",
                "修改后的表格行列结构与统一计划不一致。",
            )

    for block_id in expected_state.editable_block_ids:
        target: ParagraphSnapshot | TableSnapshot | _SnapshotTableCellTarget | None
        target = output_blocks.get(block_id)
        if target is None:
            target = output_targets.get(block_id)
        if target is None or not _snapshot_block_is_editable(target):
            raise DocxError(
                "edit_verification_failed",
                "修改后的目标不再满足安全编辑结构。",
            )

    _verify_strict_tables(
        output_snapshot,
        output_body,
        expected_state.strict_table_block_ids,
    )
    _verify_format_ranges(
        output_body,
        expected_state.format_ranges,
    )
    _verify_block_remap(
        initial_snapshot,
        expected_state,
    )

    metadata_operation = next(
        (
            operation
            for operation in operations
            if isinstance(operation, _ValidatedMetadataOperation)
        ),
        None,
    )
    for field_name in _ALL_METADATA_FIELDS:
        if metadata_operation is not None and field_name in metadata_operation.fields:
            expected_value = metadata_operation.fields[field_name]
        else:
            expected_value = getattr(initial_snapshot.metadata, field_name)
        if getattr(output_snapshot.metadata, field_name) != expected_value:
            raise DocxError(
                "edit_verification_failed",
                "修改后的 metadata 与请求不一致。",
            )


def _snapshot_text_targets(
    snapshot: DocumentSnapshot,
) -> dict[str, _SnapshotTextTarget]:
    targets: dict[str, _SnapshotTextTarget] = {}
    for block in snapshot.blocks:
        if isinstance(block, ParagraphSnapshot):
            targets[block.block_id] = block
        elif isinstance(block, TableSnapshot):
            for row in block.rows:
                for cell in row:
                    targets[cell.block_id] = _SnapshotTableCellTarget(
                        cell=cell,
                        table=block,
                    )
    return targets


def _snapshot_target_text(target: _SnapshotTextTarget) -> str:
    """返回段落或带表格上下文的单元格可见文字。"""

    if isinstance(target, ParagraphSnapshot):
        return target.text
    return target.cell.text


def _snapshot_block_is_editable(
    target: ParagraphSnapshot | TableSnapshot | _SnapshotTableCellTarget,
) -> bool:
    """按 Reader 的父子级结果判断任意快照 block 是否可编辑。"""

    if isinstance(target, ParagraphSnapshot):
        return target.editable
    if isinstance(target, TableSnapshot):
        return target.editable
    return target.table.editable and target.cell.editable


def _snapshot_top_level_sequence(
    snapshot: DocumentSnapshot,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            "paragraph" if isinstance(block, ParagraphSnapshot) else "table",
            block.block_id,
        )
        for block in snapshot.blocks
    )


def _ordered_snapshot_block_ids(
    snapshot: DocumentSnapshot,
) -> tuple[str, ...]:
    block_ids: list[str] = []
    for block in snapshot.blocks:
        block_ids.append(block.block_id)
        if isinstance(block, TableSnapshot):
            for row in block.rows:
                block_ids.extend(cell.block_id for cell in row)
    return tuple(block_ids)


def _verify_block_remap(
    initial_snapshot: DocumentSnapshot,
    expected_state: _ExpectedDocumentState,
) -> None:
    if not expected_state.block_remap:
        return
    old_ids = tuple(item.old_block_id for item in expected_state.block_remap)
    if old_ids != _ordered_snapshot_block_ids(initial_snapshot):
        raise DocxError(
            "edit_verification_failed",
            "block_remap 未覆盖全部旧 block_id 或顺序不稳定。",
        )
    new_ids = [
        item.new_block_id
        for item in expected_state.block_remap
        if item.new_block_id is not None
    ]
    if len(new_ids) != len(set(new_ids)) or any(
        block_id not in expected_state.block_ids for block_id in new_ids
    ):
        raise DocxError(
            "edit_verification_failed",
            "block_remap 包含重复或不存在的新 block_id。",
        )
    if any(
        item.new_block_id is not None
        and not _same_block_id_kind(
            item.old_block_id,
            item.new_block_id,
        )
        for item in expected_state.block_remap
    ):
        raise DocxError(
            "edit_verification_failed",
            "block_remap 改变了旧 block 的结构类型。",
        )


def _verify_format_ranges(
    output_body: ElementTree.Element,
    expected_ranges: tuple[_ExpectedFormatRange, ...],
) -> None:
    targets = build_edit_target_index(output_body)
    for expected in expected_ranges:
        location = targets.get(expected.block_id)
        if isinstance(location, BodyChildLocation):
            if location.kind != "paragraph":
                raise DocxError(
                    "edit_verification_failed",
                    "格式修改后的正文目标类型不一致。",
                )
            paragraph = location.element
        elif isinstance(location, TableCellLocation):
            paragraph = get_single_cell_paragraph(location)
            if paragraph is None:
                raise DocxError(
                    "edit_verification_failed",
                    "格式修改后的表格单元格不再是单段落结构。",
                )
        else:
            raise DocxError(
                "edit_verification_failed",
                "格式修改后的目标 block_id 不存在。",
            )
        try:
            text_map = build_visible_text_map(paragraph)
            text_range = text_map.resolve_range(
                expected.start,
                expected.end,
            )
            if len(text_range.affected_run_indexes) != 1:
                raise ValueError("formatted output range crosses runs")
            actual = read_run_direct_format(text_range.start_run.element)
        except ValueError as exc:
            raise DocxError(
                "edit_verification_failed",
                "修改后的直接格式范围无法稳定复检。",
            ) from exc
        requested = (
            expected.bold,
            expected.italic,
            expected.underline,
        )
        if any(
            value is not None and value != current
            for value, current in zip(requested, actual, strict=True)
        ):
            raise DocxError(
                "edit_verification_failed",
                "修改后的直接文字格式与请求不一致。",
            )


def _verify_strict_tables(
    output_snapshot: DocumentSnapshot,
    output_body: ElementTree.Element,
    table_block_ids: frozenset[str],
) -> None:
    """用 Reader 与 locator 双重复检新增或改行后的规则表格。"""

    if not table_block_ids:
        return
    snapshot_tables = {
        block.block_id: block
        for block in output_snapshot.blocks
        if isinstance(block, TableSnapshot)
    }
    xml_tables = {
        location.block_id: location.element
        for location in iter_body_children(output_body)
        if location.kind == "table" and location.block_id is not None
    }
    for block_id in table_block_ids:
        snapshot_table = snapshot_tables.get(block_id)
        xml_table = xml_tables.get(block_id)
        if (
            snapshot_table is None
            or not snapshot_table.editable
            or xml_table is None
            or not is_strictly_editable_table(xml_table)
        ):
            raise DocxError(
                "edit_verification_failed",
                "新增或修改行后的表格未通过规则网格复检。",
            )


def _same_block_id_kind(old_block_id: str, new_block_id: str) -> bool:
    return (
        is_paragraph_block_id(old_block_id)
        and is_paragraph_block_id(new_block_id)
    ) or (
        is_table_block_id(old_block_id)
        and is_table_block_id(new_block_id)
    ) or (
        is_table_cell_block_id(old_block_id)
        and is_table_cell_block_id(new_block_id)
    )


def _snapshot_block_ids(snapshot: DocumentSnapshot) -> set[str]:
    block_ids: set[str] = set()
    for block in snapshot.blocks:
        block_ids.add(block.block_id)
        if isinstance(block, TableSnapshot):
            for row in block.rows:
                for cell in row:
                    block_ids.add(cell.block_id)
    return block_ids


def _require_revision(actual_revision: str, expected_revision: str) -> None:
    if actual_revision != expected_revision:
        raise DocxError(
            "revision_conflict",
            "源 DOCX 已发生变化，请重新读取后再编辑。",
        )


def _normalize_path(
    value: str | os.PathLike[str],
    field_name: str,
    error_type: str,
) -> Path:
    try:
        raw_value = os.fspath(value)
        if not raw_value:
            raise ValueError("empty path")
        return Path(raw_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DocxError(error_type, f"{field_name} 无法规范化。") from exc


def _paths_refer_to_same_file(source_path: Path, output_path: Path) -> bool:
    if source_path == output_path:
        return True
    try:
        return source_path.exists() and output_path.exists() and os.path.samefile(
            source_path,
            output_path,
        )
    except OSError:
        return False


def _normalize_relationship_target(target: str) -> str:
    normalized = target.replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _next_relationship_id(root: ElementTree.Element) -> str:
    used_ids = {
        child.attrib.get("Id")
        for child in root
        if child.tag == _RELATIONSHIP and child.attrib.get("Id")
    }
    candidate = 1
    while f"rId{candidate}" in used_ids:
        candidate += 1
    return f"rId{candidate}"


def _is_valid_xml_text(value: str) -> bool:
    for character in value:
        codepoint = ord(character)
        if (
            codepoint in {0x9, 0xA, 0xD}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            continue
        return False
    return True
