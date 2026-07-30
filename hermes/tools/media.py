"""主 Agent 的受限媒体分析工具。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes.tools import _metadata_registration_import_active


__hermes_metadata_only__ = _metadata_registration_import_active()


if not __hermes_metadata_only__:
    from browser.multimodal import MediaSource, MultimodalAnalyzer, MultimodalError
    from hermes.approval import build_assessment_response, is_remote_approval
    from hermes.tools.media_approval import (
        approved_media_snapshots_candidate,
        approved_media_state_matches,
        assess_media_path_policy_denial,
        assess_media_analysis,
        has_symlink_component,
        is_sensitive_media_path,
        register_media_approval_handler,
    )
    from hermes.backends import get_backend
    from hermes.backends.local import LocalBackend
    from hermes.file_state import FileStateSnapshotError, capture_file_state_snapshot
    from hermes.path_policy import ALLOW_ALL_PATH_POLICY, PathAccessDeniedError


_ALLOWED_FIELDS = frozenset({"paths", "prompt", "media_type", "timeout_ms"})
_MEDIA_TYPES = frozenset({"auto", "image", "audio"})
_MAX_MEDIA_FILES = 20
_EXTERNAL_MEDIA_APPROVAL_SUMMARY = (
    "这些本地媒体文件将被发送给外部多模态模型服务，并可能产生费用。"
)


@dataclass(frozen=True, slots=True)
class _ResolvedMedia:
    """保存已通过本地路径检查的媒体源及其规范化路径。"""

    source: MediaSource
    abs_path: str


def _result(payload: dict[str, Any]) -> str:
    """统一输出不含本地绝对路径和媒体内容的 JSON。"""
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, error: str, *, fatal: bool = False) -> str:
    """所有可预期失败都返回可供 Agent 判断的非致命结构化结果。"""
    return _result(
        {
            "ok": False,
            "error_type": error_type,
            "error": error,
            "fatal": fatal,
        }
    )


def _current_model_name() -> str:
    """读取与多模态服务相同的模型配置，用于绑定审批身份。"""
    return os.getenv("DOUBAO_MULTIMODAL_MODEL", "").strip()


def _validate_args(args: Any) -> tuple[list[str], str, str, int | None] | str:
    """校验公开参数，不让内部配置或任意供应商参数进入处理流程。"""
    if not isinstance(args, dict):
        return _error("invalid_args", "arguments must be an object")
    unknown_fields = set(args) - _ALLOWED_FIELDS
    if unknown_fields:
        return _error("invalid_args", "unexpected tool argument")

    paths = args.get("paths")
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= _MAX_MEDIA_FILES
        or not all(isinstance(path, str) and path.strip() for path in paths)
    ):
        return _error("invalid_args", "paths must contain 1 to 20 non-empty relative paths")

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("invalid_args", "prompt must be a non-empty string")

    media_type = args.get("media_type", "auto")
    if not isinstance(media_type, str) or media_type not in _MEDIA_TYPES:
        return _error("invalid_args", "media_type must be auto, image, or audio")

    timeout_ms = args.get("timeout_ms")
    if timeout_ms is not None and (
        isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0
    ):
        return _error("invalid_args", "timeout_ms must be a positive integer")
    return paths, prompt, media_type, timeout_ms


def _is_relative_input_path(raw_path: str) -> bool:
    """拒绝绝对路径和 home 展开写法；路径始终以当前 session cwd 为起点。"""
    return not (
        os.path.isabs(raw_path)
        or Path(raw_path).is_absolute()
        or raw_path.startswith("~")
    )


def _resolve_media_sources(
    backend: LocalBackend,
    paths: list[str],
    *,
    session_key: str,
) -> list[_ResolvedMedia] | str:
    """通过 LocalBackend 与共享路径策略将相对路径变成可读取的普通文件。"""
    path_policy = getattr(backend, "path_policy", ALLOW_ALL_PATH_POLICY)
    sources: list[_ResolvedMedia] = []
    for raw_path in paths:
        try:
            resolved_text = backend.resolve_path(raw_path)
            if not _is_relative_input_path(raw_path):
                return _error("invalid_media_path", "media paths must be relative to the current session cwd")
            requested_path = Path(resolved_text)
            if has_symlink_component(requested_path):
                return _error("invalid_media_path", "symbolic links are not supported for media files")
            allowed_text = path_policy.require_allowed(resolved_text, cwd=backend.cwd)
            allowed_path = Path(allowed_text)
            if not allowed_path.exists():
                return _error("media_not_found", "media file does not exist")
            if allowed_path.is_symlink() or not allowed_path.is_file():
                return _error("invalid_media_path", "media path must reference a regular file")
        except PathAccessDeniedError:
            return build_assessment_response(
                assess_media_path_policy_denial(session_key=session_key),
                _EXTERNAL_MEDIA_APPROVAL_SUMMARY,
            ) or _error(
                "path_policy_denied",
                "media path is blocked by the filesystem policy",
                fatal=True,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _error("invalid_media_path", "media path cannot be resolved safely")
        sources.append(
            _ResolvedMedia(
                source=MediaSource(
                path=allowed_path,
                source_type="workspace",
                artifact_id=None,
                filename=allowed_path.name,
                ),
                abs_path=str(allowed_path),
            )
        )
    return sources


def _public_media(media: list[Any]) -> list[dict[str, Any]]:
    """过滤服务层元数据，避免把绝对路径或额外内部字段放入模型上下文。"""
    return [
        {
            "filename": item.source.filename,
            "media_type": item.media_type,
            "size_bytes": item.size_bytes,
            "source_type": item.source.source_type,
        }
        for item in media
    ]


def _capture_media_snapshots(
    backend: LocalBackend,
    sources: list[_ResolvedMedia],
) -> tuple[dict, ...] | str:
    """在创建审批请求前捕获所有文件的稳定状态，避免审批与发送脱节。"""
    path_policy = getattr(backend, "path_policy", ALLOW_ALL_PATH_POLICY)
    snapshots: list[dict] = []
    try:
        for item in sources:
            snapshots.append(
                capture_file_state_snapshot(
                    backend,
                    item.abs_path,
                    path_policy=path_policy,
                )
            )
    except (FileStateSnapshotError, OSError, TypeError, ValueError):
        return _error(
            "approval_snapshot_unavailable",
            "could not capture a stable media file state for approval",
        )
    return tuple(snapshots)


def handle_media_analyze(args: Any, **kwargs: Any) -> str:
    """分析当前 LocalBackend 工作目录中的一个或多个已授权媒体文件。"""
    validated_args = _validate_args(args)
    if isinstance(validated_args, str):
        return validated_args
    paths, prompt, media_type, timeout_ms = validated_args

    session_key = kwargs.get("session_key") or "default"
    try:
        backend = get_backend(session_key=session_key)
    except Exception as exc:
        return _error("backend_unavailable", f"could not acquire backend: {exc.__class__.__name__}")
    if not isinstance(backend, LocalBackend):
        return _error("unsupported_backend", "media_analyze only supports the local backend")

    sources = _resolve_media_sources(
        backend,
        paths,
        session_key=session_key,
    )
    if isinstance(sources, str):
        return sources

    if any(is_sensitive_media_path(item.abs_path) for item in sources):
        return _error(
            "sensitive_media_denied",
            "sensitive local files cannot be sent to an external media service",
            fatal=True,
        )

    approval_grant = kwargs.get("approval_grant")
    approved_snapshots = approved_media_snapshots_candidate(
        approval_grant,
        args,
        session_key=session_key,
    )
    if approved_snapshots is not None:
        media_snapshots = approved_snapshots
        if not approved_media_state_matches(
            backend,
            sources,
            media_snapshots,
        ):
            return _error(
                "approval_stale",
                "approved media file state changed; request approval again",
            )
    else:
        captured_snapshots = _capture_media_snapshots(backend, sources)
        if isinstance(captured_snapshots, str):
            return captured_snapshots
        media_snapshots = captured_snapshots

    try:
        assessment = assess_media_analysis(
            args,
            normalized_paths=[item.abs_path for item in sources],
            media_snapshots=media_snapshots,
            session_key=session_key,
            remote_approval=is_remote_approval(kwargs),
            provider="doubao_ark",
            model=_current_model_name(),
            approval_grant=approval_grant,
            security_policy=backend.tool_approval_policy,
            backend_context=backend.approval_risk_context(),
            intelligent_advisor=backend.intelligent_approval_advisor,
        )
    except (TypeError, ValueError):
        return _error(
            "approval_snapshot_unavailable",
            "could not prepare media analysis approval",
        )
    policy_response = build_assessment_response(
        assessment,
        _EXTERNAL_MEDIA_APPROVAL_SUMMARY,
    )
    if policy_response is not None:
        return policy_response

    # 审批通过后再次复核，确保调用外部服务前文件没有被替换或变成敏感路径。
    if not approved_media_state_matches(
        backend,
        sources,
        media_snapshots,
    ):
        return _error(
            "approval_stale",
            "approved media file state changed; request approval again",
        )

    expected_type = None if media_type == "auto" else media_type
    try:
        analysis = MultimodalAnalyzer().analyze(
            [item.source for item in sources],
            prompt,
            timeout_ms=timeout_ms,
            expected_type=expected_type,
        )
    except MultimodalError as exc:
        return _error(exc.error_type, exc.message)
    except Exception as exc:
        # 未预期异常不返回供应商请求、授权头、媒体字节或绝对路径。
        return _error("model_request_failed", f"media analysis failed: {exc.__class__.__name__}")

    return _result(
        {
            "ok": True,
            "analysis": analysis.analysis,
            "model": analysis.model,
            "provider": analysis.provider,
            "request_id": analysis.request_id,
            "usage": analysis.usage,
            "media": _public_media(analysis.media),
        }
    )


def register(registry) -> None:
    """注册仅供交互式 CLI 会话调用的高成本媒体分析能力。"""
    if not getattr(registry, "metadata_only", False):
        register_media_approval_handler()
    registry.register(
        name="media_analyze",
        toolset="media",
        schema={
            "name": "media_analyze",
            "description": (
                "Analyze 1 to 20 local media files with the configured Doubao "
                "multimodal model. Supported formats: PNG, JPEG, WEBP, MP3, "
                "WAV, AAC, and M4A. Every path must be relative to the current "
                "session cwd and pass the shared filesystem policy. Use one "
                "call for a single item or several related images/audio files. "
                "This sends media to an external model service and may incur "
                "cost. Do not automatically repeat a request after timeout, "
                "network failure, or an unknown result."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MAX_MEDIA_FILES,
                        "items": {"type": "string"},
                        "description": "Media paths relative to the current session cwd.",
                    },
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": "What to extract, describe, compare, or summarize.",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["auto", "image", "audio"],
                        "default": "auto",
                        "description": "Restrict all inputs to image or audio, or detect automatically.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional model request timeout in milliseconds.",
                    },
                },
                "required": ["paths", "prompt"],
            },
        },
        handler=handle_media_analyze,
        execution_environments=("cli", "gateway"),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        default_enabled_environments=("cli",),
        retry_safe=False,
        unknown_on_crash=True,
    )
