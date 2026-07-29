"""独立 DOCX 创建、只读检查与安全编辑服务。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from xml.etree import ElementTree

from .atomic import (
    best_effort_unlink as _best_effort_unlink,
    commit_with_overwrite as _commit_with_overwrite,
    commit_without_overwrite as _commit_without_overwrite,
    create_temporary_output_path as _create_temporary_output_path,
)
from .editor import DocxEditor
from .errors import DocxError
from .models import (
    CreateDocumentRequest,
    CreateDocumentResult,
    DocumentSnapshot,
    EditDocumentRequest,
    EditDocumentResult,
    HeadingSpec,
    InspectDocumentRequest,
    PageBreakSpec,
    ParagraphSpec,
    TableSpec,
    TextRunSpec,
)
from .reader import DocxReader
from .runtime import NodeRuntime


_ALLOWED_ALIGNMENTS = frozenset({"left", "center", "right", "justify"})
_REQUIRED_DOCX_ENTRIES = (
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
)


class DocxService:
    """提供 Node 创建能力及纯 Python 读取与安全编辑能力。"""

    def __init__(self, node_executable: str | Path | None = None) -> None:
        self._runtime = NodeRuntime(node_executable=node_executable)
        self._reader = DocxReader()
        self._editor = DocxEditor(reader=self._reader)

    def inspect_document(self, request: InspectDocumentRequest) -> DocumentSnapshot:
        """读取现有 DOCX；该路径不检查或调用 Node runtime。"""

        return self._reader.inspect(request)

    def edit_document(self, request: EditDocumentRequest) -> EditDocumentResult:
        """编辑现有 DOCX；该路径不检查或调用 Node runtime。"""

        return self._editor.edit(request)

    def create_document(
        self,
        request: CreateDocumentRequest,
        *,
        timeout_seconds: float = 60.0,
    ) -> CreateDocumentResult:
        """校验请求并以原子替换方式写入最终 DOCX。"""

        payload = _validate_request(request, timeout_seconds)
        output_path = _normalize_output_path(request.output_path)
        _validate_extension(output_path)
        _validate_existing_target(output_path, request.overwrite)
        _validate_parent_directory(output_path)
        self._runtime.check()

        spec_path: Path | None = None
        temporary_output: Path | None = None
        try:
            spec_path = _write_temporary_spec(payload)
            temporary_output = _create_temporary_output_path(output_path)
            node_result = self._runtime.run_create(
                spec_path,
                temporary_output,
                timeout_seconds=float(timeout_seconds),
            )
            if not temporary_output.is_file():
                raise DocxError(
                    "output_not_created",
                    "DOCX 创建进程未生成输出文件。",
                )
            if node_result["block_count"] != len(payload["blocks"]):
                raise DocxError(
                    "node_result_invalid",
                    "Node runtime 返回的内容块数量不一致。",
                )

            _validate_docx(temporary_output)
            size_bytes = _read_file_size(temporary_output)
            sha256 = _calculate_sha256(temporary_output)
            if request.overwrite:
                _commit_with_overwrite(temporary_output, output_path)
            else:
                _commit_without_overwrite(temporary_output, output_path)
            return CreateDocumentResult(
                output_path=output_path,
                size_bytes=size_bytes,
                sha256=sha256,
                block_count=len(payload["blocks"]),
            )
        finally:
            _best_effort_unlink(spec_path)
            _best_effort_unlink(temporary_output)


def create_document(
    request: CreateDocumentRequest,
    *,
    node_executable: str | Path | None = None,
    timeout_seconds: float = 60.0,
) -> CreateDocumentResult:
    """使用一次性 Service 实例创建新 DOCX。"""

    return DocxService(node_executable=node_executable).create_document(
        request,
        timeout_seconds=timeout_seconds,
    )


def _validate_request(
    request: CreateDocumentRequest,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not isinstance(request, CreateDocumentRequest):
        raise DocxError("invalid_request", "request 必须是 CreateDocumentRequest。")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise DocxError("invalid_request", "timeout_seconds 必须是大于零的有限数值。")
    if not isinstance(request.overwrite, bool):
        raise DocxError("invalid_request", "overwrite 必须是布尔值。")
    if not isinstance(request.output_path, (str, os.PathLike)):
        raise DocxError("invalid_output_path", "output_path 必须是文件系统路径。")
    if not isinstance(request.blocks, list):
        raise DocxError("invalid_request", "blocks 必须是列表。")
    if request.title is not None and not isinstance(request.title, str):
        raise DocxError("invalid_request", "title 必须是字符串或 null。")
    if request.creator is not None and not isinstance(request.creator, str):
        raise DocxError("invalid_request", "creator 必须是字符串或 null。")

    blocks = [_serialize_block(block, index) for index, block in enumerate(request.blocks)]
    return {
        "title": request.title,
        "creator": request.creator,
        "blocks": blocks,
    }


def _serialize_block(block: object, index: int) -> dict[str, Any]:
    if isinstance(block, ParagraphSpec):
        if not isinstance(block.runs, list):
            _invalid_block(index, "runs 必须是列表")
        if block.style is not None and (
            not isinstance(block.style, str) or not block.style.strip()
        ):
            _invalid_block(index, "style 必须是非空字符串或 null")
        if block.alignment is not None and block.alignment not in _ALLOWED_ALIGNMENTS:
            _invalid_block(index, "alignment 不受支持")
        runs = [_serialize_run(run, index, run_index) for run_index, run in enumerate(block.runs)]
        return {
            "type": "paragraph",
            "runs": runs,
            "style": block.style,
            "alignment": block.alignment,
        }

    if isinstance(block, HeadingSpec):
        if not isinstance(block.text, str):
            _invalid_block(index, "heading text 必须是字符串")
        if isinstance(block.level, bool) or not isinstance(block.level, int):
            _invalid_block(index, "heading level 必须是整数")
        if not 1 <= block.level <= 6:
            _invalid_block(index, "heading level 必须在 1 到 6 之间")
        return {
            "type": "heading",
            "text": block.text,
            "level": block.level,
        }

    if isinstance(block, TableSpec):
        if not isinstance(block.header_row, bool):
            _invalid_block(index, "header_row 必须是布尔值")
        if not isinstance(block.rows, list) or not block.rows:
            _invalid_block(index, "table rows 必须是非空列表")
        column_count: int | None = None
        rows: list[list[str]] = []
        for row_index, row in enumerate(block.rows):
            if not isinstance(row, list) or not row:
                _invalid_block(index, f"第 {row_index} 行必须是非空列表")
            if column_count is None:
                column_count = len(row)
            elif len(row) != column_count:
                _invalid_block(index, "table 的每一行必须具有相同列数")
            if not all(isinstance(cell, str) for cell in row):
                _invalid_block(index, f"第 {row_index} 行只能包含字符串单元格")
            rows.append(list(row))
        return {
            "type": "table",
            "rows": rows,
            "header_row": block.header_row,
        }

    if isinstance(block, PageBreakSpec):
        return {"type": "page_break"}

    _invalid_block(index, "内容块类型不受支持")


def _serialize_run(run: object, block_index: int, run_index: int) -> dict[str, Any]:
    if not isinstance(run, TextRunSpec):
        _invalid_block(block_index, f"第 {run_index} 个 run 类型无效")
    if not isinstance(run.text, str):
        _invalid_block(block_index, f"第 {run_index} 个 run 的 text 必须是字符串")
    for field_name in ("bold", "italic", "underline"):
        if not isinstance(getattr(run, field_name), bool):
            _invalid_block(block_index, f"第 {run_index} 个 run 的 {field_name} 必须是布尔值")
    return {
        "text": run.text,
        "bold": run.bold,
        "italic": run.italic,
        "underline": run.underline,
    }


def _invalid_block(index: int, reason: str) -> None:
    raise DocxError("invalid_block", f"第 {index} 个内容块无效：{reason}。")


def _normalize_output_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw_value = os.fspath(value)
        if not raw_value:
            raise ValueError("empty path")
        return Path(raw_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DocxError("invalid_output_path", "output_path 无法规范化。") from exc


def _validate_extension(output_path: Path) -> None:
    if output_path.suffix.lower() != ".docx":
        raise DocxError("unsupported_extension", "输出文件扩展名必须是 .docx。")


def _validate_existing_target(output_path: Path, overwrite: bool) -> None:
    if not os.path.lexists(output_path):
        return
    if output_path.is_dir():
        raise DocxError("invalid_output_path", "目标路径不能是目录。")
    if not overwrite:
        raise DocxError("output_exists", "目标 DOCX 已存在。")


def _validate_parent_directory(output_path: Path) -> None:
    parent = output_path.parent
    if not parent.exists() or not parent.is_dir():
        raise DocxError("invalid_output_path", "目标 DOCX 的父目录必须已经存在。")


def _write_temporary_spec(payload: dict[str, Any]) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="myhermes-docx-",
            suffix=".json",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name).resolve()
            json.dump(payload, temporary_file, ensure_ascii=False, separators=(",", ":"))
            temporary_file.write("\n")
        return temporary_path
    except (OSError, TypeError, ValueError) as exc:
        _best_effort_unlink(temporary_path)
        raise DocxError("io_error", "无法创建临时 DOCX 规格文件。") from exc


def _validate_docx(path: Path) -> None:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            raise DocxError("output_invalid", "生成的 DOCX 为空或不可用。")
        with zipfile.ZipFile(path, mode="r") as archive:
            names = set()
            for entry in archive.infolist():
                entry_name = entry.filename
                normalized = entry_name.replace("\\", "/")
                posix_path = PurePosixPath(normalized)
                windows_path = PureWindowsPath(entry_name)
                if (
                    posix_path.is_absolute()
                    or windows_path.is_absolute()
                    or ".." in posix_path.parts
                    or ".." in windows_path.parts
                ):
                    raise DocxError("output_invalid", "DOCX ZIP 包含不安全的 entry 路径。")
                names.add(normalized)

            if any(required not in names for required in _REQUIRED_DOCX_ENTRIES):
                raise DocxError("output_invalid", "DOCX 缺少必要的 OOXML 文件。")
            for required in _REQUIRED_DOCX_ENTRIES:
                xml_content = archive.read(required)
                uppercase_content = xml_content.upper()
                if b"<!DOCTYPE" in uppercase_content or b"<!ENTITY" in uppercase_content:
                    raise DocxError("output_invalid", "DOCX 包含不安全的 XML 声明。")
                ElementTree.fromstring(xml_content)
    except DocxError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        ElementTree.ParseError,
        KeyError,
        OSError,
        RuntimeError,
    ) as exc:
        raise DocxError("output_invalid", "生成的 DOCX 结构无效。") from exc


def _read_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise DocxError("io_error", "无法读取生成的 DOCX 大小。") from exc


def _calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DocxError("io_error", "无法计算生成 DOCX 的 SHA-256。") from exc
    return digest.hexdigest()
