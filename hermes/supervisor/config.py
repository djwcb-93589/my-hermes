"""Supervisor profile 配置和固定 Gateway 启动环境装配。"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml

from hermes.config_environment import ConfigEnvironment, environment_reference_keys
from hermes.config_values import PROJECT_ROOT, hermes_home


_ENV_REFERENCE = re.compile(r"\$\{[^}]+\}")
_MAX_CONFIG_BYTES = 4 * 1024 * 1024


class SupervisorConfigurationError(ValueError):
    """Supervisor 无法安全确定 profile 或固定启动规范。"""


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    profile_home: Path
    config_path: Path
    db_path: str
    project_root: Path
    python_executable: str
    launch_environment: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("profile_home", "config_path", "project_root"):
            if not isinstance(getattr(self, name), Path):
                raise TypeError(f"supervisor {name} must be a Path")
        if type(self.db_path) is not str or not self.db_path.strip():
            raise ValueError("supervisor db_path must be non-empty")
        if type(self.python_executable) is not str or not self.python_executable:
            raise ValueError("supervisor python executable is invalid")
        if not isinstance(self.launch_environment, Mapping):
            raise TypeError("supervisor launch environment must be a mapping")
        copied: dict[str, str] = {}
        for key, value in self.launch_environment.items():
            if type(key) is not str or not key or type(value) is not str:
                raise ValueError("supervisor launch environment is invalid")
            copied[key] = value
        object.__setattr__(
            self,
            "launch_environment",
            MappingProxyType(copied),
        )


def load_supervisor_config() -> SupervisorConfig:
    """不导入 ``hermes.config``，按正式优先级解析 Supervisor 所需值。"""
    try:
        profile_home = hermes_home().resolve()
        config_path = profile_home / "config.yaml"
        profile_environment = _read_profile_environment(profile_home / ".env")
        raw_config = _read_config(config_path)
        config_environment = ConfigEnvironment.from_sources(
            allowed_keys=environment_reference_keys(raw_config),
            process_environment=os.environ,
            profile_environment=profile_environment,
        )
        expanded_config = config_environment.expand(raw_config)
        if not isinstance(expanded_config, dict):
            raise SupervisorConfigurationError("supervisor config is invalid")
        direct_db_path = _source_value(
            "DB_PATH",
            profile_environment,
        )
        configured_db_path = direct_db_path or expanded_config.get("db_path")
        if (
            type(configured_db_path) is not str
            or not configured_db_path.strip()
            or _ENV_REFERENCE.search(configured_db_path)
        ):
            raise SupervisorConfigurationError("supervisor db_path is unavailable")
        db_path = Path(configured_db_path)
        if not db_path.is_absolute():
            db_path = profile_home / db_path

        environment = dict(profile_environment)
        environment.update(os.environ)
        environment["HERMES_HOME"] = str(profile_home)
        environment.setdefault("PYTHONUNBUFFERED", "1")
        return SupervisorConfig(
            profile_home=profile_home,
            config_path=config_path,
            db_path=str(db_path),
            project_root=PROJECT_ROOT.resolve(),
            python_executable=sys.executable,
            launch_environment=environment,
        )
    except SupervisorConfigurationError:
        raise
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise SupervisorConfigurationError(
            "supervisor configuration cannot be loaded"
        ) from exc


def _read_profile_environment(path: Path) -> dict[str, str]:
    """读取 profile `.env` 到局部快照，不修改当前进程环境。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SupervisorConfigurationError(
            "supervisor profile environment cannot be read"
        ) from exc
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


def _read_config(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(payload) > _MAX_CONFIG_BYTES:
            raise SupervisorConfigurationError("supervisor config is too large")
        loaded = yaml.safe_load(payload.decode("utf-8-sig"))
    except FileNotFoundError as exc:
        raise SupervisorConfigurationError("supervisor config file is missing") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SupervisorConfigurationError("supervisor config file is invalid") from exc
    if not isinstance(loaded, dict):
        raise SupervisorConfigurationError("supervisor config must be a mapping")
    return loaded


def _source_value(
    name: str,
    profile_environment: Mapping[str, str],
) -> str | None:
    if name in os.environ:
        return os.environ[name]
    return profile_environment.get(name)


__all__ = [
    "SupervisorConfig",
    "SupervisorConfigurationError",
    "load_supervisor_config",
]
