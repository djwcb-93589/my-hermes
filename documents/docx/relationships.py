"""OOXML relationship 解析、分配、路径解析与超链接安全。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .errors import DocxError
from .parts import normalize_part_name
from .textmap import append_text_content


_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

RELATIONSHIPS = f"{{{_PACKAGE_REL_NS}}}Relationships"
RELATIONSHIP = f"{{{_PACKAGE_REL_NS}}}Relationship"
RELATIONSHIP_ID = f"{{{_OFFICE_REL_NS}}}id"
HYPERLINK_RELATIONSHIP_TYPE = f"{_OFFICE_REL_NS}/hyperlink"
IMAGE_RELATIONSHIP_TYPE = f"{_OFFICE_REL_NS}/image"
NUMBERING_RELATIONSHIP_TYPE = f"{_OFFICE_REL_NS}/numbering"
HEADER_RELATIONSHIP_TYPE = f"{_OFFICE_REL_NS}/header"
FOOTER_RELATIONSHIP_TYPE = f"{_OFFICE_REL_NS}/footer"

_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_HYPERLINK = f"{{{_W_NS}}}hyperlink"
_W_RUN = f"{{{_W_NS}}}r"
_W_RUN_PROPERTIES = f"{{{_W_NS}}}rPr"
_W_RUN_STYLE = f"{{{_W_NS}}}rStyle"
_W_VAL = f"{{{_W_NS}}}val"
_MAX_HYPERLINK_LENGTH = 4096


@dataclass(frozen=True)
class RelationshipSpec:
    relationship_type: str
    target: str
    target_mode: str | None = None


def relationship_part_name(source_part: str) -> str:
    """返回指定 OPC source part 对应的 `.rels` part 名称。"""

    normalized = normalize_part_name(source_part)
    if "/" in normalized:
        directory, filename = normalized.rsplit("/", 1)
        return f"{directory}/_rels/{filename}.rels"
    return f"_rels/{normalized}.rels"


def create_relationships_root() -> ElementTree.Element:
    return ElementTree.Element(RELATIONSHIPS)


def next_relationship_id(root: ElementTree.Element) -> str:
    """分配当前 relationship part 中最小的空闲正整数 rId。"""

    used: set[str] = set()
    for child in root:
        if child.tag != RELATIONSHIP:
            continue
        relationship_id = child.attrib.get("Id")
        if relationship_id:
            used.add(relationship_id)
    candidate = 1
    while f"rId{candidate}" in used:
        candidate += 1
    return f"rId{candidate}"


class RelationshipManager:
    """在不覆盖现有 Id 的前提下维护一个 relationship root。"""

    def __init__(self, root: ElementTree.Element, *, source_part: str) -> None:
        if root.tag != RELATIONSHIPS:
            raise DocxError("relationship_conflict", "relationship part 根节点无效。")
        self.root = root
        self.source_part = normalize_part_name(source_part)
        self.changed = False
        self._by_id: dict[str, ElementTree.Element] = {}
        for child in root:
            if not isinstance(child.tag, str):
                continue
            if child.tag != RELATIONSHIP:
                raise DocxError(
                    "relationship_conflict",
                    "relationship part 包含不支持的直接子节点。",
                )
            relationship_id = child.attrib.get("Id", "")
            relationship_type = child.attrib.get("Type", "")
            target = child.attrib.get("Target", "")
            if (
                not relationship_id
                or not relationship_type
                or not target
                or relationship_id in self._by_id
            ):
                raise DocxError(
                    "relationship_conflict",
                    "relationship part 包含重复或无效定义。",
                )
            self._by_id[relationship_id] = child

    def relationships_of_type(
        self,
        relationship_type: str,
    ) -> tuple[ElementTree.Element, ...]:
        return tuple(
            element
            for element in self._by_id.values()
            if element.attrib.get("Type") == relationship_type
        )

    def get(self, relationship_id: str) -> ElementTree.Element | None:
        return self._by_id.get(relationship_id)

    def iter_relationships(
        self,
    ) -> tuple[tuple[str, ElementTree.Element], ...]:
        """按 relationship part 原始顺序返回 ID 与元素。"""

        return tuple(self._by_id.items())

    def add_internal(self, relationship_type: str, target: str) -> str:
        _validate_relationship_type(relationship_type)
        _validate_new_internal_target(target)
        return self._add(
            RelationshipSpec(
                relationship_type=relationship_type,
                target=target,
            )
        )

    def add_external(self, relationship_type: str, target: str) -> str:
        _validate_relationship_type(relationship_type)
        if relationship_type != HYPERLINK_RELATIONSHIP_TYPE:
            raise DocxError(
                "relationship_conflict",
                "当前阶段只允许新增外部超链接 relationship。",
            )
        validate_hyperlink_url(target)
        return self._add(
            RelationshipSpec(
                relationship_type=relationship_type,
                target=target,
                target_mode="External",
            )
        )

    def resolve_internal(self, relationship: ElementTree.Element) -> str:
        if relationship.attrib.get("TargetMode", "").lower() == "external":
            raise DocxError(
                "relationship_conflict",
                "内部 relationship 不能使用 External TargetMode。",
            )
        return resolve_internal_target(
            self.source_part,
            relationship.attrib.get("Target", ""),
        )

    def _add(self, spec: RelationshipSpec) -> str:
        for relationship_id, element in self._by_id.items():
            existing = RelationshipSpec(
                relationship_type=element.attrib.get("Type", ""),
                target=element.attrib.get("Target", ""),
                target_mode=element.attrib.get("TargetMode"),
            )
            if existing == spec:
                return relationship_id
            if existing.target == spec.target and (
                existing.relationship_type != spec.relationship_type
                or existing.target_mode != spec.target_mode
            ):
                raise DocxError(
                    "relationship_conflict",
                    "相同 Target 已被不同 relationship 类型占用。",
                )
        relationship_id = next_relationship_id(self.root)
        attributes = {
            "Id": relationship_id,
            "Type": spec.relationship_type,
            "Target": spec.target,
        }
        if spec.target_mode is not None:
            attributes["TargetMode"] = spec.target_mode
        element = ElementTree.SubElement(self.root, RELATIONSHIP, attributes)
        self._by_id[relationship_id] = element
        self.changed = True
        return relationship_id


def resolve_internal_target(source_part: str, target: str) -> str:
    """解析已有内部 Target，并拒绝逃出 package 根目录。"""

    normalized_source = normalize_part_name(source_part)
    if (
        not target
        or "\x00" in target
        or "\\" in target
        or target.startswith("/")
        or PureWindowsPath(target).is_absolute()
        or bool(PureWindowsPath(target).drive)
        or "://" in target
    ):
        raise DocxError("relationship_conflict", "内部 relationship Target 无效。")
    base_segments = normalized_source.split("/")[:-1]
    for segment in target.split("/"):
        if segment in {"", "."}:
            raise DocxError(
                "relationship_conflict",
                "内部 relationship Target 包含空路径段。",
            )
        if segment == "..":
            if not base_segments:
                raise DocxError(
                    "relationship_conflict",
                    "内部 relationship Target 逃出 package。",
                )
            base_segments.pop()
        else:
            base_segments.append(segment)
    return normalize_part_name("/".join(base_segments))


def validate_hyperlink_url(url: object) -> str:
    """只接受不含凭据和控制字符的 http、https、mailto URL。"""

    if (
        not isinstance(url, str)
        or not url
        or len(url) > _MAX_HYPERLINK_LENGTH
        or "\\" in url
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in url
        )
    ):
        raise DocxError("invalid_hyperlink", "超链接 URL 无效。")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise DocxError("invalid_hyperlink", "HTTP 超链接 URL 无效。") from exc
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            and not 0 < port <= 65535
        ):
            raise DocxError(
                "invalid_hyperlink",
                "HTTP 超链接必须包含主机且不能包含用户凭据。",
            )
    elif scheme == "mailto":
        address = url[len(parsed.scheme) + 1 :].split("?", 1)[0]
        if not address:
            raise DocxError("invalid_hyperlink", "mailto 超链接缺少地址。")
    else:
        raise DocxError(
            "invalid_hyperlink",
            "超链接协议只允许 http、https 或 mailto。",
        )
    return url


def create_hyperlink_paragraph(text: str, relationship_id: str) -> ElementTree.Element:
    """创建使用外部 relationship 的单段落超链接。"""

    paragraph = ElementTree.Element(_W_PARAGRAPH)
    hyperlink = ElementTree.SubElement(
        paragraph,
        _W_HYPERLINK,
        {RELATIONSHIP_ID: relationship_id},
    )
    run = ElementTree.SubElement(hyperlink, _W_RUN)
    properties = ElementTree.SubElement(run, _W_RUN_PROPERTIES)
    ElementTree.SubElement(properties, _W_RUN_STYLE, {_W_VAL: "Hyperlink"})
    append_text_content(run, text)
    return paragraph


def validate_relationship_package(package: object) -> None:
    """检查所有 relationship Id 唯一且内部 Target 指向现有 part。"""

    for part_name in package.part_names:
        if not part_name.lower().endswith(".rels"):
            continue
        source_part = source_part_from_relationship_part(part_name)
        root = package.read_xml(part_name)
        manager = RelationshipManager(root, source_part=source_part)
        for relationship in manager._by_id.values():
            if relationship.attrib.get("TargetMode", "").lower() == "external":
                continue
            target_part = manager.resolve_internal(relationship)
            if not package.has_part(target_part):
                raise DocxError(
                    "edit_verification_failed",
                    "内部 relationship 指向不存在的 DOCX part。",
                )


def external_relationships(
    package: object,
) -> frozenset[tuple[str, str, str, str]]:
    """返回稳定标识的全部外部 relationship。"""

    values: set[tuple[str, str, str, str]] = set()
    for part_name in package.part_names:
        if not part_name.lower().endswith(".rels"):
            continue
        source_part = source_part_from_relationship_part(part_name)
        manager = RelationshipManager(
            package.read_xml(part_name),
            source_part=source_part,
        )
        for relationship_id, relationship in manager._by_id.items():
            if relationship.attrib.get("TargetMode", "").lower() != "external":
                continue
            values.add(
                (
                    part_name,
                    relationship_id,
                    relationship.attrib.get("Type", ""),
                    relationship.attrib.get("Target", ""),
                )
            )
    return frozenset(values)


def _validate_new_internal_target(target: str) -> None:
    if (
        not isinstance(target, str)
        or not target
        or "\x00" in target
        or "\\" in target
        or target.startswith("/")
        or any(segment in {"", ".", ".."} for segment in target.split("/"))
        or PurePosixPath(target).is_absolute()
        or PureWindowsPath(target).is_absolute()
        or bool(PureWindowsPath(target).drive)
    ):
        raise DocxError("relationship_conflict", "新增内部 relationship Target 无效。")


def _validate_relationship_type(relationship_type: str) -> None:
    if not isinstance(relationship_type, str) or not relationship_type.startswith(
        "http"
    ):
        raise DocxError("relationship_conflict", "relationship Type 无效。")


def source_part_from_relationship_part(part_name: str) -> str:
    """将 relationship part 名称映射为其 source part。"""

    normalized = normalize_part_name(part_name)
    if normalized == "_rels/.rels":
        return "package-root.xml"
    marker = "/_rels/"
    if marker not in normalized or not normalized.endswith(".rels"):
        raise DocxError(
            "relationship_conflict",
            "relationship part 名称无法映射到 source part。",
        )
    directory, relationship_name = normalized.split(marker, 1)
    source_name = relationship_name.removesuffix(".rels")
    return normalize_part_name(f"{directory}/{source_name}")
