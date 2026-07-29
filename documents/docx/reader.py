"""现有 DOCX 到稳定结构化快照的只读转换器。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .errors import DocxError
from .models import (
    DocumentMetadata,
    DocumentSnapshot,
    DocumentWarning,
    InspectDocumentRequest,
    ParagraphSnapshot,
    TableCellSnapshot,
    TableSnapshot,
    TextRunSnapshot,
)
from .package import DOCX_LIMITS, DocxPackage


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"

_W_BODY = f"{{{_W_NS}}}body"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_TABLE = f"{{{_W_NS}}}tbl"
_W_SECTION_PROPERTIES = f"{{{_W_NS}}}sectPr"
_W_RUN = f"{{{_W_NS}}}r"
_W_TEXT = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BREAK = f"{{{_W_NS}}}br"
_W_CARRIAGE_RETURN = f"{{{_W_NS}}}cr"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_PARAGRAPH_STYLE = f"{{{_W_NS}}}pStyle"
_W_JUSTIFICATION = f"{{{_W_NS}}}jc"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_BOLD = f"{{{_W_NS}}}b"
_W_ITALIC = f"{{{_W_NS}}}i"
_W_UNDERLINE = f"{{{_W_NS}}}u"
_W_HYPERLINK = f"{{{_W_NS}}}hyperlink"
_W_SIMPLE_FIELD = f"{{{_W_NS}}}fldSimple"
_W_FIELD_CHAR = f"{{{_W_NS}}}fldChar"
_W_INSTRUCTION_TEXT = f"{{{_W_NS}}}instrText"
_W_INSERTION = f"{{{_W_NS}}}ins"
_W_DELETION = f"{{{_W_NS}}}del"
_W_MOVE_FROM = f"{{{_W_NS}}}moveFrom"
_W_MOVE_TO = f"{{{_W_NS}}}moveTo"
_W_RUN_PROPERTIES_CHANGE = f"{{{_W_NS}}}rPrChange"
_W_PARAGRAPH_PROPERTIES_CHANGE = f"{{{_W_NS}}}pPrChange"
_W_TABLE_PROPERTIES_CHANGE = f"{{{_W_NS}}}tblPrChange"
_W_ROW_PROPERTIES_CHANGE = f"{{{_W_NS}}}trPrChange"
_W_CELL_PROPERTIES_CHANGE = f"{{{_W_NS}}}tcPrChange"
_W_SECTION_PROPERTIES_CHANGE = f"{{{_W_NS}}}sectPrChange"
_W_COMMENT_RANGE_START = f"{{{_W_NS}}}commentRangeStart"
_W_COMMENT_RANGE_END = f"{{{_W_NS}}}commentRangeEnd"
_W_COMMENT_REFERENCE = f"{{{_W_NS}}}commentReference"
_W_DRAWING = f"{{{_W_NS}}}drawing"
_W_OBJECT = f"{{{_W_NS}}}object"
_W_PICTURE = f"{{{_W_NS}}}pict"
_W_CONTENT_CONTROL = f"{{{_W_NS}}}sdt"
_W_ALT_CHUNK = f"{{{_W_NS}}}altChunk"
_W_ROW = f"{{{_W_NS}}}tr"
_W_CELL = f"{{{_W_NS}}}tc"
_W_TABLE_PROPERTIES = f"{{{_W_NS}}}tblPr"
_W_TABLE_GRID = f"{{{_W_NS}}}tblGrid"
_W_ROW_PROPERTIES = f"{{{_W_NS}}}trPr"
_W_CELL_PROPERTIES = f"{{{_W_NS}}}tcPr"
_W_GRID_SPAN = f"{{{_W_NS}}}gridSpan"
_W_VERTICAL_MERGE = f"{{{_W_NS}}}vMerge"
_W_GRID_BEFORE = f"{{{_W_NS}}}gridBefore"
_W_GRID_AFTER = f"{{{_W_NS}}}gridAfter"
_W_VAL = f"{{{_W_NS}}}val"
_RELATIONSHIP = f"{{{_REL_NS}}}Relationship"

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
_PARAGRAPH_PROPERTY_REVISION_TAGS = frozenset(
    {_W_RUN_PROPERTIES_CHANGE, _W_PARAGRAPH_PROPERTIES_CHANGE}
)
_TABLE_PROPERTY_REVISION_TAGS = frozenset(
    {
        _W_TABLE_PROPERTIES_CHANGE,
        _W_ROW_PROPERTIES_CHANGE,
        _W_CELL_PROPERTIES_CHANGE,
    }
)
_PARAGRAPH_REVISION_TAGS = (
    _CONTENT_REVISION_TAGS | _PARAGRAPH_PROPERTY_REVISION_TAGS
)
_COMMENT_TAGS = frozenset(
    {_W_COMMENT_RANGE_START, _W_COMMENT_RANGE_END, _W_COMMENT_REFERENCE}
)
_UNSUPPORTED_PARAGRAPH_TAGS = frozenset(
    {_W_DRAWING, _W_OBJECT, _W_PICTURE, _W_CONTENT_CONTROL, _W_ALT_CHUNK, _W_TABLE}
)
_FALSE_VALUES = frozenset({"0", "false", "off", "no", "none"})


@dataclass
class _InspectionState:
    request: InspectDocumentRequest
    warnings: list[DocumentWarning] = field(default_factory=list)
    warning_keys: set[tuple[str, str, str | None, str | None]] = field(
        default_factory=set
    )
    block_count: int = 0
    text_chars: int = 0
    run_count: int = 0
    table_rows: int = 0
    table_cells: int = 0

    def add_warning(
        self,
        warning_type: str,
        message: str,
        *,
        part: str | None = None,
        block_id: str | None = None,
    ) -> None:
        key = (warning_type, message, part, block_id)
        if key in self.warning_keys:
            return
        self.warning_keys.add(key)
        self.warnings.append(
            DocumentWarning(
                warning_type=warning_type,
                message=message,
                part=part,
                block_id=block_id,
            )
        )

    def consume_block(self) -> None:
        self.block_count += 1
        if self.block_count > DOCX_LIMITS.max_blocks:
            raise DocxError("docx_limit_exceeded", "DOCX 正文 block 数量超过安全限制。")
        if (
            self.request.max_blocks is not None
            and self.block_count > self.request.max_blocks
        ):
            raise DocxError("inspect_limit_exceeded", "DOCX 正文 block 数量超过请求限制。")

    def consume_text(self, text: str) -> None:
        self.text_chars += len(text)
        if self.text_chars > DOCX_LIMITS.max_text_chars:
            raise DocxError("docx_limit_exceeded", "DOCX 可见文本总长度超过安全限制。")
        if (
            self.request.max_text_chars is not None
            and self.text_chars > self.request.max_text_chars
        ):
            raise DocxError("inspect_limit_exceeded", "DOCX 可见文本总长度超过请求限制。")

    def consume_run(self) -> None:
        self.run_count += 1
        if self.run_count > DOCX_LIMITS.max_runs:
            raise DocxError("docx_limit_exceeded", "DOCX run 数量超过安全限制。")

    def consume_row(self) -> None:
        self.table_rows += 1
        if self.table_rows > DOCX_LIMITS.max_table_rows:
            raise DocxError("docx_limit_exceeded", "DOCX 表格行数超过安全限制。")

    def consume_cell(self) -> None:
        self.table_cells += 1
        if self.table_cells > DOCX_LIMITS.max_table_cells:
            raise DocxError("docx_limit_exceeded", "DOCX 表格单元格数量超过安全限制。")


class DocxReader:
    """安全读取 `.docx` 并生成不依赖 Node 的文档快照。"""

    def inspect(self, request: InspectDocumentRequest) -> DocumentSnapshot:
        """读取源文件，不修改源文件或其中任何 OOXML part。"""

        validated_request = _validate_request(request)
        with DocxPackage.open(validated_request.source_path) as package:
            state = _InspectionState(request=validated_request)
            metadata = _read_metadata(package, state)
            _inspect_relationships(package, state)
            _inspect_package_features(package, state)

            document_root = package.read_xml("word/document.xml")
            body = document_root.find(_W_BODY)
            if body is None:
                raise DocxError("invalid_docx_package", "word/document.xml 缺少 w:body。")
            if any(
                element.tag == _W_SECTION_PROPERTIES_CHANGE
                for element in document_root.iter()
            ):
                state.add_warning(
                    "tracked_changes",
                    "文档包含 section 属性修订。",
                    part="word/document.xml",
                )

            blocks: list[ParagraphSnapshot | TableSnapshot] = []
            paragraph_count = 0
            table_count = 0
            paragraph_index = 0
            table_index = 0

            for child in body:
                if child.tag == _W_PARAGRAPH:
                    state.consume_block()
                    block_id = f"body:p:{paragraph_index}"
                    paragraph_index += 1
                    paragraph_count += 1
                    blocks.append(
                        _read_paragraph(
                            child,
                            block_id=block_id,
                            state=state,
                            include_runs=validated_request.include_runs,
                        )
                    )
                elif child.tag == _W_TABLE:
                    state.consume_block()
                    block_id = f"body:table:{table_index}"
                    table_index += 1
                    table_count += 1
                    table = _read_table(child, block_id=block_id, state=state)
                    if validated_request.include_tables:
                        blocks.append(table)
                elif child.tag == _W_SECTION_PROPERTIES:
                    continue
                else:
                    state.add_warning(
                        "unsupported_content",
                        "正文包含尚未建模的顶层 OOXML 内容。",
                        part="word/document.xml",
                    )

            section_count = sum(
                1 for element in document_root.iter() if element.tag == _W_SECTION_PROPERTIES
            )
            image_count = sum(
                1
                for part_name in package.part_names
                if part_name.lower().startswith("word/media/")
                and not part_name.endswith("/")
            )
            return DocumentSnapshot(
                source_path=package.source_path,
                revision=package.revision,
                size_bytes=package.size_bytes,
                metadata=metadata,
                blocks=blocks,
                warnings=state.warnings,
                paragraph_count=paragraph_count,
                table_count=table_count,
                image_count=image_count,
                section_count=section_count,
            )


def inspect_document(request: InspectDocumentRequest) -> DocumentSnapshot:
    """使用一次性 reader 读取现有 DOCX。"""

    return DocxReader().inspect(request)


def _validate_request(request: InspectDocumentRequest) -> InspectDocumentRequest:
    if not isinstance(request, InspectDocumentRequest):
        raise DocxError("invalid_request", "request 必须是 InspectDocumentRequest。")
    if not isinstance(request.source_path, (str, os.PathLike)):
        raise DocxError("invalid_request", "source_path 必须是文件系统路径。")
    if not isinstance(request.include_runs, bool):
        raise DocxError("invalid_request", "include_runs 必须是布尔值。")
    if not isinstance(request.include_tables, bool):
        raise DocxError("invalid_request", "include_tables 必须是布尔值。")
    _validate_request_limit(
        request.max_blocks,
        field_name="max_blocks",
        hard_limit=DOCX_LIMITS.max_blocks,
    )
    _validate_request_limit(
        request.max_text_chars,
        field_name="max_text_chars",
        hard_limit=DOCX_LIMITS.max_text_chars,
    )
    return request


def _validate_request_limit(
    value: int | None,
    *,
    field_name: str,
    hard_limit: int,
) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DocxError("invalid_request", f"{field_name} 必须是正整数或 null。")
    if value > hard_limit:
        raise DocxError("invalid_request", f"{field_name} 不能高于模块安全上限。")


def _read_metadata(
    package: DocxPackage,
    state: _InspectionState,
) -> DocumentMetadata:
    part_name = "docProps/core.xml"
    if not package.has_part(part_name):
        state.add_warning(
            "malformed_optional_part",
            "DOCX 缺少可选的核心属性 part。",
            part=part_name,
        )
        return DocumentMetadata()
    try:
        root = package.read_xml(part_name)
    except DocxError as exc:
        if exc.error_type != "xml_parse_failed":
            raise
        state.add_warning(
            "malformed_optional_part",
            "DOCX 核心属性 part 无法解析。",
            part=part_name,
        )
        return DocumentMetadata()

    return DocumentMetadata(
        title=_element_text(root.find(f"{{{_DC_NS}}}title")),
        creator=_element_text(root.find(f"{{{_DC_NS}}}creator")),
        subject=_element_text(root.find(f"{{{_DC_NS}}}subject")),
        description=_element_text(root.find(f"{{{_DC_NS}}}description")),
        created=_element_text(root.find(f"{{{_DCTERMS_NS}}}created")),
        modified=_element_text(root.find(f"{{{_DCTERMS_NS}}}modified")),
        last_modified_by=_element_text(root.find(f"{{{_CP_NS}}}lastModifiedBy")),
    )


def _inspect_relationships(package: DocxPackage, state: _InspectionState) -> None:
    for part_name in package.part_names:
        if not part_name.lower().endswith(".rels"):
            continue
        try:
            root = package.read_xml(part_name)
        except DocxError as exc:
            if part_name == "_rels/.rels" or exc.error_type != "xml_parse_failed":
                raise
            state.add_warning(
                "malformed_optional_part",
                "可选 relationship part 无法解析。",
                part=part_name,
            )
            continue

        for relationship in root.iter():
            if relationship.tag != _RELATIONSHIP and _local_name(
                relationship.tag
            ) != "Relationship":
                continue
            if relationship.attrib.get("TargetMode", "").lower() == "external":
                state.add_warning(
                    "external_relationship",
                    "DOCX 包含未访问的外部 relationship。",
                    part=part_name,
                )


def _inspect_package_features(package: DocxPackage, state: _InspectionState) -> None:
    for part_name in package.part_names:
        lower_name = part_name.lower()
        if lower_name.startswith("word/embeddings/") and not part_name.endswith("/"):
            state.add_warning(
                "embedded_object",
                "DOCX 包含未执行的嵌入对象。",
                part=part_name,
            )
    if package.has_part("word/comments.xml"):
        state.add_warning(
            "comments_detected",
            "DOCX 包含 comments part。",
            part="word/comments.xml",
        )
        try:
            package.read_xml("word/comments.xml")
        except DocxError as exc:
            if exc.error_type != "xml_parse_failed":
                raise
            state.add_warning(
                "malformed_optional_part",
                "可选 comments part 无法解析。",
                part="word/comments.xml",
            )


def _read_paragraph(
    paragraph: ElementTree.Element,
    *,
    block_id: str,
    state: _InspectionState,
    include_runs: bool,
) -> ParagraphSnapshot:
    warning_types: list[str] = []
    paragraph_tags = {element.tag for element in paragraph.iter()}

    if _W_HYPERLINK in paragraph_tags:
        _mark_block_warning(
            state,
            warning_types,
            "complex_hyperlink",
            "段落包含未访问的 hyperlink。",
            block_id,
        )
    if paragraph_tags.intersection({_W_SIMPLE_FIELD, _W_FIELD_CHAR, _W_INSTRUCTION_TEXT}):
        _mark_block_warning(
            state,
            warning_types,
            "field_code",
            "段落包含字段代码，仅返回可见结果文本。",
            block_id,
        )
    if paragraph_tags.intersection(_PARAGRAPH_REVISION_TAGS):
        _mark_block_warning(
            state,
            warning_types,
            "tracked_changes",
            "段落包含未接受或拒绝的修订标记。",
            block_id,
        )
    if paragraph_tags.intersection(_COMMENT_TAGS):
        _mark_block_warning(
            state,
            warning_types,
            "comments_detected",
            "段落包含批注锚点。",
            block_id,
        )
    if paragraph_tags.intersection(_UNSUPPORTED_PARAGRAPH_TAGS):
        _mark_block_warning(
            state,
            warning_types,
            "unsupported_content",
            "段落包含未完整建模的 OOXML 内容。",
            block_id,
        )

    runs: list[TextRunSnapshot] = []
    text_parts: list[str] = []
    for _ in paragraph.iter(_W_RUN):
        state.consume_run()
    for run_element in _iter_visible_runs(paragraph):
        run_text = _read_run_text(run_element)
        state.consume_text(run_text)
        text_parts.append(run_text)
        if include_runs and run_text:
            bold, italic, underline = _read_run_properties(run_element)
            runs.append(
                TextRunSnapshot(
                    text=run_text,
                    bold=bold,
                    italic=italic,
                    underline=underline,
                )
            )

    paragraph_properties = paragraph.find(_W_PARAGRAPH_PROPERTIES)
    style = _child_property_value(paragraph_properties, _W_PARAGRAPH_STYLE)
    alignment = _child_property_value(paragraph_properties, _W_JUSTIFICATION)
    return ParagraphSnapshot(
        block_id=block_id,
        text="".join(text_parts),
        style=style,
        alignment=alignment,
        runs=runs,
        editable=not warning_types,
        warnings=warning_types,
    )


def _read_run_text(run: ElementTree.Element) -> str:
    parts: list[str] = []
    for element in run.iter():
        if element.tag == _W_TEXT:
            parts.append(element.text or "")
        elif element.tag == _W_TAB:
            parts.append("\t")
        elif element.tag in {_W_BREAK, _W_CARRIAGE_RETURN}:
            parts.append("\n")
    return "".join(parts)


def _iter_visible_runs(
    element: ElementTree.Element,
) -> Iterator[ElementTree.Element]:
    """按当前结果视图遍历 run，并跳过删除、移出和旧属性快照。"""

    pending = list(reversed(element))
    while pending:
        child = pending.pop()
        if (
            child.tag in _HIDDEN_CONTENT_REVISION_TAGS
            or child.tag in _PROPERTY_REVISION_TAGS
        ):
            continue
        if child.tag == _W_RUN:
            yield child
        else:
            pending.extend(reversed(child))


def _read_run_properties(
    run: ElementTree.Element,
) -> tuple[bool | None, bool | None, bool | None]:
    properties = run.find(_W_RUN_PROPERTIES)
    return (
        _read_toggle(properties, _W_BOLD),
        _read_toggle(properties, _W_ITALIC),
        _read_toggle(properties, _W_UNDERLINE),
    )


def _read_toggle(
    properties: ElementTree.Element | None,
    property_tag: str,
) -> bool | None:
    if properties is None:
        return None
    value_element = properties.find(property_tag)
    if value_element is None:
        return None
    value = value_element.attrib.get(_W_VAL, value_element.attrib.get("val"))
    if value is None:
        return True
    return value.lower() not in _FALSE_VALUES


def _read_table(
    table: ElementTree.Element,
    *,
    block_id: str,
    state: _InspectionState,
) -> TableSnapshot:
    table_warning_types: list[str] = []
    table_tags = {element.tag for element in table.iter()}
    table_has_property_revision = bool(
        table_tags.intersection(_TABLE_PROPERTY_REVISION_TAGS)
    )
    if table_has_property_revision:
        _mark_block_warning(
            state,
            table_warning_types,
            "tracked_changes",
            "表格包含属性修订。",
            block_id,
        )
    row_elements, wrapped_rows = _collect_table_rows(table)
    if wrapped_rows:
        _mark_block_warning(
            state,
            table_warning_types,
            "tracked_changes",
            "表格行包含未接受或拒绝的修订标记。",
            block_id,
        )
    if not row_elements:
        _mark_block_warning(
            state,
            table_warning_types,
            "unsupported_content",
            "表格不包含可读取的普通行。",
            block_id,
        )
    for child in table:
        if child.tag not in {
            _W_TABLE_PROPERTIES,
            _W_TABLE_GRID,
            _W_ROW,
            *_CONTENT_REVISION_TAGS,
        }:
            _mark_block_warning(
                state,
                table_warning_types,
                "unsupported_content",
                "表格包含未完整建模的顶层 OOXML 内容。",
                block_id,
            )

    rows: list[list[TableCellSnapshot]] = []
    row_lengths: list[int] = []
    for row_index, row_element in enumerate(row_elements):
        state.consume_row()
        cells, wrapped_cells = _collect_row_cells(row_element)
        row_has_property_revision = any(
            element.tag == _W_ROW_PROPERTIES_CHANGE for element in row_element.iter()
        )
        row_snapshot: list[TableCellSnapshot] = []
        row_lengths.append(len(cells))
        if not cells or wrapped_cells or _row_has_grid_offsets(row_element):
            _mark_block_warning(
                state,
                table_warning_types,
                "unsupported_content",
                "表格行包含复杂或不规则的单元格布局。",
                block_id,
            )

        for cell_index, cell_element in enumerate(cells):
            state.consume_cell()
            cell_id = f"{block_id}:row:{row_index}:cell:{cell_index}"
            cell_snapshot = _read_table_cell(
                cell_element,
                block_id=cell_id,
                state=state,
                inherited_property_revision=(
                    _W_TABLE_PROPERTIES_CHANGE in table_tags
                    or row_has_property_revision
                ),
            )
            row_snapshot.append(cell_snapshot)
            for warning_type in cell_snapshot.warnings:
                _add_warning_type(table_warning_types, warning_type)
        rows.append(row_snapshot)

    if len(set(row_lengths)) > 1:
        _mark_block_warning(
            state,
            table_warning_types,
            "unsupported_content",
            "表格的行列数量不规则。",
            block_id,
        )
    column_count = max(row_lengths, default=0)
    editable = not table_warning_types and all(
        cell.editable for row in rows for cell in row
    )
    return TableSnapshot(
        block_id=block_id,
        rows=rows,
        row_count=len(rows),
        column_count=column_count,
        editable=editable,
        warnings=table_warning_types,
    )


def _read_table_cell(
    cell: ElementTree.Element,
    *,
    block_id: str,
    state: _InspectionState,
    inherited_property_revision: bool = False,
) -> TableCellSnapshot:
    warning_types: list[str] = []
    cell_has_property_revision = any(
        element.tag == _W_CELL_PROPERTIES_CHANGE for element in cell.iter()
    )
    if inherited_property_revision or cell_has_property_revision:
        _mark_block_warning(
            state,
            warning_types,
            "tracked_changes",
            "单元格所属表格结构包含属性修订。",
            block_id,
        )
    cell_properties = cell.find(_W_CELL_PROPERTIES)
    if _cell_has_merge(cell_properties):
        _mark_block_warning(
            state,
            warning_types,
            "unsupported_content",
            "单元格包含水平或垂直合并。",
            block_id,
        )

    paragraphs, nested_tables = _collect_cell_paragraphs(cell)
    if nested_tables:
        _mark_block_warning(
            state,
            warning_types,
            "unsupported_content",
            "单元格包含未展开的嵌套表格。",
            block_id,
        )
        for row in cell.iter(_W_ROW):
            state.consume_row()
        for nested_cell in cell.iter(_W_CELL):
            if nested_cell is not cell:
                state.consume_cell()

    paragraph_texts: list[str] = []
    for paragraph in paragraphs:
        paragraph_snapshot = _read_paragraph(
            paragraph,
            block_id=block_id,
            state=state,
            include_runs=False,
        )
        paragraph_texts.append(paragraph_snapshot.text)
        for warning_type in paragraph_snapshot.warnings:
            _add_warning_type(warning_types, warning_type)

    return TableCellSnapshot(
        block_id=block_id,
        text="\n".join(paragraph_texts),
        paragraphs=paragraph_texts,
        editable=not warning_types,
        warnings=warning_types,
    )


def _collect_table_rows(
    table: ElementTree.Element,
) -> tuple[list[ElementTree.Element], bool]:
    rows: list[ElementTree.Element] = []
    wrapped_rows = False
    for child in table:
        if child.tag == _W_ROW:
            rows.append(child)
        elif child.tag in _CONTENT_REVISION_TAGS:
            wrapped_rows = True
            if child.tag not in _HIDDEN_CONTENT_REVISION_TAGS:
                rows.extend(
                    element for element in child if element.tag == _W_ROW
                )
    return rows, wrapped_rows


def _collect_row_cells(
    row: ElementTree.Element,
) -> tuple[list[ElementTree.Element], bool]:
    cells: list[ElementTree.Element] = []
    wrapped_cells = False
    for child in row:
        if child.tag == _W_CELL:
            cells.append(child)
        elif child.tag in _CONTENT_REVISION_TAGS:
            wrapped_cells = True
            if child.tag not in _HIDDEN_CONTENT_REVISION_TAGS:
                cells.extend(
                    element for element in child if element.tag == _W_CELL
                )
    return cells, wrapped_cells


def _collect_cell_paragraphs(
    cell: ElementTree.Element,
) -> tuple[list[ElementTree.Element], list[ElementTree.Element]]:
    paragraphs: list[ElementTree.Element] = []
    nested_tables: list[ElementTree.Element] = []

    def visit(element: ElementTree.Element) -> None:
        for child in element:
            if child.tag == _W_PARAGRAPH:
                paragraphs.append(child)
            elif child.tag == _W_TABLE:
                nested_tables.append(child)
                visit(child)
            elif child.tag != _W_CELL_PROPERTIES:
                visit(child)

    visit(cell)
    return paragraphs, nested_tables


def _row_has_grid_offsets(row: ElementTree.Element) -> bool:
    properties = row.find(_W_ROW_PROPERTIES)
    if properties is None:
        return False
    return (
        properties.find(_W_GRID_BEFORE) is not None
        or properties.find(_W_GRID_AFTER) is not None
    )


def _cell_has_merge(properties: ElementTree.Element | None) -> bool:
    if properties is None:
        return False
    if properties.find(_W_VERTICAL_MERGE) is not None:
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


def _child_property_value(
    properties: ElementTree.Element | None,
    child_tag: str,
) -> str | None:
    if properties is None:
        return None
    child = properties.find(child_tag)
    if child is None:
        return None
    return child.attrib.get(_W_VAL, child.attrib.get("val"))


def _mark_block_warning(
    state: _InspectionState,
    warning_types: list[str],
    warning_type: str,
    message: str,
    block_id: str,
) -> None:
    _add_warning_type(warning_types, warning_type)
    state.add_warning(
        warning_type,
        message,
        part="word/document.xml",
        block_id=block_id,
    )


def _add_warning_type(warning_types: list[str], warning_type: str) -> None:
    if warning_type not in warning_types:
        warning_types.append(warning_type)


def _element_text(element: ElementTree.Element | None) -> str | None:
    if element is None:
        return None
    return element.text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
