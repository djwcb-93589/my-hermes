"""P4.3 富内容与 section 操作的校验、统一规划和输出复检。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias
from xml.etree import ElementTree

from .errors import DocxError
from .images import (
    JPEG_START,
    PNG_SIGNATURE,
    create_inline_image_paragraph,
    next_doc_properties_id,
    validate_local_image,
)
from .locator import (
    is_paragraph_block_id,
    is_table_block_id,
    iter_body_children,
)
from .models import (
    DocumentSnapshot,
    InsertBulletListAfter,
    InsertHyperlinkAfter,
    InsertImageAfter,
    InsertNumberedListAfter,
    UpdateFooterText,
    UpdateHeaderText,
    UpdatePageSetup,
)
from .numbering import NumberingListDefinition, NumberingManager
from .package import DocxPackage
from .parts import (
    ContentTypesManager,
    FOOTER_CONTENT_TYPE,
    HEADER_CONTENT_TYPE,
    JPEG_CONTENT_TYPE,
    PNG_CONTENT_TYPE,
    allocate_image_part_name,
    allocate_indexed_part_name,
)
from .relationships import (
    FOOTER_RELATIONSHIP_TYPE,
    HEADER_RELATIONSHIP_TYPE,
    HYPERLINK_RELATIONSHIP_TYPE,
    IMAGE_RELATIONSHIP_TYPE,
    RELATIONSHIP_ID,
    RelationshipManager,
    create_hyperlink_paragraph,
    create_relationships_root,
    external_relationships,
    relationship_part_name,
    validate_relationship_package,
    validate_hyperlink_url,
)
from .sections import (
    HeaderFooterExpectation,
    PageSetupExpectation,
    SectionLocation,
    apply_page_setup,
    create_footer_xml,
    create_header_xml,
    get_default_header_footer_reference_id,
    locate_sections,
    require_section,
    set_header_footer_reference,
    validate_simple_header_footer_part,
    verify_header_footer_part,
    verify_page_setup,
)
from .writer import parse_xml_preserving_misc, serialize_xml


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_TEXT = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BREAK = f"{{{_W_NS}}}br"
_W_CARRIAGE_RETURN = f"{{{_W_NS}}}cr"
_W_HYPERLINK = f"{{{_W_NS}}}hyperlink"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_NUMBER_PROPERTIES = f"{{{_W_NS}}}numPr"
_W_NUMBER_ID = f"{{{_W_NS}}}numId"
_W_EVEN_AND_ODD_HEADERS = f"{{{_W_NS}}}evenAndOddHeaders"
_W_VAL = f"{{{_W_NS}}}val"
_WP_EXTENT = f"{{{_WP_NS}}}extent"
_WP_DOC_PROPERTIES = f"{{{_WP_NS}}}docPr"
_A_BLIP = f"{{{_A_NS}}}blip"
_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_R_EMBED = f"{{{_OFFICE_REL_NS}}}embed"

_DOCUMENT_PART = "word/document.xml"
_DOCUMENT_RELATIONSHIPS_PART = relationship_part_name(_DOCUMENT_PART)
_CONTENT_TYPES_PART = "[Content_Types].xml"
_SETTINGS_PART = "word/settings.xml"
_MAX_LIST_ITEMS = 1000
_MAX_LIST_ITEM_LENGTH = 10_000
_MAX_LIST_TOTAL_TEXT = 100_000
_MAX_RICH_TEXT_LENGTH = 32_768
_PAGE_SIZES = frozenset({"A4", "LETTER"})
_ORIENTATIONS = frozenset({"portrait", "landscape"})
_MAX_MARGIN_TWIPS = 31_680


@dataclass(frozen=True)
class ValidatedImageInsertion:
    operation_index: int
    operation_type: str
    block_id: str
    image_path: Path
    width_px: int | None
    height_px: int | None
    alt_text: str | None


@dataclass(frozen=True)
class ValidatedHyperlinkInsertion:
    operation_index: int
    operation_type: str
    block_id: str
    text: str
    url: str


@dataclass(frozen=True)
class ValidatedListInsertion:
    operation_index: int
    operation_type: str
    block_id: str
    list_kind: Literal["bullet", "decimal"]
    items: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedPageSetup:
    operation_index: int
    operation_type: str
    section_index: int
    page_size: str | None
    orientation: str | None
    margin_top_twips: int | None
    margin_bottom_twips: int | None
    margin_left_twips: int | None
    margin_right_twips: int | None


@dataclass(frozen=True)
class ValidatedHeaderUpdate:
    operation_index: int
    operation_type: str
    section_index: int
    text: str | None


@dataclass(frozen=True)
class ValidatedFooterUpdate:
    operation_index: int
    operation_type: str
    section_index: int
    text: str | None
    include_page_number: bool


ValidatedRichOperation: TypeAlias = (
    ValidatedImageInsertion
    | ValidatedHyperlinkInsertion
    | ValidatedListInsertion
    | ValidatedPageSetup
    | ValidatedHeaderUpdate
    | ValidatedFooterUpdate
)
RICH_OPERATION_TYPES = (
    ValidatedImageInsertion,
    ValidatedHyperlinkInsertion,
    ValidatedListInsertion,
    ValidatedPageSetup,
    ValidatedHeaderUpdate,
    ValidatedFooterUpdate,
)


@dataclass(frozen=True)
class PlannedRichBodyInsertion:
    operation_index: int
    operation_type: str
    block_id: str
    anchor: ElementTree.Element
    elements: tuple[ElementTree.Element, ...]
    complex_element_ids: frozenset[int]


@dataclass(frozen=True)
class ImageExpectation:
    part_name: str
    relationship_id: str
    width_emu: int
    height_emu: int
    doc_properties_id: int
    image_format: str
    alt_text: str | None


@dataclass(frozen=True)
class HyperlinkExpectation:
    relationship_id: str
    url: str
    text: str


@dataclass(frozen=True)
class ListExpectation:
    list_kind: Literal["bullet", "decimal"]
    number_id: int
    abstract_number_id: int
    items: tuple[str, ...]


@dataclass(frozen=True)
class RichContentPlan:
    body_insertions: tuple[PlannedRichBodyInsertion, ...]
    replacements: dict[str, bytes]
    additions: dict[str, bytes]
    image_expectations: tuple[ImageExpectation, ...]
    hyperlink_expectations: tuple[HyperlinkExpectation, ...]
    list_expectations: tuple[ListExpectation, ...]
    page_setup_expectations: tuple[PageSetupExpectation, ...]
    header_footer_expectations: tuple[HeaderFooterExpectation, ...]
    expected_external_relationships: frozenset[tuple[str, str, str, str]]
    document_changed: bool

    @property
    def added_image_count(self) -> int:
        return len(self.image_expectations)


EMPTY_RICH_CONTENT_PLAN = RichContentPlan(
    body_insertions=(),
    replacements={},
    additions={},
    image_expectations=(),
    hyperlink_expectations=(),
    list_expectations=(),
    page_setup_expectations=(),
    header_footer_expectations=(),
    expected_external_relationships=frozenset(),
    document_changed=False,
)


def validate_rich_operation(
    operation: object,
    operation_index: int,
) -> ValidatedRichOperation | None:
    """识别并验证一个 P4.3 公共操作；非 P4.3 类型返回 None。"""

    if isinstance(operation, InsertImageAfter):
        if not isinstance(operation.image_path, (str, os.PathLike)):
            _invalid("insert_image_after.image_path 必须是本地路径。")
        width = _validate_optional_positive_int(operation.width_px, "width_px")
        height = _validate_optional_positive_int(operation.height_px, "height_px")
        alt_text = _validate_optional_xml_text(
            operation.alt_text,
            "alt_text",
            maximum_length=1024,
        )
        return ValidatedImageInsertion(
            operation_index=operation_index,
            operation_type="insert_image_after",
            block_id=_validate_block_id(operation.block_id),
            image_path=Path(os.fspath(operation.image_path)),
            width_px=width,
            height_px=height,
            alt_text=alt_text,
        )
    if isinstance(operation, InsertHyperlinkAfter):
        return ValidatedHyperlinkInsertion(
            operation_index=operation_index,
            operation_type="insert_hyperlink_after",
            block_id=_validate_block_id(operation.block_id),
            text=_validate_required_xml_text(
                operation.text,
                "insert_hyperlink_after.text",
                maximum_length=_MAX_RICH_TEXT_LENGTH,
            ),
            url=validate_hyperlink_url(operation.url),
        )
    if isinstance(operation, (InsertBulletListAfter, InsertNumberedListAfter)):
        operation_type = (
            "insert_bullet_list_after"
            if isinstance(operation, InsertBulletListAfter)
            else "insert_numbered_list_after"
        )
        return ValidatedListInsertion(
            operation_index=operation_index,
            operation_type=operation_type,
            block_id=_validate_block_id(operation.block_id),
            list_kind=(
                "bullet"
                if isinstance(operation, InsertBulletListAfter)
                else "decimal"
            ),
            items=_validate_list_items(operation.items, operation_type),
        )
    if isinstance(operation, UpdatePageSetup):
        section_index = _validate_section_index(operation.section_index)
        if operation.page_size is not None and operation.page_size not in _PAGE_SIZES:
            _invalid("page_size 只允许 A4、LETTER 或 null。")
        if (
            operation.orientation is not None
            and operation.orientation not in _ORIENTATIONS
        ):
            _invalid("orientation 只允许 portrait、landscape 或 null。")
        margins = tuple(
            _validate_optional_margin(getattr(operation, field_name), field_name)
            for field_name in (
                "margin_top_twips",
                "margin_bottom_twips",
                "margin_left_twips",
                "margin_right_twips",
            )
        )
        if operation.page_size is None and operation.orientation is None and all(
            value is None for value in margins
        ):
            _invalid("update_page_setup 至少需要提供一个页面设置字段。")
        return ValidatedPageSetup(
            operation_index=operation_index,
            operation_type="update_page_setup",
            section_index=section_index,
            page_size=operation.page_size,
            orientation=operation.orientation,
            margin_top_twips=margins[0],
            margin_bottom_twips=margins[1],
            margin_left_twips=margins[2],
            margin_right_twips=margins[3],
        )
    if isinstance(operation, UpdateHeaderText):
        return ValidatedHeaderUpdate(
            operation_index=operation_index,
            operation_type="update_header_text",
            section_index=_validate_section_index(operation.section_index),
            text=_validate_optional_xml_text(
                operation.text,
                "update_header_text.text",
                maximum_length=_MAX_RICH_TEXT_LENGTH,
            ),
        )
    if isinstance(operation, UpdateFooterText):
        if not isinstance(operation.include_page_number, bool):
            _invalid("include_page_number 必须是布尔值。")
        return ValidatedFooterUpdate(
            operation_index=operation_index,
            operation_type="update_footer_text",
            section_index=_validate_section_index(operation.section_index),
            text=_validate_optional_xml_text(
                operation.text,
                "update_footer_text.text",
                maximum_length=_MAX_RICH_TEXT_LENGTH,
            ),
            include_page_number=operation.include_page_number,
        )
    return None


def validate_rich_operation_conflicts(
    operations: list[ValidatedRichOperation],
) -> None:
    """限制同一 section 的同类型操作最多出现一次。"""

    seen: set[tuple[str, int]] = set()
    for operation in operations:
        if isinstance(operation, ValidatedPageSetup):
            key = ("page_setup", operation.section_index)
        elif isinstance(operation, ValidatedHeaderUpdate):
            key = ("header", operation.section_index)
        elif isinstance(operation, ValidatedFooterUpdate):
            key = ("footer", operation.section_index)
        else:
            continue
        if key in seen:
            raise DocxError(
                "duplicate_edit_target",
                "同一个 section 在一次请求中只能执行一次同类型更新。",
            )
        seen.add(key)


def rich_operation_block_id(
    operation: ValidatedRichOperation,
) -> str | None:
    if isinstance(
        operation,
        (
            ValidatedImageInsertion,
            ValidatedHyperlinkInsertion,
            ValidatedListInsertion,
        ),
    ):
        return operation.block_id
    return None


def prepare_rich_content_plan(
    *,
    package: DocxPackage,
    operations: list[ValidatedRichOperation],
    document_root: ElementTree.Element,
    body: ElementTree.Element,
    snapshot: DocumentSnapshot,
    content_types_payload: bytes,
) -> RichContentPlan:
    """预读全部资源后统一分配 part、relationship、numbering 和 section。"""

    if not operations:
        return EMPTY_RICH_CONTENT_PLAN
    body_locations = {
        location.block_id: location
        for location in iter_body_children(body)
        if location.block_id is not None
    }
    snapshot_blocks = {block.block_id: block for block in snapshot.blocks}
    anchors: dict[int, ElementTree.Element] = {}
    for operation in operations:
        block_id = rich_operation_block_id(operation)
        if block_id is None:
            continue
        location = body_locations.get(block_id)
        if (
            location is None
            or block_id not in snapshot_blocks
            or location.kind not in {"paragraph", "table"}
        ):
            if location is None and block_id not in snapshot_blocks:
                raise DocxError("block_not_found", "富内容插入锚点不存在。")
            raise DocxError(
                "edit_verification_failed",
                "Reader 与富内容插入锚点定位不一致。",
            )
        anchors[operation.operation_index] = location.element

    validated_images = {
        operation.operation_index: validate_local_image(
            operation.image_path,
            width_px=operation.width_px,
            height_px=operation.height_px,
            alt_text=operation.alt_text,
        )
        for operation in operations
        if isinstance(operation, ValidatedImageInsertion)
    }
    section_locations = locate_sections(body)
    has_section_operations = any(
        isinstance(
            operation,
            (ValidatedPageSetup, ValidatedHeaderUpdate, ValidatedFooterUpdate),
        )
        for operation in operations
    )
    if has_section_operations and len(section_locations) != snapshot.section_count:
        raise DocxError(
            "edit_verification_failed",
            "Reader 与 section locator 的数量不一致。",
        )
    if any(
        isinstance(operation, (ValidatedHeaderUpdate, ValidatedFooterUpdate))
        for operation in operations
    ):
        _reject_even_and_odd_headers(package)

    content_types_root = parse_xml_preserving_misc(
        content_types_payload,
        _CONTENT_TYPES_PART,
    )
    content_types = ContentTypesManager(content_types_root)
    relationships_existed = package.has_part(_DOCUMENT_RELATIONSHIPS_PART)
    if relationships_existed:
        relationships_original = package.read_xml_bytes(
            _DOCUMENT_RELATIONSHIPS_PART
        )
        relationships_root = parse_xml_preserving_misc(
            relationships_original,
            _DOCUMENT_RELATIONSHIPS_PART,
        )
    else:
        relationships_original = None
        relationships_root = create_relationships_root()
    relationships = RelationshipManager(
        relationships_root,
        source_part=_DOCUMENT_PART,
    )
    reserved_parts: set[str] = set()
    additions: dict[str, bytes] = {}
    replacements: dict[str, bytes] = {}
    body_insertions: list[PlannedRichBodyInsertion] = []
    image_expectations: list[ImageExpectation] = []
    hyperlink_expectations: list[HyperlinkExpectation] = []
    list_expectations: list[ListExpectation] = []
    page_expectations: list[PageSetupExpectation] = []
    header_footer_expectations: list[HeaderFooterExpectation] = []
    numbering: NumberingManager | None = None
    next_doc_id = next_doc_properties_id(document_root)
    reserved_doc_ids: set[int] = set()

    for operation in operations:
        if isinstance(operation, ValidatedImageInsertion):
            image = validated_images[operation.operation_index]
            part_name = allocate_image_part_name(
                set(package.part_names),
                reserved_parts,
                extension=image.extension,
            )
            content_types.ensure_default(
                image.extension,
                PNG_CONTENT_TYPE
                if image.image_format == "png"
                else JPEG_CONTENT_TYPE,
            )
            relationship_id = relationships.add_internal(
                IMAGE_RELATIONSHIP_TYPE,
                part_name.removeprefix("word/"),
            )
            paragraph = create_inline_image_paragraph(
                relationship_id=relationship_id,
                part_name=part_name,
                width_emu=image.width_emu,
                height_emu=image.height_emu,
                doc_properties_id=next_doc_id,
                alt_text=image.alt_text,
            )
            reserved_doc_ids.add(next_doc_id)
            additions[part_name] = image.payload
            body_insertions.append(
                PlannedRichBodyInsertion(
                    operation_index=operation.operation_index,
                    operation_type=operation.operation_type,
                    block_id=operation.block_id,
                    anchor=anchors[operation.operation_index],
                    elements=(paragraph,),
                    complex_element_ids=frozenset({id(paragraph)}),
                )
            )
            image_expectations.append(
                ImageExpectation(
                    part_name=part_name,
                    relationship_id=relationship_id,
                    width_emu=image.width_emu,
                    height_emu=image.height_emu,
                    doc_properties_id=next_doc_id,
                    image_format=image.image_format,
                    alt_text=image.alt_text,
                )
            )
            next_doc_id = next_doc_properties_id(
                document_root,
                reserved_doc_ids,
            )
        elif isinstance(operation, ValidatedHyperlinkInsertion):
            relationship_id = relationships.add_external(
                HYPERLINK_RELATIONSHIP_TYPE,
                operation.url,
            )
            paragraph = create_hyperlink_paragraph(
                operation.text,
                relationship_id,
            )
            body_insertions.append(
                PlannedRichBodyInsertion(
                    operation_index=operation.operation_index,
                    operation_type=operation.operation_type,
                    block_id=operation.block_id,
                    anchor=anchors[operation.operation_index],
                    elements=(paragraph,),
                    complex_element_ids=frozenset({id(paragraph)}),
                )
            )
            hyperlink_expectations.append(
                HyperlinkExpectation(
                    relationship_id=relationship_id,
                    url=operation.url,
                    text=operation.text,
                )
            )
        elif isinstance(operation, ValidatedListInsertion):
            if numbering is None:
                numbering = NumberingManager(
                    package,
                    relationships,
                    content_types,
                )
            definition = numbering.add_list(
                operation.list_kind,
                operation.items,
            )
            body_insertions.append(
                PlannedRichBodyInsertion(
                    operation_index=operation.operation_index,
                    operation_type=operation.operation_type,
                    block_id=operation.block_id,
                    anchor=anchors[operation.operation_index],
                    elements=definition.paragraphs,
                    complex_element_ids=frozenset(),
                )
            )
            list_expectations.append(_list_expectation(definition))
        elif isinstance(operation, ValidatedPageSetup):
            location = require_section(
                section_locations,
                operation.section_index,
            )
            page_expectations.append(
                apply_page_setup(
                    location,
                    page_size=operation.page_size,
                    orientation=operation.orientation,
                    margin_top_twips=operation.margin_top_twips,
                    margin_bottom_twips=operation.margin_bottom_twips,
                    margin_left_twips=operation.margin_left_twips,
                    margin_right_twips=operation.margin_right_twips,
                )
            )
        elif isinstance(operation, ValidatedHeaderUpdate):
            expectation = _plan_header_update(
                package=package,
                reserved_parts=reserved_parts,
                additions=additions,
                replacements=replacements,
                content_types=content_types,
                relationships=relationships,
                section_locations=section_locations,
                location=require_section(
                    section_locations,
                    operation.section_index,
                ),
                operation=operation,
            )
            header_footer_expectations.append(expectation)
        else:
            assert isinstance(operation, ValidatedFooterUpdate)
            expectation = _plan_footer_update(
                package=package,
                reserved_parts=reserved_parts,
                additions=additions,
                replacements=replacements,
                content_types=content_types,
                relationships=relationships,
                section_locations=section_locations,
                location=require_section(
                    section_locations,
                    operation.section_index,
                ),
                operation=operation,
            )
            header_footer_expectations.append(expectation)

    if numbering is not None and numbering.changed:
        numbering_payload = serialize_xml(
            numbering.root,
            original_payload=numbering.original,
        )
        target = replacements if numbering.existed else additions
        target[numbering.part_name] = numbering_payload
    if content_types.changed:
        replacements[_CONTENT_TYPES_PART] = serialize_xml(
            content_types.root,
            original_payload=content_types_payload,
        )
    if relationships.changed:
        relationship_payload = serialize_xml(
            relationships.root,
            original_payload=relationships_original,
        )
        target = replacements if relationships_existed else additions
        target[_DOCUMENT_RELATIONSHIPS_PART] = relationship_payload

    return RichContentPlan(
        body_insertions=tuple(body_insertions),
        replacements=replacements,
        additions=additions,
        image_expectations=tuple(image_expectations),
        hyperlink_expectations=tuple(hyperlink_expectations),
        list_expectations=tuple(list_expectations),
        page_setup_expectations=tuple(page_expectations),
        header_footer_expectations=tuple(header_footer_expectations),
        expected_external_relationships=frozenset(
            {
                *external_relationships(package),
                *(
                    (
                        _DOCUMENT_RELATIONSHIPS_PART,
                        expectation.relationship_id,
                        HYPERLINK_RELATIONSHIP_TYPE,
                        expectation.url,
                    )
                    for expectation in hyperlink_expectations
                ),
            }
        ),
        document_changed=bool(
            body_insertions
            or page_expectations
            or header_footer_expectations
        ),
    )


def verify_rich_content_output(
    *,
    package: DocxPackage,
    plan: RichContentPlan,
    initial_snapshot: DocumentSnapshot,
    output_snapshot: DocumentSnapshot,
) -> None:
    """复核所有新增富内容、关系、编号、section 和页眉页脚。"""

    if output_snapshot.image_count != (
        initial_snapshot.image_count + plan.added_image_count
    ):
        raise DocxError(
            "edit_verification_failed",
            "修改后的图片数量与富内容计划不一致。",
        )
    document_root = package.read_xml(_DOCUMENT_PART)
    body = document_root.find(f"{{{_W_NS}}}body")
    if body is None:
        raise DocxError(
            "edit_verification_failed",
            "富内容复检时 document.xml 缺少 w:body。",
        )
    validate_relationship_package(package)
    if (
        external_relationships(package)
        != plan.expected_external_relationships
    ):
        raise DocxError(
            "edit_verification_failed",
            "输出 DOCX 引入了非预期 external relationship。",
        )
    if package.has_part(_DOCUMENT_RELATIONSHIPS_PART):
        relationships_root = package.read_xml(_DOCUMENT_RELATIONSHIPS_PART)
        relationships = RelationshipManager(
            relationships_root,
            source_part=_DOCUMENT_PART,
        )
    else:
        relationships = RelationshipManager(
            create_relationships_root(),
            source_part=_DOCUMENT_PART,
        )
    content_types = ContentTypesManager(package.read_xml(_CONTENT_TYPES_PART))
    _verify_images(
        package,
        document_root,
        relationships,
        content_types,
        plan.image_expectations,
    )
    _verify_hyperlinks(
        document_root,
        relationships,
        plan.hyperlink_expectations,
    )
    _verify_lists(
        package,
        body,
        relationships,
        content_types,
        plan.list_expectations,
    )
    section_locations = locate_sections(body)
    if len(section_locations) != output_snapshot.section_count:
        raise DocxError(
            "edit_verification_failed",
            "输出快照与 section locator 的数量不一致。",
        )
    for expectation in plan.page_setup_expectations:
        verify_page_setup(section_locations, expectation)
    for expectation in plan.header_footer_expectations:
        _verify_header_footer(
            package,
            relationships,
            content_types,
            section_locations,
            expectation,
        )


def _plan_header_update(
    *,
    package: DocxPackage,
    reserved_parts: set[str],
    additions: dict[str, bytes],
    replacements: dict[str, bytes],
    content_types: ContentTypesManager,
    relationships: RelationshipManager,
    section_locations: tuple[SectionLocation, ...],
    location: SectionLocation,
    operation: ValidatedHeaderUpdate,
) -> HeaderFooterExpectation:
    if operation.text is None:
        set_header_footer_reference(
            location,
            part_kind="header",
            relationship_id=None,
        )
        return HeaderFooterExpectation(
            section_index=operation.section_index,
            part_kind="header",
            cleared=True,
            relationship_id=None,
            part_name=None,
            text=None,
            include_page_number=False,
        )
    (
        part_name,
        relationship_id,
        original_payload,
        existed,
    ) = _prepare_header_footer_target(
        package=package,
        reserved_parts=reserved_parts,
        content_types=content_types,
        relationships=relationships,
        section_locations=section_locations,
        location=location,
        part_kind="header",
    )
    set_header_footer_reference(
        location,
        part_kind="header",
        relationship_id=relationship_id,
    )
    payload = serialize_xml(
        create_header_xml(operation.text),
        original_payload=original_payload,
    )
    (replacements if existed else additions)[part_name] = payload
    return HeaderFooterExpectation(
        section_index=operation.section_index,
        part_kind="header",
        cleared=False,
        relationship_id=relationship_id,
        part_name=part_name,
        text=operation.text,
        include_page_number=False,
    )


def _plan_footer_update(
    *,
    package: DocxPackage,
    reserved_parts: set[str],
    additions: dict[str, bytes],
    replacements: dict[str, bytes],
    content_types: ContentTypesManager,
    relationships: RelationshipManager,
    section_locations: tuple[SectionLocation, ...],
    location: SectionLocation,
    operation: ValidatedFooterUpdate,
) -> HeaderFooterExpectation:
    if operation.text is None and not operation.include_page_number:
        set_header_footer_reference(
            location,
            part_kind="footer",
            relationship_id=None,
        )
        return HeaderFooterExpectation(
            section_index=operation.section_index,
            part_kind="footer",
            cleared=True,
            relationship_id=None,
            part_name=None,
            text=None,
            include_page_number=False,
        )
    (
        part_name,
        relationship_id,
        original_payload,
        existed,
    ) = _prepare_header_footer_target(
        package=package,
        reserved_parts=reserved_parts,
        content_types=content_types,
        relationships=relationships,
        section_locations=section_locations,
        location=location,
        part_kind="footer",
    )
    set_header_footer_reference(
        location,
        part_kind="footer",
        relationship_id=relationship_id,
    )
    payload = serialize_xml(
        create_footer_xml(
            operation.text,
            include_page_number=operation.include_page_number,
        ),
        original_payload=original_payload,
    )
    (replacements if existed else additions)[part_name] = payload
    return HeaderFooterExpectation(
        section_index=operation.section_index,
        part_kind="footer",
        cleared=False,
        relationship_id=relationship_id,
        part_name=part_name,
        text=operation.text,
        include_page_number=operation.include_page_number,
    )


def _prepare_header_footer_target(
    *,
    package: DocxPackage,
    reserved_parts: set[str],
    content_types: ContentTypesManager,
    relationships: RelationshipManager,
    section_locations: tuple[SectionLocation, ...],
    location: SectionLocation,
    part_kind: Literal["header", "footer"],
) -> tuple[str, str, bytes | None, bool]:
    """复用未共享简单 part；共享时分配独立副本。"""

    old_relationship_id = get_default_header_footer_reference_id(
        location,
        part_kind=part_kind,
    )
    relationship_type = (
        HEADER_RELATIONSHIP_TYPE
        if part_kind == "header"
        else FOOTER_RELATIONSHIP_TYPE
    )
    content_type = (
        HEADER_CONTENT_TYPE
        if part_kind == "header"
        else FOOTER_CONTENT_TYPE
    )
    if old_relationship_id is not None:
        old_relationship = relationships.get(old_relationship_id)
        if (
            old_relationship is None
            or old_relationship.attrib.get("Type") != relationship_type
            or old_relationship.attrib.get("TargetMode", "").lower()
            == "external"
        ):
            raise DocxError(
                "relationship_conflict",
                "现有页眉页脚 reference 指向无效 relationship。",
            )
        old_part_name = relationships.resolve_internal(old_relationship)
        if not package.has_part(old_part_name):
            raise DocxError(
                "relationship_conflict",
                "现有页眉页脚 relationship 指向不存在的 part。",
            )
        old_root = package.read_xml(old_part_name)
        validate_simple_header_footer_part(
            old_root,
            part_kind=part_kind,
        )
        reference_count = _count_part_references(
            section_locations,
            relationships=relationships,
            part_kind=part_kind,
            part_name=old_part_name,
        )
        if reference_count == 1:
            content_types.ensure_override(old_part_name, content_type)
            return (
                old_part_name,
                old_relationship_id,
                package.read_xml_bytes(old_part_name),
                True,
            )

    part_name = allocate_indexed_part_name(
        set(package.part_names),
        reserved_parts,
        directory="word",
        stem=part_kind,
        extension="xml",
    )
    relationship_id = relationships.add_internal(
        relationship_type,
        part_name.removeprefix("word/"),
    )
    content_types.ensure_override(part_name, content_type)
    return part_name, relationship_id, None, False


def _count_part_references(
    section_locations: tuple[SectionLocation, ...],
    *,
    relationships: RelationshipManager,
    part_kind: Literal["header", "footer"],
    part_name: str,
) -> int:
    reference_tag = (
        f"{{{_W_NS}}}headerReference"
        if part_kind == "header"
        else f"{{{_W_NS}}}footerReference"
    )
    count = 0
    for location in section_locations:
        for reference in location.section_properties:
            if (
                reference.tag != reference_tag
                or reference.attrib.get(f"{{{_W_NS}}}type", "default")
                != "default"
            ):
                continue
            relationship_id = reference.attrib.get(RELATIONSHIP_ID)
            relationship = (
                relationships.get(relationship_id)
                if relationship_id is not None
                else None
            )
            if (
                relationship is not None
                and relationship.attrib.get("Type")
                in {HEADER_RELATIONSHIP_TYPE, FOOTER_RELATIONSHIP_TYPE}
                and relationship.attrib.get("TargetMode", "").lower()
                != "external"
                and relationships.resolve_internal(relationship) == part_name
            ):
                count += 1
    return count


def _verify_images(
    package: DocxPackage,
    document_root: ElementTree.Element,
    relationships: RelationshipManager,
    content_types: ContentTypesManager,
    expectations: tuple[ImageExpectation, ...],
) -> None:
    doc_property_ids = [
        element.attrib.get("id")
        for element in document_root.iter(_WP_DOC_PROPERTIES)
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
        raise DocxError(
            "edit_verification_failed",
            "document.xml 包含重复的 wp:docPr/@id。",
        )
    for expectation in expectations:
        payload = package.read_part_bytes(expectation.part_name)
        expected_content_type = (
            PNG_CONTENT_TYPE
            if expectation.image_format == "png"
            else JPEG_CONTENT_TYPE
        )
        if (
            content_types.content_type_for(expectation.part_name)
            != expected_content_type
            or expectation.image_format == "png"
            and not payload.startswith(PNG_SIGNATURE)
            or expectation.image_format == "jpeg"
            and not payload.startswith(JPEG_START)
        ):
            raise DocxError(
                "edit_verification_failed",
                "图片媒体 part 的签名或 Content Type 不一致。",
            )
        relationship = relationships.get(expectation.relationship_id)
        if (
            relationship is None
            or relationship.attrib.get("Type") != IMAGE_RELATIONSHIP_TYPE
            or relationships.resolve_internal(relationship)
            != expectation.part_name
        ):
            raise DocxError(
                "edit_verification_failed",
                "图片 relationship 未指向预期媒体 part。",
            )
        matching_blips = [
            blip
            for blip in document_root.iter(_A_BLIP)
            if blip.attrib.get(_R_EMBED) == expectation.relationship_id
        ]
        matching_properties = [
            element
            for element in document_root.iter(_WP_DOC_PROPERTIES)
            if element.attrib.get("id") == str(expectation.doc_properties_id)
        ]
        if len(matching_blips) != 1 or len(matching_properties) != 1:
            raise DocxError(
                "edit_verification_failed",
                "图片 DrawingML 的 embed 或 docPr 定位不唯一。",
            )
        if matching_properties[0].attrib.get("descr") != expectation.alt_text:
            raise DocxError(
                "edit_verification_failed",
                "图片 alt_text 与请求不一致。",
            )
        inline = _find_ancestor_extent(
            document_root,
            expectation.relationship_id,
        )
        if inline != (
            str(expectation.width_emu),
            str(expectation.height_emu),
        ):
            raise DocxError(
                "edit_verification_failed",
                "图片 wp:extent 与计算尺寸不一致。",
            )


def _verify_hyperlinks(
    document_root: ElementTree.Element,
    relationships: RelationshipManager,
    expectations: tuple[HyperlinkExpectation, ...],
) -> None:
    for expectation in expectations:
        relationship = relationships.get(expectation.relationship_id)
        if (
            relationship is None
            or relationship.attrib.get("Type") != HYPERLINK_RELATIONSHIP_TYPE
            or relationship.attrib.get("Target") != expectation.url
            or relationship.attrib.get("TargetMode") != "External"
        ):
            raise DocxError(
                "edit_verification_failed",
                "超链接 relationship 与请求不一致。",
            )
        matching_texts = [
            _read_visible_text(element)
            for element in document_root.iter(_W_HYPERLINK)
            if element.attrib.get(RELATIONSHIP_ID) == expectation.relationship_id
        ]
        if expectation.text not in matching_texts:
            raise DocxError(
                "edit_verification_failed",
                "超链接段落未引用预期 relationship 或文字不一致。",
            )


def _verify_lists(
    package: DocxPackage,
    body: ElementTree.Element,
    relationships: RelationshipManager,
    content_types: ContentTypesManager,
    expectations: tuple[ListExpectation, ...],
) -> None:
    if not expectations:
        return
    numbering_relationships = relationships.relationships_of_type(
        "http://schemas.openxmlformats.org/officeDocument/2006/"
        "relationships/numbering"
    )
    if len(numbering_relationships) != 1:
        raise DocxError(
            "edit_verification_failed",
            "输出 DOCX 缺少唯一 numbering relationship。",
        )
    numbering_part = relationships.resolve_internal(numbering_relationships[0])
    if content_types.content_type_for(numbering_part) != (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.numbering+xml"
    ):
        raise DocxError(
            "edit_verification_failed",
            "numbering part 缺少标准 Content Type。",
        )
    numbering_root = package.read_xml(numbering_part)
    abstract_ids = [
        element.attrib.get(f"{{{_W_NS}}}abstractNumId")
        for element in numbering_root
        if element.tag == f"{{{_W_NS}}}abstractNum"
    ]
    number_ids = [
        element.attrib.get(f"{{{_W_NS}}}numId")
        for element in numbering_root
        if element.tag == f"{{{_W_NS}}}num"
    ]
    if (
        None in abstract_ids
        or None in number_ids
        or len(abstract_ids) != len(set(abstract_ids))
        or len(number_ids) != len(set(number_ids))
    ):
        raise DocxError(
            "edit_verification_failed",
            "numbering.xml 包含重复或缺失的编号 ID。",
        )
    for expectation in expectations:
        number = next(
            (
                element
                for element in numbering_root
                if element.tag == f"{{{_W_NS}}}num"
                and element.attrib.get(f"{{{_W_NS}}}numId")
                == str(expectation.number_id)
            ),
            None,
        )
        abstract = next(
            (
                element
                for element in numbering_root
                if element.tag == f"{{{_W_NS}}}abstractNum"
                and element.attrib.get(f"{{{_W_NS}}}abstractNumId")
                == str(expectation.abstract_number_id)
            ),
            None,
        )
        if number is None or abstract is None:
            raise DocxError(
                "edit_verification_failed",
                "新增列表的 numId 或 abstractNumId 不存在。",
            )
        abstract_reference = number.find(f"{{{_W_NS}}}abstractNumId")
        number_format = abstract.find(f".//{{{_W_NS}}}numFmt")
        if (
            abstract_reference is None
            or abstract_reference.attrib.get(_W_VAL)
            != str(expectation.abstract_number_id)
            or number_format is None
            or number_format.attrib.get(_W_VAL) != expectation.list_kind
        ):
            raise DocxError(
                "edit_verification_failed",
                "新增列表的 numbering 类型或引用不一致。",
            )
        actual_items = [
            _read_visible_text(paragraph)
            for paragraph in body
            if paragraph.tag == _W_PARAGRAPH
            and _paragraph_number_id(paragraph) == expectation.number_id
        ]
        if actual_items != list(expectation.items):
            raise DocxError(
                "edit_verification_failed",
                "新增列表的段落文字或 numId 不一致。",
            )


def _verify_header_footer(
    package: DocxPackage,
    relationships: RelationshipManager,
    content_types: ContentTypesManager,
    section_locations: tuple[SectionLocation, ...],
    expectation: HeaderFooterExpectation,
) -> None:
    location = require_section(section_locations, expectation.section_index)
    reference_tag = (
        f"{{{_W_NS}}}headerReference"
        if expectation.part_kind == "header"
        else f"{{{_W_NS}}}footerReference"
    )
    references = [
        child
        for child in location.section_properties
        if child.tag == reference_tag
        and child.attrib.get(f"{{{_W_NS}}}type", "default") == "default"
    ]
    if expectation.cleared:
        if references:
            raise DocxError(
                "edit_verification_failed",
                "页眉页脚 default reference 未按请求清除。",
            )
        return
    if (
        len(references) != 1
        or references[0].attrib.get(RELATIONSHIP_ID)
        != expectation.relationship_id
    ):
        raise DocxError(
            "edit_verification_failed",
            "section 的页眉页脚 reference 与计划不一致。",
        )
    assert expectation.relationship_id is not None
    assert expectation.part_name is not None
    relationship = relationships.get(expectation.relationship_id)
    expected_type = (
        HEADER_RELATIONSHIP_TYPE
        if expectation.part_kind == "header"
        else FOOTER_RELATIONSHIP_TYPE
    )
    if (
        relationship is None
        or relationship.attrib.get("Type") != expected_type
        or relationships.resolve_internal(relationship) != expectation.part_name
    ):
        raise DocxError(
            "edit_verification_failed",
            "页眉页脚 relationship 与计划不一致。",
        )
    expected_content_type = (
        HEADER_CONTENT_TYPE
        if expectation.part_kind == "header"
        else FOOTER_CONTENT_TYPE
    )
    if (
        content_types.content_type_for(expectation.part_name)
        != expected_content_type
    ):
        raise DocxError(
            "edit_verification_failed",
            "页眉页脚 part 缺少标准 Content Type。",
        )
    verify_header_footer_part(
        package.read_xml(expectation.part_name),
        expectation,
    )


def _find_ancestor_extent(
    document_root: ElementTree.Element,
    relationship_id: str,
) -> tuple[str | None, str | None] | None:
    for inline in document_root.iter(f"{{{_WP_NS}}}inline"):
        if any(
            blip.attrib.get(_R_EMBED) == relationship_id
            for blip in inline.iter(_A_BLIP)
        ):
            extent = inline.find(_WP_EXTENT)
            if extent is None:
                return None
            return extent.attrib.get("cx"), extent.attrib.get("cy")
    return None


def _paragraph_number_id(paragraph: ElementTree.Element) -> int | None:
    properties = paragraph.find(_W_PARAGRAPH_PROPERTIES)
    if properties is None:
        return None
    number_properties = properties.find(_W_NUMBER_PROPERTIES)
    if number_properties is None:
        return None
    number_id = number_properties.find(_W_NUMBER_ID)
    if number_id is None:
        return None
    try:
        return int(number_id.attrib.get(_W_VAL, ""))
    except ValueError:
        return None


def _read_visible_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in element.iter():
        if child.tag == _W_TEXT:
            parts.append(child.text or "")
        elif child.tag == _W_TAB:
            parts.append("\t")
        elif child.tag in {_W_BREAK, _W_CARRIAGE_RETURN}:
            parts.append("\n")
    return "".join(parts)


def _list_expectation(
    definition: NumberingListDefinition,
) -> ListExpectation:
    return ListExpectation(
        list_kind=definition.list_kind,
        number_id=definition.number_id,
        abstract_number_id=definition.abstract_number_id,
        items=definition.items,
    )


def _reject_even_and_odd_headers(package: DocxPackage) -> None:
    if not package.has_part(_SETTINGS_PART):
        return
    settings_root = package.read_xml(_SETTINGS_PART)
    setting = settings_root.find(_W_EVEN_AND_ODD_HEADERS)
    if setting is None:
        return
    raw_value = setting.attrib.get(_W_VAL, "true").lower()
    if raw_value not in {"0", "false", "off", "no"}:
        raise DocxError(
            "block_not_editable",
            "当前阶段不支持启用奇偶页不同的页眉页脚。",
        )


def _validate_block_id(value: object) -> str:
    if not isinstance(value, str) or not (
        is_paragraph_block_id(value) or is_table_block_id(value)
    ):
        _invalid("富内容插入 block_id 必须是正文 paragraph 或 table ID。")
    return value


def _validate_list_items(value: object, operation_type: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > _MAX_LIST_ITEMS:
        _invalid(f"{operation_type}.items 必须是数量受限的非空列表。")
    items: list[str] = []
    total_length = 0
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > _MAX_LIST_ITEM_LENGTH
            or "\r" in item
            or "\n" in item
            or not _is_valid_xml_text(item)
        ):
            _invalid(f"{operation_type}.items 只能包含非空单行 XML 字符串。")
        total_length += len(item)
        if total_length > _MAX_LIST_TOTAL_TEXT:
            _invalid(f"{operation_type}.items 总文字长度超过限制。")
        items.append(item)
    return tuple(items)


def _validate_section_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid("section_index 必须是从零开始的非负整数。")
    return value


def _validate_optional_margin(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_MARGIN_TWIPS
    ):
        _invalid(f"{field_name} 必须是合理范围内的非负整数或 null。")
    return value


def _validate_optional_positive_int(
    value: object,
    field_name: str,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _invalid(f"{field_name} 必须是正整数或 null。")
    return value


def _validate_required_xml_text(
    value: object,
    field_name: str,
    *,
    maximum_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or "\r" in value
        or not _is_valid_xml_text(value)
    ):
        _invalid(f"{field_name} 必须是非空且长度受限的 XML 字符串。")
    return value


def _validate_optional_xml_text(
    value: object,
    field_name: str,
    *,
    maximum_length: int,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum_length
        or "\r" in value
        or not _is_valid_xml_text(value)
    ):
        _invalid(f"{field_name} 必须是长度受限的 XML 字符串或 null。")
    return value


def _is_valid_xml_text(value: str) -> bool:
    return all(
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
        for codepoint in map(ord, value)
    )


def _invalid(message: str) -> None:
    raise DocxError("invalid_edit_operation", message)
