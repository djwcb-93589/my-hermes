"""将独立 browser 模块适配为 Hermes 的可选工具集。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from browser.runtime import default_browser_manager
from hermes.approval import build_assessment_response, is_remote_approval
from hermes.approval_policy import (
    HIGH,
    MEDIUM,
    approved_browser_media_snapshots_candidate,
    approved_browser_operation_context_candidate,
    assess_browser_operation,
    assess_external_media_analysis,
)
from hermes.backends import get_backend


_INTERNAL_RESULT_FIELDS = {
    "path",
    "abs_path",
    "artifact_dir",
    "workspace_root",
    "parent_abs_path",
    "thread",
}
_HIGH_RISK_OPERATIONS = {
    "browser_upload_files": HIGH,
    "browser_console": HIGH,
    "browser_delete_artifact": MEDIUM,
    "browser_cleanup_artifacts": HIGH,
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
        workspace_root = _backend_workspace_root(backend)
        return default_browser_manager.get_worker(
            session_key, workspace_root=workspace_root
        ), None
    except Exception as exc:
        return None, _error("browser_worker_unavailable", f"browser worker is unavailable: {exc.__class__.__name__}")


def _call(worker: Any, method: str, *args: Any, **kwargs: Any) -> str:
    return worker.call(method, *args, **kwargs)


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


def _workspace_file_snapshot(
    value: Any,
    workspace_root: Path,
) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    raw = Path(value)
    if ".." in raw.parts:
        return None
    root = workspace_root.resolve()
    try:
        resolved = (root / raw).resolve(strict=True)
        if root != resolved and root not in resolved.parents:
            return None
    except (OSError, RuntimeError):
        return None
    return _file_state(resolved)


def _trusted_backend(kwargs: dict[str, Any]) -> Any:
    """只从工具运行上下文取得当前会话的 backend。"""
    backend = kwargs.get("backend")
    if backend is not None:
        return backend
    return get_backend(session_key=kwargs["session_key"])


def _backend_workspace_root(backend: Any) -> Path:
    """把可信 backend 的当前工作目录收敛为可供本地浏览器使用的目录。"""
    raw_cwd = getattr(backend, "cwd", None)
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        raise ValueError("backend workspace is unavailable")
    root = Path(raw_cwd).resolve()
    if not root.is_dir():
        raise ValueError("backend workspace is not available on this host")
    return root


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
        if approved_context != current_context:
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
        return _public_result(_call(worker, method, **validated))
    return handler


def handle_browser_upload_files(args: Any, **kwargs: Any) -> str:
    validated = _validate(args, {"ref", "paths", "snapshot_id"}, {"ref", "paths", "snapshot_id"})
    if isinstance(validated, str):
        return validated
    try:
        workspace_root = _backend_workspace_root(_trusted_backend(kwargs))
    except Exception:
        return _error("browser_workspace_unavailable", "browser workspace is unavailable")
    raw_paths = validated["paths"] if isinstance(validated["paths"], list) else [validated["paths"]]
    snapshots = [
        _workspace_file_snapshot(path, workspace_root) for path in raw_paths
    ]
    if not raw_paths or any(item is None for item in snapshots):
        return _error("invalid_path", "upload paths must be existing regular files inside the workspace")
    worker, error = _worker(kwargs)
    if error is not None:
        return error
    context = {
        **_current_page(worker),
        "ref": validated["ref"],
        "snapshot_id": validated["snapshot_id"],
        "uploaded_files": snapshots,
    }
    assessment = assess_browser_operation(
        "browser_upload_files", validated,
        session_key=kwargs["session_key"], source_context=context,
        risk_level=HIGH, **_approval_options(kwargs),
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
    return _public_result(_call(worker, "upload_files", **validated))


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
        source_context=context, risk_level=HIGH, **_approval_options(kwargs),
    )
    pending = build_assessment_response(assessment, "Run JavaScript in the current webpage.")
    approved = approved_browser_operation_context_candidate(kwargs.get("approval_grant"), "browser_console", validated, session_key=kwargs["session_key"])
    decision_error = _allow_high_risk_execution(
        assessment, pending, kwargs.get("approval_grant"), approved, context
    )
    if decision_error is not None:
        return decision_error
    return _public_result(_call(worker, "console", **validated))


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
        risk_level=_HIGH_RISK_OPERATIONS[tool_name], **_approval_options(kwargs),
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
        if current != snapshot:
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
        screenshot = _parse(_call(worker, "screenshot", **screenshot_args))
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


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "parameters": {"type": "object", "additionalProperties": False, "properties": properties, "required": required}}


_STRING = {"type": "string", "minLength": 1}
_SNAPSHOT = {"type": "string", "minLength": 1, "description": "Snapshot returned by a prior browser operation. Use the newest value after an operation that changes a page."}
_REF = {"type": "string", "minLength": 1, "description": "Element ref from the same page and snapshot."}
_TIMEOUT = {"type": "integer", "minimum": 1}


def register(registry) -> None:
    """注册默认关闭、显式启用 browser toolset 后才可见的浏览器工具。"""
    operations: list[tuple[str, str, str, dict[str, Any], list[str], Callable, bool]] = [
        ("browser_navigate", "navigate", "Open a URL and return a new snapshot_id.", {"url": _STRING}, ["url"], _handle_simple("navigate", {"url"}, {"url"}), False),
        ("browser_snapshot", "snapshot", "Read the current page and create a snapshot whose refs are valid only for that page.", {}, [], _handle_simple("snapshot", set(), set()), True),
        ("browser_click", "click", "Click a ref from snapshot_id. Use the returned new snapshot_id afterwards.", {"ref": _REF, "snapshot_id": _SNAPSHOT}, ["ref", "snapshot_id"], _handle_simple("click", {"ref", "snapshot_id"}, {"ref", "snapshot_id"}), False),
        ("browser_type", "type", "Enter text into an editable ref and return a new snapshot_id.", {"ref": _REF, "text": _STRING, "snapshot_id": _SNAPSHOT, "clear": {"type": "boolean", "default": True}, "mode": {"type": "string", "enum": ["fill", "type"]}, "delay_ms": {"type": "integer", "minimum": 0}}, ["ref", "text", "snapshot_id"], _handle_simple("type", {"ref", "text", "snapshot_id", "clear", "mode", "delay_ms"}, {"ref", "text", "snapshot_id"}), False),
        ("browser_press", "press", "Send a page keyboard key or shortcut and return a new snapshot_id.", {"key": _STRING, "snapshot_id": _SNAPSHOT}, ["key", "snapshot_id"], _handle_simple("press", {"key", "snapshot_id"}, {"key", "snapshot_id"}), False),
        ("browser_select", "select", "Select one or more option values and return a new snapshot_id.", {"ref": _REF, "value": {"oneOf": [_STRING, {"type": "array", "minItems": 1, "items": _STRING}]}, "snapshot_id": _SNAPSHOT}, ["ref", "value", "snapshot_id"], _handle_simple("select", {"ref", "value", "snapshot_id"}, {"ref", "value", "snapshot_id"}), False),
    ]
    for name in ("back", "forward", "reload"):
        operations.append((f"browser_{name}", name, f"Navigate browser history with {name}; use its new snapshot_id.", {"snapshot_id": _SNAPSHOT}, ["snapshot_id"], _handle_simple(name, {"snapshot_id"}, {"snapshot_id"}), False))
    operations.extend([
        ("browser_scroll", "scroll", "Scroll the current page and return a new snapshot_id.", {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "snapshot_id": _SNAPSHOT, "amount": {"type": "number", "exclusiveMinimum": 0}}, ["direction", "snapshot_id"], _handle_simple("scroll", {"direction", "snapshot_id", "amount"}, {"direction", "snapshot_id"}), False),
        ("browser_wait_for_url", "wait_for_url", "Wait for a URL pattern and return a new snapshot_id.", {"pattern": _STRING, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT}, ["pattern", "snapshot_id"], _handle_simple("wait_for_url", {"pattern", "snapshot_id", "timeout_ms"}, {"pattern", "snapshot_id"}), False),
        ("browser_wait_for_text", "wait_for_text", "Wait for visible text and return a new snapshot_id.", {"text": _STRING, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT}, ["text", "snapshot_id"], _handle_simple("wait_for_text", {"text", "snapshot_id", "timeout_ms"}, {"text", "snapshot_id"}), False),
        ("browser_wait_for_ref", "wait_for_ref", "Wait only for the original backend node represented by ref; it does not migrate after framework rerendering.", {"ref": _REF, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT}, ["ref", "snapshot_id"], _handle_simple("wait_for_ref", {"ref", "snapshot_id", "timeout_ms"}, {"ref", "snapshot_id"}), False),
        ("browser_wait_for_load_state", "wait_for_load_state", "Wait for a load state and return a new snapshot_id.", {"state": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"]}, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT}, ["state", "snapshot_id"], _handle_simple("wait_for_load_state", {"state", "snapshot_id", "timeout_ms"}, {"state", "snapshot_id"}), False),
        ("browser_get_text", "get_text", "Read text for a ref without changing the snapshot.", {"ref": _REF, "snapshot_id": _SNAPSHOT, "max_chars": {"type": "integer", "minimum": 1}}, ["ref", "snapshot_id"], _handle_simple("get_text", {"ref", "snapshot_id", "max_chars"}, {"ref", "snapshot_id"}), True),
        ("browser_find_in_page", "find_in_page", "Find visible text without scrolling or changing the snapshot.", {"query": _STRING, "snapshot_id": _SNAPSHOT, "max_results": {"type": "integer", "minimum": 1}}, ["query", "snapshot_id"], _handle_simple("find_in_page", {"query", "snapshot_id", "max_results"}, {"query", "snapshot_id"}), True),
    ])
    for name, method, description in (("links", "extract_links", "Extract structured links without changing the snapshot."), ("tables", "extract_tables", "Extract structured tables without changing the snapshot."), ("forms", "extract_forms", "Extract structured forms without changing the snapshot.")):
        operations.append((f"browser_extract_{name}", method, description, {"snapshot_id": _SNAPSHOT, "max_items": {"type": "integer", "minimum": 1}}, ["snapshot_id"], _handle_simple(method, {"snapshot_id", "max_items"}, {"snapshot_id"}), True))
    operations.extend([
        ("browser_extract_metadata", "extract_metadata", "Extract page metadata without changing the snapshot.", {"snapshot_id": _SNAPSHOT}, ["snapshot_id"], _handle_simple("extract_metadata", {"snapshot_id"}, {"snapshot_id"}), True),
        ("browser_collect_paginated", "collect_paginated", "Follow explicit next-page controls within a finite budget; this changes the snapshot.", {"snapshot_id": _SNAPSHOT, "extract_kind": {"type": "string", "enum": ["links", "tables", "forms", "metadata"]}, "max_pages": {"type": "integer", "minimum": 1}, "max_items": {"type": "integer", "minimum": 1}, "max_text_chars": {"type": "integer", "minimum": 1}, "same_origin": {"type": "boolean", "default": True}, "timeout_ms": _TIMEOUT}, ["snapshot_id", "extract_kind"], _handle_simple("collect_paginated", {"snapshot_id", "extract_kind", "max_pages", "max_items", "max_text_chars", "same_origin", "timeout_ms"}, {"snapshot_id", "extract_kind"}), False),
        ("browser_list_pages", "list_pages", "List browser tabs/pages without changing a snapshot.", {}, [], _handle_simple("list_pages", set(), set()), True),
        ("browser_switch_page", "switch_page", "Switch to a registered page and return its new snapshot_id.", {"page_id": _STRING}, ["page_id"], _handle_simple("switch_page", {"page_id"}, {"page_id"}), False),
        ("browser_close_page", "close_page", "Close a registered page and return a new current-page snapshot when available.", {"page_id": _STRING}, ["page_id"], _handle_simple("close_page", {"page_id"}, {"page_id"}), False),
        ("browser_screenshot", "screenshot", "Save a page PNG artifact without changing snapshot_id.", {"snapshot_id": _SNAPSHOT, "full_page": {"type": "boolean", "default": False}}, ["snapshot_id"], _handle_simple("screenshot", {"snapshot_id", "full_page"}, {"snapshot_id"}), False),
        ("browser_screenshot_element", "screenshot_element", "Save a visible element PNG artifact without scrolling or changing snapshot_id.", {"ref": _REF, "snapshot_id": _SNAPSHOT}, ["ref", "snapshot_id"], _handle_simple("screenshot_element", {"ref", "snapshot_id"}, {"ref", "snapshot_id"}), False),
        ("browser_download", "download", "Click a download ref and save only to the browser artifact directory; it returns a new snapshot_id.", {"ref": _REF, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT, "event_timeout_ms": _TIMEOUT, "completion_timeout_ms": _TIMEOUT}, ["ref", "snapshot_id"], _handle_simple("download", {"ref", "snapshot_id", "timeout_ms", "event_timeout_ms", "completion_timeout_ms"}, {"ref", "snapshot_id"}), False),
        ("browser_list_artifacts", "list_artifacts", "List safe metadata for browser artifacts.", {}, [], _handle_simple("list_artifacts", set(), set()), True),
        ("browser_get_artifact", "get_artifact", "Get safe metadata for one browser artifact.", {"artifact_id": _STRING}, ["artifact_id"], _handle_simple("get_artifact", {"artifact_id"}, {"artifact_id"}), True),
    ])
    for name, method, description, props, required, handler, retry_safe in operations:
        registry.register(name=name, toolset="browser", schema=_schema(name, description, props, required), handler=handler, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="none", risk_level="low" if retry_safe else "medium", default_enabled_environments=(), retry_safe=retry_safe, unknown_on_crash=not retry_safe)
    registry.register(name="browser_upload_files", toolset="browser", schema=_schema("browser_upload_files", "Upload workspace files to a file-input ref after one-time approval.", {"ref": _REF, "paths": {"oneOf": [_STRING, {"type": "array", "minItems": 1, "items": _STRING}]}, "snapshot_id": _SNAPSHOT}, ["ref", "paths", "snapshot_id"]), handler=handle_browser_upload_files, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="interactive_or_remote", risk_level="high", default_enabled_environments=(), retry_safe=False, unknown_on_crash=True)
    registry.register(name="browser_console", toolset="browser", schema=_schema("browser_console", "Run JavaScript in the current page after one-time approval; it may change page state.", {"expression": _STRING, "snapshot_id": _SNAPSHOT, "max_chars": {"type": "integer", "minimum": 1}}, ["expression", "snapshot_id"]), handler=handle_browser_console, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="interactive_or_remote", risk_level="high", default_enabled_environments=(), retry_safe=False, unknown_on_crash=True)
    registry.register(name="browser_delete_artifact", toolset="browser", schema=_schema("browser_delete_artifact", "Delete one browser artifact after one-time approval.", {"artifact_id": _STRING}, ["artifact_id"]), handler=handle_browser_delete_artifact, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="interactive_or_remote", risk_level="medium", default_enabled_environments=(), retry_safe=False, unknown_on_crash=True)
    registry.register(name="browser_cleanup_artifacts", toolset="browser", schema=_schema("browser_cleanup_artifacts", "Delete all current-session browser artifacts after one-time approval.", {}, []), handler=handle_browser_cleanup_artifacts, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="interactive_or_remote", risk_level="high", default_enabled_environments=(), retry_safe=False, unknown_on_crash=True)
    registry.register(name="browser_analyze_page", toolset="browser", schema=_schema("browser_analyze_page", "Capture the current page, then send that exact screenshot to the configured external model after one-time approval.", {"snapshot_id": _SNAPSHOT, "prompt": _STRING, "full_page": {"type": "boolean", "default": False}, "timeout_ms": _TIMEOUT}, ["snapshot_id", "prompt"]), handler=handle_browser_analyze_page, execution_environments=("cli", "gateway"), unattended_allowed=False, approval_mode="interactive_or_remote", risk_level="high", default_enabled_environments=(), retry_safe=False, unknown_on_crash=True)
