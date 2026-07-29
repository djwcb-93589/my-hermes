"""基于 revision 与 block_id 的独立 DOCX 安全编辑器。"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
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
    TableCellLocation,
    build_edit_target_index,
    get_single_cell_paragraph,
    is_paragraph_block_id,
    is_strictly_editable_paragraph,
    is_strictly_editable_table_cell,
    is_table_cell_block_id,
)
from .models import (
    AppliedEdit,
    DocumentSnapshot,
    EditDocumentRequest,
    EditDocumentResult,
    InspectDocumentRequest,
    ParagraphSnapshot,
    ReplaceParagraphText,
    ReplaceTableCellText,
    ReplaceTextMatch,
    TableCellSnapshot,
    TableSnapshot,
    UpdateDocumentMetadata,
)
from .package import DocxPackage
from .reader import DocxReader
from .search import build_match_id, iter_literal_matches
from .textmap import (
    VisibleTextMap,
    append_text_content,
    build_visible_text_map,
    replace_visible_text_range,
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
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_W_BODY = f"{{{_W_NS}}}body"
_W_RUN = f"{{{_W_NS}}}r"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"

_CORE_PROPERTIES = f"{{{_CP_NS}}}coreProperties"
_CONTENT_TYPES = f"{{{_CONTENT_TYPES_NS}}}Types"
_CONTENT_TYPE_OVERRIDE = f"{{{_CONTENT_TYPES_NS}}}Override"
_RELATIONSHIPS = f"{{{_PACKAGE_REL_NS}}}Relationships"
_RELATIONSHIP = f"{{{_PACKAGE_REL_NS}}}Relationship"

_CORE_PART = "docProps/core.xml"
_DOCUMENT_PART = "word/document.xml"
_CONTENT_TYPES_PART = "[Content_Types].xml"
_ROOT_RELATIONSHIPS_PART = "_rels/.rels"
_CORE_PART_NAME = "/docProps/core.xml"
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


_ValidatedOperation = (
    _ValidatedTextOperation
    | _ValidatedMatchOperation
    | _ValidatedMetadataOperation
)


@dataclass(frozen=True)
class _PlannedTextEdit:
    operation: _ValidatedTextOperation
    paragraph: ElementTree.Element
    current_text: str


@dataclass(frozen=True)
class _PlannedMatchEdit:
    operation: _ValidatedMatchOperation
    text_map: VisibleTextMap
    start: int
    end: int


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
    core_exists: bool
    content_types_root: ElementTree.Element
    content_types_original: bytes
    core_override: ElementTree.Element | None
    relationships_root: ElementTree.Element
    relationships_original: bytes
    core_relationship: ElementTree.Element | None


class DocxEditor:
    """执行全有或全无的简单 DOCX 内容替换。"""

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
        applied_edits = [
            AppliedEdit(
                operation_index=operation.operation_index,
                operation_type=operation.operation_type,
                block_id=(
                    operation.block_id
                    if isinstance(
                        operation,
                        (_ValidatedTextOperation, _ValidatedMatchOperation),
                    )
                    else None
                ),
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

            (
                planned_text_edits,
                planned_match_edits,
                expected_target_texts,
            ) = _plan_text_edits(
                operations,
                body=body,
                snapshot=initial_snapshot,
            )
            metadata_context = _prepare_metadata_context(package, operations)

            document_changed = False
            for plan in planned_text_edits:
                if _apply_text_edit(plan):
                    document_changed = True
            for plan in planned_match_edits:
                if _apply_match_edit(plan):
                    document_changed = True
            if document_changed:
                replacements[_DOCUMENT_PART] = serialize_xml(
                    document_root,
                    original_payload=document_original,
                )

            if metadata_context is not None:
                replacements.update(_apply_metadata_edit(metadata_context))

        changed = bool(replacements)
        temporary_output: Path | None = None
        try:
            with DocxPackage.open(source_path) as current_package:
                _require_revision(current_package.revision, expected_revision)
                temporary_output = create_temporary_output_path(output_path)
                if changed:
                    write_package(current_package, temporary_output, replacements)
                else:
                    write_original_package(current_package, temporary_output)

            output_snapshot = _inspect_temporary_output(
                self._reader,
                temporary_output,
            )
            _verify_output(
                initial_snapshot=initial_snapshot,
                output_snapshot=output_snapshot,
                operations=operations,
                expected_target_texts=expected_target_texts,
                changed=changed,
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
            )
        finally:
            best_effort_unlink(temporary_output)


def edit_document(request: EditDocumentRequest) -> EditDocumentResult:
    """使用一次性 Editor 实例修改现有 DOCX。"""

    return DocxEditor().edit(request)


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
    seen_targets: set[str] = set()
    seen_match_ids: set[str] = set()
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
            raise DocxError(
                "invalid_edit_operation",
                f"第 {operation_index} 个编辑操作类型不受支持。",
            )

        if isinstance(
            validated_operation,
            (_ValidatedTextOperation, _ValidatedMatchOperation),
        ):
            if validated_operation.block_id in seen_targets:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个 block_id 在一次请求中只能修改一次。",
                )
            seen_targets.add(validated_operation.block_id)
        if isinstance(validated_operation, _ValidatedMatchOperation):
            if validated_operation.match_id in seen_match_ids:
                raise DocxError(
                    "duplicate_edit_target",
                    "同一个 match_id 在一次请求中只能修改一次。",
                )
            seen_match_ids.add(validated_operation.match_id)
        validated.append(validated_operation)
    return validated


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


def _plan_text_edits(
    operations: list[_ValidatedOperation],
    *,
    body: ElementTree.Element,
    snapshot: DocumentSnapshot,
) -> tuple[
    list[_PlannedTextEdit],
    list[_PlannedMatchEdit],
    dict[str, str],
]:
    targets = build_edit_target_index(body)
    snapshot_targets = _snapshot_text_targets(snapshot)
    text_plans: list[_PlannedTextEdit] = []
    match_plans: list[_PlannedMatchEdit] = []
    expected_target_texts: dict[str, str] = {}

    for operation in operations:
        if not isinstance(
            operation,
            (_ValidatedTextOperation, _ValidatedMatchOperation),
        ):
            continue
        location = targets.get(operation.block_id)
        snapshot_target = snapshot_targets.get(operation.block_id)
        if location is None and snapshot_target is None:
            error_type = (
                "match_not_found"
                if isinstance(operation, _ValidatedMatchOperation)
                else "block_not_found"
            )
            raise DocxError(error_type, "指定的 block_id 不存在。")
        if location is None or snapshot_target is None:
            raise DocxError(
                "edit_verification_failed",
                "Reader 与 locator 的 block_id 定位结果不一致。",
            )

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
            if isinstance(operation, _ValidatedMatchOperation):
                raise DocxError(
                    "match_not_editable",
                    "匹配内容或其所属表格包含复杂结构，当前阶段不允许局部编辑。",
                )
            raise DocxError(
                "block_not_editable",
                "指定内容块或其所属表格包含复杂结构，当前阶段不允许编辑。",
            )

        if isinstance(operation, _ValidatedTextOperation):
            text_plans.append(
                _PlannedTextEdit(
                    operation=operation,
                    paragraph=paragraph,
                    current_text=current_text,
                )
            )
            expected_target_texts[operation.block_id] = operation.text
            continue

        text_map = build_visible_text_map(paragraph)
        if text_map.text != current_text:
            raise DocxError(
                "edit_verification_failed",
                "Reader 与文字映射的可见内容不一致。",
            )
        start, end = _locate_match_span(
            operation,
            text=text_map.text,
            revision=snapshot.revision,
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
        match_plans.append(
            _PlannedMatchEdit(
                operation=operation,
                text_map=text_map,
                start=start,
                end=end,
            )
        )
        expected_target_texts[operation.block_id] = (
            text_map.text[:start]
            + operation.replacement_text
            + text_map.text[end:]
        )
    return text_plans, match_plans, expected_target_texts


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


def _prepare_metadata_context(
    package: DocxPackage,
    operations: list[_ValidatedOperation],
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

    content_types_original = package.read_xml_bytes(_CONTENT_TYPES_PART)
    content_types_root = parse_xml_preserving_misc(
        content_types_original,
        _CONTENT_TYPES_PART,
    )
    if content_types_root.tag != _CONTENT_TYPES:
        raise DocxError(
            "edit_operation_conflict",
            "[Content_Types].xml 根节点无效。",
        )
    matching_overrides = [
        child
        for child in content_types_root
        if child.tag == _CONTENT_TYPE_OVERRIDE
        and child.attrib.get("PartName") == _CORE_PART_NAME
    ]
    if len(matching_overrides) > 1:
        raise DocxError(
            "edit_operation_conflict",
            "core properties content type 存在重复定义。",
        )
    core_override = matching_overrides[0] if matching_overrides else None
    if (
        core_override is not None
        and core_override.attrib.get("ContentType") != _CORE_CONTENT_TYPE
    ):
        raise DocxError(
            "edit_operation_conflict",
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
        core_exists=core_exists,
        content_types_root=content_types_root,
        content_types_original=content_types_original,
        core_override=core_override,
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


def _apply_match_edit(plan: _PlannedMatchEdit) -> bool:
    try:
        return replace_visible_text_range(
            plan.text_map,
            start=plan.start,
            end=plan.end,
            replacement=plan.operation.replacement_text,
            preserve_format=plan.operation.preserve_format,
        )
    except ValueError as exc:
        raise DocxError(
            "edit_verification_failed",
            "已规划的局部文字范围无法安全写回。",
        ) from exc


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

    if context.core_override is None:
        ElementTree.SubElement(
            context.content_types_root,
            _CONTENT_TYPE_OVERRIDE,
            {
                "PartName": _CORE_PART_NAME,
                "ContentType": _CORE_CONTENT_TYPE,
            },
        )
        replacements[_CONTENT_TYPES_PART] = serialize_xml(
            context.content_types_root,
            original_payload=context.content_types_original,
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


def _verify_output(
    *,
    initial_snapshot: DocumentSnapshot,
    output_snapshot: DocumentSnapshot,
    operations: list[_ValidatedOperation],
    expected_target_texts: dict[str, str],
    changed: bool,
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
        output_snapshot.paragraph_count != initial_snapshot.paragraph_count
        or output_snapshot.table_count != initial_snapshot.table_count
        or output_snapshot.image_count != initial_snapshot.image_count
        or output_snapshot.section_count != initial_snapshot.section_count
        or _snapshot_block_ids(output_snapshot)
        != _snapshot_block_ids(initial_snapshot)
    ):
        raise DocxError(
            "edit_verification_failed",
            "编辑前后的文档 block 结构不一致。",
        )

    output_targets = _snapshot_text_targets(output_snapshot)
    initial_targets = _snapshot_text_targets(initial_snapshot)
    edited_block_ids = {
        operation.block_id
        for operation in operations
        if isinstance(
            operation,
            (_ValidatedTextOperation, _ValidatedMatchOperation),
        )
    }
    for operation in operations:
        if isinstance(
            operation,
            (_ValidatedTextOperation, _ValidatedMatchOperation),
        ):
            target = output_targets.get(operation.block_id)
            if operation.target_kind == "paragraph":
                if not isinstance(target, ParagraphSnapshot):
                    raise DocxError(
                        "edit_verification_failed",
                        "修改后的目标类型与请求不一致。",
                    )
            elif not isinstance(target, _SnapshotTableCellTarget):
                raise DocxError(
                    "edit_verification_failed",
                    "修改后的目标类型与请求不一致。",
                )
            if (
                _snapshot_target_text(target)
                != expected_target_texts.get(operation.block_id)
                or not _snapshot_target_is_editable(target)
            ):
                raise DocxError(
                    "edit_verification_failed",
                    "修改后的目标文字与请求不一致。",
                )

    for block_id, initial_target in initial_targets.items():
        if block_id in edited_block_ids:
            continue
        output_target = output_targets.get(block_id)
        if (
            output_target is None
            or type(output_target) is not type(initial_target)
            or _snapshot_target_text(output_target)
            != _snapshot_target_text(initial_target)
        ):
            raise DocxError(
                "edit_verification_failed",
                "未请求修改的内容块文字发生了变化。",
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


def _snapshot_target_is_editable(target: _SnapshotTextTarget) -> bool:
    """按 Reader 的父子级结果判断快照目标是否可编辑。"""

    if isinstance(target, ParagraphSnapshot):
        return target.editable
    return target.table.editable and target.cell.editable


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
