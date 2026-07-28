"""不依赖运行时组件的配置值处理 helper。"""

from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def hermes_home() -> Path:
    """返回与生产配置一致的 Hermes 用户配置目录。"""
    return Path(os.getenv("HERMES_HOME") or PROJECT_ROOT)


def load_env_values(env_path: Path | None = None) -> None:
    """轻量读取 .env，并且不覆盖进程已经提供的环境变量。"""
    if env_path is None:
        env_path = hermes_home() / ".env"
    else:
        env_path = Path(env_path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def expand_env_vars(value):
    """递归展开 `${VAR}`；环境变量缺失时保留原占位符。"""
    if isinstance(value, str):
        def replacer(match):
            return os.getenv(match.group(1), match.group(0))

        return re.sub(r"\$\{(\w+)\}", replacer, value)
    if isinstance(value, dict):
        return {
            key: expand_env_vars(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value
