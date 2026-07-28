"""PluginManager 测试共享 fixture。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch) -> Path:
    """隔离 HERMES_HOME 与 .env，避免读真实环境。"""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "plugins").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def config_path(isolated_home) -> Path:
    """一份可写的 config.yaml，含最小 plugins 段。"""
    p = isolated_home / "config.yaml"
    p.write_text(
        "plugins:\n"
        "  enabled: []\n"
        "  search_paths: []\n"
        "  enable_project_plugins: false\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def plugin_root(isolated_home) -> Path:
    """用户 plugin 根目录。"""
    return isolated_home / "plugins"


@pytest.fixture
def make_plugin(plugin_root):
    """在 plugin_root 下创建 plugin。register_body 需自带 4 空格缩进。"""
    def _make(name="demo-plugin", *, register_body="    pass", version="1.0.0", description=None):
        d = plugin_root / name
        d.mkdir()
        manifest = f"name: {name}\nversion: {version}\n"
        if description is not None:
            manifest += f"description: {description}\n"
        (d / "plugin.yaml").write_text(manifest, encoding="utf-8")
        (d / "__init__.py").write_text(
            "def register(context):\n"
            f"{register_body}\n",
            encoding="utf-8",
        )
        return d
    return _make


@pytest.fixture
def manager(config_path, isolated_home):
    """构造指向隔离 config 的 PluginManager。"""
    from hermes.plugins.manager import PluginManager
    return PluginManager(
        config_path=config_path,
        project_root=isolated_home,
        user_plugin_root=isolated_home / "plugins",
    )
