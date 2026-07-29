"""创建 DOCX 所需的公共数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias


@dataclass
class TextRunSpec:
    """一段具有相同字符格式的文本。"""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class ParagraphSpec:
    """由文本片段组成的普通段落。"""

    runs: list[TextRunSpec]
    style: str | None = None
    alignment: str | None = None


@dataclass
class HeadingSpec:
    """一至六级标题。"""

    text: str
    level: int


@dataclass
class TableSpec:
    """仅包含普通文本单元格的简单表格。"""

    rows: list[list[str]]
    header_row: bool = False


@dataclass
class PageBreakSpec:
    """显式分页。"""


DocumentBlock: TypeAlias = ParagraphSpec | HeadingSpec | TableSpec | PageBreakSpec


@dataclass
class CreateDocumentRequest:
    """创建新 DOCX 的完整请求。"""

    output_path: Path
    blocks: list[DocumentBlock]
    overwrite: bool = False
    title: str | None = None
    creator: str | None = None


@dataclass
class CreateDocumentResult:
    """成功创建 DOCX 后返回的结果信息。"""

    output_path: Path
    size_bytes: int
    sha256: str
    block_count: int
