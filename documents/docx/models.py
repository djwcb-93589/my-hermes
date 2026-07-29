"""独立 DOCX 模块的公共数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias


class _UnsetPropertyValue:
    """表示段落属性字段未参与本次更新。"""

    __slots__ = ()


UNSET = _UnsetPropertyValue()


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


@dataclass(frozen=True)
class SearchDocumentRequest:
    """按当前结果视图搜索现有 DOCX 的请求。"""

    source_path: Path
    query: str
    case_sensitive: bool = True
    whole_word: bool = False
    include_paragraphs: bool = True
    include_table_cells: bool = True
    max_matches: int = 100


@dataclass(frozen=True)
class TextMatch:
    """一个位于单个内容块内的稳定可见文字匹配。"""

    match_id: str
    block_id: str
    matched_text: str
    start: int
    end: int
    prefix: str
    suffix: str
    editable: bool
    warnings: list[str]


@dataclass(frozen=True)
class SearchDocumentResult:
    """一次只读搜索的 revision 与有界匹配结果。"""

    source_path: Path
    revision: str
    query: str
    matches: list[TextMatch]
    total_matches: int


@dataclass(frozen=True)
class ReplaceParagraphText:
    """替换正文顶层普通段落文字。"""

    block_id: str
    text: str
    preserve_first_run_format: bool = True


@dataclass(frozen=True)
class ReplaceTableCellText:
    """替换简单表格单元格的唯一段落文字。"""

    block_id: str
    text: str
    preserve_first_run_format: bool = True


@dataclass(frozen=True)
class ReplaceTextMatch:
    """根据当前 revision 下的稳定 match_id 局部替换文字。"""

    match_id: str
    block_id: str
    expected_text: str
    replacement_text: str
    preserve_format: bool = True


@dataclass(frozen=True)
class InsertParagraphBefore:
    """在一个稳定定位的正文段落前插入普通段落。"""

    block_id: str
    runs: list[TextRunSpec]
    style: str | None = None
    alignment: str | None = None


@dataclass(frozen=True)
class InsertParagraphAfter:
    """在一个稳定定位的正文段落后插入普通段落。"""

    block_id: str
    runs: list[TextRunSpec]
    style: str | None = None
    alignment: str | None = None


@dataclass(frozen=True)
class AppendParagraph:
    """在正文末尾、最终 section properties 之前追加普通段落。"""

    runs: list[TextRunSpec]
    style: str | None = None
    alignment: str | None = None


@dataclass(frozen=True)
class DeleteParagraph:
    """删除一个简单正文顶层段落。"""

    block_id: str


@dataclass(frozen=True)
class UpdateParagraphProperties:
    """更新显式给出的段落基础属性；None 表示清空。"""

    block_id: str
    style: str | None | object = UNSET
    alignment: str | None | object = UNSET
    heading_level: int | None | object = UNSET


@dataclass(frozen=True)
class FormatTextMatch:
    """根据稳定 match_id 修改单个普通 run 内的直接格式。"""

    match_id: str
    block_id: str
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    expected_text: str | None = None


@dataclass(frozen=True)
class InsertTableAfter:
    """在正文顶层 block 后插入简单规则表格。"""

    block_id: str
    rows: list[list[str]]
    header_row: bool = False


@dataclass(frozen=True)
class AppendTableRow:
    """在规则表格末尾追加普通行。"""

    table_block_id: str
    cells: list[str]


@dataclass(frozen=True)
class DeleteTableRow:
    """按旧快照中的行号删除规则表格普通行。"""

    table_block_id: str
    row_index: int


@dataclass(frozen=True)
class UpdateDocumentMetadata:
    """按显式字段集合更新基础核心属性。"""

    fields: dict[str, str | None]


EditOperation: TypeAlias = (
    ReplaceParagraphText
    | ReplaceTableCellText
    | ReplaceTextMatch
    | InsertParagraphBefore
    | InsertParagraphAfter
    | AppendParagraph
    | DeleteParagraph
    | UpdateParagraphProperties
    | FormatTextMatch
    | InsertTableAfter
    | AppendTableRow
    | DeleteTableRow
    | UpdateDocumentMetadata
)


@dataclass(frozen=True)
class EditDocumentRequest:
    """基于已知 revision 编辑现有 DOCX 的请求。"""

    source_path: Path
    output_path: Path
    expected_revision: str
    operations: list[EditOperation]
    overwrite: bool = False


@dataclass(frozen=True)
class AppliedEdit:
    """按请求顺序记录的已验证编辑操作。"""

    operation_index: int
    operation_type: str
    block_id: str | None


@dataclass(frozen=True)
class BlockRemap:
    """结构编辑后一个旧 block_id 对应的新位置。"""

    old_block_id: str
    new_block_id: str | None


@dataclass(frozen=True)
class EditDocumentResult:
    """成功写出并重新验证后的编辑结果。"""

    source_path: Path
    output_path: Path
    old_revision: str
    new_revision: str
    size_bytes: int
    sha256: str
    changed: bool
    applied_edits: list[AppliedEdit]
    block_remap: list[BlockRemap] = field(default_factory=list)
