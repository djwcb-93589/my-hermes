"""独立 DOCX 创建模块的稳定公共接口。"""

from .errors import DocxError
from .models import (
    CreateDocumentRequest,
    CreateDocumentResult,
    HeadingSpec,
    PageBreakSpec,
    ParagraphSpec,
    TableSpec,
    TextRunSpec,
)
from .service import DocxService, create_document

__all__ = [
    "CreateDocumentRequest",
    "CreateDocumentResult",
    "DocxError",
    "DocxService",
    "HeadingSpec",
    "PageBreakSpec",
    "ParagraphSpec",
    "TableSpec",
    "TextRunSpec",
    "create_document",
]

