"""无运行时装配副作用的正式配置默认值与校验模型。"""

from __future__ import annotations

import copy
import math
import os
import re
from collections.abc import Mapping

from hermes.approval_security import ApprovalSecurityPolicy
from hermes.config_environment import ConfigEnvironment
from hermes.config_values import expand_env_vars as _expand_env_vars
from hermes.path_policy import PathAccessPolicy


DEFAULT_CONFIG = {
    "browser": {
        "enabled": False,
        "headless": True,
        "channel": "chrome",
        "idle_timeout_seconds": 1800.0,
        "startup_timeout_seconds": 30.0,
        "operation_timeout_seconds": 60.0,
    },
    "security": {
        "filesystem": {
            "denied_paths": [],
        },
        "approval": {
            "denied_command_patterns": [],
            "denied_executables": [],
            "protected_paths": [],
            "denied_file_rules": [],
            "approval_command_patterns": [
                r"\b(?:rm|rmdir|del|erase|remove-item)\b",
                r"\b(?:chmod|chown|chgrp|setfacl|icacls|takeown)\b",
                r"\b(?:git\s+push|curl|wget|scp|sftp|ssh|ftp)\b",
                r"\b(?:docker|kubectl|terraform|ansible)\b",
            ],
            "approval_file_rules": [],
            "remote_default_allow": True,
            "sensitive_file_patterns": [
                r"(^|/)\.env(\..*)?$",
                r"\.(key|pem|pfx|p12)$",
                r"/id_(rsa|dsa|ed25519|ecdsa)(\.pub)?$",
                r"\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$",
                r"(^|/)\.git($|/)",
            ],
            "request_ttl_seconds": 600.0,
            "intelligent_approval": {
                "enabled": False,
            },
        },
    },
    "terminal": {
        "docker_mounts": [],
    },
    "gateway": {
        "busy_input_mode": "steer",
        "platforms": {
            "cli": {"enabled": False},
            "feishu": {"enabled": False},
            "weixin": {"enabled": False},
        },
    },
    "background_review": {
        "enabled": False,
        "memory_interval": 3,
        "skill_tool_batch_interval": 0,
        "claim_ttl_seconds": 1800,
        "retry_cooldown_seconds": 60,
        "max_iterations": 8,
        "max_concurrent_jobs": 1,
        "max_pending_jobs": 32,
    },
    "plugins": {
        "enabled": [],
        "search_paths": [],
        "enable_project_plugins": False,
    },
}

GATEWAY_BUSY_INPUT_MODES = frozenset({"queue", "steer", "interrupt"})
DEFAULT_GATEWAY_BUSY_INPUT_MODE = "steer"


_SUPPORTED_BROWSER_CHANNELS = frozenset({
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
    "msedge-canary",
})
_PLUGIN_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_GATEWAY_PLATFORM_NAMES = ("cli", "feishu", "weixin")


def load_gateway_busy_input_mode(gateway_cfg: dict) -> str:
    """校验并返回 Gateway 的通用忙碌输入策略。"""
    if not isinstance(gateway_cfg, dict):
        raise ValueError("gateway must be a mapping")

    raw_mode = gateway_cfg.get(
        "busy_input_mode",
        DEFAULT_GATEWAY_BUSY_INPUT_MODE,
    )
    if not isinstance(raw_mode, str):
        raise ValueError(
            "gateway.busy_input_mode must be one of: interrupt, queue, steer"
        )
    mode = raw_mode.strip().lower()
    if mode not in GATEWAY_BUSY_INPUT_MODES:
        raise ValueError(
            "gateway.busy_input_mode must be one of: interrupt, queue, steer"
        )
    return mode


def _validate_gateway_config(config: dict) -> None:
    """补齐并校验 Gateway 的通用策略和平台启用开关。"""
    gateway = config.get("gateway")
    if gateway is None:
        gateway = {}
        config["gateway"] = gateway
    if not isinstance(gateway, dict):
        raise ValueError("gateway must be a mapping")

    gateway["busy_input_mode"] = load_gateway_busy_input_mode(gateway)

    platforms = gateway.get("platforms")
    if platforms is None:
        platforms = {}
        gateway["platforms"] = platforms
    if not isinstance(platforms, dict):
        raise ValueError("gateway.platforms must be a mapping")

    platform_defaults = DEFAULT_CONFIG["gateway"]["platforms"]
    for platform_name in _GATEWAY_PLATFORM_NAMES:
        platform = platforms.get(platform_name)
        if platform is None:
            platform = {}
            platforms[platform_name] = platform
        if not isinstance(platform, dict):
            raise ValueError(
                f"gateway.platforms.{platform_name} must be a mapping"
            )
        enabled = platform.get(
            "enabled",
            platform_defaults[platform_name]["enabled"],
        )
        if not isinstance(enabled, bool):
            raise ValueError(
                f"gateway.platforms.{platform_name}.enabled must be a boolean"
            )
        platform["enabled"] = enabled


def _validate_filesystem_security_config(config: dict) -> None:
    """补齐并校验统一文件系统策略配置。"""
    security = config.get("security")
    if security is None:
        security = {}
        config["security"] = security
    if not isinstance(security, dict):
        raise ValueError("security must be a mapping")

    filesystem = security.get("filesystem")
    if filesystem is None:
        filesystem = {}
        security["filesystem"] = filesystem
    if not isinstance(filesystem, dict):
        raise ValueError("security.filesystem must be a mapping")

    denied_paths = filesystem.get(
        "denied_paths",
        DEFAULT_CONFIG["security"]["filesystem"]["denied_paths"],
    )
    if not isinstance(denied_paths, list):
        raise ValueError(
            "security.filesystem.denied_paths must be a list"
        )
    for index, path in enumerate(denied_paths):
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                "security.filesystem.denied_paths entries must be "
                f"non-empty strings (invalid item at index {index})"
            )
    filesystem["denied_paths"] = list(denied_paths)

    approval = security.get("approval")
    if approval is None:
        approval = {}
        security["approval"] = approval
    if not isinstance(approval, dict):
        raise ValueError("security.approval must be a mapping")

    defaults = DEFAULT_CONFIG["security"]["approval"]
    for field in (
        "denied_command_patterns",
        "denied_executables",
        "protected_paths",
        "denied_file_rules",
        "approval_command_patterns",
        "approval_file_rules",
        "sensitive_file_patterns",
    ):
        value = approval.get(field, defaults[field])
        if not isinstance(value, list):
            raise ValueError(f"security.approval.{field} must be a list")
        approval[field] = list(value)

    for index, rule in enumerate(approval["approval_file_rules"]):
        if not isinstance(rule, dict):
            raise ValueError(
                "security.approval.approval_file_rules entries must be "
                f"mappings (invalid item at index {index})"
            )

    for index, pattern in enumerate(approval["sensitive_file_patterns"]):
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError(
                "security.approval.sensitive_file_patterns entries must be "
                f"non-empty strings (invalid item at index {index})"
            )
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                "security.approval.sensitive_file_patterns contains an "
                f"invalid regex at index {index}"
            ) from exc

    intelligent = approval.get(
        "intelligent_approval",
        defaults["intelligent_approval"],
    )
    if not isinstance(intelligent, dict):
        raise ValueError(
            "security.approval.intelligent_approval must be a mapping"
        )
    enabled = intelligent.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            "security.approval.intelligent_approval.enabled must be a boolean"
        )
    intelligent["enabled"] = enabled
    approval["intelligent_approval"] = intelligent

    remote_default_allow = approval.get(
        "remote_default_allow",
        defaults["remote_default_allow"],
    )
    if not isinstance(remote_default_allow, bool):
        raise ValueError(
            "security.approval.remote_default_allow must be a boolean"
        )
    approval["remote_default_allow"] = remote_default_allow

    raw_ttl_seconds = approval.get(
        "request_ttl_seconds",
        defaults["request_ttl_seconds"],
    )
    try:
        if isinstance(raw_ttl_seconds, bool):
            raise TypeError
        ttl_seconds = float(raw_ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "security.approval.request_ttl_seconds must be a positive number"
        ) from exc
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError(
            "security.approval.request_ttl_seconds must be a positive number"
        )
    approval["request_ttl_seconds"] = ttl_seconds

    # 构造一次不可变策略，让正则、File 规则和路径在配置加载阶段即失败。
    ApprovalSecurityPolicy(
        denied_command_patterns=approval["denied_command_patterns"],
        denied_executables=approval["denied_executables"],
        protected_paths=approval["protected_paths"],
        denied_file_rules=approval["denied_file_rules"],
        approval_command_patterns=approval["approval_command_patterns"],
        approval_file_rules=approval["approval_file_rules"],
        remote_default_allow=remote_default_allow,
        intelligent_approval_enabled=enabled,
        cwd=os.getcwd(),
    )


def _validate_terminal_backend_config(config: dict) -> None:
    """校验 Docker 宿主挂载描述，供 backend 风险画像使用。"""
    terminal = config.get("terminal")
    if terminal is None:
        terminal = {}
        config["terminal"] = terminal
    if not isinstance(terminal, dict):
        raise ValueError("terminal must be a mapping")
    mounts = terminal.get(
        "docker_mounts",
        DEFAULT_CONFIG["terminal"]["docker_mounts"],
    )
    if not isinstance(mounts, list):
        raise ValueError("terminal.docker_mounts must be a list")
    filesystem_policy = PathAccessPolicy(
        config["security"]["filesystem"]["denied_paths"],
        cwd=os.getcwd(),
    )
    normalized_mounts: list[dict] = []
    for index, mount in enumerate(mounts):
        if not isinstance(mount, dict):
            raise ValueError(
                "terminal.docker_mounts entries must be mappings "
                f"(invalid item at index {index})"
            )
        source = mount.get("source")
        target = mount.get("target")
        read_only = mount.get("read_only", False)
        if not isinstance(source, str) or not source.strip():
            raise ValueError(
                "terminal.docker_mounts source must be a non-empty string "
                f"(invalid item at index {index})"
            )
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or target == "/"
        ):
            raise ValueError(
                "terminal.docker_mounts target must be an absolute non-root "
                f"container path (invalid item at index {index})"
            )
        if not isinstance(read_only, bool):
            raise ValueError(
                "terminal.docker_mounts read_only must be a boolean "
                f"(invalid item at index {index})"
            )
        if filesystem_policy.intersects_denied_tree(
            source,
            cwd=os.getcwd(),
        ):
            raise ValueError(
                "terminal.docker_mounts source intersects the configured "
                f"filesystem deny policy (invalid item at index {index})"
            )
        normalized_mounts.append({
            "source": source,
            "target": target,
            "read_only": read_only,
        })
    terminal["docker_mounts"] = normalized_mounts


def _validate_background_review_config(config: dict) -> None:
    """补齐并校验后台审视配置，默认关闭以避免意外模型调用。"""
    review = config.get("background_review")
    if review is None:
        review = {}
        config["background_review"] = review
    if not isinstance(review, dict):
        raise ValueError("background_review must be a mapping")

    defaults = DEFAULT_CONFIG["background_review"]
    enabled = review.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("background_review.enabled must be a boolean")
    review["enabled"] = enabled

    for field_name in ("memory_interval", "skill_tool_batch_interval"):
        value = review.get(field_name, defaults[field_name])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"background_review.{field_name} must be a non-negative integer"
            )
        review[field_name] = value
    for field_name in ("claim_ttl_seconds", "retry_cooldown_seconds"):
        value = review.get(field_name, defaults[field_name])
        if isinstance(value, bool):
            raise ValueError(f"background_review.{field_name} must be a number")
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"background_review.{field_name} must be a number"
            ) from exc
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError(
                f"background_review.{field_name} must be non-negative"
            )
        if field_name == "claim_ttl_seconds" and normalized == 0:
            raise ValueError("background_review.claim_ttl_seconds must be positive")
        review[field_name] = normalized

    for field_name in ("max_iterations", "max_concurrent_jobs"):
        value = review.get(field_name, defaults[field_name])
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"background_review.{field_name} must be a positive integer"
            )
        review[field_name] = value

    max_pending_jobs = review.get(
        "max_pending_jobs",
        defaults["max_pending_jobs"],
    )
    if (
        isinstance(max_pending_jobs, bool)
        or not isinstance(max_pending_jobs, int)
        or max_pending_jobs < 0
    ):
        raise ValueError(
            "background_review.max_pending_jobs must be a non-negative integer"
        )
    review["max_pending_jobs"] = max_pending_jobs


def _validate_plugins_config(config: dict) -> None:
    """补齐并校验显式 Plugin 加载配置，避免运行会话时才暴露错误。"""
    plugins = config.get("plugins")
    if plugins is None:
        plugins = {}
        config["plugins"] = plugins
    if not isinstance(plugins, dict):
        raise ValueError("plugins must be a mapping")

    defaults = DEFAULT_CONFIG["plugins"]
    enabled = plugins.get("enabled", defaults["enabled"])
    if not isinstance(enabled, list) or any(
        not isinstance(name, str) or not _PLUGIN_NAME_PATTERN.fullmatch(name)
        for name in enabled
    ):
        raise ValueError("plugins.enabled must contain valid plugin names")
    if len(set(enabled)) != len(enabled):
        raise ValueError("plugins.enabled must not contain duplicates")
    plugins["enabled"] = list(enabled)

    search_paths = plugins.get("search_paths", defaults["search_paths"])
    if not isinstance(search_paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in search_paths
    ):
        raise ValueError("plugins.search_paths must contain non-empty strings")
    plugins["search_paths"] = list(search_paths)

    enable_project_plugins = plugins.get(
        "enable_project_plugins",
        defaults["enable_project_plugins"],
    )
    if not isinstance(enable_project_plugins, bool):
        raise ValueError("plugins.enable_project_plugins must be a boolean")
    plugins["enable_project_plugins"] = enable_project_plugins


def _validate_browser_config(config: dict) -> None:
    """补齐并校验浏览器运行时配置，避免工具调用时才暴露配置错误。"""
    browser = config.get("browser")
    if browser is None:
        browser = {}
        config["browser"] = browser
    if not isinstance(browser, dict):
        raise ValueError("browser must be a mapping")

    defaults = DEFAULT_CONFIG["browser"]
    for name in ("enabled", "headless"):
        value = browser.get(name, defaults[name])
        if not isinstance(value, bool):
            raise ValueError(f"browser.{name} must be a boolean")
        browser[name] = value

    raw_channel = browser.get("channel", defaults["channel"])
    if raw_channel is None:
        channel = None
    elif not isinstance(raw_channel, str):
        raise ValueError("browser.channel must be a string or null")
    else:
        channel = raw_channel.strip() or None
        if channel is not None and channel not in _SUPPORTED_BROWSER_CHANNELS:
            raise ValueError(
                "browser.channel must be null, empty, or one of: "
                f"{sorted(_SUPPORTED_BROWSER_CHANNELS)}"
            )
    browser["channel"] = channel

    for name in (
        "idle_timeout_seconds",
        "startup_timeout_seconds",
        "operation_timeout_seconds",
    ):
        raw_value = browser.get(name, defaults[name])
        if isinstance(raw_value, bool):
            raise ValueError(f"browser.{name} must be a positive number")
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"browser.{name} must be a positive number"
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"browser.{name} must be a positive number")
        browser[name] = value


def validate_config_mapping(
    raw: Mapping[str, object],
    *,
    expand_environment: bool = True,
    environment: ConfigEnvironment | None = None,
) -> dict:
    """返回经过完整正式校验的独立配置副本。"""
    if not isinstance(raw, Mapping):
        raise ValueError("config must be a mapping")
    if environment is not None and not isinstance(
        environment,
        ConfigEnvironment,
    ):
        raise TypeError("environment must be a ConfigEnvironment or None")

    config = copy.deepcopy(dict(raw))
    if expand_environment:
        config = (
            _expand_env_vars(config)
            if environment is None
            else environment.expand(config)
        )

    _validate_gateway_config(config)
    _validate_filesystem_security_config(config)
    _validate_terminal_backend_config(config)
    _validate_background_review_config(config)
    _validate_plugins_config(config)
    _validate_browser_config(config)
    return config
