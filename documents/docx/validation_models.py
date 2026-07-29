"""独立 DOCX 核心验证器的公共请求、问题与结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ValidateDocumentRequest:
    """请求对一个本地 DOCX 执行纯 Python 核心结构验证。"""

    source_path: Path
    strict: bool = True


@dataclass(frozen=True)
class ValidationIssue:
    """不包含原始 XML 或二进制内容的稳定验证问题。"""

    code: str
    message: str
    part_name: str | None = None
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class ValidateDocumentResult:
    """核心验证结果；warning 不影响 valid。"""

    source_path: Path
    valid: bool
    revision: str
    size_bytes: int
    issues: list[ValidationIssue]
    checked_parts: list[str]
