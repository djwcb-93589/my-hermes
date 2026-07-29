"""可选 LibreOffice PDF 转换与可选本地 PDF 页面图片导出。"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .errors import DocxError
from .validation_models import ValidateDocumentRequest
from .validator import DocxValidator


_LIBREOFFICE_ENV = "LIBREOFFICE_PATH"
_LIBREOFFICE_VERSION_TIMEOUT_SECONDS = 10.0
_MAX_RENDER_TIMEOUT_SECONDS = 600
_MAX_PDF_SIZE = 512 * 1024 * 1024
_MAX_RENDERED_PAGES = 200
_MAX_PAGE_PIXELS = 40_000_000
_MAX_PAGE_IMAGE_TOTAL_SIZE = 512 * 1024 * 1024
_PAGE_RENDER_SCALE = 1.5


@dataclass(frozen=True)
class RenderDocumentRequest:
    """请求将一个已验证 DOCX 渲染为 PDF 和可选页面 PNG。"""

    source_path: Path
    output_dir: Path
    overwrite: bool = False
    export_page_images: bool = False
    timeout_seconds: int = 120


@dataclass(frozen=True)
class RenderedPage:
    """一个从 PDF 页面导出的稳定编号 PNG。"""

    page_number: int
    image_path: Path


@dataclass(frozen=True)
class RenderDocumentResult:
    """LibreOffice 渲染成功后的最终输出路径。"""

    source_path: Path
    pdf_path: Path
    pages: list[RenderedPage]
    renderer: str


class DocxRenderer:
    """按需发现 LibreOffice，并使用隔离 profile 输出受控文件。"""

    def __init__(
        self,
        *,
        validator: DocxValidator | None = None,
        libreoffice_executable: str | Path | None = None,
    ) -> None:
        self._validator = validator or DocxValidator()
        self._configured_executable = libreoffice_executable

    def render(
        self,
        request: RenderDocumentRequest,
    ) -> RenderDocumentResult:
        validated_request = _validate_request(request)
        validation_result = self._validator.validate(
            ValidateDocumentRequest(
                source_path=validated_request.source_path,
                strict=True,
            )
        )
        if not validation_result.valid:
            raise DocxError(
                "validation_failed",
                "源 DOCX 未通过核心验证，未启动 Renderer。",
            )
        source_path = validation_result.source_path
        output_dir, created_output_dir = _prepare_output_directory(
            validated_request.output_dir,
            source_path=source_path,
        )
        target_pdf = output_dir / f"{source_path.stem}.pdf"
        try:
            if target_pdf.exists() and not validated_request.overwrite:
                raise DocxError("output_exists", "目标 PDF 已存在。")
            if validated_request.export_page_images:
                require_pdf_page_renderer()
            executable = find_libreoffice_executable(
                self._configured_executable
            )
            version = read_libreoffice_version(executable)
            with (
                tempfile.TemporaryDirectory(
                    prefix="myhermes-lo-profile-"
                ) as profile_directory,
                tempfile.TemporaryDirectory(
                    prefix=".myhermes-docx-render-",
                    dir=output_dir,
                ) as staging_directory,
            ):
                staging_path = Path(staging_directory).resolve()
                staged_pdf = _run_libreoffice(
                    executable=executable,
                    source_path=source_path,
                    staging_path=staging_path,
                    profile_path=Path(profile_directory).resolve(),
                    timeout_seconds=validated_request.timeout_seconds,
                )
                page_paths = (
                    _render_pdf_pages(staged_pdf, staging_path)
                    if validated_request.export_page_images
                    else []
                )
                outputs = [(staged_pdf, target_pdf)]
                outputs.extend(
                    (
                        page_path,
                        output_dir / page_path.name,
                    )
                    for page_path in page_paths
                )
                _commit_render_outputs(
                    outputs,
                    overwrite=validated_request.overwrite,
                    staging_path=staging_path,
                )
        except DocxError:
            _cleanup_created_output_directory(
                output_dir,
                created=created_output_dir,
            )
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            _cleanup_created_output_directory(
                output_dir,
                created=created_output_dir,
            )
            raise DocxError(
                "render_failed",
                "DOCX Renderer 执行失败。",
            ) from exc

        pages = [
            RenderedPage(
                page_number=index,
                image_path=output_dir / f"page-{index:04d}.png",
            )
            for index in range(1, len(page_paths) + 1)
        ]
        return RenderDocumentResult(
            source_path=source_path,
            pdf_path=target_pdf,
            pages=pages,
            renderer=version,
        )


def render_document(
    request: RenderDocumentRequest,
) -> RenderDocumentResult:
    """使用一次性 Renderer 转换本地 DOCX。"""

    return DocxRenderer().render(request)


def find_libreoffice_executable(
    configured: str | Path | None = None,
) -> Path:
    """在显式路径、环境变量、PATH 和有限常见目录中发现可执行文件。"""

    candidate_value = configured
    from_environment = False
    if candidate_value is None:
        candidate_value = os.environ.get(_LIBREOFFICE_ENV)
        from_environment = candidate_value is not None
    if candidate_value is not None:
        try:
            raw_value = os.fspath(candidate_value).strip()
        except TypeError as exc:
            raise DocxError(
                "renderer_unavailable",
                "配置的 LibreOffice 路径无效。",
            ) from exc
        if (
            not raw_value
            or "://" in raw_value
            or "\x00" in raw_value
        ):
            raise DocxError(
                "renderer_unavailable",
                "配置的 LibreOffice 路径无效。",
            )
        candidate = Path(raw_value).expanduser()
        if from_environment and not candidate.is_absolute():
            raise DocxError(
                "renderer_unavailable",
                "LIBREOFFICE_PATH 必须指向绝对可执行文件。",
            )
        if candidate.is_absolute():
            return _require_executable(candidate)
        if candidate.parent != Path("."):
            raise DocxError(
                "renderer_unavailable",
                "LibreOffice 路径必须是绝对路径或 PATH 命令名。",
            )
        discovered = shutil.which(raw_value)
        if discovered is None:
            raise DocxError(
                "renderer_unavailable",
                "配置的 LibreOffice 可执行文件不可用。",
            )
        return _require_executable(Path(discovered))

    for command_name in ("libreoffice", "soffice"):
        discovered = shutil.which(command_name)
        if discovered is not None:
            return _require_executable(Path(discovered))
    if os.name == "nt":
        for candidate in _windows_libreoffice_candidates():
            if candidate.is_file():
                return _require_executable(candidate)
    raise DocxError(
        "renderer_unavailable",
        "未找到可用的 LibreOffice Renderer。",
    )


def read_libreoffice_version(executable: Path) -> str:
    """以无 GUI 的版本命令读取受控单行版本信息。"""

    try:
        completed = subprocess.run(
            [str(executable), "--headless", "--version"],
            shell=False,
            cwd=executable.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LIBREOFFICE_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocxError(
            "renderer_unavailable",
            "LibreOffice 版本检查超时。",
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise DocxError(
            "renderer_unavailable",
            "LibreOffice 可执行文件无法启动。",
        ) from exc
    output_lines = [
        line.strip()
        for line in (
            completed.stdout.splitlines()
            + completed.stderr.splitlines()
        )
        if line.strip()
    ]
    version_line = next(
        (
            line[:200]
            for line in output_lines
            if "libreoffice" in line.lower()
        ),
        None,
    )
    if completed.returncode != 0 or version_line is None:
        raise DocxError(
            "renderer_unavailable",
            "无法确认 LibreOffice 版本。",
        )
    return version_line


def pdf_page_renderer_status() -> tuple[bool, str | None]:
    """返回可选 PyMuPDF 模块是否可导入及受控版本。"""

    loaded = _load_pdf_renderer_module()
    if loaded is None:
        return False, None
    _, module = loaded
    version = (
        getattr(module, "VersionBind", None)
        or getattr(module, "__version__", None)
    )
    return True, str(version)[:100] if version is not None else None


def require_pdf_page_renderer() -> str:
    """要求可选 PDF 页面渲染模块存在，但不自动安装。"""

    loaded = _load_pdf_renderer_module()
    if loaded is None:
        raise DocxError(
            "pdf_renderer_unavailable",
            "请求页面图片，但未安装可用的 PyMuPDF。",
        )
    return loaded[0]


def _load_pdf_renderer_module() -> tuple[str, ModuleType] | None:
    for module_name in ("pymupdf", "fitz"):
        try:
            if importlib.util.find_spec(module_name) is None:
                continue
            module = importlib.import_module(module_name)
        except (ImportError, OSError, RuntimeError, ValueError):
            continue
        if callable(getattr(module, "open", None)) and callable(
            getattr(module, "Matrix", None)
        ):
            return module_name, module
    return None


def _validate_request(
    request: RenderDocumentRequest,
) -> RenderDocumentRequest:
    if not isinstance(request, RenderDocumentRequest):
        raise DocxError(
            "invalid_request",
            "request 必须是 RenderDocumentRequest。",
        )
    if not isinstance(request.source_path, (str, os.PathLike)):
        raise DocxError("invalid_request", "source_path 必须是文件系统路径。")
    if not isinstance(request.output_dir, (str, os.PathLike)):
        raise DocxError("invalid_request", "output_dir 必须是文件系统路径。")
    if not isinstance(request.overwrite, bool):
        raise DocxError("invalid_request", "overwrite 必须是布尔值。")
    if not isinstance(request.export_page_images, bool):
        raise DocxError(
            "invalid_request",
            "export_page_images 必须是布尔值。",
        )
    if (
        isinstance(request.timeout_seconds, bool)
        or not isinstance(request.timeout_seconds, int)
        or not 1
        <= request.timeout_seconds
        <= _MAX_RENDER_TIMEOUT_SECONDS
    ):
        raise DocxError(
            "invalid_request",
            "timeout_seconds 必须是 1 到 600 之间的整数。",
        )
    return request


def _prepare_output_directory(
    output_dir: Path,
    *,
    source_path: Path,
) -> tuple[Path, bool]:
    try:
        requested = Path(os.fspath(output_dir)).expanduser().absolute()
        if requested.is_symlink():
            raise DocxError(
                "invalid_output_path",
                "output_dir 不能是符号链接。",
            )
        normalized = requested.resolve(strict=False)
        if normalized == source_path:
            raise DocxError(
                "invalid_output_path",
                "output_dir 不能与源 DOCX 文件相同。",
            )
        if normalized.exists():
            if normalized.is_symlink() or not normalized.is_dir():
                raise DocxError(
                    "invalid_output_path",
                    "output_dir 必须是普通目录。",
                )
            return normalized, False
        parent = normalized.parent
        if not parent.exists() or not parent.is_dir():
            raise DocxError(
                "invalid_output_path",
                "output_dir 的父目录必须已经存在。",
            )
        normalized.mkdir()
        return normalized, True
    except DocxError:
        raise
    except OSError as exc:
        raise DocxError(
            "invalid_output_path",
            "无法安全准备 output_dir。",
        ) from exc


def _cleanup_created_output_directory(
    output_dir: Path,
    *,
    created: bool,
) -> None:
    if not created:
        return
    try:
        output_dir.rmdir()
    except OSError:
        pass


def _require_executable(candidate: Path) -> Path:
    try:
        resolved = candidate.expanduser().resolve(strict=True)
        file_stat = resolved.stat()
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise DocxError(
            "renderer_unavailable",
            "LibreOffice 可执行文件不可用。",
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise DocxError(
            "renderer_unavailable",
            "LibreOffice 路径不是普通文件。",
        )
    if os.name == "nt" and resolved.suffix.lower() != ".exe":
        raise DocxError(
            "renderer_unavailable",
            "Windows LibreOffice 路径必须指向 .exe 可执行文件。",
        )
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise DocxError(
            "renderer_unavailable",
            "LibreOffice 文件不可执行。",
        )
    return resolved


def _windows_libreoffice_candidates() -> tuple[Path, ...]:
    roots: list[Path] = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        raw_root = os.environ.get(variable)
        if raw_root:
            roots.append(Path(raw_root))
    roots.extend((Path("C:/Program Files"), Path("C:/Program Files (x86)")))
    seen: set[str] = set()
    candidates: list[Path] = []
    for root in roots:
        candidate = root / "LibreOffice" / "program" / "soffice.exe"
        normalized = str(candidate).lower()
        if normalized not in seen:
            seen.add(normalized)
            candidates.append(candidate)
    return tuple(candidates)


def _run_libreoffice(
    *,
    executable: Path,
    source_path: Path,
    staging_path: Path,
    profile_path: Path,
    timeout_seconds: int,
) -> Path:
    command = [
        str(executable),
        f"-env:UserInstallation={profile_path.as_uri()}",
        "--headless",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        str(staging_path),
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            cwd=staging_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocxError(
            "render_timeout",
            "LibreOffice 渲染超时。",
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        raise DocxError(
            "render_failed",
            "无法启动 LibreOffice Renderer。",
        ) from exc
    if completed.returncode != 0:
        raise DocxError(
            "render_failed",
            f"LibreOffice 渲染失败，退出码 {completed.returncode}。",
        )
    expected_pdf = staging_path / f"{source_path.stem}.pdf"
    _validate_pdf_output(expected_pdf)
    return expected_pdf


def _validate_pdf_output(pdf_path: Path) -> None:
    try:
        file_stat = pdf_path.stat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_size <= 5
            or file_stat.st_size > _MAX_PDF_SIZE
        ):
            raise DocxError(
                "render_output_invalid",
                "Renderer 未生成大小有效的 PDF。",
            )
        with pdf_path.open("rb") as source:
            signature = source.read(5)
    except DocxError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise DocxError(
            "render_output_invalid",
            "Renderer 未生成可读取的 PDF。",
        ) from exc
    if signature != b"%PDF-":
        raise DocxError(
            "render_output_invalid",
            "Renderer 输出缺少 PDF 签名。",
        )


def _render_pdf_pages(
    pdf_path: Path,
    staging_path: Path,
) -> list[Path]:
    module_name = require_pdf_page_renderer()
    try:
        pdf_module = importlib.import_module(module_name)
        document = pdf_module.open(str(pdf_path))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise DocxError(
            "render_failed",
            "PDF 页面渲染器无法打开输出 PDF。",
        ) from exc
    page_paths: list[Path] = []
    total_size = 0
    try:
        page_count = int(document.page_count)
        if page_count < 1 or page_count > _MAX_RENDERED_PAGES:
            raise DocxError(
                "render_output_invalid",
                "PDF 页数超出页面图片导出限制。",
            )
        matrix = pdf_module.Matrix(
            _PAGE_RENDER_SCALE,
            _PAGE_RENDER_SCALE,
        )
        for page_index in range(page_count):
            page = document.load_page(page_index)
            projected_width = max(
                1,
                round(float(page.rect.width) * _PAGE_RENDER_SCALE),
            )
            projected_height = max(
                1,
                round(float(page.rect.height) * _PAGE_RENDER_SCALE),
            )
            if (
                projected_width * projected_height
                > _MAX_PAGE_PIXELS
            ):
                raise DocxError(
                    "render_output_invalid",
                    "PDF 单页像素超过页面图片导出限制。",
                )
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            if pixmap.width * pixmap.height > _MAX_PAGE_PIXELS:
                raise DocxError(
                    "render_output_invalid",
                    "PDF 单页实际像素超过页面图片导出限制。",
                )
            page_path = staging_path / f"page-{page_index + 1:04d}.png"
            pixmap.save(str(page_path))
            page_size = page_path.stat().st_size
            total_size += page_size
            if page_size <= 0 or total_size > _MAX_PAGE_IMAGE_TOTAL_SIZE:
                raise DocxError(
                    "render_output_invalid",
                    "页面图片输出大小超过安全限制。",
                )
            page_paths.append(page_path)
    except DocxError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DocxError(
            "render_failed",
            "PDF 页面图片导出失败。",
        ) from exc
    finally:
        try:
            document.close()
        except Exception:
            pass
    return page_paths


def _commit_render_outputs(
    outputs: list[tuple[Path, Path]],
    *,
    overwrite: bool,
    staging_path: Path,
) -> None:
    if len({target for _, target in outputs}) != len(outputs):
        raise DocxError(
            "render_output_invalid",
            "Renderer 产生了重复目标文件名。",
        )
    for staged, target in outputs:
        if (
            staged.parent != staging_path
            or target.parent == staging_path
            or staged.is_symlink()
            or not staged.is_file()
        ):
            raise DocxError(
                "render_output_invalid",
                "Renderer 输出提交计划无效。",
            )
        if target.is_symlink() or (
            target.exists() and not target.is_file()
        ):
            raise DocxError(
                "invalid_output_path",
                "Renderer 目标必须是普通文件路径。",
            )
        if target.exists() and not overwrite:
            raise DocxError("output_exists", "Renderer 目标文件已存在。")

    if not overwrite:
        committed: list[Path] = []
        try:
            for staged, target in outputs:
                os.link(staged, target)
                committed.append(target)
        except FileExistsError as exc:
            _rollback_created_outputs(committed)
            raise DocxError(
                "output_exists",
                "Renderer 目标文件在提交时已存在。",
            ) from exc
        except OSError as exc:
            _rollback_created_outputs(committed)
            raise DocxError(
                "render_failed",
                "无法以原子 no-clobber 方式提交 Renderer 输出。",
            ) from exc
        return

    backup_directory = staging_path / "backups"
    backup_directory.mkdir()
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for index, (_, target) in enumerate(outputs):
            if target.exists():
                backup = backup_directory / f"backup-{index:04d}"
                os.replace(target, backup)
                backups.append((backup, target))
        for staged, target in outputs:
            os.replace(staged, target)
            committed.append(target)
    except OSError as exc:
        _rollback_created_outputs(committed)
        for backup, target in reversed(backups):
            try:
                os.replace(backup, target)
            except OSError:
                pass
        raise DocxError(
            "render_failed",
            "无法原子提交 Renderer 输出。",
        ) from exc


def _rollback_created_outputs(paths: list[Path]) -> None:
    for path in reversed(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
