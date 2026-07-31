"""
Configuration: .env loading, config.yaml parsing, and module constants.

This is the foundation module — imported by everything else. Loading this module
triggers load_env() and load_config() as side effects, exactly as the original
s15 file did at import time.
"""

from __future__ import annotations

import os
import re
from types import MappingProxyType

import yaml
from openai import AsyncOpenAI, OpenAI

from hermes.approval_security import ApprovalSecurityPolicy
from hermes.config_model import (
    DEFAULT_CONFIG as DEFAULT_CONFIG,
    DEFAULT_GATEWAY_BUSY_INPUT_MODE as DEFAULT_GATEWAY_BUSY_INPUT_MODE,
    GATEWAY_BUSY_INPUT_MODES as GATEWAY_BUSY_INPUT_MODES,
    _PLUGIN_NAME_PATTERN as _PLUGIN_NAME_PATTERN,
    _SUPPORTED_BROWSER_CHANNELS as _SUPPORTED_BROWSER_CHANNELS,
    _validate_background_review_config as _validate_background_review_config,
    _validate_browser_config as _validate_browser_config,
    _validate_filesystem_security_config as _validate_filesystem_security_config,
    _validate_gateway_config as _validate_gateway_config,
    _validate_plugins_config as _validate_plugins_config,
    _validate_terminal_backend_config as _validate_terminal_backend_config,
    load_gateway_busy_input_mode as load_gateway_busy_input_mode,
    validate_config_mapping,
)
from hermes.config_values import (
    expand_env_vars as _expand_env_vars,
    hermes_home,
    load_env_values as _load_env_values,
)
from hermes.path_policy import PathAccessPolicy


def load_env(env_path=None):
    """兼容旧接口，并复用轻量 .env 加载实现。"""
    if env_path is None:
        env_path = HERMES_HOME / ".env"
    return _load_env_values(env_path)


def load_config(config_path=None) -> dict:
    """加载 config.yaml 并展开环境变量。配置缺失或非法时明确报错。"""
    from pathlib import Path
    if config_path is None:
        config_path = HERMES_HOME / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to load config file: {config_path}") from exc

    if not isinstance(config, dict):
        raise ValueError(f"config file must contain a mapping: {config_path}")
    return validate_config_mapping(config, expand_environment=True)


def save_config(config: dict, config_path=None):
    """Save config to config.yaml with 0600 file permissions."""
    from pathlib import Path
    if config_path is None:
        config_path = HERMES_HOME / "config.yaml"

    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(
        config,
        default_flow_style=False,
        allow_unicode=True,
    )
    config_path.write_text(text, encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        # Windows: chmod is mostly a no-op for 0600; ignore.
        pass


# ===========================================================================
# Profile + module-level constants
# ===========================================================================

from pathlib import Path as _Path

# Project root = parent of the hermes/ package directory. Falls back to
# $HERMES_HOME if set; never touches the user's home directory.
HERMES_HOME = hermes_home()
CONFIG_PATH = HERMES_HOME / "config.yaml"

load_env()

_config = load_config(CONFIG_PATH)

# 仅供 Hermes 入口和 Browser 工具适配层读取；browser 包本身不反向依赖 Hermes。
BROWSER_CONFIG = dict(_config["browser"])
BACKGROUND_REVIEW_CONFIG = MappingProxyType(dict(_config["background_review"]))

PATH_ACCESS_POLICY = PathAccessPolicy(
    _config["security"]["filesystem"]["denied_paths"],
    cwd=os.getcwd(),
)


def _hardline_protected_paths() -> tuple[str, ...]:
    """返回模型不可修改的审批配置和当前系统安全关键路径。"""
    paths = [str(CONFIG_PATH)]
    if os.name == "nt":
        system_root = os.getenv("SystemRoot") or r"C:\Windows"
        paths.append(os.path.join(system_root, "System32", "config"))
    else:
        paths.extend([
            "/etc/passwd",
            "/etc/shadow",
            "/etc/group",
            "/etc/gshadow",
            "/etc/sudoers",
            "/etc/sudoers.d",
            "/etc/ssh/sshd_config",
            "/etc/pam.d",
            "/etc/security",
        ])
    return tuple(paths)


_approval_cfg = _config["security"]["approval"]
SENSITIVE_FILE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in _approval_cfg["sensitive_file_patterns"]
)
APPROVAL_SECURITY_POLICY = ApprovalSecurityPolicy(
    denied_command_patterns=_approval_cfg["denied_command_patterns"],
    denied_executables=_approval_cfg["denied_executables"],
    protected_paths=_approval_cfg["protected_paths"],
    denied_file_rules=_approval_cfg["denied_file_rules"],
    approval_command_patterns=_approval_cfg["approval_command_patterns"],
    approval_file_rules=_approval_cfg["approval_file_rules"],
    remote_default_allow=_approval_cfg["remote_default_allow"],
    hardline_protected_paths=_hardline_protected_paths(),
    intelligent_approval_enabled=(
        _approval_cfg["intelligent_approval"]["enabled"]
    ),
    cwd=os.getcwd(),
)
APPROVAL_REQUEST_TTL_SECONDS = float(
    _approval_cfg["request_ttl_seconds"]
)

BASE_URL = os.getenv("OPENAI_BASE_URL") or _config["base_url"]
API_KEY = os.getenv("OPENAI_API_KEY") or _config["api_key"]
MODEL = os.getenv("MODEL") or _config["model"]


def _positive_int_setting(value, name: str) -> int:
    """把模型额度配置规范化为正整数。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        normalized = int(value.strip())
    else:
        raise ValueError(f"{name} must be a positive integer")
    if normalized <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return normalized


MODEL_MAX_OUTPUT_TOKENS = _positive_int_setting(
    os.getenv("MODEL_MAX_OUTPUT_TOKENS")
    or _config.get("max_output_tokens", 8192),
    "max_output_tokens",
)
MAX_ITERATIONS = int(
    os.getenv("MAX_ITERATIONS") or _config["limits"]["max_iterations"]
)
# Resolve DB_PATH relative to HERMES_HOME so the database lives inside the
# project (e.g. <root>/database/hermes.db) regardless of CWD.
DB_PATH = os.getenv("DB_PATH") or _config["db_path"]
if not _Path(DB_PATH).is_absolute():
    DB_PATH = str(HERMES_HOME / DB_PATH)

FALLBACK_MODEL = (
    os.getenv("FALLBACK_MODEL") or _config["fallback"]["model"]
)
FALLBACK_BASE_URL = (
    os.getenv("FALLBACK_BASE_URL")
    or _config["fallback"]["base_url"]
    or BASE_URL
)
FALLBACK_API_KEY = (
    os.getenv("FALLBACK_API_KEY")
    or _config["fallback"]["api_key"]
    or API_KEY
)
FALLBACK_MAX_OUTPUT_TOKENS = _positive_int_setting(
    os.getenv("FALLBACK_MAX_OUTPUT_TOKENS")
    or _config["fallback"].get("max_output_tokens", 8192),
    "fallback.max_output_tokens",
)

COMPRESSION_THRESHOLD = _config["compression"]["threshold"]
PROTECT_FIRST = _config["compression"]["protect_first"]
KEEP_RECENT_TOOL_RESULTS = _config["compression"]["keep_recent_tool_results"]
TAIL_TOKEN_BUDGET = _config["compression"]["tail_token_budget"]
MAX_RETRIES = _config["limits"]["max_retries"]
MAX_CONTINUATIONS = _config["limits"]["max_continuations"]
MAX_CHILD_ITERATIONS = _config["limits"]["max_child_iterations"]
MODEL_TIMEOUT_SECONDS = float(
    os.getenv("MODEL_TIMEOUT_SECONDS")
    or _config["limits"].get("model_timeout_seconds", 120)
)
if MODEL_TIMEOUT_SECONDS <= 0:
    raise ValueError("model_timeout_seconds must be greater than 0")
MEMORY_CHAR_LIMIT = _config["memory"]["memory_char_limit"]
USER_CHAR_LIMIT = _config["memory"]["user_char_limit"]

CONTINUE_MESSAGE = "Please continue from where you left off."

GATEWAY_AGENT_NAME = _config.get("gateway", {}).get("agent_name", "main")

client = OpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    timeout=MODEL_TIMEOUT_SECONDS,
)


def create_async_client() -> AsyncOpenAI:
    """创建 Gateway 专用异步模型客户端,由调用方负责关闭。"""
    return AsyncOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        timeout=MODEL_TIMEOUT_SECONDS,
    )
