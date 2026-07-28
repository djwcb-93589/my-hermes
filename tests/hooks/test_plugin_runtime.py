"""PluginRuntime：发现、校验、事务加载、命名空间隔离与清理。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes.hooks import SyncHookRegistry, AsyncHookRegistry, HookEventName
from hermes.plugins import (
    PluginConfigurationError, PluginManifestError,
    SyncPluginRuntime, AsyncPluginRuntime,
)


def _config(enabled=None, search_paths=None, enable_project_plugins=False):
    return {
        "enabled": list(enabled or []),
        "search_paths": [str(p) for p in (search_paths or [])],
        "enable_project_plugins": enable_project_plugins,
    }


# ===================== 配置校验 =====================

def test_validate_plugins_config_rejects_non_mapping():
    with pytest.raises(PluginConfigurationError):
        SyncPluginRuntime(SyncHookRegistry(), plugins_config="not a dict")


def test_validate_rejects_invalid_plugin_name():
    with pytest.raises(PluginConfigurationError):
        SyncPluginRuntime(
            SyncHookRegistry(),
            plugins_config={"enabled": ["UPPER CASE"], "search_paths": [], "enable_project_plugins": False},
        )


def test_validate_rejects_duplicate_enabled():
    with pytest.raises(PluginConfigurationError):
        SyncPluginRuntime(
            SyncHookRegistry(),
            plugins_config={"enabled": ["foo", "foo"], "search_paths": [], "enable_project_plugins": False},
        )


def test_validate_rejects_invalid_search_paths():
    with pytest.raises(PluginConfigurationError):
        SyncPluginRuntime(
            SyncHookRegistry(),
            plugins_config={"enabled": [], "search_paths": [""], "enable_project_plugins": False},
        )


def test_validate_rejects_non_bool_project_plugins():
    with pytest.raises(PluginConfigurationError):
        SyncPluginRuntime(
            SyncHookRegistry(),
            plugins_config={"enabled": [], "search_paths": [], "enable_project_plugins": "yes"},
        )


# ===================== Registry 类型校验 =====================

def test_sync_runtime_rejects_async_registry():
    with pytest.raises(TypeError):
        SyncPluginRuntime(AsyncHookRegistry(), plugins_config=_config())


def test_async_runtime_rejects_sync_registry():
    with pytest.raises(TypeError):
        AsyncPluginRuntime(SyncHookRegistry(), plugins_config=_config())


# ===================== 加载流程 =====================

def test_load_empty_enabled_returns_empty():
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(reg, plugins_config=_config(), user_plugin_root=Path("/nonexistent"))
    assert rt.load() == ()


def test_load_missing_plugin_reports_not_found():
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["missing"]),
        user_plugin_root=Path("/nonexistent"),
    )
    results = rt.load()
    assert len(results) == 1
    assert results[0].error_type == "PluginNotFound"
    assert results[0].enabled is False


def test_load_valid_plugin_registers_hooks(make_plugin, plugin_root):
    """加载一个注册了 hook 的 plugin，验证 hook 进程级 Registry。"""
    reg = SyncHookRegistry()
    register_body = (
        "    context.register_hook('pre_tool_call', lambda ctx: None, hook_id='my_hook')"
    )
    make_plugin("demo-plugin", register_body=register_body)
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert len(results) == 1
    r = results[0]
    assert r.enabled is True
    assert r.error_type is None
    assert r.name == "demo-plugin"
    assert r.version == "1.0.0"
    assert "pre_tool_call" in r.registered_events
    assert r.registered_hook_count == 1
    # hook 进程级 Registry
    hooks = reg.registered_hooks("pre_tool_call")
    assert len(hooks) == 1
    # hook_id 被 namespace 化为 plugin:local
    assert hooks[0].hook_id == "demo-plugin:my_hook"


def test_load_plugin_failure_does_not_affect_others(make_plugin, plugin_root):
    """单个 plugin 加载失败不影响其他。"""
    reg = SyncHookRegistry()
    # 失败的 plugin：register 抛异常
    make_plugin("bad-plugin", register_body="    raise RuntimeError('boom')")
    # 正常的 plugin
    make_plugin("good-plugin", register_body="    context.register_hook('post_tool_call', lambda ctx: 1, hook_id='g')")
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["bad-plugin", "good-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    by_name = {r.name: r for r in results}
    assert by_name["bad-plugin"].error_type == "RuntimeError"
    assert by_name["good-plugin"].enabled is True
    assert len(reg.registered_hooks("post_tool_call")) == 1


def test_load_results_cached(make_plugin, plugin_root):
    """load 幂等：第二次返回同一结果。"""
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda ctx: None, hook_id='h')")
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    first = rt.load()
    second = rt.load()
    assert first is second


def test_load_after_close_raises(make_plugin, plugin_root):
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    make_plugin("demo-plugin")
    rt.close()
    with pytest.raises(RuntimeError):
        rt.load()


# ===================== manifest 校验 =====================

def test_manifest_name_must_match_directory(make_plugin, plugin_root):
    """manifest name 必须与目录名一致。"""
    d = plugin_root / "dir-name"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: wrong-name\nversion: 1.0.0\n", encoding="utf-8")
    (d / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["dir-name"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].error_type == "PluginManifestError"


def test_manifest_missing_files(plugin_root):
    """缺少 plugin.yaml 或 __init__.py 报错。"""
    d = plugin_root / "incomplete"
    d.mkdir()
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["incomplete"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].error_type == "PluginManifestError"


def test_plugin_must_have_callable_register(make_plugin, plugin_root):
    """__init__.py 无 register 函数报错。"""
    d = make_plugin("noreg-plugin")
    (d / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["noreg-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].error_type == "PluginManifestError"


def test_async_register_rejected(make_plugin, plugin_root):
    """register 返回 awaitable 报错。"""
    d = make_plugin("async-plugin")
    (d / "__init__.py").write_text(
        "async def register(ctx):\n    return 1\n", encoding="utf-8"
    )
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["async-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].error_type == "PluginManifestError"


def test_plugin_modifying_sys_path_rejected(make_plugin, plugin_root):
    """plugin 修改 sys.path 报错。"""
    d = make_plugin("path-plugin")
    (d / "__init__.py").write_text(
        "import sys\n"
        "def register(ctx):\n"
        "    sys.path.append('/sneaky')\n",
        encoding="utf-8",
    )
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["path-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].error_type == "PluginManifestError"
    # sys.path 恢复
    assert "/sneaky" not in sys.path


# ===================== hook_id namespace =====================

def test_hook_id_namespaced_and_rejects_colon(make_plugin, plugin_root):
    """plugin hook_id 被前缀为 plugin:local，且 local 不许含冒号。"""
    reg = SyncHookRegistry()
    # 显式传含冒号的 hook_id
    make_plugin("demo-plugin", register_body=(
        "    try:\n"
        "        context.register_hook('post_tool_call', lambda ctx: None, hook_id='a:b')\n"
        "        raise AssertionError('should have failed')\n"
        "    except Exception as e:\n"
        "        if 'must not contain' not in str(e):\n"
        "            raise\n"
    ))
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    # plugin 因 register 内捕获异常而"成功"加载，但实际校验了冒号拒绝
    assert results[0].enabled is True


def test_default_hook_id_namespaced(make_plugin, plugin_root):
    """未传 hook_id 时用 plugin 内相对模块名生成 namespace。"""
    reg = SyncHookRegistry()
    make_plugin("demo-plugin", register_body=(
        "    def my_cb(ctx):\n        return None\n"
        "    context.register_hook('post_tool_call', my_cb)"
    ))
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    rt.load()
    hooks = reg.registered_hooks("post_tool_call")
    assert len(hooks) == 1
    assert hooks[0].hook_id.startswith("demo-plugin:")


# ===================== search_paths / project plugins =====================

def test_search_paths_loaded(tmp_path, make_plugin):
    """search_paths 下的 plugin 被发现。"""
    search = tmp_path / "search"
    search.mkdir()
    d = search / "extra-plugin"
    d.mkdir()
    (d / "plugin.yaml").write_text("name: extra-plugin\nversion: 1.0.0\n", encoding="utf-8")
    (d / "__init__.py").write_text(
        "def register(ctx):\n    ctx.register_hook('run_end', lambda c: None, hook_id='h')\n",
        encoding="utf-8",
    )
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg,
        plugins_config=_config(enabled=["extra-plugin"], search_paths=[search]),
        user_plugin_root=tmp_path / "empty",
    )
    results = rt.load()
    assert results[0].enabled is True
    assert results[0].source_type == "search_path"


def test_project_plugins_enabled(tmp_path, make_plugin):
    """enable_project_plugins 时项目根 .my-hermes/plugins 被发现。"""
    project = tmp_path / "project"
    project.mkdir()
    pd = project / ".my-hermes" / "plugins" / "proj-plugin"
    pd.mkdir(parents=True)
    (pd / "plugin.yaml").write_text("name: proj-plugin\nversion: 1.0.0\n", encoding="utf-8")
    (pd / "__init__.py").write_text(
        "def register(ctx):\n    ctx.register_hook('run_end', lambda c: None, hook_id='h')\n",
        encoding="utf-8",
    )
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg,
        plugins_config=_config(enabled=["proj-plugin"], enable_project_plugins=True),
        user_plugin_root=tmp_path / "empty",
        project_root=project,
    )
    results = rt.load()
    assert results[0].enabled is True
    assert results[0].source_type == "project"


def test_project_plugins_disabled_skips(tmp_path, make_plugin):
    """enable_project_plugins=False 时不发现项目 plugin。"""
    project = tmp_path / "project"
    project.mkdir()
    pd = project / ".my-hermes" / "plugins" / "proj-plugin"
    pd.mkdir(parents=True)
    (pd / "plugin.yaml").write_text("name: proj-plugin\nversion: 1.0.0\n", encoding="utf-8")
    (pd / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    reg = SyncHookRegistry()
    rt = SyncPluginRuntime(
        reg,
        plugins_config=_config(enabled=["proj-plugin"], enable_project_plugins=False),
        user_plugin_root=tmp_path / "empty",
        project_root=project,
    )
    results = rt.load()
    assert results[0].error_type == "PluginNotFound"


# ===================== close 清理 =====================

def test_close_cleans_plugin_modules(make_plugin, plugin_root):
    """close 移除动态加载的 plugin 模块。"""
    # 清理历史测试可能残留的同类动态模块
    for n in [m for m in list(sys.modules) if m.startswith("hermes_plugin_demo_plugin_")]:
        sys.modules.pop(n, None)

    reg = SyncHookRegistry()
    make_plugin("demo-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    rt.load()
    # 本次加载产生 1 个动态模块
    modules = [n for n in sys.modules if n.startswith("hermes_plugin_demo_plugin_")]
    assert len(modules) == 1, f"应加载 1 个模块，实际 {modules}"
    loaded_name = modules[0]
    rt.close()
    # close 后该模块被移除
    assert loaded_name not in sys.modules, "close 后模块未清理"
    assert rt.results == ()


def test_close_idempotent(make_plugin, plugin_root):
    reg = SyncHookRegistry()
    make_plugin("demo-plugin")
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    rt.load()
    rt.close()
    rt.close()  # 不报错


def test_summary(make_plugin, plugin_root):
    reg = SyncHookRegistry()
    make_plugin("good-plugin", register_body="    context.register_hook('run_end', lambda c: None, hook_id='h')")
    make_plugin("bad-plugin", register_body="    raise ValueError('nope')")
    rt = SyncPluginRuntime(
        reg, plugins_config=_config(enabled=["good-plugin", "bad-plugin"]),
        user_plugin_root=plugin_root,
    )
    rt.load()
    s = rt.summary
    assert s.loaded == 1
    assert s.failed == 1


# ===================== AsyncPluginRuntime =====================

def test_async_runtime_loads_plugin(make_plugin, plugin_root):
    reg = AsyncHookRegistry(default_timeout_seconds=1.0)
    make_plugin("demo-plugin", register_body=(
        "    async def cb(ctx):\n        return None\n"
        "    context.register_hook('post_tool_call', cb, hook_id='h')"
    ))
    rt = AsyncPluginRuntime(
        reg, plugins_config=_config(enabled=["demo-plugin"]),
        user_plugin_root=plugin_root,
    )
    results = rt.load()
    assert results[0].enabled is True
    assert len(reg.registered_hooks("post_tool_call")) == 1
