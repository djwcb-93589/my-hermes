"""独立 DOCX 模块的公共数据模型。"""

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


@dataclass(frozen=True)
class DocumentMetadata:
    """DOCX 核心属性的稳定快照。"""

    title: str | None = None
    creator: str | None = None
    subject: str | None = None
    description: str | None = None
    created: str | None = None
    modified: str | None = None
    last_modified_by: str | None = None


@dataclass(frozen=True)
class TextRunSnapshot:
    """具有一致直接字符格式的可见文本片段。"""

    text: str
    bold: bool | None
    italic: bool | None
    underline: bool | None


@dataclass(frozen=True)
class ParagraphSnapshot:
    """正文或单元格内段落的结构化快照。"""

    block_id: str
    text: str
    style: str | None
    alignment: str | None
    runs: list[TextRunSnapshot]
    editable: bool
    warnings: list[str]


@dataclass(frozen=True)
class TableCellSnapshot:
    """表格单元格的纯文本与段落快照。"""

    block_id: str
    text: str
    paragraphs: list[str]
    editable: bool
    warnings: list[str]


@dataclass(frozen=True)
class TableSnapshot:
    """正文顶层表格的稳定二维快照。"""

    block_id: str
    rows: list[list[TableCellSnapshot]]
    row_count: int
    column_count: int
    editable: bool
    warnings: list[str]


ReadDocumentBlock: TypeAlias = ParagraphSnapshot | TableSnapshot


@dataclass(frozen=True)
class DocumentWarning:
    """读取复杂或可疑结构时返回的稳定警告。"""

    warning_type: str
    message: str
    part: str | None = None
    block_id: str | None = None


@dataclass(frozen=True)
class DocumentSnapshot:
    """现有 DOCX 的只读结构化快照。"""

    source_path: Path
    revision: str
    size_bytes: int
    metadata: DocumentMetadata
    blocks: list[ReadDocumentBlock]
    warnings: list[DocumentWarning]
    paragraph_count: int
    table_count: int
    image_count: int
    section_count: int


@dataclass(frozen=True)
class InspectDocumentRequest:
    """读取现有 DOCX 的请求参数。"""

    source_path: Path
    include_runs: bool = True
    include_tables: bool = True
    max_blocks: int | None = None
    max_text_chars: int | None = None
