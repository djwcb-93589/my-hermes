"""Dashboard 启动配置的无副作用读取与安全校验。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from hermes.config_values import hermes_home
from hermes.web.security import (
    ControlAuthenticator,
    is_loopback_host,
    normalize_dashboard_host,
)


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_AUTH_REQUIRED = False
_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")


class DashboardConfigurationError(ValueError):
    """Dashboard 配置不满足安全启动条件。"""


@dataclass(frozen=True)
class DashboardConfig:
    """正式 Dashboard 实例所需的已规范化配置。"""

    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    auth_required: bool = _DEFAULT_AUTH_REQUIRED
    db_path: str | None = None
    control_token_digest: str | None = None
    config_path: str | None = None


def load_dashboard_config() -> DashboardConfig:
    """按环境变量、profile、YAML、默认值顺序解析 Dashboard 配置。"""
    profile_home = hermes_home()
    profile_env = _read_profile_env(profile_home)
    config_path = profile_home / "config.yaml"
    raw_config = _read_config_mapping(profile_home)
    dashboard = raw_config.get("dashboard", {})
    if dashboard is None:
        dashboard = {}
    if not isinstance(dashboard, dict):
        raise DashboardConfigurationError("dashboard must be a mapping")

    host = _parse_host(
        _setting_value(
            "HERMES_DASHBOARD_HOST",
            dashboard,
            "host",
            _DEFAULT_HOST,
            profile_env,
        )
    )
    port = _parse_port(
        _setting_value(
            "HERMES_DASHBOARD_PORT",
            dashboard,
            "port",
            _DEFAULT_PORT,
            profile_env,
        )
    )
    configured_auth_required = _parse_bool(
        _setting_value(
            "HERMES_DASHBOARD_AUTH_REQUIRED",
            dashboard,
            "auth_required",
            _DEFAULT_AUTH_REQUIRED,
            profile_env,
        ),
        "dashboard.auth_required",
    )
    control_token = _environment_or_profile_value(
        "HERMES_WEB_CONTROL_TOKEN",
        profile_env,
    )
    control_token_digest = _token_digest(control_token)
    db_path = _resolve_db_path(raw_config, profile_home, profile_env)

    # 非回环地址即使显式关闭认证也必须提升为认证模式。
    auth_required = configured_auth_required or not is_loopback_host(host)
    config = DashboardConfig(
        host=host,
        port=port,
        auth_required=auth_required,
        db_path=db_path,
        control_token_digest=control_token_digest,
        config_path=str(config_path),
    )
    validate_dashboard_config(config)
    return config


def validate_dashboard_config(config: DashboardConfig) -> None:
    """校验直接构造的配置也不能绕过绑定和认证安全边界。"""
    if not isinstance(config, DashboardConfig):
        raise DashboardConfigurationError("dashboard config is invalid")
    _parse_host(config.host)
    _parse_port(config.port)
    if not isinstance(config.auth_required, bool):
        raise DashboardConfigurationError("dashboard.auth_required must be a boolean")
    if config.db_path is not None and not isinstance(config.db_path, str):
        raise DashboardConfigurationError("dashboard db_path must be a string or null")
    if config.config_path is not None and (
        not isinstance(config.config_path, str)
        or not config.config_path.strip()
    ):
        raise DashboardConfigurationError(
            "dashboard config_path must be a non-empty string or null"
        )
    if not ControlAuthenticator.is_valid_digest(config.control_token_digest):
        raise DashboardConfigurationError("dashboard control token digest is invalid")
    if not is_loopback_host(config.host) and not config.auth_required:
        raise DashboardConfigurationError(
            "dashboard authentication is required for a non-loopback host"
        )
    if config.auth_required and config.control_token_digest is None:
        raise DashboardConfigurationError(
            "HERMES_WEB_CONTROL_TOKEN is required when dashboard authentication is enabled"
        )


def _read_profile_env(profile_home: Path) -> dict[str, str]:
    """轻量读取 profile .env，且不修改当前进程环境。"""
    try:
        lines = (profile_home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, _, value = candidate.partition("=")
        name = key.strip()
        if not name or name in values:
            continue
        normalized = value.strip()
        if (
            len(normalized) >= 2
            and normalized[0] in {"'", '"'}
            and normalized[-1] == normalized[0]
        ):
            normalized = normalized[1:-1]
        values[name] = normalized
    return values


def _read_config_mapping(profile_home: Path) -> dict[str, object]:
    """读取 YAML 原始映射，不触发完整 Agent 配置模块的导入副作用。"""
    config_path = profile_home / "config.yaml"
    try:
        if not config_path.exists():
            return {}
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DashboardConfigurationError("dashboard config file cannot be loaded") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DashboardConfigurationError("dashboard config file must contain a mapping")
    return raw


def _setting_value(
    env_name: str,
    mapping: dict[str, object],
    key: str,
    default: object,
    profile_env: dict[str, str],
) -> object:
    """取得单个 Dashboard 配置，并保留环境变量的最高优先级。"""
    value = _environment_or_profile_value(env_name, profile_env)
    if value is not None:
        return value
    if key not in mapping:
        return default
    return _expand_value(mapping[key], profile_env)


def _environment_or_profile_value(
    name: str,
    profile_env: dict[str, str],
) -> str | None:
    """进程环境优先于 profile .env，并保留空值供调用方校验。"""
    if name in os.environ:
        return os.environ[name]
    return profile_env.get(name)


def _resolve_db_path(
    raw_config: dict[str, object],
    profile_home: Path,
    profile_env: dict[str, str],
) -> str | None:
    """按生产配置同一优先级解析数据库路径，但不打开或创建数据库。"""
    configured_path = _environment_or_profile_value("DB_PATH", profile_env)
    if configured_path is None and "db_path" in raw_config:
        configured_path = _expand_value(raw_config["db_path"], profile_env)
    if not isinstance(configured_path, str) or not configured_path.strip():
        return None
    if _ENV_REFERENCE.search(configured_path):
        return None
    path = Path(configured_path)
    return str(path if path.is_absolute() else profile_home / path)


def _expand_value(value: object, profile_env: dict[str, str]) -> object:
    """按进程环境优先、profile 补充的规则展开 YAML 字符串引用。"""
    if not isinstance(value, str):
        return value
    expanded = os.path.expandvars(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, profile_env.get(name, match.group(0)))

    return _ENV_REFERENCE.sub(replace, expanded)


def _parse_host(value: object) -> str:
    """规范化 Uvicorn 绑定主机，并拒绝空白或非字符串配置。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _ENV_REFERENCE.search(value)
    ):
        raise DashboardConfigurationError("dashboard.host must be a non-empty string")
    normalized = normalize_dashboard_host(value)
    if normalized is None:
        raise DashboardConfigurationError("dashboard.host must be a non-empty string")
    if value.startswith("[") or value.endswith("]"):
        if normalized != "::1":
            raise DashboardConfigurationError(
                "dashboard.host only accepts brackets for [::1]"
            )
    return normalized


def _parse_port(value: object) -> int:
    """解析端口并显式拒绝布尔值伪装成整数。"""
    if isinstance(value, bool):
        raise DashboardConfigurationError("dashboard.port must be an integer from 1 to 65535")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        port = int(value.strip())
    else:
        raise DashboardConfigurationError("dashboard.port must be an integer from 1 to 65535")
    if not 1 <= port <= 65535:
        raise DashboardConfigurationError("dashboard.port must be an integer from 1 to 65535")
    return port


def _parse_bool(value: object, name: str) -> bool:
    """解析 YAML 或环境变量布尔值，避免 bool("false") 误判。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise DashboardConfigurationError(f"{name} must be a boolean")


def _token_digest(token: str | None) -> str | None:
    """将 Token 立即转为摘要；显式提供的非法 Token 直接拒绝启动。"""
    if token is None:
        return None
    digest = ControlAuthenticator.digest_token(token)
    if digest is None:
        raise DashboardConfigurationError(
            "HERMES_WEB_CONTROL_TOKEN must be a trimmed string of at least 32 characters"
        )
    return digest
