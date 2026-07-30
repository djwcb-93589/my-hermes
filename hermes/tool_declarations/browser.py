"""Browser Toolset 的轻量声明和无执行语义的操作映射。"""

from __future__ import annotations

from types import MappingProxyType

from hermes.tool_declarations.contracts import ToolDeclaration


_STRING = {"type": "string", "minLength": 1}
_SNAPSHOT = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Snapshot returned by a prior browser operation. Use the newest value "
        "after an operation that changes a page."
    ),
}
_REF = {
    "type": "string",
    "minLength": 1,
    "description": "Element ref from the same page and snapshot.",
}
_TIMEOUT = {"type": "integer", "minimum": 1}
_LOW_RISK_READ_METHODS = frozenset({
    "extract_forms",
    "extract_links",
    "extract_metadata",
    "extract_tables",
    "find_in_page",
    "get_artifact",
    "get_text",
    "list_artifacts",
    "list_pages",
    "snapshot",
})


def _schema(
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str],
) -> dict[str, object]:
    """从操作声明构建唯一的运行时与目录共享 schema。"""
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        },
    }


_OPERATION_SPECS = (
    (
        "browser_navigate", "navigate",
        "Open a URL and return a new snapshot_id.",
        {"url": _STRING}, ["url"], False, True,
    ),
    (
        "browser_snapshot", "snapshot",
        "Read the current page and create a new snapshot. Optionally pass the latest snapshot_id to verify it is still current before refreshing.",
        {"snapshot_id": _SNAPSHOT}, [], False, False,
    ),
    (
        "browser_click", "click",
        "Click a ref from snapshot_id. Use the returned new snapshot_id afterwards.",
        {"ref": _REF, "snapshot_id": _SNAPSHOT}, ["ref", "snapshot_id"], False, True,
    ),
    (
        "browser_type", "type",
        "Enter text into an editable ref and return a new snapshot_id.",
        {
            "ref": _REF,
            "text": _STRING,
            "snapshot_id": _SNAPSHOT,
            "clear": {"type": "boolean", "default": True},
            "mode": {"type": "string", "enum": ["fill", "type"]},
            "delay_ms": {"type": "integer", "minimum": 0},
        },
        ["ref", "text", "snapshot_id"], False, True,
    ),
    (
        "browser_press", "press",
        "Send a page keyboard key or shortcut and return a new snapshot_id.",
        {"key": _STRING, "snapshot_id": _SNAPSHOT},
        ["key", "snapshot_id"], False, True,
    ),
    (
        "browser_select", "select",
        "Select one or more option values and return a new snapshot_id.",
        {
            "ref": _REF,
            "value": {
                "oneOf": [
                    _STRING,
                    {
                        "type": "array",
                        "minItems": 1,
                        "items": _STRING,
                    },
                ],
            },
            "snapshot_id": _SNAPSHOT,
        },
        ["ref", "value", "snapshot_id"], False, True,
    ),
    *(
        (
            f"browser_{name}", name,
            f"Navigate browser history with {name}; use its new snapshot_id.",
            {"snapshot_id": _SNAPSHOT}, ["snapshot_id"], False, True,
        )
        for name in ("back", "forward", "reload")
    ),
    (
        "browser_scroll", "scroll",
        "Scroll the current page and return a new snapshot_id.",
        {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
            },
            "snapshot_id": _SNAPSHOT,
            "amount": {"type": "number", "exclusiveMinimum": 0},
        },
        ["direction", "snapshot_id"], False, False,
    ),
    (
        "browser_wait_for_url", "wait_for_url",
        "Wait for a URL pattern and return a new snapshot_id.",
        {"pattern": _STRING, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT},
        ["pattern", "snapshot_id"], False, True,
    ),
    (
        "browser_wait_for_text", "wait_for_text",
        "Wait for visible text and return a new snapshot_id.",
        {"text": _STRING, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT},
        ["text", "snapshot_id"], False, True,
    ),
    (
        "browser_wait_for_ref", "wait_for_ref",
        "Wait only for the original backend node represented by ref; it does not migrate after framework rerendering.",
        {"ref": _REF, "snapshot_id": _SNAPSHOT, "timeout_ms": _TIMEOUT},
        ["ref", "snapshot_id"], False, True,
    ),
    (
        "browser_wait_for_load_state", "wait_for_load_state",
        "Wait for a load state and return a new snapshot_id.",
        {
            "state": {
                "type": "string",
                "enum": ["domcontentloaded", "load", "networkidle"],
            },
            "snapshot_id": _SNAPSHOT,
            "timeout_ms": _TIMEOUT,
        },
        ["state", "snapshot_id"], False, True,
    ),
    (
        "browser_get_text", "get_text",
        "Read text for a ref without changing the snapshot.",
        {
            "ref": _REF,
            "snapshot_id": _SNAPSHOT,
            "max_chars": {"type": "integer", "minimum": 1},
        },
        ["ref", "snapshot_id"], False, False,
    ),
    (
        "browser_find_in_page", "find_in_page",
        "Find visible text without scrolling or changing the snapshot.",
        {
            "query": _STRING,
            "snapshot_id": _SNAPSHOT,
            "max_results": {"type": "integer", "minimum": 1},
        },
        ["query", "snapshot_id"], False, False,
    ),
    *(
        (
            f"browser_extract_{name}", method, description,
            {
                "snapshot_id": _SNAPSHOT,
                "max_items": {"type": "integer", "minimum": 1},
            },
            ["snapshot_id"], False, False,
        )
        for name, method, description in (
            ("links", "extract_links", "Extract structured links without changing the snapshot."),
            ("tables", "extract_tables", "Extract structured tables without changing the snapshot."),
            ("forms", "extract_forms", "Extract structured forms without changing the snapshot."),
        )
    ),
    (
        "browser_extract_metadata", "extract_metadata",
        "Extract page metadata without changing the snapshot.",
        {"snapshot_id": _SNAPSHOT}, ["snapshot_id"], False, False,
    ),
    (
        "browser_collect_paginated", "collect_paginated",
        "Follow explicit next-page controls within a finite budget; this changes the snapshot.",
        {
            "snapshot_id": _SNAPSHOT,
            "extract_kind": {
                "type": "string",
                "enum": ["links", "tables", "forms", "metadata"],
            },
            "max_pages": {"type": "integer", "minimum": 1},
            "max_items": {"type": "integer", "minimum": 1},
            "max_text_chars": {"type": "integer", "minimum": 1},
            "same_origin": {"type": "boolean", "default": True},
            "timeout_ms": _TIMEOUT,
        },
        ["snapshot_id", "extract_kind"], False, True,
    ),
    (
        "browser_list_pages", "list_pages",
        "List browser tabs/pages without changing a snapshot.",
        {}, [], False, False,
    ),
    (
        "browser_switch_page", "switch_page",
        "Switch to a registered page and return its new snapshot_id.",
        {"page_id": _STRING}, ["page_id"], False, False,
    ),
    (
        "browser_close_page", "close_page",
        "Close a registered page and return a new current-page snapshot when available.",
        {"page_id": _STRING}, ["page_id"], False, False,
    ),
    (
        "browser_screenshot", "screenshot",
        "Save a page PNG to browser/screenshot without changing snapshot_id; use artifact.agent_path for later file delivery.",
        {
            "snapshot_id": _SNAPSHOT,
            "full_page": {"type": "boolean", "default": False},
        },
        ["snapshot_id"], False, False,
    ),
    (
        "browser_screenshot_element", "screenshot_element",
        "Save a visible element PNG to browser/screenshot without scrolling or changing snapshot_id; use artifact.agent_path for later file delivery.",
        {"ref": _REF, "snapshot_id": _SNAPSHOT},
        ["ref", "snapshot_id"], False, False,
    ),
    (
        "browser_download", "download",
        "Click a download ref and save it to browser/download; the artifact.agent_path identifies the file for later delivery.",
        {
            "ref": _REF,
            "snapshot_id": _SNAPSHOT,
            "timeout_ms": _TIMEOUT,
            "event_timeout_ms": _TIMEOUT,
            "completion_timeout_ms": _TIMEOUT,
        },
        ["ref", "snapshot_id"], False, True,
    ),
    (
        "browser_list_artifacts", "list_artifacts",
        "List safe browser artifact metadata, including agent_path when the file is available to the session workspace.",
        {}, [], False, False,
    ),
    (
        "browser_get_artifact", "get_artifact",
        "Get safe metadata and agent_path for one browser artifact.",
        {"artifact_id": _STRING}, ["artifact_id"], False, False,
    ),
)


_OPERATION_DECLARATIONS = tuple(
    ToolDeclaration(
        name=name,
        toolset="browser",
        schema=_schema(name, description, properties, required),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="none",
        risk_level=("low" if method in _LOW_RISK_READ_METHODS else "medium"),
        retry_safe=retry_safe,
        unknown_on_crash=True,
        supports_cancellation=supports_cancellation,
    )
    for (
        name,
        method,
        description,
        properties,
        required,
        retry_safe,
        supports_cancellation,
    ) in _OPERATION_SPECS
)

_SPECIAL_DECLARATIONS = (
    ToolDeclaration(
        name="browser_upload_files",
        toolset="browser",
        schema=_schema(
            "browser_upload_files",
            "Upload workspace files to a file-input ref after one-time approval.",
            {
                "ref": _REF,
                "paths": {
                    "oneOf": [
                        _STRING,
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": _STRING,
                        },
                    ],
                },
                "snapshot_id": _SNAPSHOT,
            },
            ["ref", "paths", "snapshot_id"],
        ),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
    ),
    ToolDeclaration(
        name="browser_console",
        toolset="browser",
        schema=_schema(
            "browser_console",
            "Run JavaScript in the current page after one-time approval; it may change page state.",
            {
                "expression": _STRING,
                "snapshot_id": _SNAPSHOT,
                "max_chars": {"type": "integer", "minimum": 1},
            },
            ["expression", "snapshot_id"],
        ),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
    ),
    ToolDeclaration(
        name="browser_delete_artifact",
        toolset="browser",
        schema=_schema(
            "browser_delete_artifact",
            "Delete one browser artifact after one-time approval.",
            {"artifact_id": _STRING},
            ["artifact_id"],
        ),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="medium",
        retry_safe=False,
        unknown_on_crash=True,
    ),
    ToolDeclaration(
        name="browser_cleanup_artifacts",
        toolset="browser",
        schema=_schema(
            "browser_cleanup_artifacts",
            "Delete all current-session browser artifacts after one-time approval.",
            {},
            [],
        ),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
    ),
    ToolDeclaration(
        name="browser_analyze_page",
        toolset="browser",
        schema=_schema(
            "browser_analyze_page",
            "Capture the current page, then send that exact screenshot to the configured external model after one-time approval.",
            {
                "snapshot_id": _SNAPSHOT,
                "prompt": _STRING,
                "full_page": {"type": "boolean", "default": False},
                "timeout_ms": _TIMEOUT,
            },
            ["snapshot_id", "prompt"],
        ),
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
    ),
)


TOOL_DECLARATIONS = _OPERATION_DECLARATIONS + _SPECIAL_DECLARATIONS
BROWSER_OPERATION_METHODS = MappingProxyType({
    name: method
    for name, method, *_ in _OPERATION_SPECS
})


__all__ = [
    "BROWSER_OPERATION_METHODS",
    "TOOL_DECLARATIONS",
]
