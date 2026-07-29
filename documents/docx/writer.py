"""修改后 OOXML part 的序列化与安全 ZIP 写回。"""

from __future__ import annotations

import copy
import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from .errors import DocxError
from .package import DocxPackage
from .package_mutation import PackageMutation, validate_package_mutation


def write_original_package(package: DocxPackage, output_path: Path) -> None:
    """无实际修改时逐字节复制已安全打开的源 DOCX。"""

    try:
        with output_path.open("wb") as destination:
            destination.write(package.read_source_bytes())
    except OSError as exc:
        raise DocxError("io_error", "无法写入临时 DOCX。") from exc


def write_package(
    package: DocxPackage,
    output_path: Path,
    mutation: PackageMutation,
) -> None:
    """保留未修改 part 字节与 ZIP 属性，并应用受控 package mutation。"""

    validate_package_mutation(package, mutation)

    written_parts: set[str] = set()
    try:
        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as destination:
            for part_name in package.ordered_part_names:
                if part_name in mutation.deletions:
                    continue
                if part_name in written_parts:
                    raise DocxError("invalid_docx_package", "DOCX 包含重复的 ZIP entry。")
                written_parts.add(part_name)
                source_info = package.get_part_info(part_name)
                target_info = copy.copy(source_info)
                payload = mutation.replacements.get(part_name)
                if payload is None:
                    payload = package.read_part_bytes(part_name)
                destination.writestr(target_info, payload)

            for part_name, payload in mutation.additions.items():
                if part_name in written_parts:
                    raise DocxError(
                        "package_mutation_conflict",
                        "addition 产生了重复 ZIP entry。",
                    )
                target_info = zipfile.ZipInfo(filename=part_name)
                target_info.compress_type = zipfile.ZIP_DEFLATED
                destination.writestr(target_info, payload)
                written_parts.add(part_name)
    except DocxError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        raise DocxError("io_error", "无法写入临时 DOCX ZIP 包。") from exc


def parse_xml_preserving_misc(payload: bytes, part_name: str) -> ElementTree.Element:
    """解析已通过安全层检查的 XML，并保留注释和处理指令。"""

    parser = ElementTree.XMLParser(
        target=ElementTree.TreeBuilder(insert_comments=True, insert_pis=True)
    )
    try:
        return ElementTree.fromstring(payload, parser=parser)
    except ElementTree.ParseError as exc:
        raise DocxError("xml_parse_failed", f"XML part 解析失败：{part_name}。") from exc


def serialize_xml(
    root: ElementTree.Element,
    *,
    original_payload: bytes | None,
) -> bytes:
    """序列化修改后的 XML，并保留原根节点所声明的 namespace。"""

    namespaces = _read_namespaces(original_payload) if original_payload is not None else []
    for prefix, namespace in namespaces:
        try:
            ElementTree.register_namespace(prefix, namespace)
        except ValueError:
            continue

    payload = ElementTree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    return _inject_missing_namespace_declarations(payload, namespaces)


def _read_namespaces(payload: bytes) -> list[tuple[str, str]]:
    namespaces: list[tuple[str, str]] = []
    seen_prefixes: set[str] = set()
    try:
        for _, value in ElementTree.iterparse(
            io.BytesIO(payload),
            events=("start-ns",),
        ):
            prefix, namespace = value
            normalized_prefix = prefix or ""
            if normalized_prefix in seen_prefixes:
                continue
            seen_prefixes.add(normalized_prefix)
            namespaces.append((normalized_prefix, namespace))
    except ElementTree.ParseError as exc:
        raise DocxError("xml_parse_failed", "无法保留 OOXML namespace。") from exc
    return namespaces


def _inject_missing_namespace_declarations(
    payload: bytes,
    namespaces: list[tuple[str, str]],
) -> bytes:
    declaration_end = payload.find(b"?>")
    root_start = payload.find(b"<", declaration_end + 2 if declaration_end >= 0 else 0)
    root_end = payload.find(b">", root_start)
    if root_start < 0 or root_end < 0:
        raise DocxError("xml_parse_failed", "修改后的 OOXML 缺少根节点。")

    root_opening = payload[root_start:root_end]
    additions: list[bytes] = []
    for prefix, namespace in namespaces:
        if prefix in {"xml", "xmlns"}:
            continue
        attribute = f"xmlns:{prefix}" if prefix else "xmlns"
        marker = f"{attribute}=".encode("utf-8")
        if marker in root_opening:
            continue
        additions.append(
            f" {attribute}={quoteattr(namespace)}".encode("utf-8")
        )
    if not additions:
        return payload
    insertion_offset = root_end
    if payload[root_end - 1 : root_end] == b"/":
        insertion_offset -= 1
    return (
        payload[:insertion_offset]
        + b"".join(additions)
        + payload[insertion_offset:]
    )
