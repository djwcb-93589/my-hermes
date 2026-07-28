"""PluginManager：list / enable / disable / doctor。"""

from __future__ import annotations

import yaml

import pytest

from hermes.plugins.manager import (
    PluginManager, PluginManagerError,
)


# ===================== list_plugins =====================

def test_list_empty(manager):
    assert manager.list_plugins() == ()


def test_list_ready_plugin(make_plugin, manager):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    items = manager.list_plugins()
    assert len(items) == 1
    item = items[0]
    assert item.name == "demo-plugin"
    assert item.version == "1.0.0"
    assert item.source_type == "user"
    assert item.enabled is False  # 未 enable
    assert item.manifest_valid is True
    assert item.duplicate is False
    assert item.status == "disabled"


def test_list_enabled_plugin_shown_as_ready(make_plugin, manager, config_path):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    manager.enable("demo-plugin")
    items = manager.list_plugins()
    assert items[0].enabled is True
    assert items[0].status == "ready"


def test_list_not_found_enabled(manager, config_path):
    """enabled 列表里有但目录不存在的 plugin 显示 not_found。"""
    # 直接改 config
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["plugins"]["enabled"] = ["ghost"]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    items = manager.list_plugins()
    ghost = [i for i in items if i.name == "ghost"][0]
    assert ghost.enabled is True
    assert ghost.status == "not_found"
    assert ghost.error_type == "PluginNotFound"
    assert ghost.manifest_valid is False


def test_list_invalid_manifest(make_plugin, manager):
    """manifest 损坏显示 invalid_manifest。"""
    d = make_plugin("bad-plugin")
    (d / "plugin.yaml").write_text("name: bad-plugin\nversion: ''\n", encoding="utf-8")  # 空 version
    items = manager.list_plugins()
    item = [i for i in items if i.name == "bad-plugin"][0]
    assert item.manifest_valid is False
    assert item.status == "invalid_manifest"
    assert item.error_type == "InvalidManifest"


def test_list_duplicate_names(tmp_path, isolated_home, config_path, plugin_root):
    """两个搜索根下同名 plugin 显示 duplicate。"""
    search = tmp_path / "search"
    search.mkdir()
    # 在 user root 和 search root 各放一个同名
    for root in (plugin_root, search):
        d = root / "dup-plugin"
        d.mkdir()
        (d / "plugin.yaml").write_text("name: dup-plugin\nversion: 1.0.0\n", encoding="utf-8")
        (d / "__init__.py").write_text("def register(c): pass\n", encoding="utf-8")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["plugins"]["search_paths"] = [str(search)]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    manager = PluginManager(
        config_path=config_path, project_root=isolated_home,
        user_plugin_root=plugin_root,
    )
    items = manager.list_plugins()
    dup = [i for i in items if i.name == "dup-plugin"][0]
    assert dup.duplicate is True
    assert dup.status == "duplicate"
    assert dup.error_type == "DuplicatePluginName"


# ===================== enable =====================

def test_enable_appends_to_enabled(make_plugin, manager, config_path):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    result = manager.enable("demo-plugin")
    assert result.success is True
    assert result.status == "enabled"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["plugins"]["enabled"] == ["demo-plugin"]


def test_enable_idempotent(make_plugin, manager):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    manager.enable("demo-plugin")
    result = manager.enable("demo-plugin")
    assert result.success is True
    assert result.status == "already_enabled"


def test_enable_not_found(manager):
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("ghost")
    assert exc.value.error_code == "PluginNotFound"


def test_enable_invalid_name(manager):
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("UPPER CASE")
    assert exc.value.error_code == "InvalidPluginName"


def test_enable_invalid_manifest(make_plugin, manager):
    d = make_plugin("bad-plugin")
    (d / "plugin.yaml").write_text("name: bad-plugin\nversion: ''\n", encoding="utf-8")
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("bad-plugin")
    assert exc.value.error_code == "InvalidManifest"


def test_enable_async_register_rejected(make_plugin, manager):
    """register 是 async 函数 -> 静态 AST 校验拒绝。"""
    d = make_plugin("async-plugin")
    (d / "__init__.py").write_text("async def register(ctx):\n    return 1\n", encoding="utf-8")
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("async-plugin")
    assert exc.value.error_code == "AsyncRegisterNotAllowed"


def test_enable_no_register_function(make_plugin, manager):
    """__init__.py 无 register 函数 -> RegisterNotFound。"""
    d = make_plugin("noreg-plugin")
    (d / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("noreg-plugin")
    assert exc.value.error_code == "RegisterNotFound"


def test_enable_register_reassigned_rejected(make_plugin, manager):
    """register 被赋值覆盖（非函数定义）-> RegisterEntrypointUnsupported。"""
    d = make_plugin("reassign-plugin")
    (d / "__init__.py").write_text("register = 42\n", encoding="utf-8")
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("reassign-plugin")
    assert exc.value.error_code == "RegisterEntrypointUnsupported"


def test_enable_duplicate_names_rejected(tmp_path, isolated_home, config_path, plugin_root):
    search = tmp_path / "search"
    search.mkdir()
    for root in (plugin_root, search):
        d = root / "dup-plugin"
        d.mkdir()
        (d / "plugin.yaml").write_text("name: dup-plugin\nversion: 1.0.0\n", encoding="utf-8")
        (d / "__init__.py").write_text("def register(c): pass\n", encoding="utf-8")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["plugins"]["search_paths"] = [str(search)]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    manager = PluginManager(
        config_path=config_path, project_root=isolated_home,
        user_plugin_root=plugin_root,
    )
    with pytest.raises(PluginManagerError) as exc:
        manager.enable("dup-plugin")
    assert exc.value.error_code == "DuplicatePluginName"


# ===================== disable =====================

def test_disable_removes_from_enabled(make_plugin, manager, config_path):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    manager.enable("demo-plugin")
    result = manager.disable("demo-plugin")
    assert result.success is True
    assert result.status == "disabled"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["plugins"]["enabled"] == []


def test_disable_idempotent(manager):
    result = manager.disable("ghost")
    assert result.success is True
    assert result.status == "already_disabled"


def test_disable_invalid_name(manager):
    with pytest.raises(PluginManagerError) as exc:
        manager.disable("BAD NAME")
    assert exc.value.error_code == "InvalidPluginName"


def test_disable_preserves_other_plugins(make_plugin, manager, config_path):
    make_plugin("a", register_body="    context.register_hook('run_end', lambda c: None, hook_id='a')")
    make_plugin("b", register_body="    context.register_hook('run_end', lambda c: None, hook_id='b')")
    manager.enable("a")
    manager.enable("b")
    manager.disable("a")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["plugins"]["enabled"] == ["b"]


# ===================== doctor =====================

def test_doctor_ready_plugin(make_plugin, manager):
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    result = manager.doctor("demo-plugin")
    assert result.ready is True
    assert result.name == "demo-plugin"
    assert result.version == "1.0.0"
    check_names = [c.name for c in result.checks]
    assert "plugin discovered" in check_names
    assert "manifest valid" in check_names
    assert "register callable" in check_names
    assert "sync registration compatible" in check_names
    assert "async registration compatible" in check_names
    assert all(c.status == "PASS" for c in result.checks)


def test_doctor_not_found(manager):
    result = manager.doctor("ghost")
    assert result.ready is False
    assert result.checks[0].status == "FAIL"
    assert result.checks[0].detail == "PluginNotFound"


def test_doctor_invalid_name(manager):
    with pytest.raises(PluginManagerError) as exc:
        manager.doctor("BAD")
    assert exc.value.error_code == "InvalidPluginName"


def test_doctor_invalid_manifest(make_plugin, manager):
    d = make_plugin("bad-plugin")
    (d / "plugin.yaml").write_text("name: bad-plugin\nversion: ''\n", encoding="utf-8")
    result = manager.doctor("bad-plugin")
    assert result.ready is False
    manifest_check = [c for c in result.checks if c.name == "manifest valid"][0]
    assert manifest_check.status == "FAIL"


def test_doctor_register_failure(make_plugin, manager):
    """register 内部抛异常 -> sync/async 注册检查失败。"""
    make_plugin("fail-plugin", register_body="    raise RuntimeError('boom')")
    result = manager.doctor("fail-plugin")
    assert result.ready is False
    sync_check = [c for c in result.checks if c.name == "sync registration compatible"][0]
    assert sync_check.status == "FAIL"


def test_doctor_duplicate(make_plugin, tmp_path, isolated_home, config_path, plugin_root):
    search = tmp_path / "search"
    search.mkdir()
    for root in (plugin_root, search):
        d = root / "dup-plugin"
        d.mkdir()
        (d / "plugin.yaml").write_text("name: dup-plugin\nversion: 1.0.0\n", encoding="utf-8")
        (d / "__init__.py").write_text("def register(c): pass\n", encoding="utf-8")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["plugins"]["search_paths"] = [str(search)]
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    manager = PluginManager(
        config_path=config_path, project_root=isolated_home,
        user_plugin_root=plugin_root,
    )
    result = manager.doctor("dup-plugin")
    assert result.ready is False
    unique_check = [c for c in result.checks if c.name == "unique plugin name"][0]
    assert unique_check.status == "FAIL"
    assert unique_check.detail == "DuplicatePluginName"


def test_doctor_does_not_register_to_production(make_plugin, manager):
    """doctor 在隔离 Registry 诊断，不污染生产 Registry。"""
    from hermes.hooks import SyncHookRegistry
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    manager.doctor("demo-plugin")
    # manager 不持有生产 registry，但确认 doctor 后无残留动态模块
    import sys
    leftover = [n for n in sys.modules if n.startswith("hermes_plugin_demo_plugin_")]
    assert leftover == [], f"doctor 未清理动态模块: {leftover}"


# ===================== doctor project plugins disabled =====================

def test_doctor_project_plugin_disabled(isolated_home, config_path):
    """project plugin 存在但 enable_project_plugins=False -> ProjectPluginsDisabled。"""
    project = isolated_home / "project-root"
    pd = project / ".my-hermes" / "plugins" / "proj-plugin"
    pd.mkdir(parents=True)
    (pd / "plugin.yaml").write_text("name: proj-plugin\nversion: 1.0.0\n", encoding="utf-8")
    (pd / "__init__.py").write_text("def register(c): pass\n", encoding="utf-8")
    manager = PluginManager(
        config_path=config_path, project_root=project,
        user_plugin_root=isolated_home / "plugins",
    )
    result = manager.doctor("proj-plugin")
    assert result.ready is False
    proj_check = [c for c in result.checks if "project plugins" in c.name][0]
    assert proj_check.status == "FAIL"
    assert proj_check.detail == "ProjectPluginsDisabled"
