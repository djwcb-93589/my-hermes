"""独立 DOCX 创建与只读检查模块的稳定公共接口。"""

from .errors import DocxError
from .models import (
    CreateDocumentRequest,
    CreateDocumentResult,
    DocumentMetadata,
    DocumentSnapshot,
    DocumentWarning,
    HeadingSpec,
    InspectDocumentRequest,
    PageBreakSpec,
    ParagraphSnapshot,
    ParagraphSpec,
    TableCellSnapshot,
    TableSnapshot,
    TableSpec,
    TextRunSnapshot,
    TextRunSpec,
)
from .reader import DocxReader, inspect_document
from .service import DocxService, create_document

__all__ = [
    "CreateDocumentRequest",
    "CreateDocumentResult",
    "DocumentMetadata",
    "DocumentSnapshot",
    "DocumentWarning",
    "DocxError",
    "DocxReader",
    "DocxService",
    "HeadingSpec",
    "InspectDocumentRequest",
    "PageBreakSpec",
    "ParagraphSnapshot",
    "ParagraphSpec",
    "TableCellSnapshot",
    "TableSnapshot",
    "TableSpec",
    "TextRunSnapshot",
    "TextRunSpec",
    "create_document",
    "inspect_document",
]
