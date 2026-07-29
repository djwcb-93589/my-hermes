"""基于 Reader 当前可见文字快照的确定性 DOCX 搜索。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import DocxError
from .models import (
    DocumentSnapshot,
    InspectDocumentRequest,
    ParagraphSnapshot,
    SearchDocumentRequest,
    SearchDocumentResult,
    TableSnapshot,
    TextMatch,
)
from .reader import DocxReader


_MATCH_CONTEXT_CHARACTERS = 40
_MAX_MATCH_LIMIT = 10_000


@dataclass(frozen=True)
class _SearchableBlock:
    block_id: str
    text: str
    ranges: tuple[tuple[int, str], ...]
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

        matches: list[TextMatch] = []
        total_matches = 0
        for target in _iter_searchable_blocks(snapshot, validated_request):
            for base_offset, range_text in target.ranges:
                for range_start, range_end in iter_literal_matches(
                    range_text,
                    validated_request.query,
                    case_sensitive=validated_request.case_sensitive,
                    whole_word=validated_request.whole_word,
                ):
                    start = base_offset + range_start
                    end = base_offset + range_end
                    total_matches += 1
                    if len(matches) >= validated_request.max_matches:
                        continue
                    matched_text = target.text[start:end]
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
                                max(0, start - _MATCH_CONTEXT_CHARACTERS) : start
                            ],
                            suffix=target.text[
                                end : end + _MATCH_CONTEXT_CHARACTERS
                            ],
                            editable=target.editable,
                            warnings=list(target.warnings),
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
) -> Iterator[_SearchableBlock]:
    for block in snapshot.blocks:
        if isinstance(block, ParagraphSnapshot):
            if request.include_paragraphs:
                yield _SearchableBlock(
                    block_id=block.block_id,
                    text=block.text,
                    ranges=((0, block.text),),
                    editable=block.editable,
                    warnings=list(block.warnings),
                )
        elif isinstance(block, TableSnapshot) and request.include_table_cells:
            for row in block.rows:
                for cell in row:
                    yield _SearchableBlock(
                        block_id=cell.block_id,
                        text=cell.text,
                        ranges=_cell_search_ranges(cell.paragraphs),
                        editable=block.editable and cell.editable,
                        warnings=_merge_warning_types(
                            block.warnings,
                            cell.warnings,
                        ),
                    )


def _cell_search_ranges(paragraphs: list[str]) -> tuple[tuple[int, str], ...]:
    ranges: list[tuple[int, str]] = []
    offset = 0
    for paragraph_text in paragraphs:
        ranges.append((offset, paragraph_text))
        offset += len(paragraph_text) + 1
    return tuple(ranges)


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
