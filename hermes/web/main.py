"""启动本地只读 Web 管理 API。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml


def _hermes_home() -> Path:
    """解析 profile 根目录，不导入完整配置模块。"""
    project_root = Path(__file__).resolve().parents[2]
    return Path(os.getenv("HERMES_HOME") or project_root)


def _web_db_path(profile_env: dict[str, str] | None = None) -> str | None:
    """只读取启动配置中的数据库路径，避免导入会创建模型客户端的配置模块。"""
    hermes_home = _hermes_home()
    profile_env = profile_env if profile_env is not None else _profile_env(hermes_home)
    configured_path = os.getenv("DB_PATH")
    if not configured_path:
        configured_path = profile_env.get("DB_PATH")
    if not configured_path:
        try:
            raw_config = yaml.safe_load(
                (hermes_home / "config.yaml").read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError):
            return None
        configured_path = raw_config.get("db_path") if isinstance(raw_config, dict) else None

    if not isinstance(configured_path, str) or not configured_path.strip():
        return None
    expanded_path = _expand_db_path(configured_path, profile_env)
    if expanded_path is None:
        return None
    path = Path(expanded_path)
    return str(path if path.is_absolute() else hermes_home / path)


def _profile_env(hermes_home: Path) -> dict[str, str]:
    """以既有简单规则读取 .env，不修改进程环境。"""
    try:
        lines = (hermes_home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, _, value = candidate.partition("=")
        normalized_key = key.strip()
        if not normalized_key or normalized_key in values:
            continue
        normalized_value = value.strip()
        if (
            len(normalized_value) >= 2
            and normalized_value[0] in {"'", '"'}
            and normalized_value[-1] == normalized_value[0]
        ):
            normalized_value = normalized_value[1:-1]
        values[normalized_key] = normalized_value
    return values


_ENV_REFERENCE = re.compile(r"\$\{([^}]+)\}")


def _expand_db_path(value: str, profile_env: dict[str, str]) -> str | None:
    """按进程环境优先、profile 补充的规则展开 ${VAR}。"""
    expanded = os.path.expandvars(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, profile_env.get(name, match.group(0)))

    expanded = _ENV_REFERENCE.sub(replace, expanded)
    return None if _ENV_REFERENCE.search(expanded) else expanded


def main() -> None:
    """仅绑定本机回环地址，避免意外暴露管理接口。"""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Web 管理 API 需要安装 FastAPI 和 Uvicorn。"
        ) from exc

    from hermes.web.app import create_app
    from hermes.web.control_service import CronControlService
    from hermes.web.read_service import ReadService
    from hermes.web.security import ControlAuthenticator

    hermes_home = _hermes_home()
    profile_env = _profile_env(hermes_home)
    db_path = _web_db_path(profile_env)
    control_token = os.getenv("HERMES_WEB_CONTROL_TOKEN") or profile_env.get(
        "HERMES_WEB_CONTROL_TOKEN"
    )
    app = create_app(
        ReadService(db_path),
        CronControlService(db_path),
        ControlAuthenticator.from_token(control_token),
    )

    # 关闭访问日志，避免把查询参数或会话标识写入日志。
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=False,
    )
