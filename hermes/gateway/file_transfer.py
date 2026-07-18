"""Gateway 文件传输配置的集中解析。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Mapping, TypedDict

from hermes.path_policy import PathAccessDeniedError, PathAccessPolicy


class GatewayFileTransferConfig(TypedDict):
    """Runner 与 Adapter 之间传递的已校验配置。"""

    enabled: bool
    download_dir: str
    max_inbound_file_bytes: int
    max_outbound_file_bytes: int
    download_timeout_seconds: float
    upload_timeout_seconds: float
    cache_retention_seconds: float
    outbound_allowed_roots: list[str]


DEFAULT_FILE_TRANSFER_CONFIG = {
    "enabled": False,
    "download_dir": "cache/files",
    "max_inbound_file_bytes": 20 * 1024 * 1024,
    "max_outbound_file_bytes": 20 * 1024 * 1024,
    "download_timeout_seconds": 30.0,
    "upload_timeout_seconds": 60.0,
    "cache_retention_seconds": 24 * 60 * 60,
    "outbound_allowed_roots": [],
}


def _positive_integer(value: object, path: str) -> int:
    """读取正整数，同时允许环境变量展开后的十进制字符串。"""
    if isinstance(value, bool):
        raise ValueError(f"{path} must be a positive integer")
    if isinstance(value, str):
        if not re.fullmatch(r"\+?\d+", value.strip()):
            raise ValueError(f"{path} must be a positive integer")
    elif isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{path} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return parsed


def _positive_number(value: object, path: str) -> float:
    """读取有限正数，拒绝 YAML 布尔值被当作 0 或 1。"""
    if isinstance(value, bool):
        raise ValueError(f"{path} must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{path} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{path} must be a positive number")
    return parsed


def load_file_transfer_config(
    gateway_config: Mapping[str, object],
    *,
    hermes_home: str | Path,
    path_policy: PathAccessPolicy | None = None,
) -> GatewayFileTransferConfig:
    """解析 ``gateway.file_transfer``，不创建目录或执行文件操作。"""
    raw_config = gateway_config.get("file_transfer", {})
    if not isinstance(raw_config, Mapping):
        raise ValueError("gateway.file_transfer must be a mapping")

    enabled = raw_config.get(
        "enabled",
        DEFAULT_FILE_TRANSFER_CONFIG["enabled"],
    )
    if not isinstance(enabled, bool):
        raise ValueError("gateway.file_transfer.enabled must be a boolean")

    download_dir = raw_config.get(
        "download_dir",
        DEFAULT_FILE_TRANSFER_CONFIG["download_dir"],
    )
    if not isinstance(download_dir, str) or not download_dir.strip():
        raise ValueError(
            "gateway.file_transfer.download_dir must be a non-empty string"
        )
    download_path = Path(download_dir.strip())
    if not download_path.is_absolute():
        download_path = Path(hermes_home) / download_path
    try:
        resolved_download_dir = str(download_path.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise ValueError(
            "gateway.file_transfer.download_dir must be a valid path"
        ) from exc
    if enabled and path_policy is not None:
        try:
            resolved_download_dir = path_policy.require_allowed(
                resolved_download_dir,
            )
        except PathAccessDeniedError as exc:
            raise ValueError(
                "gateway.file_transfer.download_dir is blocked by "
                "security.filesystem.denied_paths"
            ) from exc

    outbound_allowed_roots = raw_config.get(
        "outbound_allowed_roots",
        DEFAULT_FILE_TRANSFER_CONFIG["outbound_allowed_roots"],
    )
    if not isinstance(outbound_allowed_roots, list):
        raise ValueError(
            "gateway.file_transfer.outbound_allowed_roots must be a list "
            "of strings"
        )
    resolved_outbound_roots: list[str] = []
    for index, root in enumerate(outbound_allowed_roots):
        if not isinstance(root, str) or not root.strip():
            raise ValueError(
                "gateway.file_transfer.outbound_allowed_roots entries must "
                f"be non-empty strings (invalid item at index {index})"
            )
        root_path = Path(root.strip())
        if not root_path.is_absolute():
            root_path = Path(hermes_home) / root_path
        try:
            resolved_root = str(root_path.resolve(strict=False))
            if enabled and path_policy is not None:
                resolved_root = path_policy.require_allowed(resolved_root)
        except PathAccessDeniedError as exc:
            raise ValueError(
                "gateway.file_transfer.outbound_allowed_roots contains a "
                "path blocked by security.filesystem.denied_paths"
            ) from exc
        except (OSError, ValueError) as exc:
            raise ValueError(
                "gateway.file_transfer.outbound_allowed_roots contains an "
                f"invalid path at index {index}"
            ) from exc
        if resolved_root not in resolved_outbound_roots:
            resolved_outbound_roots.append(resolved_root)

    # 入站缓存可直接回发，但不会因此放宽到整个 HERMES_HOME。
    if resolved_download_dir not in resolved_outbound_roots:
        resolved_outbound_roots.append(resolved_download_dir)

    return GatewayFileTransferConfig(
        enabled=enabled,
        download_dir=resolved_download_dir,
        max_inbound_file_bytes=_positive_integer(
            raw_config.get(
                "max_inbound_file_bytes",
                DEFAULT_FILE_TRANSFER_CONFIG["max_inbound_file_bytes"],
            ),
            "gateway.file_transfer.max_inbound_file_bytes",
        ),
        max_outbound_file_bytes=_positive_integer(
            raw_config.get(
                "max_outbound_file_bytes",
                DEFAULT_FILE_TRANSFER_CONFIG["max_outbound_file_bytes"],
            ),
            "gateway.file_transfer.max_outbound_file_bytes",
        ),
        download_timeout_seconds=_positive_number(
            raw_config.get(
                "download_timeout_seconds",
                DEFAULT_FILE_TRANSFER_CONFIG["download_timeout_seconds"],
            ),
            "gateway.file_transfer.download_timeout_seconds",
        ),
        upload_timeout_seconds=_positive_number(
            raw_config.get(
                "upload_timeout_seconds",
                DEFAULT_FILE_TRANSFER_CONFIG["upload_timeout_seconds"],
            ),
            "gateway.file_transfer.upload_timeout_seconds",
        ),
        cache_retention_seconds=_positive_number(
            raw_config.get(
                "cache_retention_seconds",
                DEFAULT_FILE_TRANSFER_CONFIG["cache_retention_seconds"],
            ),
            "gateway.file_transfer.cache_retention_seconds",
        ),
        outbound_allowed_roots=resolved_outbound_roots,
    )
