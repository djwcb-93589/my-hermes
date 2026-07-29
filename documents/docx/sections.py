"""section 定位、页面设置以及简单页眉页脚 XML 生成。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, NoReturn
from xml.etree import ElementTree

from .errors import DocxError
from .relationships import RELATIONSHIP_ID
from .textmap import append_text_content


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_RUN = f"{{{_W_NS}}}r"
_W_SECTION_PROPERTIES = f"{{{_W_NS}}}sectPr"
_W_SECTION_PROPERTIES_CHANGE = f"{{{_W_NS}}}sectPrChange"
_W_FOOTNOTE_PROPERTIES = f"{{{_W_NS}}}footnotePr"
_W_ENDNOTE_PROPERTIES = f"{{{_W_NS}}}endnotePr"
_W_SECTION_TYPE = f"{{{_W_NS}}}type"
_W_TITLE_PAGE = f"{{{_W_NS}}}titlePg"
_W_PAGE_SIZE = f"{{{_W_NS}}}pgSz"
_W_PAGE_MARGIN = f"{{{_W_NS}}}pgMar"
_W_PAPER_SOURCE = f"{{{_W_NS}}}paperSrc"
_W_PAGE_BORDERS = f"{{{_W_NS}}}pgBorders"
_W_LINE_NUMBER_TYPE = f"{{{_W_NS}}}lnNumType"
_W_PAGE_NUMBER_TYPE = f"{{{_W_NS}}}pgNumType"
_W_COLUMNS = f"{{{_W_NS}}}cols"
_W_FORM_PROTECTION = f"{{{_W_NS}}}formProt"
_W_VERTICAL_ALIGNMENT = f"{{{_W_NS}}}vAlign"
_W_NO_ENDNOTE = f"{{{_W_NS}}}noEndnote"
_W_TEXT_DIRECTION = f"{{{_W_NS}}}textDirection"
_W_BIDI = f"{{{_W_NS}}}bidi"
_W_RTL_GUTTER = f"{{{_W_NS}}}rtlGutter"
_W_DOCUMENT_GRID = f"{{{_W_NS}}}docGrid"
_W_PRINTER_SETTINGS = f"{{{_W_NS}}}printerSettings"
_W_HEADER_REFERENCE = f"{{{_W_NS}}}headerReference"
_W_FOOTER_REFERENCE = f"{{{_W_NS}}}footerReference"
_W_HEADER = f"{{{_W_NS}}}hdr"
_W_FOOTER = f"{{{_W_NS}}}ftr"
_W_FIELD_CHARACTER = f"{{{_W_NS}}}fldChar"
_W_INSTRUCTION_TEXT = f"{{{_W_NS}}}instrText"
_W_TEXT = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BREAK = f"{{{_W_NS}}}br"
_W_CARRIAGE_RETURN = f"{{{_W_NS}}}cr"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_VAL = f"{{{_W_NS}}}val"
_W_TYPE = f"{{{_W_NS}}}type"
_W_WIDTH = f"{{{_W_NS}}}w"
_W_HEIGHT = f"{{{_W_NS}}}h"
_W_ORIENTATION = f"{{{_W_NS}}}orient"
_W_TOP = f"{{{_W_NS}}}top"
_W_BOTTOM = f"{{{_W_NS}}}bottom"
_W_LEFT = f"{{{_W_NS}}}left"
_W_RIGHT = f"{{{_W_NS}}}right"
_W_HEADER_MARGIN = f"{{{_W_NS}}}header"
_W_FOOTER_MARGIN = f"{{{_W_NS}}}footer"
_W_GUTTER = f"{{{_W_NS}}}gutter"
_W_FIELD_CHARACTER_TYPE = f"{{{_W_NS}}}fldCharType"
_XML_SPACE = f"{{{_XML_NS}}}space"

PAGE_SIZES = {
    "A4": (11906, 16838),
    "LETTER": (12240, 15840),
}
DEFAULT_MARGINS = {
    _W_TOP: 1440,
    _W_BOTTOM: 1440,
    _W_LEFT: 1440,
    _W_RIGHT: 1440,
    _W_HEADER_MARGIN: 720,
    _W_FOOTER_MARGIN: 720,
    _W_GUTTER: 0,
}
MAX_MARGIN_TWIPS = 31_680
_SECTPR_CHILD_ORDER: Mapping[str, int] = MappingProxyType(
    {
        _W_HEADER_REFERENCE: 10,
        _W_FOOTER_REFERENCE: 20,
        _W_FOOTNOTE_PROPERTIES: 30,
        _W_ENDNOTE_PROPERTIES: 40,
        _W_SECTION_TYPE: 50,
        _W_PAGE_SIZE: 60,
        _W_PAGE_MARGIN: 70,
        _W_PAPER_SOURCE: 80,
        _W_PAGE_BORDERS: 90,
        _W_LINE_NUMBER_TYPE: 100,
        _W_PAGE_NUMBER_TYPE: 110,
        _W_COLUMNS: 120,
        _W_FORM_PROTECTION: 130,
        _W_VERTICAL_ALIGNMENT: 140,
        _W_NO_ENDNOTE: 150,
        _W_TITLE_PAGE: 160,
        _W_TEXT_DIRECTION: 170,
        _W_BIDI: 180,
        _W_RTL_GUTTER: 190,
        _W_DOCUMENT_GRID: 200,
        _W_PRINTER_SETTINGS: 210,
        _W_SECTION_PROPERTIES_CHANGE: 1000,
    }
)
_SECTPR_SINGLETON_CHILDREN = frozenset(
    set(_SECTPR_CHILD_ORDER) - {_W_HEADER_REFERENCE, _W_FOOTER_REFERENCE}
)


@dataclass(frozen=True)
class SectionLocation:
    section_index: int
    section_properties: ElementTree.Element
    owner_paragraph: ElementTree.Element | None


@dataclass(frozen=True)
class PageSetupExpectation:
    section_index: int
    page_size_attributes: tuple[tuple[str, str], ...] | None
    page_margin_attributes: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class HeaderFooterExpectation:
    section_index: int
    part_kind: Literal["header", "footer"]
    cleared: bool
    relationship_id: str | None
    part_name: str | None
    text: str | None
    include_page_number: bool


@dataclass(frozen=True)
class SectionStructureExpectation:
    section_index: int
    expected_child_tags: tuple[str, ...]


def locate_sections(body: ElementTree.Element) -> tuple[SectionLocation, ...]:
    """按正文顺序定位顶层段落和 body 直接承载的 sectPr。"""

    locations: list[SectionLocation] = []
    for child in body:
        if child.tag == _W_PARAGRAPH:
            properties = child.find(_W_PARAGRAPH_PROPERTIES)
            if properties is None:
                continue
            section_properties = [
                element
                for element in properties
                if element.tag == _W_SECTION_PROPERTIES
            ]
            if len(section_properties) > 1:
                raise DocxError(
                    "block_not_editable",
                    "段落包含重复的 section properties。",
                )
            if section_properties:
                locations.append(
                    SectionLocation(
                        section_index=len(locations),
                        section_properties=section_properties[0],
                        owner_paragraph=child,
                    )
                )
        elif child.tag == _W_SECTION_PROPERTIES:
            locations.append(
                SectionLocation(
                    section_index=len(locations),
                    section_properties=child,
                    owner_paragraph=None,
                )
            )
    return tuple(locations)


def require_section(
    locations: tuple[SectionLocation, ...],
    section_index: int,
) -> SectionLocation:
    if section_index >= len(locations):
        raise DocxError("section_not_found", "指定的 section_index 不存在。")
    return locations[section_index]


def validate_section_child_order(
    section: ElementTree.Element,
    *,
    error_type: Literal["block_not_editable", "edit_verification_failed"],
) -> None:
    """验证 sectPr 白名单、单例约束及规范的直接子节点顺序。"""

    if section.tag != _W_SECTION_PROPERTIES:
        _raise_section_structure_error(error_type)
    previous_order = -1
    singleton_tags: set[str] = set()
    for child in section:
        if not isinstance(child.tag, str):
            continue
        child_order = _require_known_section_child_order(
            child.tag,
            error_type=error_type,
        )
        if child_order < previous_order:
            _raise_section_structure_error(error_type)
        previous_order = child_order
        if child.tag not in _SECTPR_SINGLETON_CHILDREN:
            continue
        if child.tag in singleton_tags:
            _raise_section_structure_error(error_type)
        singleton_tags.add(child.tag)


def build_section_structure_expectation(
    location: SectionLocation,
) -> SectionStructureExpectation:
    """记录修改计划完成后的 section 直接元素标签顺序。"""

    validate_section_child_order(
        location.section_properties,
        error_type="block_not_editable",
    )
    return SectionStructureExpectation(
        section_index=location.section_index,
        expected_child_tags=_section_child_tags(
            location.section_properties
        ),
    )


def verify_section_structure(
    locations: tuple[SectionLocation, ...],
    expectation: SectionStructureExpectation,
) -> None:
    """验证输出 section 的顺序合法且与内存修改计划完全一致。"""

    location = require_section(locations, expectation.section_index)
    validate_section_child_order(
        location.section_properties,
        error_type="edit_verification_failed",
    )
    if (
        _section_child_tags(location.section_properties)
        != expectation.expected_child_tags
    ):
        raise DocxError(
            "edit_verification_failed",
            "输出 section 的属性标签顺序与修改计划不一致。",
        )


def validate_section_page_geometry(
    section: ElementTree.Element,
    *,
    error_type: Literal["block_not_editable", "edit_verification_failed"],
) -> None:
    """验证 section 页面尺寸、页边距与正的可用页面区域。"""

    validate_section_child_order(section, error_type=error_type)
    page_size = section.find(_W_PAGE_SIZE)
    page_margin = section.find(_W_PAGE_MARGIN)
    try:
        width, height, _ = _read_page_dimensions(page_size)
        margins = {
            attribute: _read_margin_value(page_margin, attribute, default)
            for attribute, default in DEFAULT_MARGINS.items()
        }
    except DocxError as exc:
        if error_type == "block_not_editable":
            raise
        raise DocxError(
            error_type,
            "输出 section 的页面尺寸或页边距属性无效。",
        ) from exc
    if (
        margins[_W_LEFT] + margins[_W_RIGHT] >= width
        or margins[_W_TOP] + margins[_W_BOTTOM] >= height
    ):
        message = (
            "输出 section 的可用页面区域无效。"
            if error_type == "edit_verification_failed"
            else "section 的页边距使可用页面区域无效。"
        )
        raise DocxError(error_type, message)


def apply_page_setup(
    location: SectionLocation,
    *,
    page_size: str | None,
    orientation: str | None,
    margin_top_twips: int | None,
    margin_bottom_twips: int | None,
    margin_left_twips: int | None,
    margin_right_twips: int | None,
) -> PageSetupExpectation:
    """只更新明确字段，并验证页面仍保留正的可用宽高。"""

    section = location.section_properties
    _ensure_section_editable(section, header_footer=False)
    page_sizes = [child for child in section if child.tag == _W_PAGE_SIZE]
    page_margins = [child for child in section if child.tag == _W_PAGE_MARGIN]
    if len(page_sizes) > 1 or len(page_margins) > 1:
        raise DocxError(
            "block_not_editable",
            "section 包含重复的 pgSz 或 pgMar。",
        )

    page_size_element = page_sizes[0] if page_sizes else None
    current_width, current_height, current_orientation = _read_page_dimensions(
        page_size_element
    )
    effective_orientation = orientation or current_orientation
    if page_size is not None:
        portrait_width, portrait_height = PAGE_SIZES[page_size]
        if effective_orientation == "landscape":
            width, height = portrait_height, portrait_width
        else:
            width, height = portrait_width, portrait_height
    elif orientation is not None:
        short_side = min(current_width, current_height)
        long_side = max(current_width, current_height)
        if orientation == "landscape":
            width, height = long_side, short_side
        else:
            width, height = short_side, long_side
    else:
        width, height = current_width, current_height

    if page_size is not None or orientation is not None:
        if page_size_element is None:
            page_size_element = ElementTree.Element(_W_PAGE_SIZE)
            _insert_section_child(section, page_size_element)
        page_size_element.set(_W_WIDTH, str(width))
        page_size_element.set(_W_HEIGHT, str(height))
        if effective_orientation == "landscape":
            page_size_element.set(_W_ORIENTATION, "landscape")
        else:
            page_size_element.attrib.pop(_W_ORIENTATION, None)

    requested_margins = {
        _W_TOP: margin_top_twips,
        _W_BOTTOM: margin_bottom_twips,
        _W_LEFT: margin_left_twips,
        _W_RIGHT: margin_right_twips,
    }
    if any(value is not None for value in requested_margins.values()):
        if page_margins:
            margin_element = page_margins[0]
        else:
            margin_element = ElementTree.Element(_W_PAGE_MARGIN)
            _insert_section_child(section, margin_element)
        for attribute, default in DEFAULT_MARGINS.items():
            if attribute not in margin_element.attrib:
                margin_element.set(attribute, str(default))
        for attribute, value in requested_margins.items():
            if value is not None:
                margin_element.set(attribute, str(value))
    else:
        margin_element = page_margins[0] if page_margins else None

    margin_values = {
        attribute: _read_margin_value(margin_element, attribute, default)
        for attribute, default in DEFAULT_MARGINS.items()
    }
    if (
        margin_values[_W_LEFT] + margin_values[_W_RIGHT] >= width
        or margin_values[_W_TOP] + margin_values[_W_BOTTOM] >= height
    ):
        raise DocxError(
            "invalid_edit_operation",
            "页边距使页面可用宽度或高度变为零或负数。",
        )
    return PageSetupExpectation(
        section_index=location.section_index,
        page_size_attributes=(
            tuple(sorted(page_size_element.attrib.items()))
            if page_size_element is not None
            else None
        ),
        page_margin_attributes=(
            tuple(sorted(margin_element.attrib.items()))
            if margin_element is not None
            else None
        ),
    )


def set_header_footer_reference(
    location: SectionLocation,
    *,
    part_kind: Literal["header", "footer"],
    relationship_id: str | None,
) -> None:
    """替换或清除当前 section 唯一的 default 引用。"""

    _ensure_section_editable(
        location.section_properties,
        header_footer=True,
    )
    tag = _W_HEADER_REFERENCE if part_kind == "header" else _W_FOOTER_REFERENCE
    section = location.section_properties
    references = [child for child in section if child.tag == tag]
    if any(reference.attrib.get(_W_TYPE, "default") != "default" for reference in references):
        raise DocxError(
            "block_not_editable",
            "当前阶段不支持 first 或 even 页眉页脚引用。",
        )
    if len(references) > 1:
        raise DocxError(
            "block_not_editable",
            "section 包含重复的 default 页眉页脚引用。",
        )
    for reference in references:
        section.remove(reference)
    if relationship_id is None:
        return
    reference = ElementTree.Element(
        tag,
        {
            _W_TYPE: "default",
            RELATIONSHIP_ID: relationship_id,
        },
    )
    _insert_section_child(section, reference)


def get_default_header_footer_reference_id(
    location: SectionLocation,
    *,
    part_kind: Literal["header", "footer"],
) -> str | None:
    """返回唯一 default 引用，并拒绝 first/even 或重复结构。"""

    _ensure_section_editable(
        location.section_properties,
        header_footer=True,
    )
    tag = _W_HEADER_REFERENCE if part_kind == "header" else _W_FOOTER_REFERENCE
    references = [
        child for child in location.section_properties if child.tag == tag
    ]
    if any(reference.attrib.get(_W_TYPE, "default") != "default" for reference in references):
        raise DocxError(
            "block_not_editable",
            "当前阶段不支持 first 或 even 页眉页脚引用。",
        )
    if len(references) > 1:
        raise DocxError(
            "block_not_editable",
            "section 包含重复的 default 页眉页脚引用。",
        )
    if not references:
        return None
    relationship_id = references[0].attrib.get(RELATIONSHIP_ID)
    if not relationship_id:
        raise DocxError(
            "block_not_editable",
            "页眉页脚 reference 缺少 relationship ID。",
        )
    return relationship_id


def validate_simple_header_footer_part(
    root: ElementTree.Element,
    *,
    part_kind: Literal["header", "footer"],
) -> None:
    """确认现有 part 只有本阶段允许的单段落普通 run/页码字段。"""

    expected_root = _W_HEADER if part_kind == "header" else _W_FOOTER
    if root.tag != expected_root or len(root) != 1 or root[0].tag != _W_PARAGRAPH:
        raise DocxError(
            "block_not_editable",
            "现有页眉页脚不是简单单段落结构。",
        )
    allowed_run_children = {
        _W_RUN_PROPERTIES,
        _W_TEXT,
        _W_TAB,
        _W_BREAK,
        _W_CARRIAGE_RETURN,
        _W_FIELD_CHARACTER,
        _W_INSTRUCTION_TEXT,
    }
    for child in root[0]:
        if child.tag != _W_RUN or any(
            nested.tag not in allowed_run_children for nested in child
        ):
            raise DocxError(
                "block_not_editable",
                "现有页眉页脚包含当前阶段不支持的复杂内容。",
            )
    if part_kind == "header" and (
        list(root.iter(_W_FIELD_CHARACTER))
        or list(root.iter(_W_INSTRUCTION_TEXT))
    ):
        raise DocxError(
            "block_not_editable",
            "现有页眉包含当前阶段不支持的字段。",
        )
    if part_kind == "footer":
        field_characters = list(root.iter(_W_FIELD_CHARACTER))
        instructions = list(root.iter(_W_INSTRUCTION_TEXT))
        if field_characters or instructions:
            field_types = [
                element.attrib.get(_W_FIELD_CHARACTER_TYPE)
                for element in field_characters
            ]
            if (
                field_types != ["begin", "separate", "end"]
                or len(instructions) != 1
                or (instructions[0].text or "").strip() != "PAGE"
            ):
                raise DocxError(
                    "block_not_editable",
                    "现有页脚包含当前阶段不支持的复杂字段。",
                )


def create_header_xml(text: str) -> ElementTree.Element:
    header = ElementTree.Element(_W_HEADER)
    paragraph = ElementTree.SubElement(header, _W_PARAGRAPH)
    run = ElementTree.SubElement(paragraph, _W_RUN)
    append_text_content(run, text)
    return header


def create_footer_xml(
    text: str | None,
    *,
    include_page_number: bool,
) -> ElementTree.Element:
    footer = ElementTree.Element(_W_FOOTER)
    paragraph = ElementTree.SubElement(footer, _W_PARAGRAPH)
    if text is not None:
        run = ElementTree.SubElement(paragraph, _W_RUN)
        append_text_content(run, text)
    if include_page_number:
        _append_page_field(paragraph)
    return footer


def verify_page_setup(
    locations: tuple[SectionLocation, ...],
    expectation: PageSetupExpectation,
) -> None:
    location = require_section(locations, expectation.section_index)
    validate_section_child_order(
        location.section_properties,
        error_type="edit_verification_failed",
    )
    page_sizes = [
        child
        for child in location.section_properties
        if child.tag == _W_PAGE_SIZE
    ]
    page_margins = [
        child
        for child in location.section_properties
        if child.tag == _W_PAGE_MARGIN
    ]
    if expectation.page_size_attributes is None:
        if page_sizes:
            raise DocxError(
                "edit_verification_failed",
                "修改后的 section 意外新增了页面尺寸节点。",
            )
    elif len(page_sizes) != 1 or tuple(
        sorted(page_sizes[0].attrib.items())
    ) != expectation.page_size_attributes:
        raise DocxError(
            "edit_verification_failed",
            "修改后的 section 页面尺寸与计划不一致。",
        )
    if expectation.page_margin_attributes is None:
        if page_margins:
            raise DocxError(
                "edit_verification_failed",
                "修改后的 section 意外新增了页边距节点。",
            )
    elif len(page_margins) != 1 or tuple(
        sorted(page_margins[0].attrib.items())
    ) != expectation.page_margin_attributes:
        raise DocxError(
            "edit_verification_failed",
            "修改后的 section 页边距与计划不一致。",
        )


def verify_header_footer_part(
    root: ElementTree.Element,
    expectation: HeaderFooterExpectation,
) -> None:
    expected_root = _W_HEADER if expectation.part_kind == "header" else _W_FOOTER
    if root.tag != expected_root or len(root) != 1 or root[0].tag != _W_PARAGRAPH:
        raise DocxError(
            "edit_verification_failed",
            "页眉页脚 part 不是预期的简单单段落结构。",
        )
    paragraph = root[0]
    actual_text = _read_visible_text(paragraph)
    expected_text = expectation.text or ""
    if expectation.include_page_number:
        expected_text += "1"
    if actual_text != expected_text:
        raise DocxError(
            "edit_verification_failed",
            "页眉页脚文字或页码显示缓存与请求不一致。",
        )
    field_characters = list(paragraph.iter(_W_FIELD_CHARACTER))
    instructions = list(paragraph.iter(_W_INSTRUCTION_TEXT))
    if expectation.include_page_number:
        field_types = [
            element.attrib.get(_W_FIELD_CHARACTER_TYPE)
            for element in field_characters
        ]
        if field_types != ["begin", "separate", "end"] or len(instructions) != 1:
            raise DocxError(
                "edit_verification_failed",
                "页脚 PAGE 字段结构不完整。",
            )
        if (instructions[0].text or "").strip() != "PAGE":
            raise DocxError(
                "edit_verification_failed",
                "页脚字段不是动态 PAGE 字段。",
            )
    elif field_characters or instructions:
        raise DocxError(
            "edit_verification_failed",
            "普通页眉页脚意外包含字段代码。",
        )


def _append_page_field(paragraph: ElementTree.Element) -> None:
    begin_run = ElementTree.SubElement(paragraph, _W_RUN)
    ElementTree.SubElement(
        begin_run,
        _W_FIELD_CHARACTER,
        {_W_FIELD_CHARACTER_TYPE: "begin"},
    )
    instruction_run = ElementTree.SubElement(paragraph, _W_RUN)
    instruction = ElementTree.SubElement(
        instruction_run,
        _W_INSTRUCTION_TEXT,
        {_XML_SPACE: "preserve"},
    )
    instruction.text = " PAGE "
    separate_run = ElementTree.SubElement(paragraph, _W_RUN)
    ElementTree.SubElement(
        separate_run,
        _W_FIELD_CHARACTER,
        {_W_FIELD_CHARACTER_TYPE: "separate"},
    )
    result_run = ElementTree.SubElement(paragraph, _W_RUN)
    result_text = ElementTree.SubElement(result_run, _W_TEXT)
    result_text.text = "1"
    end_run = ElementTree.SubElement(paragraph, _W_RUN)
    ElementTree.SubElement(
        end_run,
        _W_FIELD_CHARACTER,
        {_W_FIELD_CHARACTER_TYPE: "end"},
    )


def _read_visible_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for child in element.iter():
        if child.tag == _W_TEXT:
            parts.append(child.text or "")
        elif child.tag == _W_TAB:
            parts.append("\t")
        elif child.tag in {
            _W_BREAK,
            _W_CARRIAGE_RETURN,
        }:
            parts.append("\n")
    return "".join(parts)


def _read_page_dimensions(
    page_size: ElementTree.Element | None,
) -> tuple[int, int, Literal["portrait", "landscape"]]:
    default_width, default_height = PAGE_SIZES["A4"]
    if page_size is None:
        return default_width, default_height, "portrait"
    try:
        width = int(page_size.attrib.get(_W_WIDTH, str(default_width)))
        height = int(page_size.attrib.get(_W_HEIGHT, str(default_height)))
    except ValueError as exc:
        raise DocxError(
            "block_not_editable",
            "section 的现有页面尺寸属性无效。",
        ) from exc
    if width <= 0 or height <= 0:
        raise DocxError(
            "block_not_editable",
            "section 的现有页面尺寸属性无效。",
        )
    raw_orientation = page_size.attrib.get(_W_ORIENTATION)
    if raw_orientation not in {None, "portrait", "landscape"}:
        raise DocxError(
            "block_not_editable",
            "section 的现有页面方向不受支持。",
        )
    orientation: Literal["portrait", "landscape"] = (
        "landscape"
        if raw_orientation == "landscape" or width > height
        else "portrait"
    )
    return width, height, orientation


def _read_margin_value(
    element: ElementTree.Element | None,
    attribute: str,
    default: int,
) -> int:
    if element is None:
        return default
    try:
        value = int(element.attrib.get(attribute, str(default)))
    except ValueError as exc:
        raise DocxError(
            "block_not_editable",
            "section 的现有页边距属性无效。",
        ) from exc
    if value < 0 or value > MAX_MARGIN_TWIPS:
        raise DocxError(
            "block_not_editable",
            "section 的现有页边距属性超出支持范围。",
        )
    return value


def _insert_section_child(
    section: ElementTree.Element,
    child: ElementTree.Element,
) -> None:
    target_order = _require_known_section_child_order(
        child.tag,
        error_type="block_not_editable",
    )
    insertion_index = len(section)
    for index, existing in enumerate(section):
        if not isinstance(existing.tag, str):
            continue
        existing_order = _require_known_section_child_order(
            existing.tag,
            error_type="block_not_editable",
        )
        if existing_order > target_order:
            insertion_index = index
            break
    section.insert(insertion_index, child)


def _ensure_section_editable(
    section: ElementTree.Element,
    *,
    header_footer: bool,
) -> None:
    validate_section_child_order(
        section,
        error_type="block_not_editable",
    )
    if any(
        element.tag == _W_SECTION_PROPERTIES_CHANGE
        for element in section.iter()
    ):
        raise DocxError(
            "block_not_editable",
            "包含属性修订的 section 当前阶段不可编辑。",
        )
    if header_footer and section.find(_W_TITLE_PAGE) is not None:
        raise DocxError(
            "block_not_editable",
            "当前阶段不支持启用首页不同的 section 页眉页脚。",
        )


def _require_known_section_child_order(
    tag: object,
    *,
    error_type: Literal["block_not_editable", "edit_verification_failed"],
) -> int:
    if not isinstance(tag, str) or tag not in _SECTPR_CHILD_ORDER:
        _raise_section_structure_error(error_type)
    return _SECTPR_CHILD_ORDER[tag]


def _section_child_tags(section: ElementTree.Element) -> tuple[str, ...]:
    return tuple(
        child.tag
        for child in section
        if isinstance(child.tag, str)
    )


def _raise_section_structure_error(
    error_type: Literal["block_not_editable", "edit_verification_failed"],
) -> NoReturn:
    if error_type == "edit_verification_failed":
        message = "输出 section 的属性顺序或直接子节点结构无效。"
    else:
        message = "当前 section 的属性顺序不符合本阶段支持的安全结构。"
    raise DocxError(error_type, message)
