"""启动本地只读 Web 管理 API。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def _web_db_path() -> str | None:
    """只读取启动配置中的数据库路径，避免导入会创建模型客户端的配置模块。"""
    configured_path = os.getenv("DB_PATH")
    project_root = Path(__file__).resolve().parents[2]
    hermes_home = Path(os.getenv("HERMES_HOME") or project_root)
    if not configured_path:
        configured_path = _profile_db_path(hermes_home)
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
    path = Path(os.path.expandvars(configured_path))
    return str(path if path.is_absolute() else hermes_home / path)


def _profile_db_path(hermes_home: Path) -> str | None:
    """按既有配置的优先顺序仅读取 profile 中的 DB_PATH。"""
    try:
        lines = (hermes_home / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" not in candidate:
            continue
        key, _, value = candidate.partition("=")
        if key.strip() == "DB_PATH":
            return value.strip().strip('"').strip("'")
    return None


def main() -> None:
    """仅绑定本机回环地址，避免意外暴露管理接口。"""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Web 管理 API 需要安装 FastAPI 和 Uvicorn。"
        ) from exc

    from hermes.web.app import create_app
    from hermes.web.read_service import ReadService

    app = create_app(ReadService(_web_db_path()))

    # 关闭访问日志，避免把查询参数或会话标识写入日志。
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=False,
    )
