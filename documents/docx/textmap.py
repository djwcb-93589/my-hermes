"""DOCX 段落可见文字与普通 run/XML 节点之间的内部映射。"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_RUN = f"{{{_W_NS}}}r"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_VANISH = f"{{{_W_NS}}}vanish"
_W_TEXT = f"{{{_W_NS}}}t"
_W_TAB = f"{{{_W_NS}}}tab"
_W_BREAK = f"{{{_W_NS}}}br"
_W_CARRIAGE_RETURN = f"{{{_W_NS}}}cr"
_W_TYPE = f"{{{_W_NS}}}type"
_W_DELETION = f"{{{_W_NS}}}del"
_W_MOVE_FROM = f"{{{_W_NS}}}moveFrom"
_W_RUN_PROPERTIES_CHANGE = f"{{{_W_NS}}}rPrChange"
_W_PARAGRAPH_PROPERTIES_CHANGE = f"{{{_W_NS}}}pPrChange"
_W_TABLE_PROPERTIES_CHANGE = f"{{{_W_NS}}}tblPrChange"
_W_ROW_PROPERTIES_CHANGE = f"{{{_W_NS}}}trPrChange"
_W_CELL_PROPERTIES_CHANGE = f"{{{_W_NS}}}tcPrChange"
_W_SECTION_PROPERTIES_CHANGE = f"{{{_W_NS}}}sectPrChange"
_W_VAL = f"{{{_W_NS}}}val"
_XML_SPACE = f"{{{_XML_NS}}}space"

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
_FALSE_VALUES = frozenset({"0", "false", "off", "no", "none"})


@dataclass(frozen=True)
class VisibleTextSegment:
    """一个连续可见片段在 XML 节点和段落字符串中的位置。"""

    start: int
    end: int
    run_index: int
    element: ElementTree.Element
    kind: Literal["text", "tab", "break", "page_break", "column_break"]


@dataclass(frozen=True)
class VisibleRun:
    """一个可见 run 的文字区间及直接格式标识。"""

    index: int
    start: int
    end: int
    text: str
    element: ElementTree.Element
    format_key: bytes


@dataclass(frozen=True)
class VisibleTextRange:
    """一个非空可见文字范围解析到的 run 和节点边界。"""

    start: int
    end: int
    start_segment: VisibleTextSegment
    end_segment: VisibleTextSegment
    start_run: VisibleRun
    end_run: VisibleRun
    affected_run_indexes: tuple[int, ...]
    uniform_format: bool


@dataclass(frozen=True)
class VisibleTextReplacement:
    """一个基于原始 VisibleTextMap 坐标的局部替换。"""

    start: int
    end: int
    replacement: str
    preserve_format: bool


@dataclass(frozen=True)
class VisibleTextMap:
    """段落连续可见字符串及其字符来源。"""

    paragraph: ElementTree.Element
    text: str
    runs: tuple[VisibleRun, ...]
    segments: tuple[VisibleTextSegment, ...]

    def searchable_ranges(self) -> tuple[tuple[int, int], ...]:
        """返回不包含显式分页或分栏符的可搜索半开区间。"""

        ranges: list[tuple[int, int]] = []
        range_start = 0
        for segment in self.segments:
            if segment.kind not in {"page_break", "column_break"}:
                continue
            if range_start < segment.start:
                ranges.append((range_start, segment.start))
            range_start = segment.end
        if range_start < len(self.text):
            ranges.append((range_start, len(self.text)))
        return tuple(ranges)

    def is_searchable_range(self, start: int, end: int) -> bool:
        """判断非空范围是否完整落在单个可搜索区间内。"""

        return any(
            range_start <= start and end <= range_end
            for range_start, range_end in self.searchable_ranges()
        )

    def resolve_range(self, start: int, end: int) -> VisibleTextRange:
        """把非空半开区间解析到首尾节点和受影响 run。"""

        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(self.text)
        ):
            raise ValueError("visible text range is invalid")

        start_segment = next(
            (
                segment
                for segment in self.segments
                if segment.start <= start < segment.end
            ),
            None,
        )
        end_character = end - 1
        end_segment = next(
            (
                segment
                for segment in self.segments
                if segment.start <= end_character < segment.end
            ),
            None,
        )
        if start_segment is None or end_segment is None:
            raise ValueError("visible text range cannot be mapped")

        affected_run_indexes = tuple(
            dict.fromkeys(
                segment.run_index
                for segment in self.segments
                if segment.end > start and segment.start < end
            )
        )
        if not affected_run_indexes:
            raise ValueError("visible text range has no run")
        affected_formats = {
            self.runs[run_index].format_key
            for run_index in affected_run_indexes
        }
        return VisibleTextRange(
            start=start,
            end=end,
            start_segment=start_segment,
            end_segment=end_segment,
            start_run=self.runs[start_segment.run_index],
            end_run=self.runs[end_segment.run_index],
            affected_run_indexes=affected_run_indexes,
            uniform_format=len(affected_formats) == 1,
        )


def build_visible_text_map(paragraph: ElementTree.Element) -> VisibleTextMap:
    """按 Word 当前结果视图构建跨 run 的连续文字映射。"""

    if paragraph.tag != _W_PARAGRAPH:
        raise ValueError("text map requires a paragraph")

    cursor = 0
    runs: list[VisibleRun] = []
    segments: list[VisibleTextSegment] = []
    text_parts: list[str] = []
    for run_index, run in enumerate(_iter_visible_runs(paragraph)):
        run_start = cursor
        run_parts: list[str] = []
        for element, kind, value in _iter_visible_run_content(run):
            if not value:
                continue
            segment_start = cursor
            cursor += len(value)
            segments.append(
                VisibleTextSegment(
                    start=segment_start,
                    end=cursor,
                    run_index=run_index,
                    element=element,
                    kind=kind,
                )
            )
            run_parts.append(value)
            text_parts.append(value)
        run_text = "".join(run_parts)
        runs.append(
            VisibleRun(
                index=run_index,
                start=run_start,
                end=cursor,
                text=run_text,
                element=run,
                format_key=_read_run_format_key(run),
            )
        )
    return VisibleTextMap(
        paragraph=paragraph,
        text="".join(text_parts),
        runs=tuple(runs),
        segments=tuple(segments),
    )


def replace_visible_text_range(
    text_map: VisibleTextMap,
    *,
    start: int,
    end: int,
    replacement: str,
    preserve_format: bool,
) -> bool:
    """仅重写匹配覆盖的普通 run，并保留范围外文字及其格式。"""

    if not text_map.is_searchable_range(start, end):
        raise ValueError("visible text range crosses an explicit break")
    text_range = text_map.resolve_range(start, end)
    if preserve_format and not text_range.uniform_format:
        raise ValueError("visible text range crosses different run formats")

    original_text = text_map.text[start:end]
    if original_text == replacement and text_range.uniform_format:
        return False

    if (
        text_range.start_segment is text_range.end_segment
        and text_range.start_segment.kind == "text"
        and "\t" not in replacement
        and "\n" not in replacement
    ):
        return _replace_within_text_node(
            text_map.paragraph,
            text_range.start_segment,
            start=start,
            end=end,
            replacement=replacement,
        )

    start_run = text_range.start_run
    end_run = text_range.end_run
    prefix = start_run.text[: start - start_run.start]
    suffix = end_run.text[end - end_run.start :]
    same_boundary_format = start_run.format_key == end_run.format_key

    replacement_runs: list[ElementTree.Element] = []
    if same_boundary_format:
        replacement_runs.append(
            _create_run(start_run.element, prefix + replacement + suffix)
        )
    else:
        start_text = prefix + replacement
        if start_text:
            replacement_runs.append(_create_run(start_run.element, start_text))
        if suffix:
            replacement_runs.append(_create_run(end_run.element, suffix))
        if not replacement_runs:
            replacement_runs.append(_create_run(start_run.element, ""))

    paragraph_children = list(text_map.paragraph)
    start_child_index = _identity_index(paragraph_children, start_run.element)
    end_child_index = _identity_index(paragraph_children, end_run.element)
    if start_child_index < 0 or end_child_index < start_child_index:
        raise ValueError("mapped runs are not direct paragraph children")
    affected_children = paragraph_children[start_child_index : end_child_index + 1]
    if any(child.tag != _W_RUN for child in affected_children):
        raise ValueError("mapped range crosses non-run paragraph content")

    before = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
    for child in affected_children:
        text_map.paragraph.remove(child)
    for offset, run in enumerate(replacement_runs):
        text_map.paragraph.insert(start_child_index + offset, run)
    after = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
    return before != after


def replace_visible_text_ranges(
    text_map: VisibleTextMap,
    replacements: tuple[VisibleTextReplacement, ...],
) -> bool:
    """基于一份原始映射统一写回多个不重叠局部替换。"""

    if not replacements:
        return False
    descending = tuple(
        sorted(
            replacements,
            key=lambda item: (item.start, item.end),
            reverse=True,
        )
    )
    ordered = tuple(reversed(descending))
    resolved_ranges: list[VisibleTextRange] = []
    previous_end = -1
    for replacement in ordered:
        if replacement.start < previous_end:
            raise ValueError("visible text replacements overlap")
        if not text_map.is_searchable_range(replacement.start, replacement.end):
            raise ValueError("visible text replacement crosses an explicit break")
        text_range = text_map.resolve_range(replacement.start, replacement.end)
        if replacement.preserve_format and not text_range.uniform_format:
            raise ValueError("visible text range crosses different run formats")
        resolved_ranges.append(text_range)
        previous_end = replacement.end

    if len(ordered) == 1:
        replacement = ordered[0]
        return replace_visible_text_range(
            text_map,
            start=replacement.start,
            end=replacement.end,
            replacement=replacement.replacement,
            preserve_format=replacement.preserve_format,
        )
    if all(
        text_map.text[item.start : item.end] == item.replacement
        and text_range.uniform_format
        for item, text_range in zip(ordered, resolved_ranges, strict=True)
    ):
        return False
    resolved_pairs = tuple(zip(ordered, resolved_ranges, strict=True))
    if all(
        text_range.start_segment is text_range.end_segment
        and text_range.start_segment.kind == "text"
        and "\t" not in replacement.replacement
        and "\n" not in replacement.replacement
        for replacement, text_range in resolved_pairs
    ):
        before = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
        for replacement, text_range in reversed(resolved_pairs):
            segment = text_range.start_segment
            current = segment.element.text or ""
            local_start = replacement.start - segment.start
            local_end = replacement.end - segment.start
            segment.element.text = (
                current[:local_start]
                + replacement.replacement
                + current[local_end:]
            )
            _update_xml_space(segment.element)
        after = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
        return before != after

    first_range = resolved_ranges[0]
    last_range = resolved_ranges[-1]
    envelope_start = first_range.start_run.start
    envelope_end = last_range.end_run.end
    replacement_runs: list[ElementTree.Element] = []
    cursor = envelope_start
    for replacement, text_range in zip(
        ordered,
        resolved_ranges,
        strict=True,
    ):
        _append_original_interval_runs(
            text_map,
            start=cursor,
            end=replacement.start,
            destination=replacement_runs,
        )
        if replacement.replacement:
            replacement_runs.append(
                _create_run(
                    text_range.start_run.element,
                    replacement.replacement,
                )
            )
        cursor = replacement.end
    _append_original_interval_runs(
        text_map,
        start=cursor,
        end=envelope_end,
        destination=replacement_runs,
    )
    if not replacement_runs:
        replacement_runs.append(
            _create_run(first_range.start_run.element, "")
        )

    paragraph_children = list(text_map.paragraph)
    start_child_index = _identity_index(
        paragraph_children,
        first_range.start_run.element,
    )
    end_child_index = _identity_index(
        paragraph_children,
        last_range.end_run.element,
    )
    if start_child_index < 0 or end_child_index < start_child_index:
        raise ValueError("mapped runs are not direct paragraph children")
    affected_children = paragraph_children[start_child_index : end_child_index + 1]
    if any(child.tag != _W_RUN for child in affected_children):
        raise ValueError("mapped ranges cross non-run paragraph content")

    before = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
    for child in affected_children:
        text_map.paragraph.remove(child)
    for offset, run in enumerate(replacement_runs):
        text_map.paragraph.insert(start_child_index + offset, run)
    after = ElementTree.tostring(text_map.paragraph, encoding="utf-8")
    return before != after


def append_text_content(run: ElementTree.Element, text: str) -> None:
    """把普通文字、制表符和换行写成对应的 WordprocessingML 节点。"""

    buffer: list[str] = []
    child_created = False

    def flush_text() -> None:
        nonlocal child_created
        if not buffer:
            return
        value = "".join(buffer)
        buffer.clear()
        text_element = ElementTree.SubElement(run, _W_TEXT)
        text_element.text = value
        _update_xml_space(text_element)
        child_created = True

    for character in text:
        if character == "\t":
            flush_text()
            ElementTree.SubElement(run, _W_TAB)
            child_created = True
        elif character == "\n":
            flush_text()
            ElementTree.SubElement(run, _W_BREAK)
            child_created = True
        else:
            buffer.append(character)
    flush_text()
    if not child_created:
        empty_text = ElementTree.SubElement(run, _W_TEXT)
        empty_text.text = ""


def _iter_visible_runs(
    element: ElementTree.Element,
) -> Iterator[ElementTree.Element]:
    pending = list(reversed(element))
    while pending:
        child = pending.pop()
        if (
            child.tag in _HIDDEN_CONTENT_REVISION_TAGS
            or child.tag in _PROPERTY_REVISION_TAGS
        ):
            continue
        if child.tag == _W_RUN:
            if not _run_is_hidden(child):
                yield child
        else:
            pending.extend(reversed(child))


def _iter_visible_run_content(
    run: ElementTree.Element,
) -> Iterator[
    tuple[
        ElementTree.Element,
        Literal["text", "tab", "break", "page_break", "column_break"],
        str,
    ]
]:
    pending = list(reversed(run))
    while pending:
        element = pending.pop()
        if (
            element.tag in _HIDDEN_CONTENT_REVISION_TAGS
            or element.tag in _PROPERTY_REVISION_TAGS
            or element.tag == _W_RUN_PROPERTIES
        ):
            continue
        if element.tag == _W_TEXT:
            yield element, "text", element.text or ""
        elif element.tag == _W_TAB:
            yield element, "tab", "\t"
        elif element.tag == _W_BREAK:
            break_type = element.attrib.get(
                _W_TYPE,
                element.attrib.get("type", ""),
            ).lower()
            if break_type == "page":
                yield element, "page_break", "\n"
            elif break_type == "column":
                yield element, "column_break", "\n"
            else:
                yield element, "break", "\n"
        elif element.tag == _W_CARRIAGE_RETURN:
            yield element, "break", "\n"
        else:
            pending.extend(reversed(element))


def _read_run_format_key(run: ElementTree.Element) -> bytes:
    properties = next(
        (child for child in run if child.tag == _W_RUN_PROPERTIES),
        None,
    )
    if properties is None:
        return b""
    normalized = copy.deepcopy(properties)
    for element in normalized.iter():
        element.tail = None
        if len(element) and element.text is not None and not element.text.strip():
            element.text = None
        if element.attrib:
            sorted_attributes = sorted(element.attrib.items())
            element.attrib.clear()
            element.attrib.update(sorted_attributes)
    return ElementTree.tostring(normalized, encoding="utf-8")


def _run_is_hidden(run: ElementTree.Element) -> bool:
    properties = run.find(_W_RUN_PROPERTIES)
    if properties is None:
        return False
    vanish = properties.find(_W_VANISH)
    if vanish is None:
        return False
    raw_value = vanish.attrib.get(_W_VAL, vanish.attrib.get("val"))
    return raw_value is None or raw_value.lower() not in _FALSE_VALUES


def _replace_within_text_node(
    paragraph: ElementTree.Element,
    segment: VisibleTextSegment,
    *,
    start: int,
    end: int,
    replacement: str,
) -> bool:
    original = segment.element.text or ""
    local_start = start - segment.start
    local_end = end - segment.start
    updated = original[:local_start] + replacement + original[local_end:]
    if updated == original:
        return False

    before = ElementTree.tostring(paragraph, encoding="utf-8")
    segment.element.text = updated
    _update_xml_space(segment.element)
    after = ElementTree.tostring(paragraph, encoding="utf-8")
    return before != after


def _update_xml_space(element: ElementTree.Element) -> None:
    value = element.text or ""
    if value[:1].isspace() or value[-1:].isspace():
        element.set(_XML_SPACE, "preserve")
    else:
        element.attrib.pop(_XML_SPACE, None)


def _create_run(
    format_source: ElementTree.Element,
    text: str,
) -> ElementTree.Element:
    run = _create_run_shell(format_source)
    append_text_content(run, text)
    return run


def _create_run_shell(
    format_source: ElementTree.Element,
) -> ElementTree.Element:
    run = ElementTree.Element(_W_RUN, dict(format_source.attrib))
    properties = next(
        (child for child in format_source if child.tag == _W_RUN_PROPERTIES),
        None,
    )
    if properties is not None:
        copied_properties = copy.deepcopy(properties)
        copied_properties.tail = None
        run.append(copied_properties)
    return run


def _append_original_interval_runs(
    text_map: VisibleTextMap,
    *,
    start: int,
    end: int,
    destination: list[ElementTree.Element],
) -> None:
    if end <= start:
        return

    for visible_run in text_map.runs:
        interval_start = max(start, visible_run.start)
        interval_end = min(end, visible_run.end)
        if interval_end <= interval_start:
            continue

        run = _create_run_shell(visible_run.element)
        has_content = False
        for segment in text_map.segments:
            if segment.run_index != visible_run.index:
                continue
            segment_start = max(interval_start, segment.start)
            segment_end = min(interval_end, segment.end)
            if segment_end <= segment_start:
                continue

            copied = copy.deepcopy(segment.element)
            copied.tail = None
            if segment.kind == "text":
                copied.text = text_map.text[segment_start:segment_end]
                _update_xml_space(copied)
            elif segment_start != segment.start or segment_end != segment.end:
                raise ValueError("不可拆分可见文字中的特殊字符节点。")
            run.append(copied)
            has_content = True

        if has_content:
            destination.append(run)


def _identity_index(
    elements: list[ElementTree.Element],
    target: ElementTree.Element,
) -> int:
    for index, element in enumerate(elements):
        if element is target:
            return index
    return -1
