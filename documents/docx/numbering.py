"""单级项目符号与编号定义的安全增量管理。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

from .errors import DocxError
from .package import DocxPackage
from .parts import ContentTypesManager, NUMBERING_CONTENT_TYPE
from .relationships import (
    NUMBERING_RELATIONSHIP_TYPE,
    RelationshipManager,
)
from .textmap import append_text_content
from .writer import parse_xml_preserving_misc


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_NUMBERING = f"{{{_W_NS}}}numbering"
_W_ABSTRACT_NUMBERING = f"{{{_W_NS}}}abstractNum"
_W_NUMBER = f"{{{_W_NS}}}num"
_W_ABSTRACT_NUMBER_ID = f"{{{_W_NS}}}abstractNumId"
_W_NUMBER_ID = f"{{{_W_NS}}}numId"
_W_MULTI_LEVEL_TYPE = f"{{{_W_NS}}}multiLevelType"
_W_LEVEL = f"{{{_W_NS}}}lvl"
_W_LEVEL_INDEX = f"{{{_W_NS}}}ilvl"
_W_START = f"{{{_W_NS}}}start"
_W_NUMBER_FORMAT = f"{{{_W_NS}}}numFmt"
_W_LEVEL_TEXT = f"{{{_W_NS}}}lvlText"
_W_PARAGRAPH_PROPERTIES = f"{{{_W_NS}}}pPr"
_W_INDENTATION = f"{{{_W_NS}}}ind"
_W_LEFT = f"{{{_W_NS}}}left"
_W_HANGING = f"{{{_W_NS}}}hanging"
_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_NUMBER_PROPERTIES = f"{{{_W_NS}}}numPr"
_W_RUN = f"{{{_W_NS}}}r"
_W_VAL = f"{{{_W_NS}}}val"

NUMBERING_PART = "word/numbering.xml"


@dataclass(frozen=True)
class NumberingListDefinition:
    list_kind: Literal["bullet", "decimal"]
    abstract_number_id: int
    number_id: int
    paragraphs: tuple[ElementTree.Element, ...]
    items: tuple[str, ...]


class NumberingManager:
    """读取或创建 numbering.xml，并为每个列表分配独立 numId。"""

    def __init__(
        self,
        package: DocxPackage,
        relationships: RelationshipManager,
        content_types: ContentTypesManager,
    ) -> None:
        numbering_relationships = relationships.relationships_of_type(
            NUMBERING_RELATIONSHIP_TYPE
        )
        if len(numbering_relationships) > 1:
            raise DocxError(
                "relationship_conflict",
                "document relationships 包含多个 numbering 定义。",
            )
        if numbering_relationships:
            relationship = numbering_relationships[0]
            part_name = relationships.resolve_internal(relationship)
            if not package.has_part(part_name):
                raise DocxError(
                    "relationship_conflict",
                    "numbering relationship 指向不存在的 part。",
                )
        else:
            part_name = NUMBERING_PART

        self.part_name = part_name
        self.existed = package.has_part(part_name)
        if self.existed:
            self.original = package.read_xml_bytes(part_name)
            self.root = parse_xml_preserving_misc(self.original, part_name)
            if self.root.tag != _W_NUMBERING:
                raise DocxError(
                    "package_mutation_conflict",
                    "numbering.xml 根节点无效。",
                )
        else:
            self.original = None
            self.root = ElementTree.Element(_W_NUMBERING)

        target = _target_from_document(self.part_name)
        self.relationship_id = relationships.add_internal(
            NUMBERING_RELATIONSHIP_TYPE,
            target,
        )
        content_types.ensure_override(self.part_name, NUMBERING_CONTENT_TYPE)
        self.changed = not self.existed
        self._used_abstract_ids = _read_unique_ids(
            self.root,
            _W_ABSTRACT_NUMBERING,
            _W_ABSTRACT_NUMBER_ID,
            "abstractNumId",
        )
        self._used_number_ids = _read_unique_ids(
            self.root,
            _W_NUMBER,
            _W_NUMBER_ID,
            "numId",
        )

    def add_list(
        self,
        list_kind: Literal["bullet", "decimal"],
        items: tuple[str, ...],
    ) -> NumberingListDefinition:
        abstract_number_id = _next_non_negative_id(self._used_abstract_ids)
        number_id = _next_positive_id(self._used_number_ids)
        self._used_abstract_ids.add(abstract_number_id)
        self._used_number_ids.add(number_id)
        self._append_abstract_definition(list_kind, abstract_number_id)
        self._append_number_instance(number_id, abstract_number_id)
        paragraphs = tuple(
            _create_list_paragraph(item, number_id) for item in items
        )
        self.changed = True
        return NumberingListDefinition(
            list_kind=list_kind,
            abstract_number_id=abstract_number_id,
            number_id=number_id,
            paragraphs=paragraphs,
            items=items,
        )

    def _append_abstract_definition(
        self,
        list_kind: Literal["bullet", "decimal"],
        abstract_number_id: int,
    ) -> None:
        abstract = ElementTree.Element(
            _W_ABSTRACT_NUMBERING,
            {_W_ABSTRACT_NUMBER_ID: str(abstract_number_id)},
        )
        ElementTree.SubElement(
            abstract,
            _W_MULTI_LEVEL_TYPE,
            {_W_VAL: "singleLevel"},
        )
        level = ElementTree.SubElement(
            abstract,
            _W_LEVEL,
            {_W_LEVEL_INDEX: "0"},
        )
        ElementTree.SubElement(level, _W_START, {_W_VAL: "1"})
        ElementTree.SubElement(
            level,
            _W_NUMBER_FORMAT,
            {_W_VAL: list_kind},
        )
        ElementTree.SubElement(
            level,
            _W_LEVEL_TEXT,
            {_W_VAL: "•" if list_kind == "bullet" else "%1."},
        )
        paragraph_properties = ElementTree.SubElement(
            level,
            _W_PARAGRAPH_PROPERTIES,
        )
        ElementTree.SubElement(
            paragraph_properties,
            _W_INDENTATION,
            {_W_LEFT: "720", _W_HANGING: "360"},
        )
        insertion_index = next(
            (
                index
                for index, child in enumerate(self.root)
                if child.tag == _W_NUMBER
            ),
            len(self.root),
        )
        self.root.insert(insertion_index, abstract)

    def _append_number_instance(
        self,
        number_id: int,
        abstract_number_id: int,
    ) -> None:
        number = ElementTree.SubElement(
            self.root,
            _W_NUMBER,
            {_W_NUMBER_ID: str(number_id)},
        )
        ElementTree.SubElement(
            number,
            _W_ABSTRACT_NUMBER_ID,
            {_W_VAL: str(abstract_number_id)},
        )


def _create_list_paragraph(
    text: str,
    number_id: int,
) -> ElementTree.Element:
    paragraph = ElementTree.Element(_W_PARAGRAPH)
    paragraph_properties = ElementTree.SubElement(
        paragraph,
        _W_PARAGRAPH_PROPERTIES,
    )
    number_properties = ElementTree.SubElement(
        paragraph_properties,
        _W_NUMBER_PROPERTIES,
    )
    ElementTree.SubElement(
        number_properties,
        _W_LEVEL_INDEX,
        {_W_VAL: "0"},
    )
    ElementTree.SubElement(
        number_properties,
        _W_NUMBER_ID,
        {_W_VAL: str(number_id)},
    )
    run = ElementTree.SubElement(paragraph, _W_RUN)
    append_text_content(run, text)
    return paragraph


def _read_unique_ids(
    root: ElementTree.Element,
    element_tag: str,
    attribute_name: str,
    label: str,
) -> set[int]:
    values: set[int] = set()
    for element in root:
        if element.tag != element_tag:
            continue
        raw_value = element.attrib.get(attribute_name)
        try:
            value = int(raw_value) if raw_value is not None else -1
        except ValueError as exc:
            raise DocxError(
                "package_mutation_conflict",
                f"numbering.xml 包含无效的 {label}。",
            ) from exc
        if value < 0 or value in values:
            raise DocxError(
                "package_mutation_conflict",
                f"numbering.xml 包含重复或无效的 {label}。",
            )
        values.add(value)
    return values


def _next_non_negative_id(used: set[int]) -> int:
    candidate = 0
    while candidate in used:
        candidate += 1
    return candidate


def _next_positive_id(used: set[int]) -> int:
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def _target_from_document(part_name: str) -> str:
    if not part_name.startswith("word/"):
        raise DocxError(
            "relationship_conflict",
            "numbering part 必须位于 word 目录内。",
        )
    return part_name.removeprefix("word/")
