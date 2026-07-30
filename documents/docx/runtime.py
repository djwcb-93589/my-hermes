"""固定 Node 子项目的定位、检查与执行封装。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DocxError


_MINIMUM_NODE_MAJOR = 20
_DEPENDENCY_CHECK_TIMEOUT_SECONDS = 10.0
_VERSION_PATTERN = re.compile(r"^v?(?P<major>\d+)(?:\.\d+){0,2}")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_CACHE_ENV = "MYHERMES_DOCX_RUNTIME_CACHE"
_BUNDLE_MANIFEST_NAME = "bundle-manifest.json"
_BUNDLE_SCHEMA_VERSION = 1
_BUNDLE_VERSION = 1
_RUNTIME_BUNDLE_FILES = frozenset(
    {
        "scripts/check.mjs",
        "scripts/create.mjs",
        "vendor/docx.mjs",
    }
)
_MAX_BUNDLE_MANIFEST_SIZE = 64 * 1024
_MAX_BUNDLE_FILE_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class RuntimeComponentStatus:
    """一个 DOCX 核心或可选运行组件的稳定状态。"""

    name: str
    available: bool
    version: str | None
    detail: str | None


@dataclass(frozen=True)
class DocxRuntimeStatus:
    """DOCX 核心能力与各个独立可选组件的状态。"""

    core_available: bool
    components: list[RuntimeComponentStatus]


@dataclass(frozen=True)
class _PreparedBundle:
    """经过完整性校验并写入内容寻址缓存的 Node bundle。"""

    runtime_dir: Path
    create_script: Path
    check_script: Path
    docx_version: str
    fingerprint: str


class NodeRuntime:
    """只允许执行模块内部固定创建脚本的 Node 运行时。"""

    def __init__(self, node_executable: str | Path | None = None) -> None:
        self._configured_executable = node_executable
        self._package_runtime_dir = (
            Path(__file__).resolve().parent / "node_runtime"
        )
        self._runtime_dir = self._package_runtime_dir
        self._script_path = self._runtime_dir / "scripts" / "create.mjs"
        self._dependency_check_path = self._runtime_dir / "scripts" / "check.mjs"
        self._resolved_executable: Path | None = None
        self._node_version: str | None = None
        self._docx_version: str | None = None
        self._bundle_fingerprint: str | None = None
        self._checked = False

    @property
    def node_version(self) -> str | None:
        """返回最近一次成功检查得到的 Node 版本。"""

        return self._node_version

    @property
    def docx_version(self) -> str | None:
        """返回最近一次成功检查得到的 bundle 内 docx 版本。"""

        return self._docx_version

    @property
    def bundle_fingerprint(self) -> str | None:
        """返回最近一次成功检查得到的内容寻址 bundle 摘要。"""

        return self._bundle_fingerprint

    def check(self) -> None:
        """检查 Node 版本、随包 bundle、用户缓存和固定依赖脚本。"""

        version = self.check_node()
        executable = self._resolved_executable
        if executable is None:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。")
        bundle = self._prepare_bundle()
        self._runtime_dir = bundle.runtime_dir
        self._script_path = bundle.create_script
        self._dependency_check_path = bundle.check_script
        self._check_dependencies(executable)
        self._resolved_executable = executable
        self._node_version = version
        self._docx_version = bundle.docx_version
        self._bundle_fingerprint = bundle.fingerprint
        self._checked = True

    def check_node(self) -> str:
        """只检查 Node 可执行文件和主版本，不加载 docx 依赖。"""

        self._reset_check_state()
        executable = self._resolve_executable()
        version = self._check_version(executable)
        self._resolved_executable = executable
        self._node_version = version
        return version

    def _reset_check_state(self) -> None:
        self._checked = False
        self._resolved_executable = None
        self._node_version = None
        self._docx_version = None
        self._bundle_fingerprint = None
        self._runtime_dir = self._package_runtime_dir
        self._script_path = self._runtime_dir / "scripts" / "create.mjs"
        self._dependency_check_path = (
            self._runtime_dir / "scripts" / "check.mjs"
        )

    def run_create(
        self,
        spec_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """调用固定脚本并解析唯一一行 JSON 结果。"""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise DocxError("invalid_request", "timeout_seconds 必须是大于零的有限数值。")
        if not spec_path.is_absolute() or not output_path.is_absolute():
            raise DocxError("invalid_request", "传给 Node runtime 的路径必须是绝对路径。")
        if not self._checked:
            self.check()

        executable = self._resolved_executable
        if executable is None:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。")

        command = [
            str(executable),
            str(self._script_path),
            str(spec_path),
            str(output_path),
        ]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                cwd=self._runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxError(
                "node_execution_timeout",
                "DOCX 创建进程执行超时。",
            ) from exc
        except FileNotFoundError as exc:
            self._checked = False
            raise DocxError(
                "node_runtime_unavailable",
                "Node 运行时不可用。",
            ) from exc
        except OSError as exc:
            raise DocxError(
                "node_execution_failed",
                "无法启动 DOCX 创建进程。",
            ) from exc

        if completed.returncode != 0:
            error_type = self._read_child_error_type(completed.stdout, completed.stderr)
            if error_type == "invalid_request":
                raise DocxError("invalid_request", "Node runtime 拒绝了文档规格。")
            if error_type == "invalid_block":
                raise DocxError("invalid_block", "Node runtime 拒绝了文档内容块。")
            raise DocxError("node_execution_failed", "DOCX 创建进程执行失败。")

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。")
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。") from exc
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or isinstance(result.get("block_count"), bool)
            or not isinstance(result.get("block_count"), int)
            or result["block_count"] < 0
        ):
            raise DocxError("node_result_invalid", "Node runtime 返回了无效结果。")
        return result

    def _prepare_bundle(self) -> _PreparedBundle:
        manifest, manifest_payload = self._load_bundle_manifest()
        source_payloads = self._read_verified_bundle_files(manifest)
        fingerprint = hashlib.sha256(manifest_payload).hexdigest()
        cache_root = _resolve_bundle_cache_root(self._package_runtime_dir)
        bundle_root = cache_root / fingerprint
        try:
            bundle_root.mkdir(parents=True, exist_ok=True)
            if bundle_root.is_symlink():
                raise OSError("bundle cache directory is a symbolic link")
            resolved_bundle_root = bundle_root.resolve(strict=True)
            if not _path_is_within(resolved_bundle_root, cache_root):
                raise OSError("bundle cache directory escaped cache root")
            for relative_path, payload in source_payloads.items():
                target = bundle_root / Path(*relative_path.split("/"))
                _cache_verified_file(
                    target,
                    payload,
                    expected_sha256=manifest["files"][relative_path],
                    bundle_root=resolved_bundle_root,
                )
        except DocxError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise DocxError(
                "node_dependencies_missing",
                "无法准备 DOCX Node bundle 用户缓存。",
            ) from exc

        create_script = bundle_root / "scripts" / "create.mjs"
        check_script = bundle_root / "scripts" / "check.mjs"
        return _PreparedBundle(
            runtime_dir=bundle_root,
            create_script=create_script,
            check_script=check_script,
            docx_version=manifest["dependency"]["version"],
            fingerprint=fingerprint,
        )

    def _load_bundle_manifest(self) -> tuple[dict[str, Any], bytes]:
        manifest_path = self._package_runtime_dir / _BUNDLE_MANIFEST_NAME
        try:
            if (
                not manifest_path.is_file()
                or manifest_path.stat().st_size > _MAX_BUNDLE_MANIFEST_SIZE
            ):
                raise ValueError("bundle manifest unavailable")
            payload = manifest_path.read_bytes()
            manifest = json.loads(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node bundle 清单缺失或无效。",
            ) from exc
        if not _valid_bundle_manifest(manifest):
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node bundle 清单缺失或无效。",
            )

        package_path = self._package_runtime_dir / "package.json"
        lock_path = self._package_runtime_dir / "package-lock.json"
        try:
            package_payload = json.loads(package_path.read_text(encoding="utf-8"))
            lock_payload = lock_path.read_bytes()
            package_lock = json.loads(lock_payload)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node 版本锁定文件缺失或无效。",
            ) from exc

        dependency_version = manifest["dependency"]["version"]
        if (
            package_payload.get("dependencies", {}).get("docx")
            != dependency_version
            or package_payload.get("engines", {}).get("node")
            != f">={_MINIMUM_NODE_MAJOR}"
            or package_lock.get("packages", {})
            .get("", {})
            .get("dependencies", {})
            .get("docx")
            != dependency_version
            or package_lock.get("packages", {})
            .get("", {})
            .get("engines", {})
            .get("node")
            != f">={_MINIMUM_NODE_MAJOR}"
            or package_lock.get("packages", {})
            .get("node_modules/docx", {})
            .get("version")
            != dependency_version
            or _sha256_bytes(lock_payload)
            != manifest["package_lock_sha256"]
        ):
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node bundle 与版本锁定文件不一致。",
            )
        return manifest, payload

    def _read_verified_bundle_files(
        self,
        manifest: dict[str, Any],
    ) -> dict[str, bytes]:
        payloads: dict[str, bytes] = {}
        for relative_path in sorted(_RUNTIME_BUNDLE_FILES):
            source_path = (
                self._package_runtime_dir
                / Path(*relative_path.split("/"))
            )
            try:
                if (
                    not source_path.is_file()
                    or source_path.stat().st_size > _MAX_BUNDLE_FILE_SIZE
                ):
                    raise ValueError("bundle file unavailable")
                payload = source_path.read_bytes()
            except (OSError, ValueError) as exc:
                raise DocxError(
                    "node_dependencies_missing",
                    "DOCX Node bundle 文件缺失或无效。",
                ) from exc
            if _sha256_bytes(payload) != manifest["files"][relative_path]:
                raise DocxError(
                    "node_dependencies_missing",
                    "DOCX Node bundle 完整性检查失败。",
                )
            payloads[relative_path] = payload
        return payloads

    def _resolve_executable(self) -> Path:
        configured = self._configured_executable
        if configured is None:
            configured = os.environ.get("MYHERMES_NODE_EXECUTABLE")

        if configured is None:
            discovered = shutil.which("node")
            if discovered is None:
                raise DocxError("node_runtime_unavailable", "未找到 Node 运行时。")
            return Path(discovered).resolve()

        raw_value = os.fspath(configured).strip()
        if not raw_value:
            raise DocxError("node_runtime_unavailable", "配置的 Node 路径为空。")

        candidate = Path(raw_value).expanduser()
        if candidate.is_absolute():
            executable = candidate.resolve()
        elif candidate.parent == Path("."):
            discovered = shutil.which(raw_value)
            if discovered is None:
                raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可用。")
            executable = Path(discovered).resolve()
        else:
            raise DocxError(
                "node_runtime_unavailable",
                "Node 路径必须是绝对路径或 PATH 中的命令名。",
            )

        if not executable.is_file():
            raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可用。")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise DocxError("node_runtime_unavailable", "配置的 Node 运行时不可执行。")
        return executable

    def _check_version(self, executable: Path) -> str:
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                shell=False,
                cwd=self._runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxError("node_execution_timeout", "Node 版本检查超时。") from exc
        except (FileNotFoundError, OSError) as exc:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。") from exc

        version = completed.stdout.strip()
        match = _VERSION_PATTERN.match(version)
        if completed.returncode != 0 or match is None:
            raise DocxError(
                "node_version_unsupported",
                "无法确认 Node 主版本，要求 Node 20 或更高版本。",
            )
        if int(match.group("major")) < _MINIMUM_NODE_MAJOR:
            raise DocxError(
                "node_version_unsupported",
                "Node 主版本过低，要求 Node 20 或更高版本。",
            )
        return version

    def _check_dependencies(self, executable: Path) -> None:
        try:
            completed = subprocess.run(
                [str(executable), str(self._dependency_check_path)],
                shell=False,
                cwd=self._runtime_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DEPENDENCY_CHECK_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node 依赖加载检查超时。",
            ) from exc
        except (FileNotFoundError, OSError) as exc:
            raise DocxError("node_runtime_unavailable", "Node 运行时不可用。") from exc

        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if completed.returncode != 0 or len(lines) != 1:
            raise DocxError(
                "node_dependencies_missing",
                "随包 DOCX Node bundle 无法加载。",
            )
        try:
            result = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node 依赖检查返回了无效结果。",
            ) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node 依赖检查返回了无效结果。",
            )

    @staticmethod
    def _read_child_error_type(stdout: str, stderr: str) -> str | None:
        for stream in (stdout, stderr):
            lines = [line for line in stream.splitlines() if line.strip()]
            if len(lines) != 1:
                continue
            try:
                payload = json.loads(lines[0])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("error_type"), str):
                return payload["error_type"]
        return None


def _valid_bundle_manifest(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "bundle_version",
        "minimum_node_major",
        "dependency",
        "package_lock_sha256",
        "files",
    }:
        return False
    dependency = value.get("dependency")
    files = value.get("files")
    return (
        value.get("schema_version") == _BUNDLE_SCHEMA_VERSION
        and value.get("bundle_version") == _BUNDLE_VERSION
        and value.get("minimum_node_major") == _MINIMUM_NODE_MAJOR
        and isinstance(dependency, dict)
        and set(dependency) == {"name", "version"}
        and dependency.get("name") == "docx"
        and isinstance(dependency.get("version"), str)
        and bool(dependency["version"])
        and _valid_sha256(value.get("package_lock_sha256"))
        and isinstance(files, dict)
        and set(files) == _RUNTIME_BUNDLE_FILES
        and all(_valid_sha256(digest) for digest in files.values())
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _resolve_bundle_cache_root(package_runtime_dir: Path) -> Path:
    configured = os.environ.get(_BUNDLE_CACHE_ENV)
    try:
        if configured is not None:
            raw_value = configured.strip()
            if not raw_value:
                raise ValueError("empty cache path")
            candidate = Path(raw_value).expanduser()
            if not candidate.is_absolute():
                raise ValueError("relative cache path")
        elif os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA")
            candidate = (
                Path(local_app_data)
                if local_app_data and Path(local_app_data).is_absolute()
                else Path.home() / "AppData" / "Local"
            )
            candidate = candidate / "MyHermes" / "Cache" / "docx-node"
        elif sys.platform == "darwin":
            candidate = (
                Path.home()
                / "Library"
                / "Caches"
                / "MyHermes"
                / "docx-node"
            )
        else:
            xdg_cache = os.environ.get("XDG_CACHE_HOME")
            base = (
                Path(xdg_cache)
                if xdg_cache and Path(xdg_cache).is_absolute()
                else Path.home() / ".cache"
            )
            candidate = base / "myhermes" / "docx-node"
        cache_root = candidate.resolve(strict=False)
        package_root = package_runtime_dir.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DocxError(
            "node_dependencies_missing",
            "DOCX Node bundle 缓存路径无效。",
        ) from exc

    if _path_is_within(cache_root, package_root):
        raise DocxError(
            "node_dependencies_missing",
            "DOCX Node bundle 缓存不得位于安装包目录。",
        )
    for install_path in {
        sysconfig.get_path("purelib"),
        sysconfig.get_path("platlib"),
    }:
        if not install_path:
            continue
        try:
            site_packages = Path(install_path).resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if _path_is_within(cache_root, site_packages):
            raise DocxError(
                "node_dependencies_missing",
                "DOCX Node bundle 缓存不得位于 site-packages。",
            )
    return cache_root


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _cache_verified_file(
    target: Path,
    payload: bytes,
    *,
    expected_sha256: str,
    bundle_root: Path,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve(strict=True)
    if not _path_is_within(resolved_parent, bundle_root):
        raise DocxError(
            "node_dependencies_missing",
            "DOCX Node bundle 缓存路径越界。",
        )
    if (
        target.is_file()
        and not target.is_symlink()
        and _sha256_file(target) == expected_sha256
    ):
        return

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        if target.is_symlink() or _sha256_file(target) != expected_sha256:
            raise OSError("cached bundle verification failed")
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_docx_runtime(
    *,
    node_runtime: NodeRuntime | None = None,
    libreoffice_executable: str | Path | None = None,
) -> DocxRuntimeStatus:
    """分别检查 Python 核心、Node 创建和可选渲染组件。"""

    components: list[RuntimeComponentStatus] = []
    python_available = True
    try:
        from .editor import DocxEditor
        from .reader import DocxReader
        from .search import DocxSearcher
        from .validator import DocxValidator

        reader = DocxReader()
        DocxSearcher(reader=reader)
        DocxEditor(reader=reader)
        DocxValidator(reader=reader)
    except (ImportError, RuntimeError, TypeError, ValueError):
        python_available = False
    components.append(
        RuntimeComponentStatus(
            name="python_core",
            available=python_available,
            version=None,
            detail=None if python_available else "runtime_check_failed",
        )
    )

    runtime = node_runtime or NodeRuntime()
    node_available = False
    node_version: str | None = None
    node_detail: str | None = None
    try:
        node_version = runtime.check_node()
        node_available = True
    except DocxError as exc:
        node_detail = exc.error_type
    components.append(
        RuntimeComponentStatus(
            name="node_runtime",
            available=node_available,
            version=node_version,
            detail=node_detail,
        )
    )

    dependency_available = False
    dependency_detail: str | None = (
        None if node_available else node_detail
    )
    dependency_version: str | None = None
    if node_available:
        try:
            runtime.check()
            dependency_available = True
            dependency_version = runtime.docx_version
            dependency_detail = "bundled_cache"
        except DocxError as exc:
            dependency_detail = exc.error_type
    components.append(
        RuntimeComponentStatus(
            name="node_docx_dependency",
            available=dependency_available,
            version=dependency_version,
            detail=dependency_detail,
        )
    )

    libreoffice_available = False
    libreoffice_version: str | None = None
    libreoffice_detail: str | None = None
    try:
        from .renderer import (
            find_libreoffice_executable,
            read_libreoffice_version,
        )

        executable = find_libreoffice_executable(
            libreoffice_executable
        )
        libreoffice_version = read_libreoffice_version(executable)
        libreoffice_available = True
    except DocxError as exc:
        libreoffice_detail = exc.error_type
    except (ImportError, RuntimeError, TypeError, ValueError):
        libreoffice_detail = "runtime_check_failed"
    components.append(
        RuntimeComponentStatus(
            name="libreoffice_renderer",
            available=libreoffice_available,
            version=libreoffice_version,
            detail=libreoffice_detail,
        )
    )

    pdf_available = False
    pdf_version: str | None = None
    pdf_detail: str | None = None
    try:
        from .renderer import pdf_page_renderer_status

        pdf_available, pdf_version = pdf_page_renderer_status()
        if not pdf_available:
            pdf_detail = "pdf_renderer_unavailable"
    except (ImportError, RuntimeError, TypeError, ValueError):
        pdf_detail = "runtime_check_failed"
    components.append(
        RuntimeComponentStatus(
            name="pdf_page_renderer",
            available=pdf_available,
            version=pdf_version,
            detail=pdf_detail,
        )
    )
    return DocxRuntimeStatus(
        core_available=python_available,
        components=components,
    )
