"""不依赖外部程序的 DOCX package 与受支持 OOXML 结构验证器。"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .errors import DocxError
from .images import validate_image_payload
from .locator import (
    BodyChildLocation,
    is_strictly_editable_table,
    iter_body_children,
    iter_table_cells,
    visible_row_cells,
    visible_table_rows,
)
from .models import (
    DocumentSnapshot,
    InspectDocumentRequest,
    ParagraphSnapshot,
    TableSnapshot,
)
from .package import DOCX_LIMITS, DocxPackage
from .parts import (
    ContentTypesManager,
    FOOTER_CONTENT_TYPE,
    HEADER_CONTENT_TYPE,
    JPEG_CONTENT_TYPE,
    NUMBERING_CONTENT_TYPE,
    PNG_CONTENT_TYPE,
)
from .reader import DocxReader
from .relationships import (
    FOOTER_RELATIONSHIP_TYPE,
    HEADER_RELATIONSHIP_TYPE,
    HYPERLINK_RELATIONSHIP_TYPE,
    IMAGE_RELATIONSHIP_TYPE,
    NUMBERING_RELATIONSHIP_TYPE,
    RELATIONSHIP_ID,
    RelationshipManager,
    source_part_from_relationship_part,
    validate_hyperlink_url,
)
from .sections import (
    locate_sections,
    validate_section_child_order,
    validate_section_page_geometry,
    validate_simple_header_footer_part,
)
from .validation_models import (
    ValidateDocumentRequest,
    ValidateDocumentResult,
    ValidationIssue,
)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)

_W_DOCUMENT = f"{{{_W_NS}}}document"
_W_BODY = f"{{{_W_NS}}}body"
_W_SECTION_PROPERTIES = f"{{{_W_NS}}}sectPr"
_W_SECTION_PROPERTIES_CHANGE = f"{{{_W_NS}}}sectPrChange"
_W_TITLE_PAGE = f"{{{_W_NS}}}titlePg"
_W_HEADER_REFERENCE = f"{{{_W_NS}}}headerReference"
_W_FOOTER_REFERENCE = f"{{{_W_NS}}}footerReference"
_W_TYPE = f"{{{_W_NS}}}type"
_W_NUMBER_PROPERTIES = f"{{{_W_NS}}}numPr"
_W_NUMBER_ID = f"{{{_W_NS}}}numId"
_W_VAL = f"{{{_W_NS}}}val"
_W_NUMBERING = f"{{{_W_NS}}}numbering"
_W_ABSTRACT_NUMBERING = f"{{{_W_NS}}}abstractNum"
_W_NUMBER = f"{{{_W_NS}}}num"
_W_ABSTRACT_NUMBER_ID = f"{{{_W_NS}}}abstractNumId"
_W_MULTI_LEVEL_TYPE = f"{{{_W_NS}}}multiLevelType"
_W_LEVEL = f"{{{_W_NS}}}lvl"
_W_LEVEL_INDEX = f"{{{_W_NS}}}ilvl"
_W_START = f"{{{_W_NS}}}start"
_W_NUMBER_FORMAT = f"{{{_W_NS}}}numFmt"
_W_LEVEL_TEXT = f"{{{_W_NS}}}lvlText"
_W_FIELD_CHARACTER = f"{{{_W_NS}}}fldChar"
_W_INSTRUCTION_TEXT = f"{{{_W_NS}}}instrText"
_W_FIELD_CHARACTER_TYPE = f"{{{_W_NS}}}fldCharType"
_W_HEADER = f"{{{_W_NS}}}hdr"
_W_FOOTER = f"{{{_W_NS}}}ftr"
_W_HYPERLINK = f"{{{_W_NS}}}hyperlink"
_W_EVEN_AND_ODD_HEADERS = f"{{{_W_NS}}}evenAndOddHeaders"
_CORE_PROPERTIES = f"{{{_CP_NS}}}coreProperties"

_WP_INLINE = f"{{{_WP_NS}}}inline"
_WP_ANCHOR = f"{{{_WP_NS}}}anchor"
_WP_EXTENT = f"{{{_WP_NS}}}extent"
_WP_DOC_PROPERTIES = f"{{{_WP_NS}}}docPr"
_A_BLIP = f"{{{_A_NS}}}blip"
_R_EMBED = f"{{{_OFFICE_REL_NS}}}embed"

_CONTENT_TYPES_PART = "[Content_Types].xml"
_DOCUMENT_PART = "word/document.xml"
_CORE_PROPERTIES_PART = "docProps/core.xml"
_SETTINGS_PART = "word/settings.xml"
_CORE_PROPERTIES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-package.core-properties+xml"
)
_OFFICE_DOCUMENT_RELATIONSHIP_TYPE = (
    f"{_OFFICE_REL_NS}/officeDocument"
)
_CORE_PROPERTIES_RELATIONSHIP_TYPE = (
    f"{_PACKAGE_REL_NS}/metadata/core-properties"
)
_PACKAGE_ROOT_SOURCE = "package-root.xml"
_RELATIONSHIPS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-package.relationships+xml"
)
_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.template.main+xml",
    }
)
_OPERATIONAL_ERRORS = frozenset(
    {
        "invalid_request",
        "unsupported_extension",
        "source_not_found",
        "source_not_file",
        "source_unreadable",
    }
)


@dataclass(frozen=True)
class _RelationshipRecord:
    relationship_part: str
    source_part: str
    relationship_id: str
    relationship_type: str
    target: str
    external: bool
    target_part: str | None


@dataclass
class _ValidationContext:
    package: DocxPackage
    strict: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    issue_keys: set[tuple[str, str | None, str]] = field(default_factory=set)
    checked_parts: set[str] = field(default_factory=set)
    relationships: dict[tuple[str, str], _RelationshipRecord] = field(
        default_factory=dict
    )

    def add_issue(
        self,
        code: str,
        message: str,
        *,
        part_name: str | None = None,
        severity: str = "error",
    ) -> None:
        normalized_severity = "warning" if severity == "warning" else "error"
        key = (code, part_name, normalized_severity)
        if key in self.issue_keys:
            return
        self.issue_keys.add(key)
        self.issues.append(
            ValidationIssue(
                code=code,
                message=message,
                part_name=part_name,
                severity=normalized_severity,
            )
        )


class DocxValidator:
    """验证 package 安全性及当前 DOCX 模块依赖的结构一致性。"""

    def __init__(self, reader: DocxReader | None = None) -> None:
        self._reader = reader or DocxReader()

    def validate(
        self,
        request: ValidateDocumentRequest,
    ) -> ValidateDocumentResult:
        validated_request = _validate_request(request)
        try:
            package = DocxPackage.open(validated_request.source_path)
        except DocxError as exc:
            if exc.error_type in _OPERATIONAL_ERRORS:
                raise
            source_path, revision, size_bytes = _read_failed_package_identity(
                validated_request.source_path
            )
            return ValidateDocumentResult(
                source_path=source_path,
                valid=False,
                revision=revision,
                size_bytes=size_bytes,
                issues=[
                    ValidationIssue(
                        code=exc.error_type,
                        message="DOCX package 未通过安全打开检查。",
                    )
                ],
                checked_parts=[],
            )

        try:
            with package:
                return self._validate_open_package(
                    package,
                    strict=validated_request.strict,
                )
        except DocxError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DocxError(
                "validation_failed",
                "DOCX 核心验证过程无法完成。",
            ) from exc

    def _validate_open_package(
        self,
        package: DocxPackage,
        *,
        strict: bool,
    ) -> ValidateDocumentResult:
        context = _ValidationContext(package=package, strict=strict)
        content_types = _validate_content_types(context)
        _validate_xml_parts(context)
        _validate_relationships(context, content_types)
        snapshot = _read_snapshot(context, self._reader)
        valid_number_ids = _validate_numbering(context, content_types)
        _validate_main_document(
            context,
            snapshot,
            valid_number_ids=valid_number_ids,
        )
        _validate_sections_and_headers(context, content_types)
        return ValidateDocumentResult(
            source_path=package.source_path,
            valid=not any(
                issue.severity == "error" for issue in context.issues
            ),
            revision=package.revision,
            size_bytes=package.size_bytes,
            issues=context.issues,
            checked_parts=sorted(context.checked_parts),
        )


def validate_document(
    request: ValidateDocumentRequest,
) -> ValidateDocumentResult:
    """使用一次性 Validator 验证本地 DOCX。"""

    return DocxValidator().validate(request)


def _validate_request(
    request: ValidateDocumentRequest,
) -> ValidateDocumentRequest:
    if not isinstance(request, ValidateDocumentRequest):
        raise DocxError(
            "invalid_request",
            "request 必须是 ValidateDocumentRequest。",
        )
    if not isinstance(request.source_path, (str, os.PathLike)):
        raise DocxError("invalid_request", "source_path 必须是文件系统路径。")
    if not isinstance(request.strict, bool):
        raise DocxError("invalid_request", "strict 必须是布尔值。")
    return request


def _read_failed_package_identity(
    source_path: Path,
) -> tuple[Path, str, int]:
    try:
        normalized = Path(os.fspath(source_path)).expanduser().resolve(
            strict=False
        )
        source_stat = normalized.stat()
        if not stat.S_ISREG(source_stat.st_mode):
            raise DocxError("source_not_file", "源路径不是普通文件。")
        size_bytes = source_stat.st_size
        if size_bytes > DOCX_LIMITS.max_source_size:
            return normalized, "", size_bytes
        digest = hashlib.sha256()
        with normalized.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return normalized, f"sha256:{digest.hexdigest()}", size_bytes
    except DocxError:
        raise
    except FileNotFoundError as exc:
        raise DocxError("source_not_found", "源 DOCX 不存在。") from exc
    except OSError as exc:
        raise DocxError("source_unreadable", "无法读取源 DOCX。") from exc


def _validate_content_types(
    context: _ValidationContext,
) -> ContentTypesManager | None:
    part_name = _CONTENT_TYPES_PART
    context.checked_parts.add(part_name)
    try:
        manager = ContentTypesManager(
            context.package.read_xml(part_name),
            error_type="edit_verification_failed",
        )
    except DocxError:
        context.add_issue(
            "invalid_content_types",
            "Content Types 根节点、定义或顺序无效。",
            part_name=part_name,
        )
        return None

    critical_types = {
        "_rels/.rels": _RELATIONSHIPS_CONTENT_TYPE,
        _DOCUMENT_PART: _DOCUMENT_CONTENT_TYPES,
    }
    if context.package.has_part(_CORE_PROPERTIES_PART):
        critical_types[_CORE_PROPERTIES_PART] = (
            _CORE_PROPERTIES_CONTENT_TYPE
        )
    for critical_part, expected in critical_types.items():
        actual = manager.content_type_for(critical_part)
        if (
            (
                isinstance(expected, frozenset)
                and actual not in expected
            )
            or (
                isinstance(expected, str)
                and actual != expected
            )
        ):
            context.add_issue(
                "invalid_critical_content_type",
                "关键 OOXML part 的 Content Type 无效。",
                part_name=critical_part,
            )

    for package_part in context.package.part_names:
        if package_part == _CONTENT_TYPES_PART or package_part.endswith("/"):
            continue
        content_type = manager.content_type_for(package_part)
        if content_type is None:
            context.add_issue(
                "missing_content_type",
                "DOCX part 缺少可解析的 Content Type。",
                part_name=package_part,
            )
            continue
        if package_part.lower().endswith((".xml", ".rels")) and content_type.startswith(
            "image/"
        ):
            context.add_issue(
                "xml_marked_as_image",
                "XML part 被错误标记为图片 Content Type。",
                part_name=package_part,
            )
        if (
            package_part.lower().endswith(".rels")
            and content_type != _RELATIONSHIPS_CONTENT_TYPE
        ):
            context.add_issue(
                "invalid_relationship_content_type",
                "relationship part 的 Content Type 无效。",
                part_name=package_part,
            )
        if package_part.lower().startswith("word/media/"):
            _validate_media_part(
                context,
                package_part,
                content_type,
            )
    return manager


def _validate_media_part(
    context: _ValidationContext,
    part_name: str,
    content_type: str,
) -> None:
    context.checked_parts.add(part_name)
    extension = part_name.rsplit(".", 1)[-1].lower()
    if extension not in {"png", "jpg", "jpeg"}:
        if context.strict:
            context.add_issue(
                "unsupported_image_format",
                "媒体 part 使用当前核心验证器未完整解析的图片格式。",
                part_name=part_name,
                severity="warning",
            )
        return
    try:
        info = validate_image_payload(
            context.package.read_part_bytes(part_name),
            extension=extension,
        )
    except DocxError:
        context.add_issue(
            "invalid_image_payload",
            "图片 part 的签名、基础结构或尺寸无效。",
            part_name=part_name,
        )
        return
    expected_type = (
        PNG_CONTENT_TYPE if info.image_format == "png" else JPEG_CONTENT_TYPE
    )
    if content_type != expected_type:
        context.add_issue(
            "image_content_type_mismatch",
            "图片签名与 Content Type 不一致。",
            part_name=part_name,
        )


def _validate_xml_parts(context: _ValidationContext) -> None:
    for part_name in context.package.part_names:
        if not (
            part_name == _CONTENT_TYPES_PART
            or part_name.lower().endswith((".xml", ".rels"))
        ):
            continue
        context.checked_parts.add(part_name)
        try:
            root = context.package.read_xml(part_name)
        except DocxError:
            context.add_issue(
                "invalid_xml_part",
                "XML part 无法通过安全解析。",
                part_name=part_name,
            )
            continue
        node_count = sum(1 for _ in root.iter())
        if node_count > DOCX_LIMITS.max_xml_nodes:
            context.add_issue(
                "xml_node_limit_exceeded",
                "XML part 的节点数量超过安全限制。",
                part_name=part_name,
            )
        if not isinstance(root.tag, str) or not root.tag:
            context.add_issue(
                "invalid_xml_root",
                "XML part 缺少有效根节点。",
                part_name=part_name,
            )
        if (
            part_name == _CORE_PROPERTIES_PART
            and root.tag != _CORE_PROPERTIES
        ):
            context.add_issue(
                "invalid_core_properties_root",
                "core properties part 的根节点无效。",
                part_name=part_name,
            )


def _validate_relationships(
    context: _ValidationContext,
    content_types: ContentTypesManager | None,
) -> None:
    for part_name in context.package.part_names:
        if not part_name.lower().endswith(".rels"):
            continue
        context.checked_parts.add(part_name)
        try:
            source_part = source_part_from_relationship_part(part_name)
            manager = RelationshipManager(
                context.package.read_xml(part_name),
                source_part=source_part,
            )
        except DocxError:
            context.add_issue(
                "invalid_relationship_part",
                "relationship part 的根节点或定义无效。",
                part_name=part_name,
            )
            continue
        for relationship_id, element in manager.iter_relationships():
            relationship_type = element.attrib.get("Type", "")
            target = element.attrib.get("Target", "")
            target_mode = element.attrib.get("TargetMode")
            if target_mode not in {None, "", "External"}:
                context.add_issue(
                    "invalid_relationship_target_mode",
                    "relationship 的 TargetMode 无效。",
                    part_name=part_name,
                )
            external = target_mode == "External"
            target_part: str | None = None
            if external:
                if relationship_type in {
                    IMAGE_RELATIONSHIP_TYPE,
                    HEADER_RELATIONSHIP_TYPE,
                    FOOTER_RELATIONSHIP_TYPE,
                    NUMBERING_RELATIONSHIP_TYPE,
                }:
                    context.add_issue(
                        "internal_relationship_marked_external",
                        "内部 OOXML relationship 被错误标记为 External。",
                        part_name=part_name,
                    )
                if relationship_type == HYPERLINK_RELATIONSHIP_TYPE:
                    try:
                        validate_hyperlink_url(target)
                    except DocxError:
                        context.add_issue(
                            "invalid_external_hyperlink",
                            "外部 hyperlink 使用不受支持的 URL。",
                            part_name=part_name,
                        )
            else:
                if relationship_type == HYPERLINK_RELATIONSHIP_TYPE:
                    context.add_issue(
                        "hyperlink_not_external",
                        "hyperlink relationship 缺少 External TargetMode。",
                        part_name=part_name,
                    )
                try:
                    target_part = manager.resolve_internal(element)
                except DocxError:
                    context.add_issue(
                        "unsafe_relationship_target",
                        "内部 relationship Target 无法安全解析。",
                        part_name=part_name,
                    )
                if (
                    target_part is not None
                    and not context.package.has_part(target_part)
                ):
                    context.add_issue(
                        "missing_relationship_target",
                        "内部 relationship 指向不存在的 part。",
                        part_name=part_name,
                    )
            record = _RelationshipRecord(
                relationship_part=part_name,
                source_part=source_part,
                relationship_id=relationship_id,
                relationship_type=relationship_type,
                target=target,
                external=external,
                target_part=target_part,
            )
            context.relationships[(source_part, relationship_id)] = record
            _validate_typed_relationship(
                context,
                record,
                content_types,
            )
    _validate_root_relationships(context)
    if context.strict:
        referenced_images = {
            record.target_part
            for record in context.relationships.values()
            if record.relationship_type == IMAGE_RELATIONSHIP_TYPE
            and record.target_part is not None
        }
        for part_name in context.package.part_names:
            if (
                part_name.lower().startswith("word/media/")
                and part_name not in referenced_images
            ):
                context.add_issue(
                    "orphan_image_part",
                    "DOCX 包含未被 image relationship 引用的媒体 part。",
                    part_name=part_name,
                    severity="warning",
                )


def _validate_root_relationships(context: _ValidationContext) -> None:
    root_records = [
        record
        for record in context.relationships.values()
        if record.source_part == _PACKAGE_ROOT_SOURCE
    ]
    office_documents = [
        record
        for record in root_records
        if record.relationship_type == _OFFICE_DOCUMENT_RELATIONSHIP_TYPE
    ]
    if (
        len(office_documents) != 1
        or office_documents[0].external
        or office_documents[0].target_part != _DOCUMENT_PART
    ):
        context.add_issue(
            "invalid_office_document_relationship",
            "根 relationship 必须唯一指向 word/document.xml。",
            part_name="_rels/.rels",
        )

    core_relationships = [
        record
        for record in root_records
        if record.relationship_type == _CORE_PROPERTIES_RELATIONSHIP_TYPE
    ]
    if context.package.has_part(_CORE_PROPERTIES_PART):
        if (
            len(core_relationships) != 1
            or core_relationships[0].external
            or core_relationships[0].target_part
            != _CORE_PROPERTIES_PART
        ):
            context.add_issue(
                "invalid_core_properties_relationship",
                "core properties part 缺少唯一且正确的根 relationship。",
                part_name="_rels/.rels",
            )
    elif core_relationships:
        context.add_issue(
            "invalid_core_properties_relationship",
            "根 relationship 指向不存在的 core properties part。",
            part_name="_rels/.rels",
        )


def _validate_typed_relationship(
    context: _ValidationContext,
    record: _RelationshipRecord,
    content_types: ContentTypesManager | None,
) -> None:
    if record.target_part is None:
        return
    expected_content_type: str | None = None
    if record.relationship_type == IMAGE_RELATIONSHIP_TYPE:
        if not record.target_part.lower().startswith("word/media/"):
            context.add_issue(
                "invalid_image_relationship",
                "image relationship 未指向 word/media part。",
                part_name=record.relationship_part,
            )
        if (
            content_types is not None
            and not (
                content_types.content_type_for(
                    record.target_part
                )
                or ""
            ).startswith("image/")
        ):
            context.add_issue(
                "relationship_content_type_mismatch",
                "image relationship 目标不是图片 Content Type。",
                part_name=record.relationship_part,
            )
    elif record.relationship_type == HEADER_RELATIONSHIP_TYPE:
        expected_content_type = HEADER_CONTENT_TYPE
    elif record.relationship_type == FOOTER_RELATIONSHIP_TYPE:
        expected_content_type = FOOTER_CONTENT_TYPE
    elif record.relationship_type == NUMBERING_RELATIONSHIP_TYPE:
        expected_content_type = NUMBERING_CONTENT_TYPE
    if (
        expected_content_type is not None
        and content_types is not None
        and content_types.content_type_for(record.target_part)
        != expected_content_type
    ):
        context.add_issue(
            "relationship_content_type_mismatch",
            "relationship 目标 part 的 Content Type 与类型不一致。",
            part_name=record.relationship_part,
        )


def _read_snapshot(
    context: _ValidationContext,
    reader: DocxReader,
) -> DocumentSnapshot | None:
    try:
        snapshot = reader.inspect(
            InspectDocumentRequest(
                source_path=context.package.source_path,
                include_runs=False,
                include_tables=True,
            )
        )
    except DocxError:
        context.add_issue(
            "reader_validation_failed",
            "DOCX 无法生成稳定的结构化快照。",
            part_name=_DOCUMENT_PART,
        )
        return None
    if snapshot.revision != context.package.revision:
        context.add_issue(
            "source_changed_during_validation",
            "源 DOCX 在验证期间发生变化，Reader 快照已被丢弃。",
        )
        return None
    return snapshot


def _validate_numbering(
    context: _ValidationContext,
    content_types: ContentTypesManager | None,
) -> frozenset[int]:
    numbering_parts = [
        part_name
        for part_name in context.package.part_names
        if (
            content_types is not None
            and content_types.content_type_for(part_name)
            == NUMBERING_CONTENT_TYPE
        )
        or part_name == "word/numbering.xml"
    ]
    document_numbering_relationships = [
        record
        for record in context.relationships.values()
        if record.source_part == _DOCUMENT_PART
        and record.relationship_type == NUMBERING_RELATIONSHIP_TYPE
    ]
    if not numbering_parts:
        if document_numbering_relationships:
            context.add_issue(
                "missing_numbering_part",
                "document numbering relationship 缺少目标 part。",
                part_name=_DOCUMENT_PART,
            )
        return frozenset()
    if len(set(numbering_parts)) != 1:
        context.add_issue(
            "multiple_numbering_parts",
            "DOCX 包含多个 numbering part。",
        )
    part_name = sorted(set(numbering_parts))[0]
    context.checked_parts.add(part_name)
    if (
        len(document_numbering_relationships) != 1
        or document_numbering_relationships[0].target_part != part_name
    ):
        context.add_issue(
            "invalid_numbering_relationship",
            "numbering part 缺少唯一且正确的 document relationship。",
            part_name=part_name,
        )
    if (
        content_types is not None
        and content_types.content_type_for(part_name)
        != NUMBERING_CONTENT_TYPE
    ):
        context.add_issue(
            "invalid_numbering_content_type",
            "numbering part 的 Content Type 无效。",
            part_name=part_name,
        )
    try:
        root = context.package.read_xml(part_name)
    except DocxError:
        context.add_issue(
            "invalid_numbering_xml",
            "numbering part 无法解析。",
            part_name=part_name,
        )
        return frozenset()
    if root.tag != _W_NUMBERING:
        context.add_issue(
            "invalid_numbering_root",
            "numbering part 根节点无效。",
            part_name=part_name,
        )
        return frozenset()

    abstract_ids: dict[int, ElementTree.Element] = {}
    number_ids: dict[int, int] = {}
    for child in root:
        if child.tag == _W_ABSTRACT_NUMBERING:
            abstract_id = _read_non_negative_id(
                child,
                _W_ABSTRACT_NUMBER_ID,
            )
            if abstract_id is None or abstract_id in abstract_ids:
                context.add_issue(
                    "invalid_abstract_number_id",
                    "numbering.xml 包含重复或无效的 abstractNumId。",
                    part_name=part_name,
                )
                continue
            abstract_ids[abstract_id] = child
            multi_level_type = child.find(_W_MULTI_LEVEL_TYPE)
            levels = child.findall(_W_LEVEL)
            is_single_level = (
                multi_level_type is not None
                and multi_level_type.attrib.get(_W_VAL) == "singleLevel"
                and len(levels) == 1
            )
            if context.strict and not is_single_level:
                context.add_issue(
                    "unsupported_complex_numbering",
                    "numbering.xml 包含当前模块未完整建模的复杂编号。",
                    part_name=part_name,
                    severity="warning",
                )
            elif is_single_level:
                level = levels[0]
                if (
                    level.attrib.get(_W_LEVEL_INDEX) != "0"
                    or not _has_nonempty_value(level.find(_W_START))
                    or not _has_nonempty_value(
                        level.find(_W_NUMBER_FORMAT)
                    )
                    or not _has_nonempty_value(level.find(_W_LEVEL_TEXT))
                ):
                    context.add_issue(
                        "invalid_single_level_numbering",
                        "单级 numbering 定义缺少必要的 lvl 属性或子节点。",
                        part_name=part_name,
                    )
        elif child.tag == _W_NUMBER:
            number_id = _read_non_negative_id(child, _W_NUMBER_ID)
            abstract_reference = child.find(_W_ABSTRACT_NUMBER_ID)
            abstract_id = _read_value_id(abstract_reference)
            if (
                number_id is None
                or number_id in number_ids
                or abstract_id is None
            ):
                context.add_issue(
                    "invalid_number_id",
                    "numbering.xml 包含重复或无效的 numId。",
                    part_name=part_name,
                )
                continue
            number_ids[number_id] = abstract_id
        elif context.strict:
            context.add_issue(
                "unsupported_complex_numbering",
                "numbering.xml 包含当前模块未完整建模的顶层结构。",
                part_name=part_name,
                severity="warning",
            )
    for abstract_id in number_ids.values():
        if abstract_id not in abstract_ids:
            context.add_issue(
                "missing_abstract_numbering",
                "numId 引用了不存在的 abstractNumId。",
                part_name=part_name,
            )
    return frozenset(number_ids)


def _has_nonempty_value(element: ElementTree.Element | None) -> bool:
    return element is not None and bool(element.attrib.get(_W_VAL))


def _read_non_negative_id(
    element: ElementTree.Element,
    attribute_name: str,
) -> int | None:
    try:
        value = int(element.attrib.get(attribute_name, ""))
    except ValueError:
        return None
    return value if value >= 0 else None


def _read_value_id(element: ElementTree.Element | None) -> int | None:
    if element is None:
        return None
    try:
        value = int(element.attrib.get(_W_VAL, ""))
    except ValueError:
        return None
    return value if value >= 0 else None


def _validate_main_document(
    context: _ValidationContext,
    snapshot: DocumentSnapshot | None,
    *,
    valid_number_ids: frozenset[int],
) -> None:
    context.checked_parts.add(_DOCUMENT_PART)
    try:
        root = context.package.read_xml(_DOCUMENT_PART)
    except DocxError:
        context.add_issue(
            "invalid_document_xml",
            "主文档 XML 无法解析。",
            part_name=_DOCUMENT_PART,
        )
        return
    if root.tag != _W_DOCUMENT:
        context.add_issue(
            "invalid_document_root",
            "word/document.xml 根节点不是 w:document。",
            part_name=_DOCUMENT_PART,
        )
        return
    bodies = [child for child in root if child.tag == _W_BODY]
    if len(bodies) != 1:
        context.add_issue(
            "invalid_document_body",
            "word/document.xml 必须包含唯一 w:body。",
            part_name=_DOCUMENT_PART,
        )
        return
    body = bodies[0]
    body_sections = [
        child for child in body if child.tag == _W_SECTION_PROPERTIES
    ]
    if (
        len(body_sections) > 1
        or (
            bool(body_sections)
            and body[-1] is not body_sections[0]
        )
    ):
        context.add_issue(
            "invalid_section_structure",
            "body 直接承载的 sectPr 必须唯一且位于正文末尾。",
            part_name=_DOCUMENT_PART,
        )
    locations = list(iter_body_children(body))
    locator_ids: list[str] = []
    table_locations: dict[str, BodyChildLocation] = {}
    for location in locations:
        if location.block_id is not None:
            locator_ids.append(location.block_id)
        if location.kind == "table" and location.block_id is not None:
            table_locations[location.block_id] = location
            locator_ids.extend(
                cell.block_id for cell in iter_table_cells(location)
            )
        elif location.kind == "unsupported" and context.strict:
            context.add_issue(
                "unsupported_complex_block",
                "正文包含当前模块未完整建模的顶层内容。",
                part_name=_DOCUMENT_PART,
                severity="warning",
            )
    if len(locator_ids) != len(set(locator_ids)):
        context.add_issue(
            "duplicate_block_id",
            "locator 生成了重复 block_id。",
            part_name=_DOCUMENT_PART,
        )

    if snapshot is not None:
        snapshot_ids = _snapshot_block_ids(snapshot)
        if set(locator_ids) != snapshot_ids:
            context.add_issue(
                "reader_locator_mismatch",
                "Reader 与 locator 的 block 集合不一致。",
                part_name=_DOCUMENT_PART,
            )
        try:
            section_locations = locate_sections(body)
        except DocxError:
            context.add_issue(
                "invalid_section_structure",
                "section 无法由统一 locator 安全定位。",
                part_name=_DOCUMENT_PART,
            )
        else:
            xml_section_count = sum(
                1
                for element in root.iter()
                if element.tag == _W_SECTION_PROPERTIES
            )
            if xml_section_count != snapshot.section_count:
                context.add_issue(
                    "section_count_mismatch",
                    "Reader 与 document.xml 的 section 数量不一致。",
                    part_name=_DOCUMENT_PART,
                )
        _validate_tables(
            context,
            snapshot,
            table_locations,
        )
        if context.strict:
            for block in snapshot.blocks:
                if isinstance(block, ParagraphSnapshot) and not block.editable:
                    context.add_issue(
                        "unsupported_complex_block",
                        "段落包含合法但当前模块不安全编辑的复杂结构。",
                        part_name=_DOCUMENT_PART,
                        severity="warning",
                    )

    _validate_document_drawings(context, root)
    _validate_document_hyperlinks(context, root)
    _validate_document_numbering_references(
        context,
        root,
        valid_number_ids=valid_number_ids,
    )


def _snapshot_block_ids(snapshot: DocumentSnapshot) -> set[str]:
    block_ids: set[str] = set()
    for block in snapshot.blocks:
        block_ids.add(block.block_id)
        if isinstance(block, TableSnapshot):
            for row in block.rows:
                block_ids.update(cell.block_id for cell in row)
    return block_ids


def _validate_tables(
    context: _ValidationContext,
    snapshot: DocumentSnapshot,
    table_locations: dict[str, BodyChildLocation],
) -> None:
    for block in snapshot.blocks:
        if not isinstance(block, TableSnapshot):
            continue
        location = table_locations.get(block.block_id)
        if location is None:
            context.add_issue(
                "table_locator_missing",
                "Reader 表格无法由 locator 定位。",
                part_name=_DOCUMENT_PART,
            )
            continue
        table = location.element
        rows, _ = visible_table_rows(table)
        actual_shape = tuple(
            len(visible_row_cells(row)[0]) for row in rows
        )
        if block.editable and (
            block.row_count != len(actual_shape)
            or block.column_count
            != (actual_shape[0] if actual_shape else 0)
        ):
            context.add_issue(
                "table_shape_mismatch",
                "Reader 表格行列数量与 XML 不一致。",
                part_name=_DOCUMENT_PART,
            )
        strict_structure = is_strictly_editable_table(table)
        if block.editable and not strict_structure:
            context.add_issue(
                "editable_table_not_strict",
                "Reader 可编辑表格未通过 locator 严格结构检查。",
                part_name=_DOCUMENT_PART,
            )
        elif not strict_structure and context.strict:
            context.add_issue(
                "unsupported_complex_table",
                "表格合法可读，但超出当前安全编辑范围。",
                part_name=_DOCUMENT_PART,
                severity="warning",
            )


def _validate_document_drawings(
    context: _ValidationContext,
    root: ElementTree.Element,
) -> None:
    extent_by_blip: dict[int, bool] = {}
    for container_tag in (_WP_INLINE, _WP_ANCHOR):
        for container in root.iter(container_tag):
            contained_blips = list(container.iter(_A_BLIP))
            extent = container.find(_WP_EXTENT)
            try:
                valid_extent = (
                    extent is not None
                    and int(extent.attrib.get("cx", "0")) > 0
                    and int(extent.attrib.get("cy", "0")) > 0
                )
            except ValueError:
                valid_extent = False
            if contained_blips and len(
                container.findall(_WP_DOC_PROPERTIES)
            ) != 1:
                context.add_issue(
                    "invalid_drawing_properties_id",
                    "每个图片 drawing 必须包含唯一的 wp:docPr。",
                    part_name=_DOCUMENT_PART,
                )
            for blip in contained_blips:
                extent_by_blip[id(blip)] = valid_extent

    doc_property_ids = [
        element.attrib.get("id")
        for element in root.iter(_WP_DOC_PROPERTIES)
    ]
    if (
        len(doc_property_ids) != len(set(doc_property_ids))
        or any(
            value is None
            or not value.isdigit()
            or int(value) <= 0
            for value in doc_property_ids
        )
    ):
        context.add_issue(
            "invalid_drawing_properties_id",
            "wp:docPr/@id 必须是唯一正整数。",
            part_name=_DOCUMENT_PART,
        )
    embedded_relationship_ids: set[str] = set()
    for blip in root.iter(_A_BLIP):
        relationship_id = blip.attrib.get(_R_EMBED)
        if relationship_id:
            embedded_relationship_ids.add(relationship_id)
        record = (
            context.relationships.get((_DOCUMENT_PART, relationship_id))
            if relationship_id
            else None
        )
        if (
            record is None
            or record.relationship_type != IMAGE_RELATIONSHIP_TYPE
            or record.external
            or record.target_part is None
            or not context.package.has_part(record.target_part)
        ):
            context.add_issue(
                "invalid_image_embed",
                "DrawingML r:embed 未指向有效内部图片 relationship。",
                part_name=_DOCUMENT_PART,
            )
        if not extent_by_blip.get(id(blip), False):
            context.add_issue(
                "invalid_image_extent",
                "DrawingML 图片 extent 必须使用正整数。",
                part_name=_DOCUMENT_PART,
            )
    if context.strict:
        for record in context.relationships.values():
            if (
                record.source_part == _DOCUMENT_PART
                and record.relationship_type == IMAGE_RELATIONSHIP_TYPE
                and record.relationship_id
                not in embedded_relationship_ids
            ):
                context.add_issue(
                    "unused_document_image_relationship",
                    "document image relationship 未被 DrawingML r:embed 使用。",
                    part_name=_DOCUMENT_PART,
                    severity="warning",
                )


def _validate_document_hyperlinks(
    context: _ValidationContext,
    root: ElementTree.Element,
) -> None:
    for hyperlink in root.iter(_W_HYPERLINK):
        relationship_id = hyperlink.attrib.get(RELATIONSHIP_ID)
        if relationship_id is None:
            continue
        record = context.relationships.get(
            (_DOCUMENT_PART, relationship_id)
        )
        if (
            record is None
            or record.relationship_type != HYPERLINK_RELATIONSHIP_TYPE
            or not record.external
        ):
            context.add_issue(
                "invalid_hyperlink_reference",
                "w:hyperlink 未指向有效外部 hyperlink relationship。",
                part_name=_DOCUMENT_PART,
            )


def _validate_document_numbering_references(
    context: _ValidationContext,
    root: ElementTree.Element,
    *,
    valid_number_ids: frozenset[int],
) -> None:
    for properties in root.iter(_W_NUMBER_PROPERTIES):
        number_id = _read_value_id(properties.find(_W_NUMBER_ID))
        if number_id is None or number_id not in valid_number_ids:
            context.add_issue(
                "missing_numbering_reference",
                "列表段落引用了不存在的 numId。",
                part_name=_DOCUMENT_PART,
            )


def _validate_sections_and_headers(
    context: _ValidationContext,
    content_types: ContentTypesManager | None,
) -> None:
    try:
        document_root = context.package.read_xml(_DOCUMENT_PART)
    except DocxError:
        return
    body = document_root.find(_W_BODY)
    if body is None:
        return
    referenced_parts: set[str] = set()
    try:
        section_locations = locate_sections(body)
    except DocxError:
        context.add_issue(
            "invalid_section_structure",
            "section 无法由统一 locator 安全定位。",
            part_name=_DOCUMENT_PART,
        )
        return
    for location in section_locations:
        section = location.section_properties
        try:
            validate_section_child_order(
                section,
                error_type="edit_verification_failed",
            )
            validate_section_page_geometry(
                section,
                error_type="edit_verification_failed",
            )
        except DocxError:
            context.add_issue(
                "invalid_section_structure",
                "section 子节点顺序、重复约束或页面属性无效。",
                part_name=_DOCUMENT_PART,
            )
        if section.find(_W_SECTION_PROPERTIES_CHANGE) is not None:
            context.add_issue(
                "unsupported_section_revision",
                "section 包含当前模块不编辑的属性修订。",
                part_name=_DOCUMENT_PART,
                severity="warning",
            )
        if section.find(_W_TITLE_PAGE) is not None:
            context.add_issue(
                "unsupported_title_page",
                "section 启用了当前模块不编辑的首页不同设置。",
                part_name=_DOCUMENT_PART,
                severity="warning",
            )
        reference_keys: set[tuple[str, str]] = set()
        for reference in section:
            if reference.tag not in {
                _W_HEADER_REFERENCE,
                _W_FOOTER_REFERENCE,
            }:
                continue
            reference_type = reference.attrib.get(_W_TYPE, "default")
            reference_key = (reference.tag, reference_type)
            if reference_key in reference_keys:
                context.add_issue(
                    "duplicate_header_footer_reference",
                    "同一 section 包含重复类型的页眉或页脚 reference。",
                    part_name=_DOCUMENT_PART,
                )
            reference_keys.add(reference_key)
            if reference_type not in {"default", "first", "even"}:
                context.add_issue(
                    "invalid_header_footer_reference_type",
                    "页眉页脚 reference type 无效。",
                    part_name=_DOCUMENT_PART,
                )
            elif reference_type in {"first", "even"}:
                context.add_issue(
                    "unsupported_header_footer_variant",
                    "文档使用当前模块不编辑的 first/even 页眉页脚。",
                    part_name=_DOCUMENT_PART,
                    severity="warning",
                )
            relationship_id = reference.attrib.get(RELATIONSHIP_ID)
            record = (
                context.relationships.get(
                    (_DOCUMENT_PART, relationship_id)
                )
                if relationship_id
                else None
            )
            expected_type = (
                HEADER_RELATIONSHIP_TYPE
                if reference.tag == _W_HEADER_REFERENCE
                else FOOTER_RELATIONSHIP_TYPE
            )
            if (
                record is None
                or record.relationship_type != expected_type
                or record.external
                or record.target_part is None
                or not context.package.has_part(record.target_part)
            ):
                context.add_issue(
                    "invalid_header_footer_reference",
                    "页眉页脚 reference 未指向有效内部 part。",
                    part_name=_DOCUMENT_PART,
                )
                continue
            referenced_parts.add(record.target_part)
            _validate_header_footer_part(
                context,
                record.target_part,
                part_kind=(
                    "header"
                    if reference.tag == _W_HEADER_REFERENCE
                    else "footer"
                ),
                content_types=content_types,
            )
    _validate_even_and_odd_setting(context)
    if context.strict:
        for part_name in context.package.part_names:
            if (
                part_name.startswith("word/header")
                or part_name.startswith("word/footer")
            ) and part_name.endswith(".xml") and part_name not in referenced_parts:
                context.add_issue(
                    "orphan_header_footer_part",
                    "DOCX 包含未被 section 引用的页眉或页脚 part。",
                    part_name=part_name,
                    severity="warning",
                )


def _validate_header_footer_part(
    context: _ValidationContext,
    part_name: str,
    *,
    part_kind: str,
    content_types: ContentTypesManager | None,
) -> None:
    context.checked_parts.add(part_name)
    expected_content_type = (
        HEADER_CONTENT_TYPE if part_kind == "header" else FOOTER_CONTENT_TYPE
    )
    if (
        content_types is not None
        and content_types.content_type_for(part_name)
        != expected_content_type
    ):
        context.add_issue(
            "invalid_header_footer_content_type",
            "页眉页脚 part 的 Content Type 无效。",
            part_name=part_name,
        )
    try:
        root = context.package.read_xml(part_name)
    except DocxError:
        context.add_issue(
            "invalid_header_footer_xml",
            "页眉页脚 part 无法解析。",
            part_name=part_name,
        )
        return
    expected_root = _W_HEADER if part_kind == "header" else _W_FOOTER
    if root.tag != expected_root:
        context.add_issue(
            "invalid_header_footer_root",
            "页眉页脚 part 根节点无效。",
            part_name=part_name,
        )
        return
    try:
        validate_simple_header_footer_part(
            root,
            part_kind="header" if part_kind == "header" else "footer",
        )
    except DocxError:
        if context.strict:
            context.add_issue(
                "unsupported_complex_header_footer",
                "页眉页脚合法可读，但超出当前简单编辑范围。",
                part_name=part_name,
                severity="warning",
            )
    _validate_page_fields(context, root, part_name)


def _validate_page_fields(
    context: _ValidationContext,
    root: ElementTree.Element,
    part_name: str,
) -> None:
    field_stack: list[dict[str, bool]] = []
    page_field_seen = False
    invalid = False
    for element in root.iter():
        if element.tag == _W_FIELD_CHARACTER:
            field_type = element.attrib.get(_W_FIELD_CHARACTER_TYPE)
            if field_type == "begin":
                field_stack.append({"page": False, "separate": False})
            elif field_type == "separate":
                if not field_stack or field_stack[-1]["separate"]:
                    invalid = True
                else:
                    field_stack[-1]["separate"] = True
            elif field_type == "end":
                if not field_stack:
                    invalid = True
                    continue
                field_state = field_stack.pop()
                if field_state["page"] and not field_state["separate"]:
                    invalid = True
            else:
                invalid = True
        elif (
            element.tag == _W_INSTRUCTION_TEXT
            and _is_page_instruction(element.text)
        ):
            page_field_seen = True
            if not field_stack:
                invalid = True
            else:
                field_stack[-1]["page"] = True
    if field_stack:
        invalid = True
    if page_field_seen and invalid:
        context.add_issue(
            "invalid_page_field",
            "PAGE 字段结构不完整或顺序无效。",
            part_name=part_name,
        )


def _is_page_instruction(value: str | None) -> bool:
    tokens = (value or "").strip().split()
    return bool(tokens) and tokens[0].upper() == "PAGE"


def _validate_even_and_odd_setting(context: _ValidationContext) -> None:
    if not context.package.has_part(_SETTINGS_PART):
        return
    context.checked_parts.add(_SETTINGS_PART)
    try:
        root = context.package.read_xml(_SETTINGS_PART)
    except DocxError:
        return
    setting = root.find(_W_EVEN_AND_ODD_HEADERS)
    if setting is None:
        return
    if setting.attrib.get(_W_VAL, "true").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }:
        context.add_issue(
            "unsupported_even_and_odd_headers",
            "文档启用了当前模块不编辑的奇偶页不同设置。",
            part_name=_SETTINGS_PART,
            severity="warning",
        )
