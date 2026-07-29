"""DOCX 模块的稳定错误模型。"""

from __future__ import annotations


DOCX_ERROR_TYPES = frozenset(
    {
        "invalid_request",
        "invalid_block",
        "invalid_output_path",
        "unsupported_extension",
        "output_exists",
        "node_runtime_unavailable",
        "node_version_unsupported",
        "node_dependencies_missing",
        "node_execution_failed",
        "node_execution_timeout",
        "node_result_invalid",
        "output_not_created",
        "output_invalid",
        "io_error",
        "source_not_found",
        "source_not_file",
        "source_empty",
        "source_unreadable",
        "unsupported_document_feature",
        "invalid_docx_package",
        "docx_limit_exceeded",
        "xml_parse_failed",
        "inspect_limit_exceeded",
        "revision_conflict",
        "block_not_found",
        "block_not_editable",
        "invalid_edit_operation",
        "duplicate_edit_target",
        "edit_operation_conflict",
        "source_output_same",
        "edit_verification_failed",
        "output_revision_unchanged",
        "match_not_found",
        "match_conflict",
        "match_not_editable",
        "search_verification_failed",
        "invalid_image",
        "image_limit_exceeded",
        "unsupported_image_format",
        "invalid_hyperlink",
        "section_not_found",
        "package_mutation_conflict",
        "relationship_conflict",
        "part_name_conflict",
    }
)


class DocxError(Exception):
    """DOCX 操作对外统一抛出的异常。"""

    error_type: str

    def __init__(self, error_type: str, message: str) -> None:
        if error_type not in DOCX_ERROR_TYPES:
            raise ValueError(f"Unknown DOCX error type: {error_type}")
        self.error_type = error_type
        self.message = message
        super().__init__(message)
