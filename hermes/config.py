"""
Configuration: DEFAULT_CONFIG, env loading, config merging, and module constants.

This is the foundation module — imported by everything else. Loading this module
triggers load_env() and load_config() as side effects, exactly as the original
s15 file did at import time.
"""

from __future__ import annotations

import os
import re

import yaml
from openai import OpenAI


DEFAULT_CONFIG = {
    "model": "anthropic/claude-sonnet-4",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    "fallback": {
        "model": "",
        "base_url": "",
        "api_key": "",
    },
    "limits": {
        "max_iterations": 40,
        "max_child_iterations": 20,
        "max_retries": 3,
        "max_continuations": 3,
    },
    "compression": {
        "threshold": 180000,
        "protect_first": 4,
        "keep_recent_tool_results": 4,
        "tail_token_budget": 60000,
    },
    "memory": {
        "memory_char_limit": 4000,
        "user_char_limit": 2000,
    },
    "db_path": "database/hermes.db",
    "terminal": {
        "backend": "local",          # local | docker | ssh
        "docker_image": "python:3.11-slim",
        "ssh_host": "",              # required when backend=ssh
        "ssh_user": "",              # required when backend=ssh
        "ssh_key": "",               # optional path to private key
    },
    "gateway": {
        "session_idle_timeout": 86400,
        "agent_name": "main",
        "platforms": {},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge two dicts. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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
    """Load config.yaml, deep merge with defaults, expand env vars."""
    from pathlib import Path
    if config_path is None:
        config_path = HERMES_HOME / "config.yaml"

    if not config_path.exists():
        return _expand_env_vars(DEFAULT_CONFIG.copy())

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        user_config = yaml.safe_load(raw_text) or {}
    except Exception:
        user_config = {}

    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    return _expand_env_vars(merged)


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

load_env()

_config = load_config()

BASE_URL = os.getenv("OPENAI_BASE_URL") or _config["base_url"]
API_KEY = os.getenv("OPENAI_API_KEY") or _config["api_key"]
MODEL = os.getenv("MODEL") or _config["model"]
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

COMPRESSION_THRESHOLD = _config["compression"]["threshold"]
PROTECT_FIRST = _config["compression"]["protect_first"]
KEEP_RECENT_TOOL_RESULTS = _config["compression"]["keep_recent_tool_results"]
TAIL_TOKEN_BUDGET = _config["compression"]["tail_token_budget"]
MAX_RETRIES = _config["limits"]["max_retries"]
MAX_CONTINUATIONS = _config["limits"]["max_continuations"]
MAX_CHILD_ITERATIONS = _config["limits"]["max_child_iterations"]
MEMORY_CHAR_LIMIT = _config["memory"]["memory_char_limit"]
USER_CHAR_LIMIT = _config["memory"]["user_char_limit"]

CONTINUE_MESSAGE = "Please continue from where you left off."

GATEWAY_AGENT_NAME = _config.get("gateway", {}).get("agent_name", "main")

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
