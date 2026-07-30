"""将独立 browser 模块适配为 Hermes 的可选工具集。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from browser.runtime import (
    BrowserRuntimeError,
    configure_default_browser_manager,
    default_browser_manager,
)
from hermes.approval import build_assessment_response, is_remote_approval
from hermes.backends import get_backend
from hermes.config import BROWSER_CONFIG
from hermes.tool_declarations.browser import (
    BROWSER_OPERATION_METHODS,
    TOOL_DECLARATIONS,
)
from hermes.tools.browser_approval import (
    approved_browser_media_snapshots_candidate,
    approved_browser_operation_context_candidate,
    assess_browser_operation,
    assess_external_media_analysis,
    browser_media_snapshot_matches,
    browser_operation_risk_level,
    browser_operation_state_matches,
    register_browser_approval_handlers,
)


_INTERNAL_RESULT_FIELDS = {
    "path",
    "abs_path",
    "artifact_dir",
    "workspace_root",
    "parent_abs_path",
    "thread",
}
def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _error(error_type: str, error: str) -> str:
    return _result({"ok": False, "error_type": error_type, "error": error})


def _parse(result: str) -> dict[str, Any]:
    try:
        value = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "error_type": "browser_result_invalid", "error": "browser returned invalid JSON"}
    return value if isinstance(value, dict) else {"ok": False, "error_type": "browser_result_invalid", "error": "browser returned an invalid result"}


def _public(value: Any) -> Any:
    """移除只供工具层完成复核使用的本地目录字段。"""
    if isinstance(value, dict):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if key not in _INTERNAL_RESULT_FIELDS
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _public_result(result: str) -> str:
    return _result(_public(_parse(result)))


def _validate(args: Any, allowed: set[str], required: set[str]) -> dict[str, Any] | str:
    if not isinstance(args, dict):
        return _error("invalid_args", "tool arguments must be an object")
    unknown = set(args) - allowed
    missing = [name for name in required if name not in args]
    if unknown or missing:
        return _error("invalid_args", "tool arguments do not match the declared schema")
    return dict(args)


def _worker(kwargs: dict[str, Any]):
    session_key = kwargs.get("session_key")
    if not isinstance(session_key, str) or not session_key.strip():
        return None, _error("browser_session_unavailable", "browser requires a trusted session context")
    try:
        backend = _trusted_backend(kwargs)
        workspace_root = getattr(backend, "cwd", None)
        return default_browser_manager.get_worker(
            session_key,
            workspace_root=workspace_root,
            require_workspace_root=True,
        ), None
    except BrowserRuntimeError as exc:
        return None, _error(exc.error_type, "browser worker is unavailable")
    except Exception:
        return None, _error("browser_worker_unavailable", "browser worker is unavailable")


def _call(
    worker: Any,
    method: str,
    *args: Any,
    cancel_checker=None,
    **browser_method_kwargs: Any,
) -> str:
    """把受信任运行时取消能力交给固定浏览器线程。"""
    return worker.call(
        method,
        *args,
        cancel_checker=cancel_checker,
        **browser_method_kwargs,
    )


def _multimodal_configuration(worker: Any) -> tuple[dict[str, str] | None, str | None]:
    """从固定浏览器线程读取实际模型配置，避免工具层猜测默认模型。"""
    payload = _parse(_call(worker, "multimodal_configuration"))
    if not payload.get("ok"):
        return None, _public_result(_result(payload))
    provider = payload.get("provider")
    model = payload.get("model")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        return None, _error("multimodal_not_configured", "browser multimodal configuration is invalid")
    return {"provider": provider, "model": model}, None


def _current_page(worker: Any) -> dict[str, Any]:
    payload = _parse(_call(worker, "list_pages"))
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return {}
    for page in pages:
        if isinstance(page, dict) and page.get("is_current") is True:
            return {key: page[key] for key in ("page_id", "url") if key in page}
    return {}


def _file_state(path: Path) -> dict[str, Any] | None:
    """只为一次性审批计算文件身份；结果中不会公开本地路径。"""
    try:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_file():
            return None
        stat = resolved.stat()
        if stat.st_size <= 0:
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "filename": resolved.name,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    except (OSError, RuntimeError):
        return None


def _artifact_snapshot(artifact: dict[str, Any], *, snapshot_id: str | None = None) -> dict[str, Any] | None:
    raw_path = artifact.get("path")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(raw_path, str) or not isinstance(artifact_id, str) or not artifact_id:
        return None
    state = _file_state(Path(raw_path))
    if state is None:
        return None
    state["artifact_id"] = artifact_id
    for key in ("page_id", "source_url"):
        if isinstance(artifact.get(key), str):
            state[key] = artifact[key]
    if isinstance(snapshot_id, str):
        state["snapshot_id"] = snapshot_id
    return state


def _trusted_backend(kwargs: dict[str, Any]) -> Any:
    """只从工具运行上下文取得当前会话的 backend。"""
    backend = kwargs.get("backend")
    if backend is not None:
        return backend
    return get_backend(session_key=kwargs["session_key"])


def _has_symlink_component(path: Path) -> bool:
    """检查宿主机绝对路径的每一级，拒绝通过目录符号链接绕过工作区。"""
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _normalized_upload_paths(
    backend: Any,
    worker: Any,
    raw_paths: list[Any],
) -> tuple[list[str], list[dict[str, Any]]] | str:
    """按 backend 当前目录解析上传输入，再收敛到 worker 固定工作区的相对路径。"""
    if getattr(backend, "backend_type", None) != "local":
        return "browser_workspace_unavailable"
    workspace_root = worker.workspace_root
    normalized_paths: list[str] = []
    snapshots: list[dict[str, Any]] = []
    for value in raw_paths:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            return "invalid_path"
        raw_path = Path(value)
        if ".." in raw_path.parts:
            return "invalid_path"
        try:
            candidate = Path(backend.resolve_path(value))
            if not candidate.is_absolute() or _has_symlink_component(candidate):
                return "invalid_path"
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            return "file_not_found"
        except (OSError, RuntimeError):
            return "invalid_path"
        try:
            relative = resolved.relative_to(workspace_root)
        except ValueError:
            return "path_outside_workspace"
        if not resolved.is_file() or _has_symlink_component(resolved):
            return "invalid_path"
        snapshot = _file_state(resolved)
        if snapshot is None:
            return "invalid_path"
        # 审批记录只保存固定工作区内的相对身份，不暴露宿主机绝对路径。
        workspace_path = relative.as_posix()
        snapshot["workspace_path"] = workspace_path
        normalized_paths.append(workspace_path)
        snapshots.append(snapshot)
    return normalized_paths, snapshots


def _approval_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        backend = _trusted_backend(kwargs)
    except Exception:
        backend = None
    return {
        "remote_approval": is_remote_approval(kwargs),
        "approval_grant": kwargs.get("approval_grant"),
        "security_policy": getattr(backend, "tool_approval_policy", None),
        "backend_context": {"backend_type": getattr(backend, "backend_type", "browser")},
        "intelligent_advisor": getattr(backend, "intelligent_approval_advisor", None),
    }


def _allow_high_risk_execution(
    assessment: Any,
    pending: str | None,
    approval_grant: object,
    approved_context: dict | None,
    current_context: dict,
) -> str | None:
    """区分待批准、过期 grant 与智能审批当场放行三种结果。"""
    if pending is not None:
        return pending
    if approval_grant is not None:
        if not browser_operation_state_matches(
            approval_grant, approved_context, current_context
        ):
            return _error("approval_stale", "approved browser operation changed; request approval again")
        return None
    if assessment.decision.value == "allow":
        return None
    return _error("approval_stale", "browser approval could not be validated")


def _handle_simple(method: str, allowed: set[str], required: set[str]) -> Callable[[Any], str]:
    def handler(args: Any, **kwargs: Any) -> str:
        validated = _validate(args, allowed, required)
        if isinstance(validated, str):
            return validated
        worker, error = _worker(kwargs)
        if error is not None:
            return error
        return _public_result(
            _call(
                worker,
                method,
                cancel_checker=kwargs.get("cancel_checker"),
                **validated,
            )
        )
    return handler


def handle_browser_upload_files(args: Any, **kwargs: Any) -> str:
    validated = _validate(args, {"ref", "paths", "snapshot_id"}, {"ref", "paths", "snapshot_id"})
    if isinstance(validated, str):
        return validated
    raw_paths = validated["paths"] if isinstance(validated["paths"], list) else [validated["paths"]]
    if not raw_paths:
        return _error("invalid_path", "upload paths must be a non-empty list")
    worker, error = _worker(kwargs)
    if error is not None:
        return error
    try:
        backend = _trusted_backend(kwargs)
        normalized = _normalized_upload_paths(backend, worker, raw_paths)
    except Exception:
        normalized = "invalid_path"
    if isinstance(normalized, str):
        error_type = normalized if isinstance(normalized, str) else "invalid_path"
        messages = {
            "browser_workspace_unavailable": "browser upload only supports a local backend workspace",
            "file_not_found": "upload file does not exist",
            "path_outside_workspace": "upload file is outside the fixed browser workspace",
            "invalid_path": "upload paths must be local regular files inside the fixed browser workspace",
        }
        return _error(
            error_type,
            messages[error_type],
        )
    normalized_paths, snapshots = normalized
    # BrowserSession 与审批都以这组固定工作区内的规范化相对路径为准。
    upload_args = {
        **validated,
        "paths": normalized_paths,
    }
    context = {
        **_current_page(worker),
        "ref": validated["ref"],
        "snapshot_id": validated["snapshot_id"],
        "uploaded_files": snapshots,
    }
    assessment = assess_browser_operation(
        "browser_upload_files", validated,
        session_key=kwargs["session_key"], source_context=context,
        risk_level=browser_operation_risk_level("browser_upload_files"),
        **_approval_options(kwargs),
    )
    pending = build_assessment_response(assessment, "Upload selected local files to the current webpage.")
    approved = approved_browser_operation_context_candidate(
        kwargs.get("approval_grant"), "browser_upload_files", validated,
        session_key=kwargs["session_key"],
    )
    decision_error = _allow_high_risk_execution(
        assessment, pending, kwargs.get("approval_grant"), approved, context
    )
    if decision_error is not None:
        return decision_error
    return _public_result(
        _call(
            worker,
            "upload_files",
            cancel_checker=kwargs.get("cancel_checker"),
            **upload_args,
        )
    )


def handle_browser_console(args: Any, **kwargs: Any) -> str:
    validated = _validate(args, {"expression", "snapshot_id", "max_chars"}, {"expression", "snapshot_id"})
    if isinstance(validated, str):
        return validated
    worker, error = _worker(kwargs)
    if error is not None:
        return error
    context = {**_current_page(worker), "snapshot_id": validated["snapshot_id"], "expression": validated["expression"]}
    assessment = assess_browser_operation(
        "browser_console", validated, session_key=kwargs["session_key"],
        source_context=context,
        risk_level=browser_operation_risk_level("browser_console"),
        **_approval_options(kwargs),
    )
    pending = build_assessment_response(assessment, "Run JavaScript in the current webpage.")
    approved = approved_browser_operation_context_candidate(kwargs.get("approval_grant"), "browser_console", validated, session_key=kwargs["session_key"])
    decision_error = _allow_high_risk_execution(
        assessment, pending, kwargs.get("approval_grant"), approved, context
    )
    if decision_error is not None:
        return decision_error
    return _public_result(
        _call(
            worker,
            "console",
            cancel_checker=kwargs.get("cancel_checker"),
            **validated,
        )
    )


def _handle_artifact_change(tool_name: str, method: str, args: Any, kwargs: dict[str, Any]) -> str:
    allowed = {"artifact_id"} if tool_name == "browser_delete_artifact" else set()
    required = allowed
    validated = _validate(args, allowed, required)
    if isinstance(validated, str):
        return validated
    worker, error = _worker(kwargs)
    if error is not None:
        return error
    if tool_name == "browser_delete_artifact":
        artifact_response = _parse(_call(worker, "get_artifact", validated["artifact_id"]))
        artifact = artifact_response.get("artifact")
        snapshots = [_artifact_snapshot(artifact)] if isinstance(artifact, dict) else [None]
    else:
        listed = _parse(_call(worker, "list_artifacts"))
        artifacts = listed.get("artifacts") if isinstance(listed.get("artifacts"), list) else []
        snapshots = [_artifact_snapshot(item) if isinstance(item, dict) else None for item in artifacts]
    if any(snapshot is None for snapshot in snapshots):
        return _error("artifact_not_found", "artifact state cannot be verified for approval")
    context = {"artifacts": snapshots}
    assessment = assess_browser_operation(
        tool_name, validated, session_key=kwargs["session_key"], source_context=context,
        risk_level=browser_operation_risk_level(tool_name),
        **_approval_options(kwargs),
    )
    pending = build_assessment_response(assessment, "Delete browser session artifact files.")
    approved = approved_browser_operation_context_candidate(kwargs.get("approval_grant"), tool_name, validated, session_key=kwargs["session_key"])
    decision_error = _allow_high_risk_execution(
        assessment, pending, kwargs.get("approval_grant"), approved, context
    )
    if decision_error is not None:
        return decision_error
    return _public_result(_call(worker, method, **validated))


def handle_browser_analyze_page(args: Any, **kwargs: Any) -> str:
    validated = _validate(args, {"snapshot_id", "prompt", "full_page", "timeout_ms"}, {"snapshot_id", "prompt"})
    if isinstance(validated, str):
        return validated
    worker, error = _worker(kwargs)
    if error is not None:
        return error
    configuration, configuration_error = _multimodal_configuration(worker)
    if configuration_error is not None:
        return configuration_error
    assert configuration is not None
    grant = kwargs.get("approval_grant")
    approved = approved_browser_media_snapshots_candidate(grant, validated, session_key=kwargs["session_key"])
    approved_context = approved_browser_operation_context_candidate(
        grant,
        "browser_analyze_page",
        validated,
        session_key=kwargs["session_key"],
    )
    artifact: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    context: dict[str, Any]
    if grant is not None:
        if approved is None or approved_context is None or len(approved) != 1:
            return _error("approval_stale", "approved screenshot identity is invalid")
        snapshot = approved[0]
        artifact_response = _parse(_call(worker, "get_artifact", snapshot["artifact_id"]))
        candidate = artifact_response.get("artifact")
        if not isinstance(candidate, dict):
            return _error("approval_stale", "approved screenshot no longer exists")
        current = _artifact_snapshot(
            candidate,
            snapshot_id=snapshot.get("snapshot_id"),
        )
        if not browser_media_snapshot_matches(snapshot, current):
            return _error("approval_stale", "approved screenshot state changed; request approval again")
        artifact = candidate
        context = {
            "page_id": artifact.get("page_id"),
            "url": artifact.get("source_url"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "artifact_id": snapshot["artifact_id"],
            "filename": snapshot["filename"],
            "size_bytes": snapshot["size_bytes"],
            "full_page": validated.get("full_page", False),
        }
    else:
        screenshot_args = {
            "snapshot_id": validated["snapshot_id"],
            "full_page": validated.get("full_page", False),
        }
        screenshot = _parse(
            _call(
                worker,
                "screenshot",
                cancel_checker=kwargs.get("cancel_checker"),
                **screenshot_args,
            )
        )
        if not screenshot.get("ok"):
            return _public_result(_result(screenshot))
        candidate = screenshot.get("artifact")
        snapshot = (
            _artifact_snapshot(candidate, snapshot_id=validated["snapshot_id"])
            if isinstance(candidate, dict)
            else None
        )
        if snapshot is None:
            return _error("approval_snapshot_unavailable", "could not capture browser screenshot state for approval")
        artifact = candidate
        context = {
            "page_id": screenshot.get("page_id"),
            "url": screenshot.get("url"),
            "snapshot_id": validated["snapshot_id"],
            "artifact_id": snapshot["artifact_id"],
            "filename": snapshot["filename"],
            "size_bytes": snapshot["size_bytes"],
            "full_page": screenshot_args["full_page"],
        }
    assessment = assess_external_media_analysis(
        "browser_analyze_page",
        validated,
        session_key=kwargs["session_key"],
        media_snapshots=[snapshot],
        provider=configuration["provider"],
        model=configuration["model"],
        source_context=context,
        **_approval_options(kwargs),
    )
    pending = build_assessment_response(
        assessment,
        "Send the current page screenshot to the configured external model.",
    )
    decision_error = _allow_high_risk_execution(
        assessment,
        pending,
        grant,
        approved_context,
        context,
    )
    if decision_error is not None:
        return decision_error
    assert artifact is not None and snapshot is not None
    result = _parse(
        _call(
            worker,
            "analyze_image",
            snapshot["artifact_id"],
            validated["prompt"],
            cancel_checker=kwargs.get("cancel_checker"),
            timeout_ms=validated.get("timeout_ms"),
        )
    )
    result["artifact"] = _public(artifact)
    result["snapshot_id"] = snapshot.get("snapshot_id")
    return _result(_public(result))


def handle_browser_delete_artifact(args: Any, **kwargs: Any) -> str:
    return _handle_artifact_change("browser_delete_artifact", "delete_artifact", args, kwargs)


def handle_browser_cleanup_artifacts(args: Any, **kwargs: Any) -> str:
    return _handle_artifact_change("browser_cleanup_artifacts", "cleanup_artifacts", args, kwargs)


def register(registry) -> None:
    """注册默认关闭、显式启用 browser toolset 后才可见的浏览器工具。"""
    configure_default_browser_manager(
        idle_timeout_seconds=BROWSER_CONFIG["idle_timeout_seconds"],
        headless=BROWSER_CONFIG["headless"],
        channel=BROWSER_CONFIG["channel"],
        startup_timeout_seconds=BROWSER_CONFIG["startup_timeout_seconds"],
        operation_timeout_seconds=BROWSER_CONFIG["operation_timeout_seconds"],
    )
    register_browser_approval_handlers()
    special_handlers = {
        "browser_upload_files": handle_browser_upload_files,
        "browser_console": handle_browser_console,
        "browser_delete_artifact": handle_browser_delete_artifact,
        "browser_cleanup_artifacts": handle_browser_cleanup_artifacts,
        "browser_analyze_page": handle_browser_analyze_page,
    }
    for declaration in TOOL_DECLARATIONS:
        method = BROWSER_OPERATION_METHODS.get(declaration.name)
        if method is None:
            handler = special_handlers.get(declaration.name)
            if handler is None:
                raise RuntimeError("browser declaration has no runtime handler")
        else:
            parameters = declaration.schema["parameters"]
            properties = parameters["properties"]
            required = parameters.get("required", ())
            handler = _handle_simple(method, set(properties), set(required))
        registry.register_declaration(declaration, handler)
