"""受控 OPC part 名称分配与 Content Types 管理。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, NoReturn
from xml.etree import ElementTree

from .errors import DocxError


_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CONTENT_TYPES = f"{{{_CONTENT_TYPES_NS}}}Types"
_DEFAULT = f"{{{_CONTENT_TYPES_NS}}}Default"
_OVERRIDE = f"{{{_CONTENT_TYPES_NS}}}Override"
_IMAGE_PART_PATTERN = re.compile(r"^word/media/image([1-9]\d*)\.[^/]+\Z")

PNG_CONTENT_TYPE = "image/png"
JPEG_CONTENT_TYPE = "image/jpeg"
HEADER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.header+xml"
)
FOOTER_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.footer+xml"
)
NUMBERING_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.numbering+xml"
)


def normalize_part_name(part_name: str) -> str:
    """验证并返回不带前导斜杠的规范 OPC part 名称。"""

    if (
        not isinstance(part_name, str)
        or not part_name
        or "\x00" in part_name
        or "\\" in part_name
        or part_name.startswith("/")
        or part_name.endswith("/")
    ):
        raise DocxError("part_name_conflict", "DOCX part 名称无效。")
    segments = part_name.split("/")
    posix_path = PurePosixPath(part_name)
    windows_path = PureWindowsPath(part_name)
    if (
        any(not segment or segment in {".", ".."} for segment in segments)
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise DocxError("part_name_conflict", "DOCX part 名称存在路径逃逸风险。")
    lower_name = part_name.lower()
    if (
        lower_name.endswith("vbaproject.bin")
        or lower_name.startswith("word/activex/")
    ):
        raise DocxError("part_name_conflict", "禁止写入宏或 ActiveX part。")
    return part_name


def allocate_indexed_part_name(
    existing_names: set[str],
    reserved_names: set[str],
    *,
    directory: str,
    stem: str,
    extension: str,
) -> str:
    """分配不覆盖已有或本次已保留 part 的递增 ASCII 名称。"""

    normalized_directory = normalize_part_name(f"{directory}/placeholder").rsplit(
        "/",
        1,
    )[0]
    normalized_extension = extension.lower().lstrip(".")
    if (
        not stem
        or not stem.isascii()
        or not normalized_extension
        or not normalized_extension.isascii()
        or not normalized_extension.isalnum()
    ):
        raise DocxError("part_name_conflict", "受控 part 名称模板无效。")
    occupied = set(existing_names) | set(reserved_names)
    index = 1
    while True:
        candidate = normalize_part_name(
            f"{normalized_directory}/{stem}{index}.{normalized_extension}"
        )
        if candidate not in occupied:
            reserved_names.add(candidate)
            return candidate
        index += 1


def allocate_image_part_name(
    existing_names: set[str],
    reserved_names: set[str],
    *,
    extension: str,
) -> str:
    """跨图片扩展名分配唯一的 imageN 序号。"""

    used_indexes = {
        int(match.group(1))
        for part_name in existing_names | reserved_names
        if (match := _IMAGE_PART_PATTERN.fullmatch(part_name)) is not None
    }
    index = 1
    while index in used_indexes:
        index += 1
    normalized_extension = extension.lower().lstrip(".")
    candidate = normalize_part_name(
        f"word/media/image{index}.{normalized_extension}"
    )
    if candidate in existing_names or candidate in reserved_names:
        raise DocxError(
            "part_name_conflict",
            "图片 part 分配出现不可恢复的名称冲突。",
        )
    reserved_names.add(candidate)
    return candidate


class ContentTypesManager:
    """校验并增量维护 `[Content_Types].xml`。"""

    def __init__(
        self,
        root: ElementTree.Element,
        *,
        error_type: Literal[
            "package_mutation_conflict",
            "edit_verification_failed",
        ] = "package_mutation_conflict",
        validate_order: bool = True,
    ) -> None:
        self._structure_error_type = error_type
        if root.tag != _CONTENT_TYPES:
            self._raise_structure_error(
                "[Content_Types].xml 根节点无效。",
            )
        self.root = root
        self.changed = False
        self._defaults: dict[str, tuple[str, ElementTree.Element]] = {}
        self._overrides: dict[str, tuple[str, ElementTree.Element]] = {}
        override_seen = False
        self._canonical_order = True
        for child in root:
            if not isinstance(child.tag, str):
                continue
            if child.tag == _DEFAULT:
                if override_seen:
                    self._canonical_order = False
                    if validate_order:
                        self.validate_canonical_order()
                extension = child.attrib.get("Extension", "").lower()
                content_type = child.attrib.get("ContentType", "")
                if (
                    not extension
                    or not content_type
                    or extension in self._defaults
                ):
                    self._raise_structure_error(
                        "Content Types 包含重复或无效的 Default。",
                    )
                self._defaults[extension] = (content_type, child)
            elif child.tag == _OVERRIDE:
                override_seen = True
                raw_name = child.attrib.get("PartName", "")
                content_type = child.attrib.get("ContentType", "")
                if not raw_name.startswith("/") or not content_type:
                    self._raise_structure_error(
                        "Content Types 包含无效的 Override。",
                    )
                try:
                    part_name = normalize_part_name(raw_name[1:])
                except DocxError as exc:
                    self._raise_structure_error(
                        "Content Types 包含无效的 Override。",
                        cause=exc,
                    )
                if part_name in self._overrides:
                    self._raise_structure_error(
                        "Content Types 包含重复的 Override。",
                    )
                self._overrides[part_name] = (content_type, child)
            else:
                self._raise_structure_error(
                    "Content Types 包含不支持的直接子节点。",
                )

    def validate_canonical_order(self) -> None:
        """确认全部 Default 位于第一个 Override 之前。"""

        if not self._canonical_order:
            self._raise_structure_error(
                "Content Types 的 Default 不能位于 Override 之后。",
            )

    def ensure_default(self, extension: str, content_type: str) -> None:
        normalized_extension = extension.lower().lstrip(".")
        existing = self._defaults.get(normalized_extension)
        if existing is not None:
            if existing[0] != content_type:
                raise DocxError(
                    "package_mutation_conflict",
                    "图片扩展名的 Content Type 与现有定义冲突。",
                )
            return
        element = ElementTree.Element(
            _DEFAULT,
            {
                "Extension": normalized_extension,
                "ContentType": content_type,
            },
        )
        insertion_index = next(
            (
                index
                for index, child in enumerate(self.root)
                if child.tag == _OVERRIDE
            ),
            len(self.root),
        )
        self.root.insert(insertion_index, element)
        self._defaults[normalized_extension] = (content_type, element)
        self.changed = True

    def ensure_override(self, part_name: str, content_type: str) -> None:
        normalized_name = normalize_part_name(part_name)
        existing = self._overrides.get(normalized_name)
        if existing is not None:
            if existing[0] != content_type:
                raise DocxError(
                    "package_mutation_conflict",
                    "DOCX part 的 Content Type 与现有定义冲突。",
                )
            return
        element = ElementTree.SubElement(
            self.root,
            _OVERRIDE,
            {
                "PartName": f"/{normalized_name}",
                "ContentType": content_type,
            },
        )
        self._overrides[normalized_name] = (content_type, element)
        self.changed = True

    def content_type_for(self, part_name: str) -> str | None:
        normalized_name = normalize_part_name(part_name)
        override = self._overrides.get(normalized_name)
        if override is not None:
            return override[0]
        extension = normalized_name.rsplit(".", 1)[-1].lower()
        default = self._defaults.get(extension)
        return default[0] if default is not None else None

    def override_content_type_for(self, part_name: str) -> str | None:
        """返回指定 part 的显式 Override Content Type。"""

        normalized_name = normalize_part_name(part_name)
        override = self._overrides.get(normalized_name)
        return override[0] if override is not None else None

    def _raise_structure_error(
        self,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> NoReturn:
        error = DocxError(self._structure_error_type, message)
        if cause is None:
            raise error
        raise error from cause
