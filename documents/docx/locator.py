"""Reader 与 Editor 共用的 DOCX 正文位置模型。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_RUN = f"{{{_W_NS}}}r"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_TEXT = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BREAK = f"{{{_W_NS}}}br"
_W_CARRIAGE_RETURN = f"{{{_W_NS}}}cr"
_W_TABLE = f"{{{_W_NS}}}tbl"
_W_TABLE_PROPERTIES = f"{{{_W_NS}}}tblPr"
_W_TABLE_GRID = f"{{{_W_NS}}}tblGrid"
_W_SECTION_PROPERTIES = f"{{{_W_NS}}}sectPr"
_W_ROW = f"{{{_W_NS}}}tr"
_W_ROW_PROPERTIES = f"{{{_W_NS}}}trPr"
_W_CELL = f"{{{_W_NS}}}tc"
_W_CELL_PROPERTIES = f"{{{_W_NS}}}tcPr"
_W_GRID_BEFORE = f"{{{_W_NS}}}gridBefore"
_W_GRID_AFTER = f"{{{_W_NS}}}gridAfter"
_W_GRID_SPAN = f"{{{_W_NS}}}gridSpan"
_W_HORIZONTAL_MERGE = f"{{{_W_NS}}}hMerge"
_W_VERTICAL_MERGE = f"{{{_W_NS}}}vMerge"
_W_CELL_MERGE = f"{{{_W_NS}}}cellMerge"
_W_CELL_INSERTION = f"{{{_W_NS}}}cellIns"
_W_CELL_DELETION = f"{{{_W_NS}}}cellDel"
_W_VAL = f"{{{_W_NS}}}val"

_W_HYPERLINK = f"{{{_W_NS}}}hyperlink"
_W_SIMPLE_FIELD = f"{{{_W_NS}}}fldSimple"
_W_INSTRUCTION_TEXT = f"{{{_W_NS}}}instrText"
_W_FIELD_CHAR = f"{{{_W_NS}}}fldChar"
_W_INSERTION = f"{{{_W_NS}}}ins"
_W_DELETION = f"{{{_W_NS}}}del"
_W_MOVE_FROM = f"{{{_W_NS}}}moveFrom"
_W_MOVE_TO = f"{{{_W_NS}}}moveTo"
_W_COMMENT_RANGE_START = f"{{{_W_NS}}}commentRangeStart"
_W_COMMENT_RANGE_END = f"{{{_W_NS}}}commentRangeEnd"
_W_COMMENT_REFERENCE = f"{{{_W_NS}}}commentReference"
_W_DRAWING = f"{{{_W_NS}}}drawing"
_W_OBJECT = f"{{{_W_NS}}}object"
_W_PICTURE = f"{{{_W_NS}}}pict"
_W_CONTENT_CONTROL = f"{{{_W_NS}}}sdt"
_W_ALT_CHUNK = f"{{{_W_NS}}}altChunk"
_W_BOOKMARK_START = f"{{{_W_NS}}}bookmarkStart"
_W_BOOKMARK_END = f"{{{_W_NS}}}bookmarkEnd"
_W_RUN_PROPERTIES_CHANGE = f"{{{_W_NS}}}rPrChange"
_W_PARAGRAPH_PROPERTIES_CHANGE = f"{{{_W_NS}}}pPrChange"
_W_TABLE_PROPERTIES_CHANGE = f"{{{_W_NS}}}tblPrChange"
_W_ROW_PROPERTIES_CHANGE = f"{{{_W_NS}}}trPrChange"
_W_CELL_PROPERTIES_CHANGE = f"{{{_W_NS}}}tcPrChange"
_W_SECTION_PROPERTIES_CHANGE = f"{{{_W_NS}}}sectPrChange"

_CONTENT_REVISION_TAGS = frozenset(
    {_W_INSERTION, _W_DELETION, _W_MOVE_FROM, _W_MOVE_TO}
)
_HIDDEN_CONTENT_REVISION_TAGS = frozenset({_W_DELETION, _W_MOVE_FROM})
_PROPERTY_REVISION_TAGS = frozenset(
    {
        _W_RUN_PROPERTIES_CHANGE,
        _W_PARAGRAPH_PROPERTIES_CHANGE,
        _W_TABLE_PROPERTIES_CHANGE,
        _W_ROW_PROPERTIES_CHANGE,
        _W_CELL_PROPERTIES_CHANGE,
        _W_SECTION_PROPERTIES_CHANGE,
    }
)
_CELL_REVISION_TAGS = frozenset(
    {_W_CELL_INSERTION, _W_CELL_DELETION, _W_CELL_MERGE}
)
_TABLE_REVISION_TAGS = frozenset(
    {
        *_CONTENT_REVISION_TAGS,
        _W_TABLE_PROPERTIES_CHANGE,
        _W_ROW_PROPERTIES_CHANGE,
        _W_CELL_PROPERTIES_CHANGE,
        *_CELL_REVISION_TAGS,
    }
)
_ALLOWED_TABLE_CHILDREN = frozenset(
    {_W_TABLE_PROPERTIES, _W_TABLE_GRID, _W_ROW}
)
_ALLOWED_ROW_CHILDREN = frozenset({_W_ROW_PROPERTIES, _W_CELL})
_FORBIDDEN_PARAGRAPH_TAGS = frozenset(
    {
        _W_HYPERLINK,
        _W_SIMPLE_FIELD,
        _W_INSTRUCTION_TEXT,
        _W_FIELD_CHAR,
        *_CONTENT_REVISION_TAGS,
        *_PROPERTY_REVISION_TAGS,
        _W_COMMENT_RANGE_START,
        _W_COMMENT_RANGE_END,
        _W_COMMENT_REFERENCE,
        _W_DRAWING,
        _W_OBJECT,
        _W_PICTURE,
        _W_CONTENT_CONTROL,
        _W_ALT_CHUNK,
        _W_BOOKMARK_START,
        _W_BOOKMARK_END,
    }
)
_ALLOWED_RUN_CHILDREN = frozenset(
    {_W_RUN_PROPERTIES, _W_TEXT, _W_TAB, _W_BREAK, _W_CARRIAGE_RETURN}
)
_PARAGRAPH_BLOCK_PATTERN = re.compile(r"body:p:(0|[1-9]\d*)\Z")
_TABLE_CELL_BLOCK_PATTERN = re.compile(
    r"body:table:(0|[1-9]\d*):row:(0|[1-9]\d*):cell:(0|[1-9]\d*)\Z"
)


@dataclass(frozen=True)
class BodyChildLocation:
    """正文直接子节点及其确定性 block_id。"""

    kind: Literal["paragraph", "table", "section", "unsupported"]
    block_id: str | None
    element: ElementTree.Element


@dataclass(frozen=True)
class TableCellLocation:
    """表格单元格及其祖先结构位置。"""

    block_id: str
    table: ElementTree.Element
    row: ElementTree.Element
    cell: ElementTree.Element
    revision_ancestor: bool


EditTargetLocation = BodyChildLocation | TableCellLocation


def iter_body_children(
    body: ElementTree.Element,
) -> Iterator[BodyChildLocation]:
    """按 XML 实际顺序遍历正文，并维护段落与表格独立计数。"""

    paragraph_index = 0
    table_index = 0
    for child in body:
        if child.tag == _W_PARAGRAPH:
            yield BodyChildLocation(
                kind="paragraph",
                block_id=f"body:p:{paragraph_index}",
                element=child,
            )
            paragraph_index += 1
        elif child.tag == _W_TABLE:
            yield BodyChildLocation(
                kind="table",
                block_id=f"body:table:{table_index}",
                element=child,
            )
            table_index += 1
        elif child.tag == _W_SECTION_PROPERTIES:
            yield BodyChildLocation(kind="section", block_id=None, element=child)
        else:
            yield BodyChildLocation(kind="unsupported", block_id=None, element=child)


def visible_table_rows(
    table: ElementTree.Element,
) -> tuple[list[ElementTree.Element], bool]:
    """返回当前结果视图中的表格行和修订包装标记。"""

    located_rows, wrapped = _visible_target_children(table, _W_ROW)
    return [element for element, _ in located_rows], wrapped


def visible_row_cells(
    row: ElementTree.Element,
) -> tuple[list[ElementTree.Element], bool]:
    """返回当前结果视图中的单元格和修订包装标记。"""

    located_cells, wrapped = _visible_target_children(row, _W_CELL)
    return [element for element, _ in located_cells], wrapped


def iter_table_cells(
    table_location: BodyChildLocation,
) -> Iterator[TableCellLocation]:
    """使用与 Reader 相同的可见行列规则生成单元格 block_id。"""

    if table_location.kind != "table" or table_location.block_id is None:
        return
    located_rows, _ = _visible_target_children(table_location.element, _W_ROW)
    for row_index, (row, row_revised) in enumerate(located_rows):
        located_cells, _ = _visible_target_children(row, _W_CELL)
        for cell_index, (cell, cell_revised) in enumerate(located_cells):
            yield TableCellLocation(
                block_id=(
                    f"{table_location.block_id}:row:{row_index}:cell:{cell_index}"
                ),
                table=table_location.element,
                row=row,
                cell=cell,
                revision_ancestor=row_revised or cell_revised,
            )


def build_edit_target_index(
    body: ElementTree.Element,
) -> dict[str, EditTargetLocation]:
    """构建仅包含正文段落和表格单元格的编辑目标索引。"""

    targets: dict[str, EditTargetLocation] = {}
    for location in iter_body_children(body):
        if location.kind == "paragraph" and location.block_id is not None:
            targets[location.block_id] = location
        elif location.kind == "table":
            for cell_location in iter_table_cells(location):
                targets[cell_location.block_id] = cell_location
    return targets


def is_paragraph_block_id(block_id: str) -> bool:
    """严格判断正文段落 block_id。"""

    return _PARAGRAPH_BLOCK_PATTERN.fullmatch(block_id) is not None


def is_table_cell_block_id(block_id: str) -> bool:
    """严格判断表格单元格 block_id。"""

    return _TABLE_CELL_BLOCK_PATTERN.fullmatch(block_id) is not None


def is_strictly_editable_paragraph(paragraph: ElementTree.Element) -> bool:
    """检查段落是否只包含当前阶段允许安全替换的普通结构。"""

    if paragraph.tag != _W_PARAGRAPH:
        return False
    paragraph_tags = {element.tag for element in paragraph.iter()}
    if paragraph_tags.intersection(_FORBIDDEN_PARAGRAPH_TAGS):
        return False

    properties_seen = 0
    for index, child in enumerate(paragraph):
        if child.tag == _W_PARAGRAPH_PROPERTIES:
            properties_seen += 1
            if index != 0 or properties_seen > 1:
                return False
        elif child.tag == _W_RUN:
            if not _is_strictly_editable_run(child):
                return False
        else:
            return False
    return True


def is_strictly_editable_table_cell(location: TableCellLocation) -> bool:
    """检查单元格及其表格祖先是否满足严格编辑条件。"""

    if location.revision_ancestor or not _has_strict_table_structure(location):
        return False

    properties_seen = 0
    paragraphs: list[ElementTree.Element] = []
    for index, child in enumerate(location.cell):
        if child.tag == _W_CELL_PROPERTIES:
            properties_seen += 1
            if index != 0 or properties_seen > 1 or _cell_has_merge(child):
                return False
        elif child.tag == _W_PARAGRAPH:
            paragraphs.append(child)
        else:
            return False
    return len(paragraphs) == 1 and is_strictly_editable_paragraph(paragraphs[0])


def get_single_cell_paragraph(
    location: TableCellLocation,
) -> ElementTree.Element | None:
    """返回严格单元格的唯一直接段落。"""

    paragraphs = [
        child for child in location.cell if child.tag == _W_PARAGRAPH
    ]
    if len(paragraphs) != 1:
        return None
    return paragraphs[0]


def _visible_target_children(
    parent: ElementTree.Element,
    target_tag: str,
) -> tuple[list[tuple[ElementTree.Element, bool]], bool]:
    located: list[tuple[ElementTree.Element, bool]] = []
    wrapped = False
    for child in parent:
        if child.tag == target_tag:
            located.append((child, False))
        elif child.tag in _CONTENT_REVISION_TAGS:
            wrapped = True
            if child.tag in _HIDDEN_CONTENT_REVISION_TAGS:
                continue
            for nested_child in child:
                if nested_child.tag == target_tag:
                    located.append((nested_child, True))
    return located, wrapped


def _is_strictly_editable_run(run: ElementTree.Element) -> bool:
    properties_seen = 0
    for index, child in enumerate(run):
        if child.tag not in _ALLOWED_RUN_CHILDREN:
            return False
        if child.tag == _W_RUN_PROPERTIES:
            properties_seen += 1
            if index != 0 or properties_seen > 1:
                return False
        elif len(child) != 0:
            return False
    return True


def _has_strict_table_structure(location: TableCellLocation) -> bool:
    """检查目标所属整张表格是否为规则、无修订的直接结构。"""

    table = location.table
    if table.tag != _W_TABLE or any(
        element.tag in _TABLE_REVISION_TAGS for element in table.iter()
    ):
        return False

    table_properties_seen = False
    table_grid_seen = False
    rows_started = False
    for child in table:
        if child.tag not in _ALLOWED_TABLE_CHILDREN:
            return False
        if child.tag == _W_TABLE_PROPERTIES:
            if table_properties_seen or table_grid_seen or rows_started:
                return False
            table_properties_seen = True
        elif child.tag == _W_TABLE_GRID:
            if table_grid_seen or rows_started:
                return False
            table_grid_seen = True
        else:
            rows_started = True

    rows, wrapped_rows = visible_table_rows(table)
    if wrapped_rows or not rows or not any(row is location.row for row in rows):
        return False

    row_cell_counts: list[int] = []
    target_cell_found = False
    for row in rows:
        row_properties_seen = False
        cells_started = False
        for child in row:
            if child.tag not in _ALLOWED_ROW_CHILDREN:
                return False
            if child.tag == _W_ROW_PROPERTIES:
                if row_properties_seen or cells_started:
                    return False
                row_properties_seen = True
                if (
                    child.find(_W_GRID_BEFORE) is not None
                    or child.find(_W_GRID_AFTER) is not None
                ):
                    return False
            else:
                cells_started = True

        cells, wrapped_cells = visible_row_cells(row)
        if wrapped_cells or not cells:
            return False
        row_cell_counts.append(len(cells))
        if row is location.row:
            target_cell_found = any(cell is location.cell for cell in cells)

    return target_cell_found and len(set(row_cell_counts)) == 1


def _cell_has_merge(properties: ElementTree.Element) -> bool:
    if (
        properties.find(_W_HORIZONTAL_MERGE) is not None
        or properties.find(_W_VERTICAL_MERGE) is not None
        or properties.find(_W_CELL_MERGE) is not None
    ):
        return True
    grid_span = properties.find(_W_GRID_SPAN)
    if grid_span is None:
        return False
    raw_value = grid_span.attrib.get(_W_VAL, grid_span.attrib.get("val"))
    if raw_value is None:
        return True
    try:
        return int(raw_value) > 1
    except ValueError:
        return True
