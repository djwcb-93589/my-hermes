"""Plugin 管理命令使用的发现、配置更新和静态诊断服务。"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

from hermes.hooks import (
    AsyncHookRegistry,
    HookRegistrationError,
    SyncHookRegistry,
)
from hermes.config_values import expand_env_vars, hermes_home
from hermes.plugins.context import AsyncPluginContext, SyncPluginContext
from hermes.plugins.runtime import (
    PluginManifestError,
    _PluginCandidate,
    _PluginTransaction,
    _cleanup_plugin_modules,
    _close_rejected_awaitable,
    _load_manifest,
    _load_plugin_module,
    _PLUGIN_NAME_PATTERN,
    _validate_plugins_config,
    discover_plugin_candidates,
)


_CONFIG_UPDATE_LOCK = threading.RLock()

_PLUGIN_ERROR_CODES = frozenset(
    {
        "PluginNotFound",
        "DuplicatePluginName",
        "ProjectPluginsDisabled",
        "InvalidPluginName",
        "ConfigSymlinkNotAllowed",
        "ConfigLockSymlinkNotAllowed",
        "ConfigReadFailed",
        "ConfigYamlInvalid",
        "ConfigNotMapping",
        "PluginsConfigInvalid",
        "ConfigLockFailed",
        "ConfigWriteFailed",
        "InvalidManifest",
        "PathEscape",
        "RegisterNotFound",
        "AsyncRegisterNotAllowed",
        "RegisterEntrypointUnsupported",
        "HookRegistrationError",
        "PluginImportFailed",
        "SysPathModified",
        "InvalidArguments",
        "InternalError",
    }
)


class PluginManagerError(RuntimeError):
    """Plugin 管理操作失败，错误信息只用于本地命令提示。"""

    def __init__(self, error_code: str) -> None:
        normalized = (
            error_code
            if isinstance(error_code, str) and error_code in _PLUGIN_ERROR_CODES
            else "InternalError"
        )
        self._error_code = normalized
        super().__init__(normalized)

    @property
    def error_code(self) -> str:
        """返回稳定、脱敏的管理错误码。"""
        return self._error_code


@dataclass(frozen=True, slots=True)
class PluginInspection:
    """Plugin 列表中的脱敏状态。"""

    name: str
    version: str | None
    source_type: str
    enabled: bool
    manifest_valid: bool
    duplicate: bool
    status: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class PluginOperationResult:
    """enable/disable 的结构化结果。"""

    name: str
    success: bool
    status: str


@dataclass(frozen=True, slots=True)
class PluginDoctorCheck:
    """单项诊断结果，不保存异常对象或回调对象。"""

    name: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PluginDoctorResult:
    """完整但脱敏的 Plugin 诊断结果。"""

    name: str
    version: str | None
    checks: tuple[PluginDoctorCheck, ...]
    ready: bool


class PluginManager:
    """集中管理 Plugin 发现、配置启停和隔离诊断。"""

    def __init__(
        self,
        *,
        config_path: Path | str | None = None,
        project_root: Path | None = None,
        user_plugin_root: Path | None = None,
    ) -> None:
        self.config_path = Path(config_path or _default_config_path())
        # 默认项目根目录必须跟随调用管理命令时的当前工作目录，不能依赖
        # manager.py 的源码位置，也不能在模块导入时提前固定。
        self.project_root = (project_root or Path.cwd()).resolve()
        self.user_plugin_root = (
            user_plugin_root or hermes_home() / "plugins"
        ).expanduser()

    def list_plugins(self) -> tuple[PluginInspection, ...]:
        """列出允许扫描根目录中的 Plugin，但不导入或执行代码。"""
        plugins_config = self._plugins_config()
        candidates = discover_plugin_candidates(
            plugins_config,
            project_root=self.project_root,
            user_plugin_root=self.user_plugin_root,
        )
        enabled = set(plugins_config["enabled"])
        grouped: dict[str, list[_PluginCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.name, []).append(candidate)

        inspections: list[PluginInspection] = []
        for name in sorted(grouped):
            entries = grouped[name]
            if len(entries) > 1:
                versions: list[str] = []
                manifests_valid = True
                for duplicate_candidate in entries:
                    try:
                        if not duplicate_candidate.is_safe:
                            raise PluginManifestError(
                                "plugin directory escapes search root"
                            )
                        versions.append(_load_manifest(duplicate_candidate)["version"])
                    except Exception:
                        manifests_valid = False
                inspections.append(
                    PluginInspection(
                        name=name,
                        version=versions[0] if versions and len(set(versions)) == 1 else None,
                        source_type="multiple",
                        enabled=name in enabled,
                        manifest_valid=manifests_valid,
                        duplicate=True,
                        status="duplicate",
                        error_type="DuplicatePluginName",
                    )
                )
                continue
            candidate = entries[0]
            version: str | None = None
            try:
                if not candidate.is_safe:
                    raise PluginManifestError("plugin directory escapes search root")
                manifest = _load_manifest(candidate)
                version = manifest["version"]
            except Exception as exc:
                inspections.append(
                    PluginInspection(
                        name=name,
                        version=version,
                        source_type=candidate.source_type,
                        enabled=name in enabled,
                        manifest_valid=False,
                        duplicate=False,
                        status="invalid_manifest",
                        error_type=_error_code(exc),
                    )
                )
                continue
            inspections.append(
                PluginInspection(
                    name=name,
                    version=version,
                    source_type=candidate.source_type,
                    enabled=name in enabled,
                    manifest_valid=True,
                    duplicate=False,
                    status="ready" if name in enabled else "disabled",
                )
            )
        for name in sorted(enabled - set(grouped)):
            inspections.append(
                PluginInspection(
                    name=name,
                    version=None,
                    source_type="-",
                    enabled=True,
                    manifest_valid=False,
                    duplicate=False,
                    status="not_found",
                    error_type="PluginNotFound",
                )
            )
        return tuple(inspections)

    def enable(self, name: str) -> PluginOperationResult:
        """验证 Plugin 后只原子追加 plugins.enabled。"""
        normalized_name = _validate_plugin_name(name)
        plugins_config = self._plugins_config()
        candidate = self._unique_candidate(normalized_name, plugins_config)
        _validate_candidate_for_enable(candidate)
        _validate_static_register(candidate.directory / "__init__.py")

        with _configuration_lock(self.config_path):
            raw = _read_raw_config(self.config_path)
            current_config = _plugins_from_raw(raw)
            enabled = list(current_config["enabled"])
            if normalized_name in enabled:
                return PluginOperationResult(
                    name=normalized_name,
                    success=True,
                    status="already_enabled",
                )
            enabled.append(normalized_name)
            _write_enabled_atomically(raw, enabled, self.config_path)
        return PluginOperationResult(
            name=normalized_name,
            success=True,
            status="enabled",
        )

    def disable(self, name: str) -> PluginOperationResult:
        """只原子移除 plugins.enabled 中的指定名称，不删除 Plugin 文件。"""
        normalized_name = _validate_plugin_name(name)
        with _configuration_lock(self.config_path):
            raw = _read_raw_config(self.config_path)
            current_config = _plugins_from_raw(raw)
            enabled = list(current_config["enabled"])
            if normalized_name not in enabled:
                return PluginOperationResult(
                    name=normalized_name,
                    success=True,
                    status="already_disabled",
                )
            enabled.remove(normalized_name)
            _write_enabled_atomically(raw, enabled, self.config_path)
        return PluginOperationResult(
            name=normalized_name,
            success=True,
            status="disabled",
        )

    def doctor(self, name: str) -> PluginDoctorResult:
        """在隔离的 Sync/Async Registry 中诊断 Plugin，不提交生产注册。"""
        normalized_name = _validate_plugin_name(name)
        plugins_config = self._plugins_config()
        candidates = list(
            discover_plugin_candidates(
                plugins_config,
                project_root=self.project_root,
                user_plugin_root=self.user_plugin_root,
                names=(normalized_name,),
            )
        )
        project_candidate = self._project_candidate_if_disabled(
            normalized_name,
            plugins_config,
        )
        if project_candidate is not None and all(
            item.directory != project_candidate.directory for item in candidates
        ):
            candidates.append(project_candidate)

        checks: list[PluginDoctorCheck] = []
        checks.append(
            _check(
                "plugin discovered",
                bool(candidates),
                "PluginNotFound" if not candidates else None,
            )
        )
        if not candidates:
            return PluginDoctorResult(
                name=normalized_name,
                version=None,
                checks=tuple(checks),
                ready=False,
            )

        duplicate = len(candidates) > 1
        checks.append(_check("unique plugin name", not duplicate, "DuplicatePluginName" if duplicate else None))
        if duplicate:
            return PluginDoctorResult(
                name=normalized_name,
                version=None,
                checks=tuple(checks),
                ready=False,
            )
        candidate = candidates[0]
        checks.append(_check("directory contained in search root", candidate.is_safe, "PathEscape" if not candidate.is_safe else None))
        if candidate.source_type == "project" and not plugins_config["enable_project_plugins"]:
            checks.append(_check("project plugins explicitly enabled", False, "ProjectPluginsDisabled"))
        version: str | None = None
        try:
            if not candidate.is_safe:
                raise PluginManifestError("plugin directory escapes search root")
            manifest = _load_manifest(candidate)
            version = manifest["version"]
            checks.extend(
                (
                    _check("manifest valid", True),
                    _check("manifest name matches directory", True),
                    _check("version valid", True),
                    _check("__init__.py present", True),
                )
            )
        except Exception as exc:
            checks.append(_check("manifest valid", False, _error_code(exc)))
            return PluginDoctorResult(
                name=normalized_name,
                version=version,
                checks=tuple(checks),
                ready=False,
            )

        checks.extend(self._diagnose_registration(candidate, normalized_name))
        ready = all(item.status != "FAIL" for item in checks)
        return PluginDoctorResult(
            name=normalized_name,
            version=version,
            checks=tuple(checks),
            ready=ready,
        )

    def _diagnose_registration(
        self,
        candidate: _PluginCandidate,
        plugin_name: str,
    ) -> tuple[PluginDoctorCheck, ...]:
        """分别在两个临时 Registry 中执行注册并清理动态模块。"""
        checks: list[PluginDoctorCheck] = []
        entrypoint = candidate.directory / "__init__.py"
        try:
            _validate_static_register(entrypoint)
            checks.append(_check("register callable", True))
        except Exception as exc:
            checks.append(_check("register callable", False, _error_code(exc)))
            return tuple(checks)

        sync_result = self._diagnose_one_registry(
            candidate,
            plugin_name,
            SyncHookRegistry(),
            SyncPluginContext,
            "sync registration compatible",
        )
        checks.extend(sync_result)
        async_result = self._diagnose_one_registry(
            candidate,
            plugin_name,
            AsyncHookRegistry(),
            AsyncPluginContext,
            "async registration compatible",
        )
        checks.extend(async_result)
        return tuple(checks)

    @staticmethod
    def _diagnose_one_registry(
        candidate: _PluginCandidate,
        plugin_name: str,
        registry: SyncHookRegistry | AsyncHookRegistry,
        context_type: type[SyncPluginContext | AsyncPluginContext],
        check_name: str,
    ) -> tuple[PluginDoctorCheck, ...]:
        module_namespace: str | None = None
        original_sys_path = tuple(sys.path)
        try:
            module_namespace, module = _load_plugin_module(
                candidate.directory,
                plugin_name,
            )
            register = getattr(module, "register", None)
            if not callable(register):
                raise PluginManagerError("RegisterNotFound")
            transaction = _PluginTransaction(
                registry,
                plugin_name,
                module_namespace,
            )
            outcome = register(context_type(transaction.register))
            if inspect.isawaitable(outcome):
                _close_rejected_awaitable(outcome)
                raise PluginManagerError("AsyncRegisterNotAllowed")
            if tuple(sys.path) != original_sys_path:
                raise PluginManagerError("SysPathModified")
            transaction.commit(registry)
            return (_check(check_name, True),)
        except Exception as exc:
            return (_check(check_name, False, _error_code(exc)),)
        finally:
            if tuple(sys.path) != original_sys_path:
                sys.path[:] = original_sys_path
            if module_namespace is not None:
                _cleanup_plugin_modules(module_namespace)

    def _plugins_config(self) -> dict[str, object]:
        raw = _read_raw_config(self.config_path)
        expanded = expand_env_vars(raw)
        return _plugins_from_raw(expanded)

    def _unique_candidate(
        self,
        name: str,
        plugins_config: dict[str, object],
    ) -> _PluginCandidate:
        candidates = discover_plugin_candidates(
            plugins_config,
            project_root=self.project_root,
            user_plugin_root=self.user_plugin_root,
            names=(name,),
        )
        if not candidates:
            project_candidate = self._project_candidate_if_disabled(name, plugins_config)
            if project_candidate is not None:
                raise PluginManagerError("ProjectPluginsDisabled")
            raise PluginManagerError("PluginNotFound")
        if len(candidates) > 1:
            raise PluginManagerError("DuplicatePluginName")
        return candidates[0]

    def _project_candidate_if_disabled(
        self,
        name: str,
        plugins_config: dict[str, object],
    ) -> _PluginCandidate | None:
        if plugins_config["enable_project_plugins"]:
            return None
        project_dir = self.project_root / ".my-hermes" / "plugins" / name
        if not project_dir.is_dir():
            return None
        root = project_dir.parent.parent
        try:
            resolved = project_dir.resolve(strict=True)
            safe = resolved.is_relative_to(root.resolve(strict=False))
        except OSError:
            safe = False
            resolved = project_dir
        return _PluginCandidate(name, resolved, "project", is_safe=safe)


def _error_code(exc: BaseException) -> str:
    """把内部异常转换为稳定的脱敏管理错误码。"""
    if isinstance(exc, PluginManagerError):
        return exc.error_code
    if isinstance(exc, PluginManifestError):
        return "InvalidManifest"
    if isinstance(exc, HookRegistrationError):
        return "HookRegistrationError"
    if isinstance(exc, (OSError,)):
        return "PluginImportFailed"
    if isinstance(exc, ImportError):
        return "PluginImportFailed"
    if isinstance(exc, SyntaxError):
        return "RegisterEntrypointUnsupported"
    return "InternalError"


def _check(name: str, passed: bool, detail: str | None = None) -> PluginDoctorCheck:
    return PluginDoctorCheck(name=name, status="PASS" if passed else "FAIL", detail=detail)


def _validate_plugin_name(name: str) -> str:
    if not isinstance(name, str) or not _PLUGIN_NAME_PATTERN.fullmatch(name.strip()):
        raise PluginManagerError("InvalidPluginName")
    return name.strip()


def _validate_candidate_for_enable(candidate: _PluginCandidate) -> None:
    if not candidate.is_safe:
        raise PluginManagerError("PathEscape")
    try:
        _load_manifest(candidate)
    except Exception as exc:
        raise PluginManagerError("InvalidManifest") from exc


def _validate_static_register(entrypoint: Path) -> None:
    try:
        tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))
    except (OSError, SyntaxError) as exc:
        raise PluginManagerError("RegisterEntrypointUnsupported") from exc
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "register":
            raise PluginManagerError("AsyncRegisterNotAllowed")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = tuple(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = (node.target,)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = (node.target,)
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "register"
            for target in targets
        ):
            raise PluginManagerError("RegisterEntrypointUnsupported")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "register":
            return
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if (
                    imported.name == "register"
                    and (imported.asname is None or imported.asname == "register")
                ):
                    return
    raise PluginManagerError("RegisterNotFound")


def _default_config_path() -> Path:
    return hermes_home() / "config.yaml"


def _read_raw_config(path: Path) -> dict:
    if path.is_symlink():
        raise PluginManagerError("ConfigSymlinkNotAllowed")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PluginManagerError("ConfigReadFailed") from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PluginManagerError("ConfigYamlInvalid") from exc
    if not isinstance(raw, dict):
        raise PluginManagerError("ConfigNotMapping")
    return raw


def _plugins_from_raw(raw: dict) -> dict[str, object]:
    value = raw.get("plugins")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginManagerError("PluginsConfigInvalid")
    try:
        return _validate_plugins_config(dict(value))
    except Exception as exc:
        raise PluginManagerError("PluginsConfigInvalid") from exc


@contextlib.contextmanager
def _configuration_lock(path: Path) -> Iterator[None]:
    """使用进程锁和平台文件锁串行化配置修改。"""
    lock_path = path.with_name(f"{path.name}.lock")
    if lock_path.is_symlink():
        raise PluginManagerError("ConfigLockSymlinkNotAllowed")
    with _CONFIG_UPDATE_LOCK:
        try:
            lock_file = lock_path.open("a+b")
        except OSError as exc:
            raise PluginManagerError("ConfigLockFailed") from exc
        lock_acquired = False
        try:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    lock_file.write(b"0")
                    lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                lock_acquired = True
            except (ImportError, OSError) as exc:
                raise PluginManagerError("ConfigLockFailed") from exc
            yield
        finally:
            try:
                if lock_acquired:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        # 锁释放失败不能阻止文件句柄关闭，也不能覆盖主体异常。
                        pass
            finally:
                lock_file.close()


def _write_enabled_atomically(raw: dict, enabled: list[str], path: Path) -> None:
    """只修改 enabled，并以 flush/fsync/replace 原子替换配置。"""
    plugins = raw.get("plugins")
    if plugins is None:
        plugins = {}
        raw["plugins"] = plugins
    if not isinstance(plugins, dict):
        raise PluginManagerError("PluginsConfigInvalid")
    plugins["enabled"] = list(enabled)
    parent = path.parent
    if path.is_symlink():
        raise PluginManagerError("ConfigSymlinkNotAllowed")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            yaml.safe_dump(raw, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    except (OSError, yaml.YAMLError) as exc:
        raise PluginManagerError("ConfigWriteFailed") from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
