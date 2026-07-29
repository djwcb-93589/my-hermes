"""基于 Reader 当前可见文字快照的确定性 DOCX 搜索。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from xml.etree import ElementTree

from .errors import DocxError
from .locator import (
    BodyChildLocation,
    EditTargetLocation,
    TableCellLocation,
    build_edit_target_index,
    get_single_cell_paragraph,
    is_strictly_editable_paragraph,
    is_strictly_editable_table_cell,
)
from .models import (
    DocumentSnapshot,
    InspectDocumentRequest,
    ParagraphSnapshot,
    SearchDocumentRequest,
    SearchDocumentResult,
    TableCellSnapshot,
    TableSnapshot,
    TextMatch,
)
from .package import DocxPackage
from .reader import DocxReader
from .textmap import VisibleTextMap, build_visible_text_map


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_BODY = f"{{{_W_NS}}}body"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_TABLE = f"{{{_W_NS}}}tbl"
_W_CELL_PROPERTIES = f"{{{_W_NS}}}tcPr"
_DOCUMENT_PART = "word/document.xml"
_MATCH_CONTEXT_CHARACTERS = 40
_MAX_MATCH_LIMIT = 10_000


@dataclass(frozen=True)
class _MappedSearchRange:
    """单个段落映射在所属 block 可见文字中的起始位置。"""

    base_offset: int
    text_map: VisibleTextMap


@dataclass(frozen=True)
class _SearchableBlock:
    block_id: str
    text: str
    ranges: tuple[_MappedSearchRange, ...]
    editable: bool
    warnings: list[str]


class DocxSearcher:
    """搜索单个内容块内的当前可见文字，不修改源 DOCX。"""

    def __init__(self, reader: DocxReader | None = None) -> None:
        self._reader = reader or DocxReader()

    def search(self, request: SearchDocumentRequest) -> SearchDocumentResult:
        """读取稳定快照并按正文顺序返回有界匹配。"""

        validated_request = _validate_request(request)
        snapshot = self._reader.inspect(
            InspectDocumentRequest(
                source_path=validated_request.source_path,
                include_runs=False,
                include_tables=validated_request.include_table_cells,
            )
        )
        with DocxPackage.open(snapshot.source_path) as package:
            if package.revision != snapshot.revision:
                raise DocxError(
                    "search_verification_failed",
                    "搜索期间源 DOCX 已发生变化，无法建立稳定搜索映射。",
                )
            document_root = package.read_xml(_DOCUMENT_PART)
            body = document_root.find(_W_BODY)
            if body is None:
                raise DocxError(
                    "invalid_docx_package",
                    "word/document.xml 缺少 w:body。",
                )
            targets = build_edit_target_index(body)
            searchable_blocks = tuple(
                _iter_searchable_blocks(
                    snapshot,
                    validated_request,
                    targets=targets,
                )
            )

        matches: list[TextMatch] = []
        total_matches = 0
        for target in searchable_blocks:
            for mapped_range in target.ranges:
                for searchable_start, searchable_end in (
                    mapped_range.text_map.searchable_ranges()
                ):
                    range_text = mapped_range.text_map.text[
                        searchable_start:searchable_end
                    ]
                    for range_start, range_end in iter_literal_matches(
                        range_text,
                        validated_request.query,
                        case_sensitive=validated_request.case_sensitive,
                        whole_word=validated_request.whole_word,
                    ):
                        local_start = searchable_start + range_start
                        local_end = searchable_start + range_end
                        try:
                            text_range = mapped_range.text_map.resolve_range(
                                local_start,
                                local_end,
                            )
                        except ValueError as exc:
                            raise DocxError(
                                "search_verification_failed",
                                "搜索结果无法映射回当前 DOCX 的可见文字节点。",
                            ) from exc

                        start = mapped_range.base_offset + local_start
                        end = mapped_range.base_offset + local_end
                        total_matches += 1
                        if len(matches) >= validated_request.max_matches:
                            continue
                        matched_text = target.text[start:end]
                        match_warnings = list(target.warnings)
                        if not text_range.uniform_format:
                            match_warnings = _merge_warning_types(
                                match_warnings,
                                ["format_boundary"],
                            )
                        if len(text_range.affected_run_indexes) > 1:
                            match_warnings = _merge_warning_types(
                                match_warnings,
                                ["multi_run_match"],
                            )
                        matches.append(
                            TextMatch(
                                match_id=build_match_id(
                                    snapshot.revision,
                                    target.block_id,
                                    start,
                                    end,
                                    matched_text,
                                ),
                                block_id=target.block_id,
                                matched_text=matched_text,
                                start=start,
                                end=end,
                                prefix=target.text[
                                    max(
                                        0,
                                        start - _MATCH_CONTEXT_CHARACTERS,
                                    ) : start
                                ],
                                suffix=target.text[
                                    end : end + _MATCH_CONTEXT_CHARACTERS
                                ],
                                editable=(
                                    target.editable
                                    and text_range.uniform_format
                                ),
                                warnings=match_warnings,
                            )
                        )

        return SearchDocumentResult(
            source_path=snapshot.source_path,
            revision=snapshot.revision,
            query=validated_request.query,
            matches=matches,
            total_matches=total_matches,
        )


def search_document(request: SearchDocumentRequest) -> SearchDocumentResult:
    """使用一次性 Searcher 实例搜索现有 DOCX。"""

    return DocxSearcher().search(request)


def build_match_id(
    revision: str,
    block_id: str,
    start: int,
    end: int,
    matched_text: str,
) -> str:
    """根据 revision、位置和原文生成确定性 match_id。"""

    payload = json.dumps(
        [revision, block_id, start, end, matched_text],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"match:{hashlib.sha256(payload).hexdigest()}"


def iter_literal_matches(
    text: str,
    query: str,
    *,
    case_sensitive: bool,
    whole_word: bool,
) -> Iterator[tuple[int, int]]:
    """返回原字符串上的非重叠字面量匹配区间。"""

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    for match in pattern.finditer(text):
        start, end = match.span()
        if whole_word and not _has_word_boundaries(text, start, end):
            continue
        yield start, end


def _validate_request(request: SearchDocumentRequest) -> SearchDocumentRequest:
    if not isinstance(request, SearchDocumentRequest):
        raise DocxError(
            "invalid_request",
            "request 必须是 SearchDocumentRequest。",
        )
    if not isinstance(request.query, str) or not request.query:
        raise DocxError("invalid_request", "query 必须是非空字符串。")
    boolean_fields = (
        request.case_sensitive,
        request.whole_word,
        request.include_paragraphs,
        request.include_table_cells,
    )
    if not all(isinstance(value, bool) for value in boolean_fields):
        raise DocxError("invalid_request", "搜索开关必须是布尔值。")
    if not request.include_paragraphs and not request.include_table_cells:
        raise DocxError("invalid_request", "至少需要启用一种搜索范围。")
    if (
        isinstance(request.max_matches, bool)
        or not isinstance(request.max_matches, int)
        or request.max_matches <= 0
        or request.max_matches > _MAX_MATCH_LIMIT
    ):
        raise DocxError(
            "invalid_request",
            f"max_matches 必须是 1 到 {_MAX_MATCH_LIMIT} 之间的整数。",
        )
    return request


def _iter_searchable_blocks(
    snapshot: DocumentSnapshot,
    request: SearchDocumentRequest,
    *,
    targets: dict[str, EditTargetLocation],
) -> Iterator[_SearchableBlock]:
    for block in snapshot.blocks:
        if isinstance(block, ParagraphSnapshot):
            if request.include_paragraphs:
                location = targets.get(block.block_id)
                if (
                    not isinstance(location, BodyChildLocation)
                    or location.kind != "paragraph"
                ):
                    raise DocxError(
                        "search_verification_failed",
                        "Reader 与 locator 的段落定位结果不一致。",
                    )
                text_map = build_visible_text_map(location.element)
                if text_map.text != block.text:
                    raise DocxError(
                        "search_verification_failed",
                        "Reader 与文字映射的段落可见内容不一致。",
                    )
                locator_editable = is_strictly_editable_paragraph(
                    location.element
                )
                yield _SearchableBlock(
                    block_id=block.block_id,
                    text=block.text,
                    ranges=(
                        _MappedSearchRange(
                            base_offset=0,
                            text_map=text_map,
                        ),
                    ),
                    editable=block.editable and locator_editable,
                    warnings=list(block.warnings),
                )
        elif isinstance(block, TableSnapshot) and request.include_table_cells:
            for row in block.rows:
                for cell in row:
                    location = targets.get(cell.block_id)
                    if (
                        not isinstance(location, TableCellLocation)
                        or location.block_id != cell.block_id
                        or not location.block_id.startswith(
                            f"{block.block_id}:row:"
                        )
                    ):
                        raise DocxError(
                            "search_verification_failed",
                            "Reader 与 locator 的表格单元格定位结果不一致。",
                        )
                    mapped_ranges = _build_cell_mapped_ranges(
                        cell,
                        location=location,
                    )
                    single_paragraph = get_single_cell_paragraph(location)
                    locator_editable = (
                        is_strictly_editable_table_cell(location)
                        and len(mapped_ranges) == 1
                        and single_paragraph
                        is mapped_ranges[0].text_map.paragraph
                    )
                    yield _SearchableBlock(
                        block_id=cell.block_id,
                        text=cell.text,
                        ranges=mapped_ranges,
                        editable=(
                            block.editable
                            and cell.editable
                            and locator_editable
                        ),
                        warnings=_merge_warning_types(
                            block.warnings,
                            cell.warnings,
                        ),
                    )


def _build_cell_mapped_ranges(
    cell: TableCellSnapshot,
    *,
    location: TableCellLocation,
) -> tuple[_MappedSearchRange, ...]:
    paragraphs = _collect_cell_paragraphs(location.cell)
    if len(paragraphs) != len(cell.paragraphs):
        raise DocxError(
            "search_verification_failed",
            "Reader 与文字映射的单元格段落数量不一致。",
        )

    ranges: list[_MappedSearchRange] = []
    offset = 0
    mapped_texts: list[str] = []
    for paragraph, paragraph_text in zip(
        paragraphs,
        cell.paragraphs,
        strict=True,
    ):
        text_map = build_visible_text_map(paragraph)
        if text_map.text != paragraph_text:
            raise DocxError(
                "search_verification_failed",
                "Reader 与文字映射的单元格可见内容不一致。",
            )
        ranges.append(
            _MappedSearchRange(
                base_offset=offset,
                text_map=text_map,
            )
        )
        mapped_texts.append(text_map.text)
        offset += len(text_map.text) + 1
    if "\n".join(mapped_texts) != cell.text:
        raise DocxError(
            "search_verification_failed",
            "Reader 与文字映射的单元格文字不一致。",
        )
    return tuple(ranges)


def _collect_cell_paragraphs(
    cell: ElementTree.Element,
) -> list[ElementTree.Element]:
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
    return paragraphs


def _merge_warning_types(*warning_groups: list[str]) -> list[str]:
    merged: list[str] = []
    for warning_group in warning_groups:
        for warning_type in warning_group:
            if warning_type not in merged:
                merged.append(warning_type)
    return merged


def _has_word_boundaries(text: str, start: int, end: int) -> bool:
    before_is_word = start > 0 and _is_word_character(text[start - 1])
    after_is_word = end < len(text) and _is_word_character(text[end])
    return not before_is_word and not after_is_word


def _is_word_character(character: str) -> bool:
    return character == "_" or character.isalnum()
