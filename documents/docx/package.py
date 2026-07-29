"""DOCX ZIP 包的安全、只读访问层。"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree

from .errors import DocxError


_REQUIRED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
)
_CONTENT_TYPE_ATTRIBUTE = "ContentType"


@dataclass(frozen=True)
class DocxLimits:
    """包级与结构级读取限制的唯一配置来源。"""

    max_source_size: int = 100 * 1024 * 1024
    max_zip_entries: int = 4096
    max_entry_uncompressed_size: int = 64 * 1024 * 1024
    max_total_uncompressed_size: int = 256 * 1024 * 1024
    max_xml_size: int = 16 * 1024 * 1024
    max_xml_nodes: int = 500_000
    max_compression_ratio: float = 200.0
    max_blocks: int = 10_000
    max_text_chars: int = 5_000_000
    max_table_rows: int = 10_000
    max_table_cells: int = 100_000
    max_runs: int = 100_000


DOCX_LIMITS = DocxLimits()


class DocxPackage:
    """经安全校验后可按 part 名称读取的内存 ZIP 包。"""

    def __init__(
        self,
        *,
        source_path: Path,
        source_bytes: bytes,
        buffer: io.BytesIO,
        archive: zipfile.ZipFile,
        parts: dict[str, zipfile.ZipInfo],
    ) -> None:
        self.source_path = source_path
        self.size_bytes = len(source_bytes)
        self.revision = f"sha256:{hashlib.sha256(source_bytes).hexdigest()}"
        self._buffer = buffer
        self._archive = archive
        self._parts = parts
        self._xml_cache: dict[str, ElementTree.Element] = {}

    @classmethod
    def open(cls, source_path: str | os.PathLike[str]) -> DocxPackage:
        """读取并完整校验一个 `.docx` 文件，不向磁盘解压任何 entry。"""

        normalized_path = _normalize_source_path(source_path)
        _validate_source_extension(normalized_path)
        source_bytes = _read_source_bytes(normalized_path)
        buffer = io.BytesIO(source_bytes)
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(buffer, mode="r")
            parts = _validate_archive(archive)
            package = cls(
                source_path=normalized_path,
                source_bytes=source_bytes,
                buffer=buffer,
                archive=archive,
                parts=parts,
            )
            try:
                content_types = package.read_xml("[Content_Types].xml")
                package.read_xml("_rels/.rels")
                package.read_xml("word/document.xml")
                package._reject_unsupported_features(content_types)
            except Exception:
                package.close()
                raise
            return package
        except DocxError:
            if archive is not None:
                archive.close()
            buffer.close()
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as exc:
            if archive is not None:
                archive.close()
            buffer.close()
            raise DocxError("invalid_docx_package", "源文件不是有效的 DOCX ZIP 包。") from exc

    @property
    def part_names(self) -> tuple[str, ...]:
        """按稳定顺序返回所有规范化 part 名称。"""

        return tuple(sorted(self._parts))

    @property
    def ordered_part_names(self) -> tuple[str, ...]:
        """按原 ZIP entry 顺序返回规范化 part 名称。"""

        return tuple(self._parts)

    def has_part(self, part_name: str) -> bool:
        """判断指定规范化 part 是否存在。"""

        return part_name in self._parts

    def read_source_bytes(self) -> bytes:
        """返回本次安全打开所对应的完整源文件字节。"""

        return self._buffer.getvalue()

    def read_part_bytes(self, part_name: str) -> bytes:
        """读取已通过包级大小和路径检查的单个 part。"""

        info = self._parts.get(part_name)
        if info is None:
            raise DocxError("invalid_docx_package", f"DOCX 缺少必要 part：{part_name}。")
        try:
            return self._archive.read(info)
        except (zipfile.BadZipFile, KeyError, NotImplementedError, OSError, RuntimeError) as exc:
            raise DocxError("invalid_docx_package", f"无法读取 DOCX part：{part_name}。") from exc

    def get_part_info(self, part_name: str) -> zipfile.ZipInfo:
        """返回 writer 复制 ZIP entry 属性所需的内部信息。"""

        info = self._parts.get(part_name)
        if info is None:
            raise DocxError("invalid_docx_package", f"DOCX 缺少必要 part：{part_name}。")
        return info

    def read_xml_bytes(self, part_name: str) -> bytes:
        """按 XML 专用大小与 DTD 规则读取原始 part 字节。"""

        info = self._parts.get(part_name)
        if info is None:
            raise DocxError("invalid_docx_package", f"DOCX 缺少必要 part：{part_name}。")
        if info.file_size > DOCX_LIMITS.max_xml_size:
            raise DocxError("docx_limit_exceeded", f"XML part 超过大小限制：{part_name}。")
        payload = self.read_part_bytes(part_name)
        upper_payload = payload.upper().replace(b"\x00", b"")
        if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
            raise DocxError(
                "invalid_docx_package",
                f"XML part 包含禁止的 DTD 或实体：{part_name}。",
            )
        return payload

    def read_xml(self, part_name: str) -> ElementTree.Element:
        """在 XML 大小和 DTD 限制内读取指定 part。"""

        cached = self._xml_cache.get(part_name)
        if cached is not None:
            return cached
        payload = self.read_xml_bytes(part_name)
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise DocxError("xml_parse_failed", f"XML part 解析失败：{part_name}。") from exc
        self._xml_cache[part_name] = root
        return root

    def close(self) -> None:
        """释放内存 ZIP 读取器。"""

        self._archive.close()
        self._buffer.close()
        self._xml_cache.clear()

    def __enter__(self) -> DocxPackage:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _reject_unsupported_features(self, content_types: ElementTree.Element) -> None:
        lower_names = {name.lower() for name in self._parts}
        if "word/vbaproject.bin" in lower_names:
            raise DocxError("unsupported_document_feature", "不支持包含 VBA 宏的 DOCX。")
        if any(name.startswith("word/activex/") for name in lower_names):
            raise DocxError("unsupported_document_feature", "不支持包含 ActiveX 的 DOCX。")

        content_type_values = (
            element.attrib.get(_CONTENT_TYPE_ATTRIBUTE, "").lower()
            for element in content_types.iter()
        )
        for content_type in content_type_values:
            if "macroenabled" in content_type or "vbaproject" in content_type:
                raise DocxError("unsupported_document_feature", "不支持宏启用的 Word 文档。")
            if "activex" in content_type:
                raise DocxError("unsupported_document_feature", "不支持包含 ActiveX 的 DOCX。")


def _normalize_source_path(source_path: str | os.PathLike[str]) -> Path:
    try:
        raw_path = os.fspath(source_path)
        if not raw_path:
            raise ValueError("empty path")
        return Path(raw_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DocxError("source_unreadable", "source_path 无法规范化。") from exc


def _validate_source_extension(source_path: Path) -> None:
    if source_path.suffix.lower() != ".docx":
        raise DocxError("unsupported_extension", "只支持读取 .docx 文件。")


def _read_source_bytes(source_path: Path) -> bytes:
    try:
        source_stat = source_path.stat()
    except FileNotFoundError as exc:
        raise DocxError("source_not_found", "源 DOCX 不存在。") from exc
    except OSError as exc:
        raise DocxError("source_unreadable", "无法访问源 DOCX。") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise DocxError("source_not_file", "源路径不是普通文件。")
    if source_stat.st_size == 0:
        raise DocxError("source_empty", "源 DOCX 为空。")
    if source_stat.st_size > DOCX_LIMITS.max_source_size:
        raise DocxError("docx_limit_exceeded", "源 DOCX 超过文件大小限制。")

    try:
        source = source_path.open("rb")
    except FileNotFoundError as exc:
        raise DocxError("source_not_found", "源 DOCX 不存在。") from exc
    except IsADirectoryError as exc:
        raise DocxError("source_not_file", "源路径不是普通文件。") from exc
    except OSError as exc:
        raise DocxError("source_unreadable", "无法读取源 DOCX。") from exc

    try:
        with source:
            source_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(source_stat.st_mode):
                raise DocxError("source_not_file", "源路径不是普通文件。")
            if source_stat.st_size == 0:
                raise DocxError("source_empty", "源 DOCX 为空。")
            if source_stat.st_size > DOCX_LIMITS.max_source_size:
                raise DocxError("docx_limit_exceeded", "源 DOCX 超过文件大小限制。")
            payload = source.read(DOCX_LIMITS.max_source_size + 1)
    except DocxError:
        raise
    except OSError as exc:
        raise DocxError("source_unreadable", "无法读取源 DOCX。") from exc

    if not payload:
        raise DocxError("source_empty", "源 DOCX 为空。")
    if len(payload) > DOCX_LIMITS.max_source_size:
        raise DocxError("docx_limit_exceeded", "源 DOCX 超过文件大小限制。")
    return payload


def _validate_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > DOCX_LIMITS.max_zip_entries:
        raise DocxError("docx_limit_exceeded", "DOCX ZIP entry 数量超过限制。")

    parts: dict[str, zipfile.ZipInfo] = {}
    total_uncompressed_size = 0
    for info in entries:
        normalized_name = _normalize_entry_name(info.filename)
        if normalized_name in parts:
            raise DocxError("invalid_docx_package", "DOCX ZIP 包含重复的 entry 路径。")
        if info.flag_bits & 0x1:
            raise DocxError("invalid_docx_package", "不支持加密的 DOCX ZIP entry。")
        if info.file_size > DOCX_LIMITS.max_entry_uncompressed_size:
            raise DocxError("docx_limit_exceeded", "DOCX ZIP entry 解压后大小超过限制。")
        total_uncompressed_size += info.file_size
        if total_uncompressed_size > DOCX_LIMITS.max_total_uncompressed_size:
            raise DocxError("docx_limit_exceeded", "DOCX ZIP 解压后总大小超过限制。")
        if not info.is_dir() and info.file_size > 0:
            if info.compress_size <= 0:
                raise DocxError("docx_limit_exceeded", "DOCX ZIP entry 压缩率超过限制。")
            ratio = info.file_size / info.compress_size
            if ratio > DOCX_LIMITS.max_compression_ratio:
                raise DocxError("docx_limit_exceeded", "DOCX ZIP entry 压缩率超过限制。")
        parts[normalized_name] = info

    if any(required not in parts for required in _REQUIRED_PARTS):
        raise DocxError("invalid_docx_package", "DOCX 缺少必要的 OOXML part。")
    return parts


def _normalize_entry_name(entry_name: str) -> str:
    if (
        not entry_name
        or "\x00" in entry_name
        or "\\" in entry_name
    ):
        raise DocxError("invalid_docx_package", "DOCX ZIP 包含无效的 entry 路径。")
    normalized_name = entry_name
    path_value = (
        normalized_name[:-1]
        if normalized_name.endswith("/")
        else normalized_name
    )
    segments = path_value.split("/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(entry_name)
    if (
        not path_value
        or normalized_name.startswith("//")
        or any(not segment or segment in {".", ".."} for segment in segments)
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
    ):
        raise DocxError("invalid_docx_package", "DOCX ZIP 包含不安全的 entry 路径。")
    return normalized_name
