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

