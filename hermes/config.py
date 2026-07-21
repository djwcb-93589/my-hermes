"""
Configuration: .env loading, config.yaml parsing, and module constants.

This is the foundation module — imported by everything else. Loading this module
triggers load_env() and load_config() as side effects, exactly as the original
s15 file did at import time.
"""

from __future__ import annotations

import os
import re
import math

import yaml
from openai import AsyncOpenAI, OpenAI

from hermes.approval_policy import ApprovalSecurityPolicy
from hermes.path_policy import PathAccessPolicy


DEFAULT_CONFIG = {
    "security": {
        "filesystem": {
            "denied_paths": [],
        },
        "approval": {
            "denied_command_patterns": [],
            "denied_executables": [],
            "protected_paths": [],
            "denied_file_rules": [],
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
}


def _expand_env_vars(value):
    """Recursively resolve ${VAR} references in config values."""
    if isinstance(value, str):
        def replacer(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))
        return re.sub(r'\$\{(\w+)\}', replacer, value)

    elif isinstance(value, dict):
        return {
            key: _expand_env_vars(val)
            for key, val in value.items()
        }

    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]

    return value


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
        "sensitive_file_patterns",
    ):
        value = approval.get(field, defaults[field])
        if not isinstance(value, list):
            raise ValueError(f"security.approval.{field} must be a list")
        approval[field] = list(value)

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


def load_env(env_path=None):
    """Read a .env file and set as environment variables (simple implementation)."""
    from pathlib import Path
    if env_path is None:
        env_path = HERMES_HOME / ".env"

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


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
    config = _expand_env_vars(config)
    _validate_filesystem_security_config(config)
    _validate_terminal_backend_config(config)
    return config


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
_PROJECT_ROOT = _Path(__file__).resolve().parent.parent
HERMES_HOME = _Path(os.getenv("HERMES_HOME") or _PROJECT_ROOT)
CONFIG_PATH = HERMES_HOME / "config.yaml"

load_env()

_config = load_config(CONFIG_PATH)

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
