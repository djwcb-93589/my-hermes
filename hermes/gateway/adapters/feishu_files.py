"""飞书消息资源下载与普通 file 上传边界，集中处理 HTTP 流和错误分类。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import quote

from hermes.config import PATH_ACCESS_POLICY, SENSITIVE_FILE_PATTERNS
from hermes.gateway.file_transfer import GatewayFileTransferConfig
from hermes.gateway.files.cache import (
    FileCacheCollisionError,
    FileCacheSecurityError,
    FileTooLargeError,
    InboundFileCache,
    normalize_mime_type,
)
from hermes.gateway.types import Attachment, validate_attachment
from hermes.outbound_file import (
    OutboundFileValidationError,
    capture_outbound_file_snapshot,
    normalize_display_name,
)


DOWNLOAD_CHUNK_BYTES = 64 * 1024
ERROR_BODY_LIMIT_BYTES = 64 * 1024

DownloadStatus = Literal["ready", "retry_wait", "permanent_failed"]
UploadStatus = Literal["uploaded", "retry_wait", "permanent_failed"]


class TokenResultLike(Protocol):
    """下载器只依赖 Adapter token 结果的稳定字段。"""

    success: bool
    token: str
    error: str | None
    error_code: str | None
    retryable: bool
    retry_after_seconds: float | None


TokenProvider = Callable[..., Awaitable[TokenResultLike]]
TokenInvalidator = Callable[[str], Awaitable[None]]
ErrorClassifier = Callable[[int, Any], tuple[str, bool, bool]]


@dataclass(frozen=True)
class FeishuResourceDownloadResult:
    """供 Inbox 后续选择完成、等待重试或永久失败的结构化结果。"""

    status: DownloadStatus
    local_path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None

    @property
    def success(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class FeishuFileUploadResult:
    """普通 file 上传结果；file_key 只交给持久层，不进入日志。"""

    status: UploadStatus
    platform_file_key: str | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None

    @property
    def success(self) -> bool:
        return self.status == "uploaded"


def _failure(
    error_code: str,
    *,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> FeishuResourceDownloadResult:
    return FeishuResourceDownloadResult(
        status="retry_wait" if retryable else "permanent_failed",
        error_code=error_code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _parse_retry_after(headers: object) -> float | None:
    """只接受有限的秒数形式，避免让下载器自行长时间等待。"""
    try:
        value = headers.get("Retry-After") if headers else None
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return max(0.0, parsed)


def _parse_content_length(headers: object) -> int | None:
    """预检 Content-Length；缺失可接受，畸形值按永久响应错误处理。"""
    try:
        value = headers.get("Content-Length") if headers else None
    except AttributeError as exc:
        raise ValueError("invalid_content_length") from exc
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_content_length")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_content_length") from exc
    if parsed < 0:
        raise ValueError("invalid_content_length")
    return parsed


def _encode_path_segment(value: str) -> str:
    """编码不可信路径段，并额外编码可能参与路径归一化的点号。"""
    encoded = quote(
        value,
        safe="-_~",
        encoding="utf-8",
        errors="surrogatepass",
    )
    return encoded.replace(".", "%2E")


async def _read_platform_error_code(response: Any) -> Any:
    """有界流式读取错误 JSON，只提取 code，不保留正文或 message。"""
    buffer = bytearray()
    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
        if not isinstance(chunk, bytes):
            return None
        remaining = ERROR_BODY_LIMIT_BYTES - len(buffer)
        if remaining <= 0:
            break
        buffer.extend(chunk[:remaining])
        if len(buffer) >= ERROR_BODY_LIMIT_BYTES:
            break
    if not buffer:
        return None
    try:
        payload = json.loads(buffer.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("code")


def _resource_error_code(error: str, status_code: int) -> str:
    """把发送侧通用分类收敛成资源下载领域的安全错误码。"""
    if status_code in {404, 410} or error == "reply_target_missing":
        return "resource_not_found"
    if error == "send_timeout":
        return "download_timeout"
    if error == "internal_send_error":
        return "invalid_response"
    return error


def _classify_transport_exception(exc: Exception) -> tuple[str, bool]:
    """只对明确的超时、网络或协议异常开放重试。"""
    if isinstance(exc, TimeoutError):
        return "download_timeout", True
    try:
        import httpx
    except ImportError:
        return "internal_download_error", False
    if isinstance(exc, httpx.TimeoutException):
        return "download_timeout", True
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "network_error", True
    return "internal_download_error", False


async def download_feishu_message_resource(
    *,
    http_client: Any,
    api_base: str,
    message_id: str,
    attachment: Attachment,
    file_transfer_config: GatewayFileTransferConfig | None,
    refresh_token: TokenProvider,
    invalidate_token: TokenInvalidator,
    classify_error: ErrorClassifier,
) -> FeishuResourceDownloadResult:
    """流式下载一项飞书消息资源，但不接触 Inbox 或 AgentLoop。"""
    if (
        file_transfer_config is None
        or file_transfer_config.get("enabled") is not True
    ):
        return _failure("file_transfer_disabled", retryable=False)
    if http_client is None:
        return _failure("download_client_unavailable", retryable=True)

    normalized_message_id = str(message_id or "").strip()
    if not normalized_message_id:
        return _failure("invalid_message_id", retryable=False)
    try:
        normalized_attachment = validate_attachment(attachment)
    except ValueError:
        return _failure("invalid_attachment", retryable=False)
    if normalized_attachment.get("status") != "pending":
        return _failure("invalid_attachment_status", retryable=False)

    resource_type = normalized_attachment["resource_type"]
    resource_key = normalized_attachment["resource_key"].strip()
    original_name = normalized_attachment.get("original_name")
    url = (
        f"{str(api_base).rstrip('/')}/im/v1/messages/"
        f"{_encode_path_segment(normalized_message_id)}/resources/"
        f"{_encode_path_segment(resource_key)}"
    )
    timeout_seconds = file_transfer_config["download_timeout_seconds"]
    max_bytes = file_transfer_config["max_inbound_file_bytes"]
    cache = InboundFileCache(
        file_transfer_config["download_dir"],
        max_bytes,
    )

    token_refreshed = False
    force_token_refresh = False
    while True:
        token_result = await refresh_token(force=force_token_refresh)
        if not token_result.success:
            return _failure(
                token_result.error or "token_unavailable",
                retryable=bool(token_result.retryable),
                retry_after_seconds=token_result.retry_after_seconds,
            )
        token = token_result.token
        if not token:
            return _failure("token_invalid_response", retryable=False)

        try:
            async with asyncio.timeout(timeout_seconds):
                async with http_client.stream(
                    "GET",
                    url,
                    params={"type": resource_type},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout_seconds,
                ) as response:
                    try:
                        status_code = int(response.status_code)
                    except (AttributeError, TypeError, ValueError):
                        return _failure("invalid_response", retryable=False)

                    if not 200 <= status_code < 300:
                        platform_code = await _read_platform_error_code(
                            response,
                        )
                        error, retryable, refresh_required = classify_error(
                            status_code,
                            platform_code,
                        )
                        if refresh_required and not token_refreshed:
                            await invalidate_token(token)
                            token_refreshed = True
                            force_token_refresh = True
                            continue
                        return _failure(
                            _resource_error_code(error, status_code),
                            retryable=retryable,
                            retry_after_seconds=_parse_retry_after(
                                getattr(response, "headers", None),
                            ),
                        )

                    try:
                        content_length = _parse_content_length(
                            getattr(response, "headers", None),
                        )
                    except ValueError:
                        return _failure(
                            "invalid_content_length",
                            retryable=False,
                        )
                    if (
                        content_length is not None
                        and content_length > max_bytes
                    ):
                        return _failure("file_too_large", retryable=False)

                    mime_type = normalize_mime_type(
                        response.headers.get("Content-Type")
                        if getattr(response, "headers", None)
                        else None
                    )
                    with cache.open_writer(
                        message_id=normalized_message_id,
                        original_name=original_name,
                        mime_type=mime_type,
                    ) as writer:
                        async for chunk in response.aiter_bytes(
                            DOWNLOAD_CHUNK_BYTES,
                        ):
                            writer.write(chunk)
                        cached = writer.commit()
                    return FeishuResourceDownloadResult(
                        status="ready",
                        local_path=cached.local_path,
                        mime_type=cached.mime_type,
                        size_bytes=cached.size_bytes,
                        sha256=cached.sha256,
                    )
        except asyncio.CancelledError:
            raise
        except FileTooLargeError:
            return _failure("file_too_large", retryable=False)
        except FileCacheSecurityError as exc:
            return _failure(str(exc) or exc.code, retryable=False)
        except FileCacheCollisionError:
            return _failure("cache_name_collision", retryable=True)
        except OSError:
            return _failure("cache_io_error", retryable=True)
        except Exception as exc:
            error_code, retryable = _classify_transport_exception(exc)
            return _failure(error_code, retryable=retryable)


UPLOAD_CHUNK_BYTES = 128 * 1024
_UNSAFE_UPLOAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WINDOWS_DEVICE_NAMES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def _upload_failure(
    error_code: str,
    *,
    retryable: bool,
    retry_after_seconds: float | None = None,
) -> FeishuFileUploadResult:
    return FeishuFileUploadResult(
        status="retry_wait" if retryable else "permanent_failed",
        error_code=error_code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _sanitize_upload_display_name(value: object, *, fallback: str) -> str:
    """生成可安全放入 multipart 字段与 filename 参数的展示名。"""
    normalized = normalize_display_name(value, fallback=fallback)
    cleaned = _UNSAFE_UPLOAD_NAME_RE.sub("_", normalized).strip(" .")
    if not cleaned:
        cleaned = "file.bin"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES:
        cleaned = f"_{cleaned}"
    while len(cleaned.encode("utf-8")) > 240:
        cleaned = cleaned[:-1]
    return cleaned or "file.bin"


def _open_upload_file(abs_path: str, expected_snapshot: dict):
    """用 no-follow 文件描述符打开审批后的普通文件。"""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(abs_path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OutboundFileValidationError(
                "unsupported_file",
                "upload path must be a regular file",
            )
        expected_identity = (
            int(expected_snapshot["device"]),
            int(expected_snapshot["inode"]),
            int(expected_snapshot["size_bytes"]),
            int(expected_snapshot["mtime_ns"]),
            int(expected_snapshot["ctime_ns"]),
        )
        actual_identity = (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )
        if actual_identity != expected_identity:
            raise OutboundFileValidationError(
                "file_changed",
                "upload file identity changed after validation",
            )
        return os.fdopen(descriptor, "rb", closefd=True), info
    except Exception:
        os.close(descriptor)
        raise


def _multipart_prefix(boundary: str, display_name: str) -> bytes:
    """构造不含文件正文的固定 multipart 前缀。"""
    escaped_name = quote(display_name, safe="", encoding="utf-8")
    parts = [
        f"--{boundary}\r\n",
        'Content-Disposition: form-data; name="file_type"\r\n\r\n',
        "stream\r\n",
        f"--{boundary}\r\n",
        'Content-Disposition: form-data; name="file_name"\r\n\r\n',
        f"{display_name}\r\n",
        f"--{boundary}\r\n",
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="upload.bin"; filename*=UTF-8\'\'{escaped_name}\r\n'
        ),
        "Content-Type: application/octet-stream\r\n\r\n",
    ]
    return "".join(parts).encode("utf-8")


async def _iter_upload_multipart(
    file_obj: Any,
    prefix: bytes,
    suffix: bytes,
    state: dict,
):
    """异步分块读取文件，并记录真正交给 HTTP 客户端的字节摘要。"""
    yield prefix
    while True:
        chunk = await asyncio.to_thread(file_obj.read, UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        state["size_bytes"] += len(chunk)
        state["digest"].update(chunk)
        yield chunk
    yield suffix


def _upload_stream_changed(
    file_obj: Any,
    opened_stat: os.stat_result,
    state: dict,
    *,
    expected_size_bytes: int,
    expected_sha256: str,
    require_complete: bool,
) -> bool:
    """核对描述符身份和已消费字节；传输提前中断本身不误判为文件变化。"""
    try:
        after_stat = os.fstat(file_obj.fileno())
    except (OSError, ValueError):
        return True
    if (
        int(after_stat.st_dev) != int(opened_stat.st_dev)
        or int(after_stat.st_ino) != int(opened_stat.st_ino)
        or int(after_stat.st_size) != int(opened_stat.st_size)
        or int(after_stat.st_mtime_ns) != int(opened_stat.st_mtime_ns)
        or int(after_stat.st_ctime_ns) != int(opened_stat.st_ctime_ns)
    ):
        return True
    streamed_size = int(state.get("size_bytes", 0))
    if streamed_size > expected_size_bytes:
        return True
    if streamed_size == expected_size_bytes:
        if state["digest"].hexdigest() != expected_sha256:
            return True
    elif require_complete:
        return True
    return False


def _upload_transport_error(exc: Exception) -> tuple[str, bool]:
    """上传只对超时和明确传输故障开放重试。"""
    if isinstance(exc, TimeoutError):
        return "upload_timeout", True
    try:
        import httpx
    except ImportError:
        return "internal_upload_error", False
    if isinstance(exc, httpx.TimeoutException):
        return "upload_timeout", True
    if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "network_error", True
    return "internal_upload_error", False


async def upload_feishu_file(
    *,
    http_client: Any,
    api_base: str,
    local_path: str,
    display_name: str,
    expected_size_bytes: int,
    expected_sha256: str,
    database_path: str,
    file_transfer_config: GatewayFileTransferConfig | None,
    refresh_token: TokenProvider,
    invalidate_token: TokenInvalidator,
    classify_error: ErrorClassifier,
) -> FeishuFileUploadResult:
    """流式上传普通 file；成功前后均不解释或记录文件正文。"""
    if (
        file_transfer_config is None
        or file_transfer_config.get("enabled") is not True
    ):
        return _upload_failure("file_transfer_disabled", retryable=False)
    if http_client is None:
        return _upload_failure("upload_client_unavailable", retryable=True)
    if (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or expected_size_bytes <= 0
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        return _upload_failure("invalid_upload_task", retryable=False)

    try:
        snapshot = await asyncio.to_thread(
            capture_outbound_file_snapshot,
            local_path,
            path_policy=PATH_ACCESS_POLICY,
            allowed_roots=file_transfer_config.get(
                "outbound_allowed_roots"
            ),
            max_file_bytes=file_transfer_config.get(
                "max_outbound_file_bytes"
            ),
            database_path=database_path,
            sensitive_patterns=SENSITIVE_FILE_PATTERNS,
        )
        cleaned_name = _sanitize_upload_display_name(
            display_name,
            fallback=os.path.basename(snapshot["abs_path"]),
        )
    except asyncio.CancelledError:
        raise
    except OutboundFileValidationError as exc:
        return _upload_failure(exc.error_code, retryable=False)
    except (OSError, ValueError):
        return _upload_failure("invalid_upload_path", retryable=False)
    if (
        snapshot["size_bytes"] != expected_size_bytes
        or snapshot["sha256"] != expected_sha256
    ):
        return _upload_failure("file_changed", retryable=False)

    token_refreshed = False
    force_token_refresh = False
    while True:
        token_result = await refresh_token(force=force_token_refresh)
        force_token_refresh = False
        if not token_result.success:
            return _upload_failure(
                token_result.error or "token_unavailable",
                retryable=bool(token_result.retryable),
                retry_after_seconds=token_result.retry_after_seconds,
            )
        token = token_result.token
        if not token:
            return _upload_failure("token_invalid_response", retryable=False)

        file_obj = None
        opened_stat = None
        state = None
        try:
            file_obj, opened_stat = await asyncio.to_thread(
                _open_upload_file,
                snapshot["abs_path"],
                snapshot,
            )
            boundary = f"hermes-{uuid.uuid4().hex}"
            prefix = _multipart_prefix(boundary, cleaned_name)
            suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
            state = {
                "size_bytes": 0,
                "digest": hashlib.sha256(),
            }
            content_length = len(prefix) + expected_size_bytes + len(suffix)
            timeout_seconds = file_transfer_config[
                "upload_timeout_seconds"
            ]
            async with asyncio.timeout(timeout_seconds):
                response = await http_client.post(
                    f"{str(api_base).rstrip('/')}/im/v1/files",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": (
                            f"multipart/form-data; boundary={boundary}"
                        ),
                        "Content-Length": str(content_length),
                    },
                    content=_iter_upload_multipart(
                        file_obj,
                        prefix,
                        suffix,
                        state,
                    ),
                    timeout=timeout_seconds,
                )
            if _upload_stream_changed(
                file_obj,
                opened_stat,
                state,
                expected_size_bytes=expected_size_bytes,
                expected_sha256=expected_sha256,
                require_complete=True,
            ):
                return _upload_failure("file_changed", retryable=False)

            try:
                status_code = int(response.status_code)
            except (AttributeError, TypeError, ValueError):
                return _upload_failure(
                    "invalid_upload_response",
                    retryable=False,
                )
            retry_after = _parse_retry_after(
                getattr(response, "headers", None)
            )
            try:
                data = response.json()
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            raw_code = data.get("code")
            try:
                normalized_code = int(raw_code)
            except (TypeError, ValueError):
                normalized_code = None
            if 200 <= status_code < 300 and normalized_code == 0:
                response_data = data.get("data")
                file_key = (
                    response_data.get("file_key")
                    if isinstance(response_data, dict)
                    else None
                )
                if not isinstance(file_key, str) or not file_key.strip():
                    return _upload_failure(
                        "invalid_upload_response",
                        retryable=False,
                    )
                return FeishuFileUploadResult(
                    status="uploaded",
                    platform_file_key=file_key,
                )

            error, retryable, refresh_required = classify_error(
                status_code,
                normalized_code,
            )
            if refresh_required and not token_refreshed:
                await invalidate_token(token)
                token_refreshed = True
                force_token_refresh = True
                continue
            return _upload_failure(
                "upload_timeout" if error == "send_timeout" else error,
                retryable=retryable,
                retry_after_seconds=retry_after,
            )
        except asyncio.CancelledError:
            raise
        except OutboundFileValidationError as exc:
            return _upload_failure(exc.error_code, retryable=False)
        except PermissionError:
            return _upload_failure("permission_denied", retryable=False)
        except (FileNotFoundError, IsADirectoryError):
            return _upload_failure("invalid_upload_path", retryable=False)
        except OSError:
            if (
                file_obj is not None
                and opened_stat is not None
                and state is not None
                and _upload_stream_changed(
                    file_obj,
                    opened_stat,
                    state,
                    expected_size_bytes=expected_size_bytes,
                    expected_sha256=expected_sha256,
                    require_complete=False,
                )
            ):
                return _upload_failure("file_changed", retryable=False)
            return _upload_failure("file_io_error", retryable=False)
        except Exception as exc:
            if (
                file_obj is not None
                and opened_stat is not None
                and state is not None
                and _upload_stream_changed(
                    file_obj,
                    opened_stat,
                    state,
                    expected_size_bytes=expected_size_bytes,
                    expected_sha256=expected_sha256,
                    require_complete=False,
                )
            ):
                return _upload_failure("file_changed", retryable=False)
            error_code, retryable = _upload_transport_error(exc)
            return _upload_failure(error_code, retryable=retryable)
        finally:
            if file_obj is not None:
                file_obj.close()
