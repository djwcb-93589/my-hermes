"""显式发现并加载 Python Plugin 的进程级运行时。"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

import yaml

from hermes.hooks import (
    AsyncHookRegistry,
    HookCallback,
    HookEventName,
    HookRegistration,
    HookRegistrationError,
    SyncHookRegistry,
)
from hermes.plugins.context import AsyncPluginContext, PluginContext, SyncPluginContext


_PLUGIN_NAME_PATTERN = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z"
)


class PluginConfigurationError(ValueError):
    """Plugin 配置结构不合法时抛出。"""


class PluginManifestError(ValueError):
    """Plugin manifest 不合法时抛出。"""


@dataclass(frozen=True, slots=True)
class PluginLoadResult:
    """一个 Plugin 的脱敏加载状态。"""

    name: str
    version: str | None
    source_type: Literal["user", "search_path", "project"] | None
    enabled: bool
    registered_events: tuple[str, ...] = ()
    registered_hook_count: int = 0
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class PluginLoadSummary:
    """启动日志所需的简短加载汇总。"""

    loaded: int
    skipped: int
    failed: int


@dataclass(frozen=True, slots=True)
class _PluginCandidate:
    name: str
    directory: Path
    source_type: Literal["user", "search_path", "project"]
    is_safe: bool = True


class _PluginTransaction:
    """在提交共享 Registry 前隔离单个 Plugin 的全部注册。"""

    def __init__(
        self,
        registry: SyncHookRegistry | AsyncHookRegistry,
        plugin_name: str,
    ) -> None:
        self._plugin_name = plugin_name
        if isinstance(registry, SyncHookRegistry):
            self._staging_registry: SyncHookRegistry | AsyncHookRegistry = (
                SyncHookRegistry()
            )
        else:
            self._staging_registry = AsyncHookRegistry(
                default_timeout_seconds=registry.default_timeout_seconds,
            )

    def register(
        self,
        event_name: HookEventName,
        callback: HookCallback,
        hook_id: str | None,
        timeout_seconds: float | None,
    ) -> HookRegistration:
        """将本地标识规范化为 Plugin 命名空间内的标识。"""
        local_hook_id = hook_id
        if local_hook_id is None:
            callback_type = type(callback)
            module = getattr(callback, "__module__", callback_type.__module__)
            name = getattr(
                callback,
                "__qualname__",
                getattr(callback, "__name__", callback_type.__qualname__),
            )
            local_hook_id = f"{module}.{name}"
        if not isinstance(local_hook_id, str) or not local_hook_id.strip():
            raise HookRegistrationError("hook_id must be a non-empty string")
        if ":" in local_hook_id:
            raise HookRegistrationError("plugin hook_id must not contain ':'")
        return self._staging_registry.register(
            event_name,
            callback,
            hook_id=f"{self._plugin_name}:{local_hook_id.strip()}",
            timeout_seconds=timeout_seconds,
        )

    def commit(
        self,
        registry: SyncHookRegistry | AsyncHookRegistry,
    ) -> tuple[HookRegistration, ...]:
        """一次性把暂存项提交给进程级 Registry。"""
        registrations: list[HookRegistration] = []
        for event_name in HookEventName:
            registrations.extend(
                self._staging_registry.registered_hooks(event_name.value)
            )
        return registry._commit_registrations(tuple(registrations))


class _PluginRuntimeBase:
    """同步和异步 Plugin Runtime 的共享发现、校验与事务加载逻辑。"""

    _context_type: type[PluginContext]

    def __init__(
        self,
        registry: SyncHookRegistry | AsyncHookRegistry,
        *,
        plugins_config: object,
        project_root: Path | None = None,
        user_plugin_root: Path | None = None,
    ) -> None:
        self._registry = registry
        self._plugins_config = _validate_plugins_config(plugins_config)
        self._project_root = (project_root or Path.cwd()).resolve()
        if user_plugin_root is None:
            # 仅在实际构造 Runtime 时读取基础配置，避免 PluginContext 导入配置副作用。
            from hermes.config import HERMES_HOME

            user_plugin_root = HERMES_HOME / "plugins"
        self._user_plugin_root = user_plugin_root.expanduser()
        self._results: tuple[PluginLoadResult, ...] = ()
        self._modules: dict[str, ModuleType] = {}
        self._closed = False

    @property
    def results(self) -> tuple[PluginLoadResult, ...]:
        """返回不可变的脱敏加载状态快照。"""
        return self._results

    @property
    def summary(self) -> PluginLoadSummary:
        """返回启动阶段的加载、跳过和失败数量。"""
        loaded = sum(result.enabled and result.error_type is None for result in self._results)
        failed = sum(result.error_type is not None for result in self._results)
        return PluginLoadSummary(loaded=loaded, skipped=0, failed=failed)

    def load(self) -> tuple[PluginLoadResult, ...]:
        """发现并加载明确启用的 Plugin；单个失败不会影响其他 Plugin。"""
        if self._closed:
            raise RuntimeError("plugin runtime is closed")
        if self._results:
            return self._results
        enabled = tuple(self._plugins_config["enabled"])
        candidates = self._discover_candidates(enabled)
        results: list[PluginLoadResult] = []
        by_name: dict[str, list[_PluginCandidate]] = {}
        for candidate in candidates:
            by_name.setdefault(candidate.name, []).append(candidate)

        discovered_names = set(by_name)
        for name in enabled:
            if name not in discovered_names:
                results.append(_failed_result(name, None, None, "PluginNotFound"))

        for name in enabled:
            entries = by_name.get(name, [])
            if len(entries) > 1:
                results.extend(
                    _failed_result(
                        name,
                        None,
                        candidate.source_type,
                        "DuplicatePluginName",
                    )
                    for candidate in entries
                )
                continue
            if len(entries) == 1:
                results.append(self._load_candidate(entries[0]))
        self._results = tuple(results)
        return self._results

    def close(self) -> None:
        """释放 Runtime 对动态模块、Registry 和状态的引用。"""
        if self._closed:
            return
        self._closed = True
        for module_name, module in tuple(self._modules.items()):
            if sys.modules.get(module_name) is module:
                sys.modules.pop(module_name, None)
        self._modules.clear()
        self._results = ()
        self._registry = None  # type: ignore[assignment]

    def _discover_candidates(
        self,
        enabled: tuple[str, ...],
    ) -> tuple[_PluginCandidate, ...]:
        candidates: list[_PluginCandidate] = []
        seen_roots: set[Path] = set()
        for root, source_type in self._plugin_roots():
            try:
                resolved_root = root.resolve(strict=False)
            except OSError:
                continue
            if resolved_root in seen_roots or not root.is_dir():
                continue
            seen_roots.add(resolved_root)
            try:
                children = tuple(root.iterdir())
            except OSError:
                continue
            for child in children:
                if child.name not in enabled or not child.is_dir():
                    continue
                try:
                    resolved_child = child.resolve(strict=True)
                    resolved_child.relative_to(resolved_root)
                except (OSError, ValueError):
                    candidates.append(
                        _PluginCandidate(
                            child.name,
                            child,
                            source_type,
                            is_safe=False,
                        )
                    )
                    continue
                candidates.append(
                    _PluginCandidate(child.name, resolved_child, source_type)
                )
        return tuple(candidates)

    def _plugin_roots(
        self,
    ) -> tuple[tuple[Path, Literal["user", "search_path", "project"]], ...]:
        roots: list[tuple[Path, Literal["user", "search_path", "project"]]] = [
            (self._user_plugin_root, "user"),
        ]
        roots.extend(
            (Path(value).expanduser(), "search_path")
            for value in self._plugins_config["search_paths"]
        )
        if self._plugins_config["enable_project_plugins"]:
            roots.append((self._project_root / ".my-hermes" / "plugins", "project"))
        return tuple(roots)

    def _load_candidate(self, candidate: _PluginCandidate) -> PluginLoadResult:
        original_sys_path = tuple(sys.path)
        try:
            if not candidate.is_safe:
                raise PluginManifestError("plugin directory escapes search root")
            manifest = _load_manifest(candidate)
            module_name, module = _load_plugin_module(candidate.directory, manifest["name"])
            register = getattr(module, "register", None)
            if not callable(register):
                raise PluginManifestError("plugin register must be callable")
            transaction = _PluginTransaction(self._registry, manifest["name"])
            context = self._context_type(transaction.register)
            outcome = register(context)
            if inspect.isawaitable(outcome):
                raise PluginManifestError("plugin register must not be async")
            if tuple(sys.path) != original_sys_path:
                raise PluginManifestError("plugin must not modify sys.path")
            committed = transaction.commit(self._registry)
        except Exception as exc:
            if tuple(sys.path) != original_sys_path:
                sys.path[:] = original_sys_path
            module_name = locals().get("module_name")
            if isinstance(module_name, str):
                sys.modules.pop(module_name, None)
            return _failed_result(
                candidate.name,
                None,
                candidate.source_type,
                type(exc).__name__,
            )
        self._modules[module_name] = module
        return PluginLoadResult(
            name=manifest["name"],
            version=manifest["version"],
            source_type=candidate.source_type,
            enabled=True,
            registered_events=tuple(dict.fromkeys(item.event_name for item in committed)),
            registered_hook_count=len(committed),
        )


class SyncPluginRuntime(_PluginRuntimeBase):
    """CLI 进程使用的同步 Plugin Runtime。"""

    _context_type = SyncPluginContext

    def __init__(self, registry: SyncHookRegistry, **kwargs: object) -> None:
        if not isinstance(registry, SyncHookRegistry):
            raise TypeError("registry must be a SyncHookRegistry")
        super().__init__(registry, **kwargs)


class AsyncPluginRuntime(_PluginRuntimeBase):
    """Gateway 进程使用的异步 Plugin Runtime。"""

    _context_type = AsyncPluginContext

    def __init__(self, registry: AsyncHookRegistry, **kwargs: object) -> None:
        if not isinstance(registry, AsyncHookRegistry):
            raise TypeError("registry must be an AsyncHookRegistry")
        super().__init__(registry, **kwargs)


def _validate_plugins_config(value: object) -> dict[str, object]:
    """校验 Runtime 需要的 Plugin 配置，并返回独立副本。"""
    if not isinstance(value, dict):
        raise PluginConfigurationError("plugins must be a mapping")
    enabled = value.get("enabled", [])
    search_paths = value.get("search_paths", [])
    project_plugins = value.get("enable_project_plugins", False)
    if not isinstance(enabled, list) or any(
        not isinstance(name, str) or not _PLUGIN_NAME_PATTERN.fullmatch(name)
        for name in enabled
    ):
        raise PluginConfigurationError("plugins.enabled must contain valid plugin names")
    if len(set(enabled)) != len(enabled):
        raise PluginConfigurationError("plugins.enabled must not contain duplicates")
    if not isinstance(search_paths, list) or any(
        not isinstance(path, str) or not path.strip() for path in search_paths
    ):
        raise PluginConfigurationError("plugins.search_paths must contain non-empty strings")
    if not isinstance(project_plugins, bool):
        raise PluginConfigurationError("plugins.enable_project_plugins must be a boolean")
    return {
        "enabled": list(enabled),
        "search_paths": list(search_paths),
        "enable_project_plugins": project_plugins,
    }


def _load_manifest(candidate: _PluginCandidate) -> dict[str, str]:
    """读取并校验固定的 Plugin manifest，拒绝目录与链接逃逸。"""
    root = candidate.directory.resolve(strict=True)
    manifest_path = candidate.directory / "plugin.yaml"
    entrypoint = candidate.directory / "__init__.py"
    try:
        if not manifest_path.is_file() or not entrypoint.is_file():
            raise PluginManifestError("plugin files are invalid")
        manifest_path.resolve(strict=True).relative_to(root)
        entrypoint.resolve(strict=True).relative_to(root)
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise PluginManifestError("plugin files are invalid") from exc
    if not isinstance(raw, dict):
        raise PluginManifestError("plugin manifest must be a mapping")
    name = raw.get("name")
    version = raw.get("version")
    description = raw.get("description")
    if not isinstance(name, str) or not _PLUGIN_NAME_PATTERN.fullmatch(name):
        raise PluginManifestError("plugin manifest name is invalid")
    if name != candidate.name:
        raise PluginManifestError("plugin name does not match directory")
    if not isinstance(version, str) or not version.strip():
        raise PluginManifestError("plugin manifest version is invalid")
    if description is not None and not isinstance(description, str):
        raise PluginManifestError("plugin manifest description is invalid")
    return {"name": name, "version": version.strip()}


def _load_plugin_module(directory: Path, plugin_name: str) -> tuple[str, ModuleType]:
    """不修改 sys.path 地加载目录内唯一命名的 Python 包模块。"""
    entrypoint = directory / "__init__.py"
    module_name = f"hermes_plugin_{plugin_name.replace('-', '_')}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise PluginManifestError("plugin module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module_name, module


def _failed_result(
    name: str,
    version: str | None,
    source_type: Literal["user", "search_path", "project"] | None,
    error_type: str,
) -> PluginLoadResult:
    return PluginLoadResult(
        name=name,
        version=version,
        source_type=source_type,
        enabled=False,
        error_type=error_type,
    )
