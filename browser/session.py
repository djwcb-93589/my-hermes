"""
同步浏览器会话。

封装 Playwright 的 launch / context / page 生命周期,对外暴露读取 + 交互
操作:

读取:
- ``navigate(url)`` -- 打开 URL,返回带 ``snapshot_id`` 的观察结果 JSON
- ``snapshot()`` -- 对当前 page 取一次 accessibility snapshot,返回观察结果 JSON

交互(P1,操作后自动返回新快照):
- ``click(ref, snapshot_id)`` -- 点击元素
- ``type(ref, text, snapshot_id, clear=True)`` -- 在输入框填文字,默认先清空
- ``press(key, snapshot_id)`` -- 按键盘键(Enter/Tab/Escape 等)
- ``select(ref, value, snapshot_id)`` -- 下拉选择(<select> 元素)

文件传输与页面产物(P7):
- ``upload_files(ref, paths, snapshot_id)`` -- 向工作区内的文件选择框上传文件
- ``download(ref, snapshot_id)`` -- 点击下载并将文件登记为会话产物
- ``screenshot(snapshot_id)`` / ``screenshot_element(ref, snapshot_id)`` -- 保存 PNG 截图

图片与音频理解(P8):
- ``analyze_media(sources, prompt)`` -- 分析会话产物或工作区相对路径中的媒体
- ``analyze_image`` / ``analyze_audio`` -- 限定单一媒体类型的简化入口
- ``analyze_page(snapshot_id, prompt)`` -- 截图当前页面后交给配置好的多模态模型

自主浏览与结构化提取(P9):
- ``find_in_page`` / ``extract_links`` / ``extract_tables`` / ``extract_forms``
  / ``extract_metadata`` -- 使用现有快照只读提取页面内容
- ``collect_paginated`` -- 在页面、结果、文本和时间预算内跟随明确的下一页控件

导航(P2,操作后自动返回新快照):
- ``back(snapshot_id)`` -- 回退到上一页
- ``forward(snapshot_id)`` -- 前进到下一页
- ``reload(snapshot_id)`` -- 重新加载当前页
- ``scroll(direction, snapshot_id, amount=400)`` -- 滚动页面(up/down/left/right)

高级读取(P3):
- ``get_text(ref, snapshot_id, max_chars=5000)`` -- 读元素/整页连贯文本。
  纯读取:不失效旧 snapshot_id、不取新快照(ref 仍可用)。整页文本默认截断。
- ``console(expression, snapshot_id)`` -- 执行任意 JS,返回序列化结果。
  逃生舱:AX tree 看不到的元素、结构化数据用它。危险:JS 改 DOM 后旧 ref
  失效,按交互操作处理(失效旧观察、取新快照)。接 agent 时必须 unknown_on_crash。

条件等待(P4,结束后自动返回新快照):
- ``wait_for_url(pattern, snapshot_id, timeout_ms=None)`` -- 等待 URL 匹配 glob 模式
- ``wait_for_text(text, snapshot_id, timeout_ms=None)`` -- 等待可见文本出现
- ``wait_for_ref(ref, snapshot_id, timeout_ms=None)`` -- 等待已有 ref 对应元素可见
- ``wait_for_load_state(state, snapshot_id, timeout_ms=None)`` -- 等待页面加载状态
  等待超时或被 ``cancel_event`` 取消时也会返回当前页面的新快照，调用方可继续决策。

为什么是同步
------------
独立测试阶段 sync 更简洁:CLI 不用 ``asyncio.run()``,测试不用 async def。
Playwright 的 sync API 内部用 greenlet,不能在已有 asyncio event loop
的线程里跑 -- 接入 agent 时如果 handler 在 async 上下文,用
``asyncio.to_thread`` 把 sync 调用丢到线程池即可。

ref -> DOM 元素解析
-------------------
snapshot 时缓存 ``ref -> backendDOMNodeId`` 映射，并生成递增的
``snapshot_id``。交互必须提交产生该 ref 的版本号，避免页面变化后旧 ``e1``
意外指向新页面的另一个元素。交互流程是
``backendDOMNodeId -> 临时原生定位锚点 -> Playwright Locator -> 清理锚点``。
backendDOMNodeId 直接来自浏览器内部，因此不依赖页面作者提供的脆弱 CSS
选择器；临时锚点只存在于这一次动作，随后恢复网页原有属性。代价是 ref 在
snapshot 之间失效，调用方必须重新取快照才能拿到新 ref。

session_key 单例池
------------------
参照 ``hermes/backends/__init__.py:575`` 的 ``get_backend(session_key)``
模式,同一个 ``session_key`` 复用同一个 ``BrowserSession`` -- cookie、
localStorage 跨调用保持。默认 key 为 ``"default"``。调用方在会话结束时
用 ``close_session(session_key)`` 释放。
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import sys
import threading
from uuid import uuid4
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable
from urllib.parse import urlsplit

from browser.accessibility import format_snapshot
from browser.multimodal import (
    MediaSource,
    MultimodalAnalyzer,
    MultimodalError,
)


_WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_MAX_ARTIFACT_FILENAME_BYTES = 240


@dataclass(frozen=True)
class _RefTextReadResult:
    """元素文本读取的内部结果，避免用 JSON 字符串承载控制流。"""

    text: str | None = None
    error_type: str | None = None
    error: str | None = None


@dataclass
class _PageState:
    """会话内一个标签页的标识、快照与 ref 映射。"""

    page_id: str
    page: Any
    ref_to_backend_id: dict[str, int]
    active_snapshot_id: str | None = None
    snapshot_text: str | None = None
    history_urls: list[str] = field(default_factory=list)
    history_markers: list[str] = field(default_factory=list)
    history_index: int = -1


@dataclass(frozen=True)
class _NativeTarget:
    """一次原生交互临时创建的定位锚点及其页面原有属性。"""

    page: Any
    locator: Any
    token: str
    marker_existed: bool
    previous_marker: str | None


@dataclass(frozen=True)
class _Artifact:
    """当前会话登记的本地页面产物。"""

    artifact_id: str
    kind: str
    path: Path
    filename: str
    # 仅按文件名扩展名推测，不能作为内容类型或安全判断依据。
    mime_type: str | None
    size_bytes: int
    page_id: str | None
    source_url: str


class BrowserSession:
    """单浏览器实例的同步包装。

    生命周期::

        with BrowserSession() as s:
            observation = json.loads(s.navigate("https://example.com"))
            print(observation["snapshot"])

    退出时自动关 page / context / browser / playwright-runtime。
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_ms: int = 30000,
        channel: str | None = "chrome",
        workspace_root: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        multimodal_analyzer: MultimodalAnalyzer | None = None,
    ):
        self._headless = headless
        self._timeout_ms = timeout_ms
        # channel="chrome" 用系统装的 Google Chrome,避免下载 Playwright 自带
        # Chromium(~150MB)。传 None 回退到 Playwright 自带 Chromium。
        # 常用值:"chrome"、"msedge"、None。
        self._channel = channel
        self.workspace_root = self._resolve_workspace_root(workspace_root)
        self.artifact_dir = self._resolve_artifact_dir(artifact_dir)
        self._artifacts: dict[str, _Artifact] = {}
        self._artifact_counter = 0
        # P9 提取器只读取当前页面；延迟创建以避免普通浏览会话加载无关逻辑。
        self._page_extractor: Any | None = None
        # 多模态服务不参与页面状态管理。未显式提供时延迟按环境变量创建，
        # 避免未配置 ARK 时影响只使用浏览器能力的会话。
        self._multimodal_analyzer = multimodal_analyzer
        # Playwright 资源句柄;在 __enter__ 里赋值。
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        # RLock 而不是 asyncio.Lock -- sync 调用串行化;
        # 接 agent 时 to_thread 把 handler 丢线程池,多个 tool call 可能并发。
        self._lock = threading.RLock()
        # Playwright sync API 的对象严格绑定创建它的线程。显式记录所有者，
        # 避免跨线程调用时泄露难理解的 greenlet.error。
        self._owner_thread_id: int | None = None
        # ref(e1) -> backendDOMNodeId 映射。每次 snapshot 重新填充。
        # 调用方拿到 snapshot 文本后,用 ref 调交互操作;交互操作内部
        # 用这个映射定位 DOM 元素。snapshot 之间 ref 失效 -- 这是文档约定。
        self._ref_to_backend_id: dict[str, int] = {}
        # 每次观察都会生成递增版本。交互必须携带该版本，防止旧 ref 错指
        # 页面已经变化后的另一个元素。
        self._snapshot_counter = 0
        self._active_snapshot_id: str | None = None
        # 多页面注册表。_page 始终是当前页面，保留它以兼容原有同步实现。
        self._pages: dict[str, _PageState] = {}
        self._page_ids_by_object: dict[int, str] = {}
        self._current_page_id: str | None = None
        self._page_counter = 0
        self._frame_ids_by_object: dict[int, str] = {}
        self._frame_counter = 0
        # 对话框必须在触发动作的同步调用中立即处理，不能跨调用保存 Dialog 对象。
        self._active_dialog_policy: tuple[str, str | None] | None = None
        self._next_dialog_policy: tuple[str, str | None] | None = None
        self._last_dialog_event: dict[str, str] | None = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        """判断已解析路径是否仍位于指定根目录内。"""
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _contains_parent_reference(path: Path) -> bool:
        """拒绝调用方显式传入的 ``..``，避免把规范化当作授权。"""
        return any(part == ".." for part in path.parts)

    @staticmethod
    def _has_symlink_component(path: Path, root: Path) -> bool:
        """检查 root 到 path 的已有组成部分，防止符号链接改变路径含义。"""
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        current = root
        if current.is_symlink():
            return True
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink():
                    return True
        return False

    def _resolve_workspace_root(self, workspace_root: str | Path | None) -> Path:
        """解析会话允许读取上传文件的根目录。"""
        raw_root = Path.cwd() if workspace_root is None else Path(workspace_root)
        if self._contains_parent_reference(raw_root):
            raise ValueError("workspace_root 不允许包含 '..'")
        try:
            resolved = raw_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"workspace_root 无法解析: {exc}") from exc
        if not resolved.is_dir():
            raise ValueError("workspace_root 必须是已存在的目录")
        return resolved

    def _resolve_artifact_dir(self, artifact_dir: str | Path | None) -> Path:
        """创建并验证只用于会话产物的受限输出目录。"""
        raw_dir = Path(".browser-artifacts") if artifact_dir is None else Path(artifact_dir)
        if raw_dir.is_absolute() or self._contains_parent_reference(raw_dir):
            raise ValueError("artifact_dir 必须是 workspace_root 内不含 '..' 的相对目录")
        candidate = self.workspace_root / raw_dir
        if self._has_symlink_component(candidate, self.workspace_root):
            raise ValueError("artifact_dir 不能包含符号链接")
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"artifact_dir 无法创建或解析: {exc}") from exc
        if not self._is_relative_to(resolved, self.workspace_root) or not resolved.is_dir():
            raise ValueError("artifact_dir 必须位于 workspace_root 内")
        if self._has_symlink_component(resolved, self.workspace_root):
            raise ValueError("artifact_dir 不能包含符号链接")
        return resolved

    def _configured_artifact_dir_locked(self, artifact_dir: str | Path) -> Path:
        """解析已有会话的目录参数，但不因冲突检查创建新目录。"""
        raw_dir = Path(artifact_dir)
        if raw_dir.is_absolute() or self._contains_parent_reference(raw_dir):
            raise ValueError("artifact_dir 必须是 workspace_root 内不含 '..' 的相对目录")
        candidate = self.workspace_root / raw_dir
        if self._has_symlink_component(candidate, self.workspace_root):
            raise ValueError("artifact_dir 不能包含符号链接")
        resolved = candidate.resolve(strict=False)
        if not self._is_relative_to(resolved, self.workspace_root):
            raise ValueError("artifact_dir 必须位于 workspace_root 内")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("artifact_dir 必须是目录")
        return resolved

    def _resolve_upload_path_locked(self, value: str | Path) -> tuple[Path | None, str | None]:
        """验证单个上传文件，始终拒绝符号链接和工作区外的路径。"""
        if not isinstance(value, (str, Path)):
            return None, "invalid_path"
        raw_path = Path(value)
        if not str(raw_path) or self._contains_parent_reference(raw_path):
            return None, "invalid_path"
        candidate = raw_path if raw_path.is_absolute() else self.workspace_root / raw_path
        try:
            if candidate.is_symlink():
                return None, "invalid_path"
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            return None, "file_not_found"
        except (OSError, RuntimeError):
            return None, "invalid_path"
        if not self._is_relative_to(resolved, self.workspace_root):
            return None, "path_outside_workspace"
        if self._has_symlink_component(candidate, self.workspace_root):
            return None, "invalid_path"
        if not resolved.is_file():
            return None, "invalid_path"
        return resolved, None

    def _safe_artifact_filename_locked(
        self,
        suggested: str | None,
        *,
        prefix: str,
        suffix: str | None = None,
    ) -> str:
        """把浏览器建议名称收敛为 artifact_dir 内的单一安全文件名。"""
        candidate = Path(suggested or "").name
        candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate)
        # 路径项最终按 UTF-8 写入文件系统；先替换异常代理字符，避免后续长度
        # 计算与实际写入不一致。
        candidate = candidate.encode("utf-8", errors="replace").decode("utf-8")
        # 保留开头的点，才能把 `.txt` 识别为“只有扩展名”的建议名称。
        candidate = candidate.strip(" ").rstrip(". ")
        last_dot = candidate.rfind(".")
        if last_dot > 0:
            stem, extension = candidate[:last_dot], candidate[last_dot:]
        elif last_dot == 0:
            stem, extension = "", candidate
        else:
            stem, extension = candidate, ""
        stem = stem.strip(" .") or prefix
        extension = extension.strip(" .")
        if extension:
            extension = f".{extension.lstrip('.')}"
        if suffix is not None:
            extension = f".{suffix.lstrip('.')}"
        # 过长扩展名本身也会挤占目录项长度，且保留其开头仍足以说明用途。
        extension = self._utf8_truncate(extension, 32)
        if stem.upper() in _WINDOWS_RESERVED_FILENAMES:
            stem = f"_{stem}"
        return self._fit_artifact_filename_locked(
            stem, extension, max_bytes=120, fallback=prefix
        )

    @staticmethod
    def _utf8_truncate(text: str, max_bytes: int) -> str:
        """按 UTF-8 字节预算截断文本，始终在完整字符边界结束。"""
        if max_bytes <= 0:
            return ""
        normalized = text.encode("utf-8", errors="replace").decode("utf-8")
        used_bytes = 0
        characters: list[str] = []
        for character in normalized:
            character_bytes = len(character.encode("utf-8"))
            if used_bytes + character_bytes > max_bytes:
                break
            characters.append(character)
            used_bytes += character_bytes
        return "".join(characters)

    @staticmethod
    def _fit_artifact_filename_locked(
        stem: str,
        extension: str,
        *,
        max_bytes: int,
        fallback: str,
    ) -> str:
        """在 UTF-8 字节预算内保留扩展名，并保证结果不是空文件名。"""
        if max_bytes < 2:
            raise ValueError("产物文件名预算不足")
        safe_extension = BrowserSession._utf8_truncate(
            extension, min(32, max_bytes - 1)
        )
        allowed_stem = max_bytes - len(safe_extension.encode("utf-8"))
        safe_stem = BrowserSession._utf8_truncate(
            stem.strip(" .") or fallback, allowed_stem
        ).strip(" .")
        if not safe_stem:
            safe_stem = BrowserSession._utf8_truncate(fallback, allowed_stem).strip(" .") or "f"
        if safe_stem.upper() in _WINDOWS_RESERVED_FILENAMES:
            safe_stem = BrowserSession._utf8_truncate(
                f"_{safe_stem}", allowed_stem
            ).strip(" .") or "f"
        return f"{safe_stem}{safe_extension}"

    def _new_artifact_paths_locked(self, kind: str, filename: str) -> tuple[Path, Path]:
        """原子预留带随机标识的正式文件名，并分配同目录临时文件。"""
        # 临时名为 .{kind}-{uuid}-{filename}.{uuid}.tmp，长度开销比正式名更大。
        # 文件系统限制的是编码后的目录项字节数，而不是 Python 字符数。
        filename_budget = _MAX_ARTIFACT_FILENAME_BYTES - len(kind.encode("utf-8")) - 72
        last_dot = filename.rfind(".")
        if last_dot > 0:
            filename_stem, filename_extension = filename[:last_dot], filename[last_dot:]
        else:
            filename_stem, filename_extension = filename, ""
        filename = self._fit_artifact_filename_locked(
            filename_stem,
            filename_extension,
            max_bytes=min(120, filename_budget),
            fallback="artifact",
        )
        while True:
            self._artifact_counter += 1
            unique_name = f"{kind}-{uuid4().hex}-{filename}"
            temporary_name = f".{unique_name}.{uuid4().hex}.tmp"
            if (
                len(unique_name.encode("utf-8")) > _MAX_ARTIFACT_FILENAME_BYTES
                or len(temporary_name.encode("utf-8")) > _MAX_ARTIFACT_FILENAME_BYTES
            ):
                raise ValueError("产物文件名超出文件系统长度限制")
            final_path = (self.artifact_dir / unique_name).resolve(strict=False)
            if not self._is_relative_to(final_path, self.artifact_dir):
                raise ValueError("产物路径越界")
            # UUID 降低碰撞概率；排他创建才是跨会话不覆盖的最终保证。
            try:
                descriptor = os.open(
                    final_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise OSError(f"无法预留产物路径: {exc}") from exc
            else:
                os.close(descriptor)
                break
        temporary_path = self.artifact_dir / temporary_name
        return final_path, temporary_path

    def _artifact_payload_locked(self, artifact: _Artifact) -> dict[str, Any]:
        return {
            "artifact_id": artifact.artifact_id,
            "kind": artifact.kind,
            "path": str(artifact.path),
            "filename": artifact.filename,
            "mime_type": artifact.mime_type,
            "size_bytes": artifact.size_bytes,
            "page_id": artifact.page_id,
            "source_url": artifact.source_url,
        }

    def _discard_artifact_locked(self, artifact: _Artifact) -> None:
        """尽力删除刚生成但不应发布的会话产物，且不保留登记。"""
        try:
            resolved = artifact.path.resolve(strict=True)
            if (
                not artifact.path.is_symlink()
                and self._is_relative_to(resolved, self.artifact_dir)
                and resolved.is_file()
            ):
                resolved.unlink()
        except Exception:
            # 截图或下载的原始失败语义不能被清理问题覆盖。
            pass
        finally:
            self._artifacts.pop(artifact.artifact_id, None)

    def _discard_new_artifact_on_failure_locked(
        self, artifact: _Artifact | None
    ) -> None:
        """只回滚本次新建的产物；清理异常不影响原始失败结果。"""
        if artifact is not None:
            self._discard_artifact_locked(artifact)

    def _publish_artifact_locked(
        self,
        kind: str,
        filename: str,
        writer: Callable[[Path], None],
        *,
        page_id: str | None,
        source_url: str,
    ) -> _Artifact:
        """先写临时文件、校验后原子改名，并登记会话产物。"""
        final_path, temporary_path = self._new_artifact_paths_locked(kind, filename)
        try:
            writer(temporary_path)
            if not temporary_path.exists() or temporary_path.is_symlink() or not temporary_path.is_file():
                raise OSError("产物写入未生成普通文件")
            os.replace(temporary_path, final_path)
            resolved = final_path.resolve(strict=True)
            if not self._is_relative_to(resolved, self.artifact_dir) or resolved.is_symlink():
                raise OSError("产物路径越界或不是普通文件")
            stat = resolved.stat()
        except Exception:
            for path in (temporary_path, final_path):
                try:
                    if path.exists() and not path.is_symlink() and self._is_relative_to(path.resolve(), self.artifact_dir):
                        path.unlink()
                except Exception:
                    pass
            raise
        artifact_id = f"a{self._artifact_counter}"
        artifact = _Artifact(
            artifact_id=artifact_id,
            kind=kind,
            path=resolved,
            filename=resolved.name,
            mime_type=mimetypes.guess_type(resolved.name)[0],
            size_bytes=stat.st_size,
            page_id=page_id,
            source_url=source_url,
        )
        self._artifacts[artifact_id] = artifact
        return artifact

    def start(self) -> None:
        """启动 Playwright + Chromium。重复调用是 no-op。"""
        with self._lock:
            self._bind_owner_thread()
            if self._playwright is not None:
                return
            # 局部 import:playwright 是 optional 依赖,未装时让 ImportError 自然抛出,
            # 由调用方(测试 / CLI)捕获并给出友好提示。
            from playwright.sync_api import sync_playwright

            try:
                self._playwright = sync_playwright().start()
                launch_kwargs: dict[str, Any] = {"headless": self._headless}
                if self._channel:
                    launch_kwargs["channel"] = self._channel
                # 系统 Chrome 不存在时回退到 Playwright 自带 Chromium。
                try:
                    self._browser = self._playwright.chromium.launch(**launch_kwargs)
                except Exception as exc:
                    if not self._channel:
                        raise
                    print(
                        f"[browser] 系统 {self._channel} 启动失败({exc.__class__.__name__});"
                        "回退到 Playwright 自带 Chromium。如未下载,执行 "
                        "`playwright install chromium`。",
                        file=sys.stderr,
                    )
                    self._browser = self._playwright.chromium.launch(
                        headless=self._headless,
                    )
                # 单一 context:cookie / localStorage 在 context 内跨 page 保持。
                self._context = self._browser.new_context()
                self._context.on("page", self._on_context_page)
                self._page = self._context.new_page()
                self._page.set_default_timeout(self._timeout_ms)
                self._register_page_locked(self._page, make_current=True)
            except Exception:
                # 启动半途失败时必须释放已创建资源，不能留下残缺 session。
                self._close_resources_locked()
                self._owner_thread_id = None
                raise

    def close(self) -> None:
        """释放全部资源。重复调用安全。"""
        with self._lock:
            if self._owner_thread_id is not None:
                self._assert_owner_thread()
            self._close_resources_locked()
            self._owner_thread_id = None

    def _close_resources_locked(self) -> None:
        """调用方已持锁时释放 Playwright 资源。"""
        # 按 page -> context -> browser -> playwright 顺序关。
        # 每一层都独立 try,避免某层失败阻塞后续清理。
        for resource in (self._page, self._context, self._browser):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._ref_to_backend_id.clear()
        self._active_snapshot_id = None
        self._pages.clear()
        self._page_ids_by_object.clear()
        self._current_page_id = None
        self._frame_ids_by_object.clear()
        self._active_dialog_policy = None
        self._next_dialog_policy = None
        self._last_dialog_event = None

    def _bind_owner_thread(self) -> None:
        """首次启动时绑定 Playwright 所属线程。"""
        if self._owner_thread_id is None:
            self._owner_thread_id = threading.get_ident()
            return
        self._assert_owner_thread()

    def _assert_owner_thread(self) -> None:
        """拒绝跨线程访问同步 Playwright 对象。"""
        if (
            self._owner_thread_id is not None
            and threading.get_ident() != self._owner_thread_id
        ):
            raise RuntimeError(
                "BrowserSession 只能在创建它的线程中使用。"
                "接入并发 Agent 时，应由固定 BrowserWorker 线程持有该 session。"
            )

    def _require_started_locked(self) -> None:
        """调用方已持锁时确认会话已启动且线程正确。"""
        self._assert_owner_thread()
        if self._playwright is None or self._context is None:
            raise RuntimeError("BrowserSession 未启动;请用 `with` 或先 start()")

    def _invalidate_snapshot_locked(self) -> None:
        """使当前页面观察结果和 ref 映射失效。"""
        self._ref_to_backend_id.clear()
        self._active_snapshot_id = None
        if self._current_page_id and self._current_page_id in self._pages:
            state = self._pages[self._current_page_id]
            state.ref_to_backend_id = {}
            state.active_snapshot_id = None
            state.snapshot_text = None

    def _register_page_locked(self, page: Any, *, make_current: bool = False) -> str:
        """登记新标签页并绑定对话框、关闭事件。"""
        known = self._page_ids_by_object.get(id(page))
        if known is not None:
            if make_current:
                self._select_page_locked(known, invalidate=True)
            return known
        self._page_counter += 1
        page_id = f"p{self._page_counter}"
        state = _PageState(page_id=page_id, page=page, ref_to_backend_id={})
        self._pages[page_id] = state
        self._page_ids_by_object[id(page)] = page_id
        page.on("dialog", lambda dialog, pid=page_id: self._on_dialog(pid, dialog))
        page.on("close", lambda pid=page_id: self._on_page_closed(pid))
        self._ensure_dom_version_locked(page)
        if make_current or self._current_page_id is None:
            self._select_page_locked(page_id, invalidate=False)
        return page_id

    def _on_context_page(self, page: Any) -> None:
        """Playwright 在 popup/new tab 创建时调用，登记但不抢占当前页。"""
        with self._lock:
            self._register_page_locked(page)

    def _on_page_closed(self, page_id: str) -> None:
        """页面被外部关闭时移除登记，并选择仍可用的页面。"""
        with self._lock:
            state = self._pages.pop(page_id, None)
            if state is None:
                return
            self._page_ids_by_object.pop(id(state.page), None)
            if self._current_page_id == page_id:
                replacement = next(iter(self._pages), None)
                if replacement is None:
                    self._current_page_id = None
                    self._page = None
                    self._ref_to_backend_id = {}
                    self._active_snapshot_id = None
                else:
                    self._select_page_locked(replacement, invalidate=True)

    def _on_dialog(self, page_id: str, dialog: Any) -> None:
        """在触发操作的同步调用内按预设策略处理对话框。"""
        with self._lock:
            policy = self._active_dialog_policy
            event = {
                "page_id": page_id,
                "type": dialog.type,
                "message": dialog.message,
                "default_value": dialog.default_value,
            }
            if policy is None:
                # 必须主动 dismiss 才能解除同步 Playwright 调用的阻塞；结果会明确报错。
                dialog.dismiss()
                event["result"] = "unhandled"
            else:
                strategy, prompt_text = policy
                if strategy == "dismiss":
                    dialog.dismiss()
                    event["result"] = "dismissed"
                else:
                    dialog.accept(prompt_text) if strategy == "prompt" else dialog.accept()
                    event["result"] = "accepted"
            self._last_dialog_event = event

    def _select_page_locked(self, page_id: str, *, invalidate: bool) -> None:
        state = self._pages.get(page_id)
        if state is None:
            raise KeyError(page_id)
        self._current_page_id = page_id
        self._page = state.page
        self._ref_to_backend_id = state.ref_to_backend_id
        self._active_snapshot_id = state.active_snapshot_id
        if invalidate:
            self._invalidate_snapshot_locked()

    def _current_page_state_locked(self) -> _PageState:
        """返回当前页独占的快照与浏览历史状态。"""
        if self._current_page_id is None:
            raise RuntimeError("当前会话没有可用页面")
        state = self._pages.get(self._current_page_id)
        if state is None:
            raise RuntimeError("当前页面状态不存在")
        return state

    def _require_current_page_locked(self) -> str | None:
        if self._current_page_id is None or self._page is None:
            return _err("no_pages", "当前会话没有可用页面")
        return None

    def _frame_tree_locked(self) -> list[dict[str, Any]]:
        """返回当前页面的 frame 树，为后续 frame 内交互保留稳定定位结构。"""
        frames: list[dict[str, Any]] = []
        for frame in self._page.frames:
            frame_key = id(frame)
            frame_id = self._frame_ids_by_object.get(frame_key)
            if frame_id is None:
                self._frame_counter += 1
                frame_id = f"f{self._frame_counter}"
                self._frame_ids_by_object[frame_key] = frame_id
            parent = frame.parent_frame
            parent_id = None
            if parent is not None:
                parent_id = self._frame_ids_by_object.get(id(parent))
                if parent_id is None:
                    self._frame_counter += 1
                    parent_id = f"f{self._frame_counter}"
                    self._frame_ids_by_object[id(parent)] = parent_id
            frames.append({
                "frame_id": frame_id,
                "url": frame.url,
                "name": frame.name,
                "parent_frame_id": parent_id,
                "is_main_frame": frame == self._page.main_frame,
            })
        return frames

    def _ensure_dom_version_locked(self, page: Any | None = None) -> None:
        """在页面侧安装轻量 MutationObserver，记录 DOM 版本而非猜测 HTML 长度。"""
        target = page or self._page
        try:
            target.evaluate("""() => {
                if (window.__browserDomVersionInstalled) return;
                window.__browserDomVersionInstalled = true;
                window.__browserDomVersion = 0;
                new MutationObserver((records) => {
                    // 工具临时定位属性不属于网页内容变化，不能影响事件判断。
                    if (records.some((record) =>
                        record.type !== 'attributes' ||
                        record.attributeName !== 'data-browser-native-ref')) {
                        window.__browserDomVersion += 1;
                    }
                })
                    .observe(document.documentElement, {subtree: true, childList: true,
                    attributes: true, characterData: true});
            }""")
        except Exception:
            pass

    def _ok_snapshot_locked(
        self,
        snapshot: str,
        snapshot_id: str,
        *,
        event_type: str = "snapshot",
        used_fallback: bool = False,
    ) -> str:
        return _observation_result(
            snapshot,
            snapshot_id,
            self._page.url,
            page_id=self._current_page_id,
            frames=self._frame_tree_locked(),
            event_type=event_type,
            used_fallback=used_fallback,
            dialogs=self._dialogs_payload_locked(),
        )

    def _err_observation_locked(
        self,
        error_type: str,
        error: str,
        snapshot: str,
        snapshot_id: str,
        *,
        event_type: str = "none",
        used_fallback: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """用与成功观察相同的页面字段返回可恢复错误。"""
        result = _observation_result(
            snapshot,
            snapshot_id,
            self._page.url,
            page_id=self._current_page_id,
            frames=self._frame_tree_locked(),
            event_type=event_type,
            used_fallback=used_fallback,
            dialogs=self._dialogs_payload_locked(),
            error_type=error_type,
            error=error,
        )
        if not extra:
            return result
        payload = json.loads(result)
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def _current_observation_locked(self) -> tuple[str, str] | None:
        """返回仍有效的当前观察，不重新生成快照也不改变 ref 生命周期。"""
        try:
            state = self._current_page_state_locked()
        except RuntimeError:
            return None
        if state.active_snapshot_id and state.snapshot_text:
            return state.snapshot_text, state.active_snapshot_id
        return None

    def _current_observation_error_locked(
        self,
        error_type: str,
        error: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """在未发出页面动作时，复用旧快照补全可恢复错误。"""
        observation = self._current_observation_locked()
        if observation is None:
            return _err(error_type, error)
        snapshot, snapshot_id = observation
        try:
            return self._err_observation_locked(
                error_type, error, snapshot, snapshot_id, extra=extra
            )
        except Exception:
            return _err(error_type, error)

    def _p7_ref_error_with_observation_locked(self, result: str) -> str:
        """P7 的 ref 解析失败时，保留仍有效的当前观察而不换发快照。"""
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        error_type = payload.get("error_type")
        error = payload.get("error")
        if not isinstance(error_type, str) or not isinstance(error, str):
            return result
        return self._current_observation_error_locked(error_type, error)

    @staticmethod
    def _add_result_fields(result: str, **fields: Any) -> str:
        """在既有 JSON 结果上附加操作专属字段，保留统一观察结构。"""
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        payload.update(fields)
        return json.dumps(payload, ensure_ascii=False)

    def _console_observation_locked(
        self,
        result: Any,
        snapshot: str,
        snapshot_id: str,
        *,
        truncated: bool,
        original_length: int,
        event_type: str,
    ) -> str:
        """在统一页面观察结构上附加 console 的序列化结果。"""
        payload = json.loads(
            self._ok_snapshot_locked(
                snapshot, snapshot_id, event_type=event_type
            )
        )
        payload.update(
            {
                "result": result,
                "truncated": truncated,
                "original_length": original_length,
            }
        )
        return json.dumps(payload, ensure_ascii=False)

    def _dialogs_payload_locked(self) -> list[dict[str, str]]:
        return [] if self._last_dialog_event is None else [self._last_dialog_event]

    # --- 读取操作 ---

    def navigate(self, url: str) -> str:
        """打开 URL 并返回带 ``snapshot_id`` 的观察结果 JSON。

        会等待 ``load`` 事件 + 一小段网络空闲,避免拿到 AJAX 半截页面。
        snapshot 内 ref 从 e1 起重新编号。
        """
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._navigate_locked(url)
            )

    def _navigate_locked(
        self,
        url: str,
        *,
        navigation_timeout_ms: int | None = None,
        settle_navigation: bool = True,
        post_navigation_wait_ms: int = 500,
        event_collection_timeout_ms: int = 250,
    ) -> str:
        """执行导航并返回统一观察结果；调用方已完成页面可用性检查。"""
        previous_url = self._page.url
        previous_position = self._position_marker_locked()
        previous_marker = self._dom_marker_locked()
        before_pages = set(self._pages)
        self._invalidate_snapshot_locked()
        try:
            goto_kwargs: dict[str, Any] = {"wait_until": "load"}
            if navigation_timeout_ms is not None:
                goto_kwargs["timeout"] = navigation_timeout_ms
            self._page.goto(_normalize_url(url), **goto_kwargs)
            # 给 AJAX 一点喘息时间;不用 networkidle --对长轮询站点会一直等。
            if post_navigation_wait_ms > 0:
                self._page.wait_for_timeout(post_navigation_wait_ms)
            return self._finalize_interaction_locked(
                previous_url,
                previous_position,
                "navigate",
                False,
                before_pages,
                previous_marker,
                settle_navigation=settle_navigation,
                event_collection_timeout_ms=event_collection_timeout_ms,
            )
        except Exception as exc:
            return self._interaction_failure_with_observation_locked(
                self._classify_interaction_error(exc),
                f"导航失败: {exc}",
                previous_url,
                previous_marker,
                before_pages,
                settle_navigation=settle_navigation,
                event_collection_timeout_ms=event_collection_timeout_ms,
            )

    def snapshot(self) -> str:
        """对当前 page 取一次带 ``snapshot_id`` 的观察结果 JSON。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            snapshot, snapshot_id = self._snapshot_locked()
            return self._ok_snapshot_locked(snapshot, snapshot_id)

    def _snapshot_locked(self) -> tuple[str, str]:
        """调用方已持锁时取快照。

        走 CDP ``Accessibility.getFullAXTree``。CDP session 是 per-page
        的,每次创建开销很小,用完即关。同时填充 ``_ref_to_backend_id``
        映射,供后续交互操作定位 DOM。
        """
        client = self._context.new_cdp_session(self._page)
        try:
            cdp_result = client.send("Accessibility.getFullAXTree")
        finally:
            client.detach()
        # 重建 ref 映射:format_snapshot 内部按 INTERACTIVE_ROLES 顺序
        # 分配 e1、e2...,这里要复刻同一顺序才能对齐。直接从 cdp_result
        # 里按交互角色出现顺序重新编号,与 format_snapshot 保持一致。
        self._ref_to_backend_id = _build_ref_map(cdp_result)
        if self._current_page_id is not None:
            self._pages[self._current_page_id].ref_to_backend_id = self._ref_to_backend_id
        self._snapshot_counter += 1
        self._active_snapshot_id = f"{self._current_page_id}:s{self._snapshot_counter}"
        snapshot = format_snapshot(cdp_result)
        if self._current_page_id is not None:
            state = self._pages[self._current_page_id]
            state.active_snapshot_id = self._active_snapshot_id
            state.snapshot_text = snapshot
        return snapshot, self._active_snapshot_id

    def cookies(self) -> list[dict]:
        """返回当前 context 的 cookie 列表;测试用。"""
        with self._lock:
            self._require_started_locked()
            return self._context.cookies()

    # --- 会话产物管理(P7) ---

    def list_artifacts(self) -> str:
        """列出当前会话已登记的下载和截图产物，不扫描目录中的其他文件。"""
        with self._lock:
            artifacts = [
                self._artifact_payload_locked(artifact)
                for artifact in self._artifacts.values()
            ]
            return json.dumps({"ok": True, "artifacts": artifacts}, ensure_ascii=False)

    def get_artifact(self, artifact_id: str) -> str:
        """读取已登记产物的元数据，不返回文件内容。"""
        if not isinstance(artifact_id, str) or not artifact_id:
            return _err("artifact_not_found", "artifact_id 必须是非空字符串")
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None:
                return _err("artifact_not_found", f"未找到产物 {artifact_id}")
            return json.dumps(
                {"ok": True, "artifact": self._artifact_payload_locked(artifact)},
                ensure_ascii=False,
            )

    def delete_artifact(self, artifact_id: str) -> str:
        """仅删除当前会话登记且仍位于 artifact_dir 内的单个文件。"""
        if not isinstance(artifact_id, str) or not artifact_id:
            return _err("artifact_not_found", "artifact_id 必须是非空字符串")
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None:
                return _err("artifact_not_found", f"未找到产物 {artifact_id}")
            try:
                resolved = artifact.path.resolve(strict=True)
                if (
                    artifact.path.is_symlink()
                    or not self._is_relative_to(resolved, self.artifact_dir)
                    or not resolved.is_file()
                ):
                    return _err("artifact_not_found", f"产物 {artifact_id} 不再是可删除的会话文件")
                resolved.unlink()
            except FileNotFoundError:
                return _err("artifact_not_found", f"产物 {artifact_id} 已不存在")
            except OSError as exc:
                return _err("artifact_write_failed", f"删除产物 {artifact_id} 失败: {exc}")
            self._artifacts.pop(artifact_id, None)
            return json.dumps({"ok": True, "artifact_id": artifact_id}, ensure_ascii=False)

    def cleanup_artifacts(self) -> str:
        """清理当前会话登记的产物；失败项不会阻止其余文件和浏览器资源处理。"""
        with self._lock:
            deleted: list[str] = []
            failures: list[dict[str, str]] = []
            for artifact_id in list(self._artifacts):
                artifact = self._artifacts.get(artifact_id)
                if artifact is None:
                    continue
                try:
                    resolved = artifact.path.resolve(strict=True)
                    if (
                        artifact.path.is_symlink()
                        or not self._is_relative_to(resolved, self.artifact_dir)
                        or not resolved.is_file()
                    ):
                        raise OSError("产物不再是 artifact_dir 内的普通文件")
                    resolved.unlink()
                except FileNotFoundError:
                    # 外部已删除时不再保留不可恢复的登记项。
                    self._artifacts.pop(artifact_id, None)
                    deleted.append(artifact_id)
                except OSError as exc:
                    failures.append({"artifact_id": artifact_id, "error": str(exc)})
                else:
                    self._artifacts.pop(artifact_id, None)
                    deleted.append(artifact_id)
            return json.dumps(
                {"ok": not failures, "deleted_artifact_ids": deleted, "failures": failures},
                ensure_ascii=False,
            )

    # --- 自主浏览与结构化提取(P9) ---

    def _page_extractor_locked(self) -> Any:
        """延迟创建独立提取器，避免把页面解析细节堆入会话类。"""
        if self._page_extractor is None:
            from browser.extractor import PageExtractor

            self._page_extractor = PageExtractor(self)
        return self._page_extractor

    def _p9_refs_for_selector_locked(self, selector: str) -> dict[int, str]:
        """为只读提取结果补充现有快照中可确定的 ref，不写入临时属性。"""
        if not isinstance(selector, str) or not selector:
            return {}
        refs: dict[int, str] = {}
        client = self._context.new_cdp_session(self._page)
        try:
            for ref, backend_id in self._ref_to_backend_id.items():
                try:
                    resolved = client.send(
                        "DOM.resolveNode", {"backendNodeId": backend_id}
                    )
                    object_id = resolved.get("object", {}).get("objectId")
                    if not object_id:
                        continue
                    result = client.send(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": object_id,
                            "functionDeclaration": """function(selector) {
                                return Array.prototype.indexOf.call(
                                    document.querySelectorAll(selector), this
                                );
                            }""",
                            "arguments": [{"value": selector}],
                            "returnByValue": True,
                        },
                    )
                    index = result.get("result", {}).get("value")
                    if isinstance(index, int) and index >= 0 and index not in refs:
                        refs[index] = ref
                except Exception as exc:
                    if self._is_permanent_browser_error(exc):
                        raise
                    # 某个 ref 在读取期间被框架重绘时，只是不再能为该项补 ref。
                    if self._is_transient_wait_error(exc):
                        continue
                    raise
        finally:
            try:
                client.detach()
            except Exception:
                pass
        return refs

    def _p9_navigate_locked(self, url: str, timeout_ms: int) -> str:
        """分页专用 URL 跟随，复用导航、对话框和快照收尾规则。"""
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms <= 0
        ):
            return _err("invalid_args", "分页导航 timeout_ms 必须是正整数")
        return self._execute_with_dialog_policy_locked(
            lambda: self._navigate_locked(
                url,
                navigation_timeout_ms=timeout_ms,
                settle_navigation=False,
                post_navigation_wait_ms=min(200, timeout_ms),
                event_collection_timeout_ms=min(250, timeout_ms),
            )
        )

    def _p9_click_next_button_locked(self, index: int, timeout_ms: int) -> str:
        """只点击提取器识别出的明确下一页按钮，不接受任意页面选择器。"""
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return _err("invalid_args", "下一页按钮索引无效")
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms <= 0
        ):
            return _err("invalid_args", "分页点击 timeout_ms 必须是正整数")

        def click_next() -> str:
            before_pages = set(self._pages)
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            previous_marker = self._dom_marker_locked()
            self._invalidate_snapshot_locked()
            try:
                self._page.locator('button, [role="button"]').nth(index).click(
                    timeout=timeout_ms
                )
            except Exception as exc:
                return self._finalize_interaction_locked(
                    previous_url,
                    None,
                    "collect_paginated_next",
                    False,
                    before_pages,
                    previous_marker,
                    error_type=self._classify_interaction_error(exc),
                    error=f"collect_paginated_next 失败: {exc}",
                    record_navigation=False,
                    settle_navigation=False,
                    event_collection_timeout_ms=min(250, timeout_ms),
                )
            return self._finalize_interaction_locked(
                previous_url,
                previous_position,
                "collect_paginated_next",
                False,
                before_pages,
                previous_marker,
                settle_navigation=False,
                event_collection_timeout_ms=min(250, timeout_ms),
            )

        return self._execute_with_dialog_policy_locked(click_next)

    def _p9_read_result_locked(
        self,
        snapshot_id: str,
        operation: Callable[[Any], dict[str, Any]],
    ) -> str:
        """为纯读取 P9 接口共享页面可用性和快照校验，不换发快照。"""
        self._require_started_locked()
        no_page_error = self._require_current_page_locked()
        if no_page_error is not None:
            return no_page_error
        stale_error = self._validate_snapshot_locked(snapshot_id)
        if stale_error is not None:
            return stale_error
        try:
            payload = operation(self._page_extractor_locked())
        except Exception as exc:
            from browser.extractor import ExtractionError

            if isinstance(exc, ExtractionError):
                return _err(exc.error_type, exc.message)
            return _err("extract_failed", f"页面结构化提取失败: {exc}")
        if not isinstance(payload, dict):
            return _err("extract_failed", "页面结构化提取返回了无效结果")
        payload.update(
            {
                "ok": True,
                "snapshot_id": snapshot_id,
                "url": self._page.url,
                "page_id": self._current_page_id,
            }
        )
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return _err("extract_failed", f"页面结构化结果无法序列化: {exc}")

    def find_in_page(self, query: str, snapshot_id: str, max_results: int = 20) -> str:
        """只读查找当前页面文本，不滚动、不调用浏览器查找窗口。"""
        with self._lock:
            return self._p9_read_result_locked(
                snapshot_id,
                lambda extractor: extractor.find_in_page(query, max_results),
            )

    def extract_links(self, snapshot_id: str, max_items: int = 100) -> str:
        """只读提取当前页面链接及可用 ref，不改变快照生命周期。"""
        with self._lock:
            return self._p9_read_result_locked(
                snapshot_id, lambda extractor: extractor.extract_links(max_items)
            )

    def extract_tables(self, snapshot_id: str, max_items: int = 100) -> str:
        """只读提取页面表格，不滚动或修改 DOM。"""
        with self._lock:
            return self._p9_read_result_locked(
                snapshot_id, lambda extractor: extractor.extract_tables(max_items)
            )

    def extract_forms(self, snapshot_id: str, max_items: int = 100) -> str:
        """只读提取表单结构，绝不提交、填充或聚焦控件。"""
        with self._lock:
            return self._p9_read_result_locked(
                snapshot_id, lambda extractor: extractor.extract_forms(max_items)
            )

    def extract_metadata(self, snapshot_id: str) -> str:
        """只读提取 title、meta、Open Graph 与合法 JSON-LD。"""
        with self._lock:
            return self._p9_read_result_locked(
                snapshot_id, lambda extractor: extractor.extract_metadata()
            )

    def collect_paginated(
        self,
        snapshot_id: str,
        *,
        extract_kind: str,
        max_pages: int = 5,
        max_items: int = 200,
        max_text_chars: int = 100_000,
        same_origin: bool = True,
        timeout_ms: int | None = None,
    ) -> str:
        """在固定预算内提取并跟随明确下一页控件；自动翻页会换发快照。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            try:
                from browser.extractor import BrowseBudget, ExtractionError

                budget = BrowseBudget.create(
                    max_pages=max_pages,
                    max_items=max_items,
                    max_text_chars=max_text_chars,
                    timeout_ms=timeout_ms,
                )
                payload = self._page_extractor_locked().collect_paginated(
                    snapshot_id,
                    extract_kind=extract_kind,
                    budget=budget,
                    same_origin=same_origin,
                )
            except Exception as exc:
                if isinstance(exc, ExtractionError):
                    return self._current_observation_error_locked(
                        exc.error_type, exc.message
                    )
                return self._current_observation_error_locked(
                    "extract_failed", f"分页收集失败: {exc}"
                )
            if not isinstance(payload, dict):
                return self._current_observation_error_locked(
                    "extract_failed", "分页收集返回了无效结果"
                )
            observation = self._current_observation_locked()
            if (
                observation is not None
                and payload.get("snapshot_id") == observation[1]
            ):
                payload["snapshot"] = observation[0]
            try:
                frames = self._frame_tree_locked()
            except Exception as exc:
                return self._current_observation_error_locked(
                    "page_closed"
                    if self._is_permanent_browser_error(exc)
                    else "extract_failed",
                    f"分页收集后读取页面结构失败: {exc}",
                )
            dialogs = payload.get("dialogs")
            payload.update(
                {
                    "ok": True,
                    "page_id": self._current_page_id,
                    "url": self._page.url,
                    "frames": frames,
                    "dialogs": dialogs if isinstance(dialogs, list) else [],
                    "event_type": payload.get("event_type")
                    if isinstance(payload.get("event_type"), str)
                    else "none",
                    "used_fallback": payload.get("used_fallback") is True,
                }
            )
            try:
                return json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                return self._current_observation_error_locked(
                    "extract_failed", f"分页收集结果无法序列化: {exc}"
                )

    # --- 图片与音频理解(P8) ---

    def _analyzer_locked(self) -> MultimodalAnalyzer:
        """延迟建立模型服务，未配置时只在实际分析时报告错误。"""
        if self._multimodal_analyzer is None:
            self._multimodal_analyzer = MultimodalAnalyzer()
        return self._multimodal_analyzer

    def _artifact_media_source_locked(self, artifact: _Artifact) -> MediaSource | str:
        """再次确认登记产物仍是专用目录内未链接的普通文件。"""
        try:
            if artifact.path.is_symlink():
                return _err("invalid_media_path", "产物不能是符号链接")
            resolved = artifact.path.resolve(strict=True)
        except FileNotFoundError:
            return _err("media_not_found", f"产物 {artifact.artifact_id} 已不存在")
        except (OSError, RuntimeError):
            return _err("invalid_media_path", f"产物 {artifact.artifact_id} 无法安全解析")
        if (
            not self._is_relative_to(resolved, self.artifact_dir)
            or self._has_symlink_component(artifact.path, self.artifact_dir)
            or not resolved.is_file()
        ):
            return _err("invalid_media_path", f"产物 {artifact.artifact_id} 不再是安全媒体文件")
        return MediaSource(
            path=resolved,
            source_type="artifact",
            artifact_id=artifact.artifact_id,
            filename=artifact.filename,
        )

    def _media_source_locked(self, source: str | Path) -> MediaSource | str:
        """把 artifact_id 或工作区相对路径收敛为经过授权的媒体文件。"""
        if not isinstance(source, (str, Path)):
            return _err("invalid_media_path", "媒体来源必须是 artifact_id 或工作区相对路径")
        if isinstance(source, str) and source in self._artifacts:
            return self._artifact_media_source_locked(self._artifacts[source])
        raw_path = Path(source)
        if not str(raw_path) or raw_path.is_absolute() or self._contains_parent_reference(raw_path):
            return _err("invalid_media_path", "媒体路径必须是 workspace_root 内不含 '..' 的相对路径")
        resolved, error_type = self._resolve_upload_path_locked(raw_path)
        if resolved is not None:
            return MediaSource(
                path=resolved,
                source_type="workspace",
                artifact_id=None,
                filename=resolved.name,
            )
        if error_type == "file_not_found":
            return _err("media_not_found", "媒体文件不存在")
        return _err("invalid_media_path", "媒体路径必须是工作区内未链接的普通文件")

    @staticmethod
    def _media_sources_argument(sources: Any) -> list[str | Path] | None:
        """接受一个媒体来源或列表，避免把任意可迭代对象误当成路径集合。"""
        if isinstance(sources, (str, Path)):
            return [sources]
        if isinstance(sources, (list, tuple)) and sources:
            if all(isinstance(source, (str, Path)) for source in sources):
                return list(sources)
        return None

    def _analysis_payload_locked(self, analysis: Any) -> str:
        """将服务层结果变成不含路径、Base64 或原始响应的公开 JSON。"""
        artifact_payloads: list[dict[str, Any]] = []
        for media in analysis.media:
            artifact_id = media.source.artifact_id
            if artifact_id is not None and artifact_id in self._artifacts:
                artifact_payloads.append(
                    self._artifact_payload_locked(self._artifacts[artifact_id])
                )
        payload = {
            "ok": True,
            "analysis": analysis.analysis,
            "model": analysis.model,
            "provider": analysis.provider,
            "media": [item.public_payload() for item in analysis.media],
            "request_id": analysis.request_id,
            "usage": analysis.usage,
            "artifact": artifact_payloads[0] if len(artifact_payloads) == 1 else None,
        }
        if len(artifact_payloads) > 1:
            payload["artifacts"] = artifact_payloads
        return json.dumps(payload, ensure_ascii=False)

    def _analyze_media_locked(
        self,
        sources: Any,
        prompt: str,
        *,
        timeout_ms: int | None,
        expected_type: str | None = None,
    ) -> str:
        """在会话安全边界内解析来源，再委托独立服务层请求模型。"""
        raw_sources = self._media_sources_argument(sources)
        if raw_sources is None:
            return _err("invalid_media_path", "sources 必须是单个来源或非空来源列表")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("invalid_args", "prompt 必须是非空字符串")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0
        ):
            return _err("invalid_args", "timeout_ms 必须是正整数")
        resolved_sources: list[MediaSource] = []
        for source in raw_sources:
            resolved = self._media_source_locked(source)
            if isinstance(resolved, str):
                return resolved
            resolved_sources.append(resolved)
        try:
            analysis = self._analyzer_locked().analyze(
                resolved_sources,
                prompt,
                timeout_ms=timeout_ms,
                expected_type=expected_type,
            )
        except MultimodalError as exc:
            return _err(exc.error_type, exc.message)
        except Exception as exc:
            # 供应商实现异常不能泄露请求体、授权头或媒体内容。
            return _err("model_request_failed", f"多模态模型请求出现未预期错误: {exc.__class__.__name__}")
        return self._analysis_payload_locked(analysis)

    def analyze_media(
        self,
        sources: str | Path | list[str | Path] | tuple[str | Path, ...],
        prompt: str,
        *,
        timeout_ms: int | None = None,
    ) -> str:
        """分析当前会话产物或工作区相对路径指定的一张或多张图片、音频。"""
        with self._lock:
            self._assert_owner_thread()
            return self._analyze_media_locked(sources, prompt, timeout_ms=timeout_ms)

    def analyze_image(
        self,
        source: str | Path,
        prompt: str,
        timeout_ms: int | None = None,
    ) -> str:
        """只接受图片来源的 ``analyze_media`` 简化入口。"""
        with self._lock:
            self._assert_owner_thread()
            return self._analyze_media_locked(
                source, prompt, timeout_ms=timeout_ms, expected_type="image"
            )

    def analyze_audio(
        self,
        source: str | Path,
        prompt: str,
        timeout_ms: int | None = None,
    ) -> str:
        """只接受音频来源的 ``analyze_media`` 简化入口。"""
        with self._lock:
            self._assert_owner_thread()
            return self._analyze_media_locked(
                source, prompt, timeout_ms=timeout_ms, expected_type="audio"
            )

    def analyze_page(
        self,
        snapshot_id: str,
        prompt: str,
        *,
        full_page: bool = False,
        timeout_ms: int | None = None,
    ) -> str:
        """截图当前页面并分析该截图；页面快照本身不会因分析而失效。"""
        if not isinstance(full_page, bool):
            return _err("invalid_args", "full_page 必须是布尔值")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("invalid_args", "prompt 必须是非空字符串")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0
        ):
            return _err("invalid_args", "timeout_ms 必须是正整数")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            screenshot_result = self.screenshot(snapshot_id, full_page=full_page)
            try:
                screenshot_payload = json.loads(screenshot_result)
            except (TypeError, json.JSONDecodeError):
                return _err("screenshot_failed", "页面截图返回了无效结果")
            if not screenshot_payload.get("ok"):
                return screenshot_result
            artifact_payload = screenshot_payload.get("artifact")
            artifact_id = (
                artifact_payload.get("artifact_id")
                if isinstance(artifact_payload, dict)
                else None
            )
            if not isinstance(artifact_id, str) or not artifact_id:
                return _err("screenshot_failed", "页面截图未登记为会话产物")
            result = self._analyze_media_locked(
                artifact_id,
                prompt,
                timeout_ms=timeout_ms,
                expected_type="image",
            )
            page_fields = {
                key: screenshot_payload[key]
                for key in (
                    "snapshot_id", "snapshot", "page_id", "url", "frames",
                    "dialogs", "event_type", "used_fallback",
                )
                if key in screenshot_payload
            }
            return self._add_result_fields(
                result,
                artifact=artifact_payload,
                **page_fields,
            )

    # --- 页面与对话框管理(P5) ---
    def list_pages(self) -> str:
        """返回当前会话所有仍登记的标签页。"""
        with self._lock:
            self._require_started_locked()
            pages = []
            for page_id, state in self._pages.items():
                try:
                    closed = state.page.is_closed()
                    url = state.page.url if not closed else ""
                except Exception:
                    closed, url = True, ""
                pages.append({"page_id": page_id, "url": url, "is_current": page_id == self._current_page_id, "closed": closed})
            return json.dumps({"ok": True, "current_page_id": self._current_page_id, "pages": pages}, ensure_ascii=False)

    def switch_page(self, page_id: str) -> str:
        """切换当前页并立即返回该页新快照。"""
        if not isinstance(page_id, str) or not page_id:
            return _err("invalid_args", "page_id 必须是非空字符串")
        with self._lock:
            self._require_started_locked()
            state = self._pages.get(page_id)
            if state is None:
                return _err("invalid_page", f"page_id {page_id} 不存在")
            try:
                if state.page.is_closed():
                    return _err("page_closed", f"page_id {page_id} 已关闭")
            except Exception:
                return _err("page_closed", f"page_id {page_id} 不可用")
            self._select_page_locked(page_id, invalidate=True)
            try:
                snapshot, snapshot_id = self._snapshot_locked()
            except Exception as exc:
                return _err("page_closed", f"切换页面后取快照失败: {exc}")
            return self._ok_snapshot_locked(snapshot, snapshot_id, event_type="page_switch")

    def close_page(self, page_id: str) -> str:
        """关闭页面，并把 beforeunload 等对话框纳入本次策略。"""
        if not isinstance(page_id, str) or not page_id:
            return _err("invalid_args", "page_id 必须是非空字符串")
        with self._lock:
            self._require_started_locked()
            if page_id not in self._pages:
                return _err("invalid_page", f"page_id {page_id} 不存在")
            return self._execute_with_dialog_policy_locked(
                lambda: self._close_page_locked(page_id)
            )

    def _close_page_locked(self, page_id: str) -> str:
        """关闭指定页；当前页关闭后自动切到一个仍可用页面。"""
        if not isinstance(page_id, str) or not page_id:
            return _err("invalid_args", "page_id 必须是非空字符串")
        with self._lock:
            self._require_started_locked()
            state = self._pages.get(page_id)
            if state is None:
                return _err("invalid_page", f"page_id {page_id} 不存在")
            try:
                state.page.close()
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    _err("page_closed", f"关闭页面失败: {exc}")
                )
            # close 事件通常同步触发；若运行时延后回调，这里兜底清理。
            if page_id in self._pages:
                self._on_page_closed(page_id)
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return self._finish_dialog_operation_locked(no_page_error)
            try:
                snapshot, snapshot_id = self._snapshot_locked()
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    _err("page_closed", f"切换剩余页面后取快照失败: {exc}")
                )
            return self._finish_dialog_operation_locked(
                self._ok_snapshot_locked(snapshot, snapshot_id, event_type="page_closed")
            )

    def list_dialogs(self) -> str:
        """兼容接口：同步架构没有跨调用待处理的对话框。"""
        with self._lock:
            self._require_started_locked()
            return json.dumps(
                {
                    "ok": True,
                    "dialogs": [],
                    "message": "对话框详情只存在于触发操作的返回结果中",
                },
                ensure_ascii=False,
            )

    def set_dialog_strategy(self, strategy: str, prompt_text: str | None = None) -> str:
        """为下一次会触发页面事件的操作声明对话框处理方式。"""
        if strategy not in {"accept", "dismiss", "prompt"}:
            return _err("invalid_args", "strategy 必须是 accept、dismiss 或 prompt")
        if strategy == "prompt" and not isinstance(prompt_text, str):
            return _err("invalid_args", "prompt 策略需要字符串 prompt_text")
        if strategy != "prompt" and prompt_text is not None:
            return _err("invalid_args", "只有 prompt 策略可以传 prompt_text")
        with self._lock:
            self._next_dialog_policy = (strategy, prompt_text)
            return json.dumps({"ok": True, "strategy": strategy}, ensure_ascii=False)

    def accept_dialog(self, dialog_id: str, prompt_text: str | None = None) -> str:
        """兼容接口：同步架构下不存在跨调用待处理的对话框。"""
        return self._resolve_dialog(dialog_id, accept=True, prompt_text=prompt_text)

    def dismiss_dialog(self, dialog_id: str) -> str:
        """兼容接口：同步架构下不存在跨调用待处理的对话框。"""
        return self._resolve_dialog(dialog_id, accept=False, prompt_text=None)

    def _resolve_dialog(self, dialog_id: str, *, accept: bool, prompt_text: str | None) -> str:
        return _err("no_pending_dialog", "对话框只能在触发操作时按 dialog_strategy 立即处理")

    def _validate_snapshot_locked(self, snapshot_id: str) -> str | None:
        """确认 ref 来自当前观察结果，失败时返回结构化错误。"""
        if not snapshot_id:
            return _err("missing_snapshot_id", "交互操作需要 snapshot_id")
        if snapshot_id != self._active_snapshot_id:
            return _err(
                "stale_snapshot",
                "snapshot_id 已失效。页面可能已导航或执行过操作，请先调用 snapshot 获取新 ref。",
            )
        return None

    def _observe_after_action_locked(
        self,
        previous_url: str,
        *,
        previous_position: str | None = None,
        record_navigation: bool = True,
    ) -> tuple[str, str]:
        """等待动作引起的短暂更新或导航，再取新的观察结果。"""
        # 先让事件处理器有机会改变 URL；只有 URL 确实变化时才等待页面加载，
        # 避免普通 AJAX 点击无谓等待完整 load 事件。
        self._page.wait_for_timeout(100)
        if self._page.url != previous_url:
            # 导航场景:优先等 load(子资源加载完),覆盖重 AJAX 页面 --
            # 只等 domcontentloaded 会拿到半截快照(JS 延迟渲染的内容还没注入)。
            # load 可能对长轮询页面卡住,用较短超时;失败再退回 domcontentloaded。
            try:
                self._page.wait_for_load_state("load", timeout=8000)
            except Exception:
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
            # load 事件不等 AJAX 注入的内容(它们在 load 之后异步发生)。
            # 额外等待一段,对齐 navigate 的策略,让短延迟 AJAX 有时间完成。
            self._page.wait_for_timeout(500)
        else:
            # 长轮询页面不使用 networkidle；短暂等待覆盖常见同步 DOM 更新。
            self._page.wait_for_timeout(200)
        snapshot, snapshot_id = self._snapshot_locked()
        if record_navigation:
            self._record_navigation_locked(previous_url, previous_position)
        return snapshot, snapshot_id

    def _position_marker_locked(self) -> str:
        """返回能区分同 URL 页面内历史项的位置标记。"""
        return self._page.evaluate(
            """() => {
                let state;
                try {
                    state = JSON.stringify(history.state);
                } catch (_) {
                    state = '[unserializable history state]';
                }
                return location.href + '\\n' + (state === undefined ? 'undefined' : state);
            }"""
        )

    def _record_navigation_locked(
        self,
        previous_url: str,
        previous_position: str | None,
    ) -> None:
        """记录公开 API 造成的新页面位置。

        浏览器自身的历史还包含启动时的 about:blank。这里单独保存工具已
        观察过的位置，使 back 在无可用历史时无需先跳到空白页再恢复。
        """
        current_url = self._page.url
        current_position = self._position_marker_locked()
        state = self._current_page_state_locked()
        if not state.history_urls:
            state.history_urls.append(current_url)
            state.history_markers.append(current_position)
            state.history_index = 0
            return
        if current_url == previous_url and current_position == previous_position:
            return
        # 在回退后打开新页面会形成新分支，旧的前进方向不再可用。
        del state.history_urls[state.history_index + 1 :]
        del state.history_markers[state.history_index + 1 :]
        state.history_urls.append(current_url)
        state.history_markers.append(current_position)
        state.history_index = len(state.history_urls) - 1

    # --- 导航操作(P2) ---
    # back / forward / reload / scroll 虽然不针对特定元素，但仍会改变当前
    # 页面。它们也必须携带 snapshot_id，避免晚到请求作用于新页面。

    def back(self, snapshot_id: str) -> str:
        """回退一页，并统一消费本次对话框策略。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._back_locked(snapshot_id)
            )

    def _back_locked(self, snapshot_id: str) -> str:
        """回退到上一页。没有工具可用的历史时保持当前页面不变。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            state = self._current_page_state_locked()
            if state.history_index <= 0:
                snapshot, snapshot_id = self._snapshot_locked()
                return self._err_observation_locked(
                    "no_history",
                    "没有浏览历史可回退/前进",
                    snapshot,
                    snapshot_id,
                    event_type="none",
                )
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            previous_marker = self._dom_marker_locked()
            before_pages = set(self._pages)
            self._invalidate_snapshot_locked()
            try:
                self._page.go_back(wait_until="domcontentloaded")
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    self._interaction_failure_with_observation_locked(
                        "back_failed", f"回退失败: {exc}", previous_url,
                        previous_marker, before_pages,
                    )
                )
            if self._position_marker_locked() == previous_position:
                result = self._finalize_interaction_locked(
                    previous_url, previous_position, "back", False, before_pages,
                    previous_marker, record_navigation=False,
                )
                return self._finish_dialog_operation_locked(_as_error_with_observation(
                    result,
                    "no_history",
                    "没有浏览历史可回退/前进",
                ))
            state.history_index -= 1
            state.history_urls[state.history_index] = self._page.url
            state.history_markers[state.history_index] = self._position_marker_locked()
            return self._finish_dialog_operation_locked(
                self._finalize_interaction_locked(
                    previous_url, previous_position, "back", False, before_pages,
                    previous_marker, record_navigation=False,
                )
            )

    def forward(self, snapshot_id: str) -> str:
        """前进一页，并统一消费本次对话框策略。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._forward_locked(snapshot_id)
            )

    def _forward_locked(self, snapshot_id: str) -> str:
        """前进到下一页。没有历史时返回 ``no_history`` 错误,页面状态不变。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            state = self._current_page_state_locked()
            if state.history_index >= len(state.history_urls) - 1:
                snapshot, snapshot_id = self._snapshot_locked()
                return self._err_observation_locked(
                    "no_history",
                    "没有浏览历史可回退/前进",
                    snapshot,
                    snapshot_id,
                    event_type="none",
                )
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            previous_marker = self._dom_marker_locked()
            before_pages = set(self._pages)
            self._invalidate_snapshot_locked()
            try:
                self._page.go_forward(wait_until="domcontentloaded")
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    self._interaction_failure_with_observation_locked(
                        "forward_failed", f"前进失败: {exc}", previous_url,
                        previous_marker, before_pages,
                    )
                )
            if self._position_marker_locked() == previous_position:
                result = self._finalize_interaction_locked(
                    previous_url, previous_position, "forward", False, before_pages,
                    previous_marker, record_navigation=False,
                )
                return self._finish_dialog_operation_locked(_as_error_with_observation(
                    result,
                    "no_history",
                    "没有浏览历史可回退/前进",
                ))
            state.history_index += 1
            state.history_urls[state.history_index] = self._page.url
            state.history_markers[state.history_index] = self._position_marker_locked()
            return self._finish_dialog_operation_locked(
                self._finalize_interaction_locked(
                    previous_url, previous_position, "forward", False, before_pages,
                    previous_marker, record_navigation=False,
                )
            )

    def reload(self, snapshot_id: str) -> str:
        """重新加载当前页，并统一消费本次对话框策略。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._reload_locked(snapshot_id)
            )

    def _reload_locked(self, snapshot_id: str) -> str:
        """重新加载当前页。返回新快照。"""
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            previous_marker = self._dom_marker_locked()
            before_pages = set(self._pages)
            self._invalidate_snapshot_locked()
            try:
                self._page.reload(wait_until="load")
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    self._interaction_failure_with_observation_locked(
                        "reload_failed", f"刷新失败: {exc}", previous_url,
                        previous_marker, before_pages,
                    )
                )
            return self._finish_dialog_operation_locked(
                self._finalize_interaction_locked(
                    previous_url, previous_position, "reload", False, before_pages,
                    previous_marker, record_navigation=False,
                )
            )

    def scroll(
        self,
        direction: str,
        snapshot_id: str,
        amount: int | float = 400,
    ) -> str:
        """滚动页面。``direction`` 为 ``up`` / ``down`` / ``left`` / ``right``。

        ``amount`` 是滚动像素数,默认 400(约半屏)。滚动不触发导航,
        但页面 DOM 可能因懒加载变化,仍需取新快照。
        """
        if not isinstance(direction, str):
            return _err("invalid_args", "direction 必须是字符串")
        direction = direction.lower().strip()
        if direction not in ("up", "down", "left", "right"):
            return _err(
                "invalid_args",
                f"direction 必须是 up/down/left/right,收到: {direction!r}",
            )
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount <= 0
        ):
            return _err("invalid_args", f"amount 必须是有限的正数,收到: {amount!r}")
        dx = -amount if direction == "left" else (amount if direction == "right" else 0)
        dy = -amount if direction == "up" else (amount if direction == "down" else 0)
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._scroll_locked(dx, dy)
            )

    def _scroll_locked(self, dx: float, dy: float) -> str:
        """执行滚动，并用与其他页面操作相同的事件收尾。"""
        previous_url = self._page.url
        previous_position = self._position_marker_locked()
        previous_marker = self._dom_marker_locked()
        before_pages = set(self._pages)
        # 滚动可能触发懒加载，先废弃旧观察。
        self._invalidate_snapshot_locked()
        try:
            self._page.evaluate(f"window.scrollBy({dx}, {dy})")
        except Exception as exc:
            return self._interaction_failure_with_observation_locked(
                "scroll_failed", f"滚动失败: {exc}", previous_url,
                previous_marker, before_pages,
            )
        return self._finalize_interaction_locked(
            previous_url, previous_position, "scroll", False, before_pages,
            previous_marker, record_navigation=False,
        )

    # --- 条件等待(P4) ---
    # 等待会在页面状态变化后生成新快照，因此与交互操作一样会使旧 snapshot_id 失效。
    def wait_for_url(
        self,
        pattern: str,
        snapshot_id: str,
        *,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """等待当前 URL 匹配 glob ``pattern``，例如 ``"**/search*"``。"""
        if not isinstance(pattern, str) or not pattern.strip():
            return _err("invalid_args", "pattern 必须是非空字符串")
        options_error = self._validate_wait_options(timeout_ms, cancel_event)
        if options_error is not None:
            return options_error
        with self._lock:
            return self._wait_with_condition_locked(
                snapshot_id=snapshot_id,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
                condition=lambda: fnmatchcase(self._page.url, pattern),
                description=f"URL 匹配 {pattern!r}",
            )

    def wait_for_text(
        self,
        text: str,
        snapshot_id: str,
        *,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """等待页面出现可见的 ``text``。"""
        if not isinstance(text, str) or not text.strip():
            return _err("invalid_args", "text 必须是非空字符串")
        options_error = self._validate_wait_options(timeout_ms, cancel_event)
        if options_error is not None:
            return options_error

        def text_is_visible() -> bool:
            locator = self._page.get_by_text(text, exact=False)
            for index in range(locator.count()):
                if locator.nth(index).is_visible(timeout=1):
                    return True
            return False

        with self._lock:
            return self._wait_with_condition_locked(
                snapshot_id=snapshot_id,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
                condition=text_is_visible,
                description=f"可见文本 {text!r}",
            )

    def wait_for_ref(
        self,
        ref: str,
        snapshot_id: str,
        *,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """等待指定快照中的 ``ref`` 对应的原 backend DOM 节点变为可见。

        前端框架重绘后即使出现外观相同的新元素，也不会自动迁移到它；
        这能避免等待期间把旧 ref 静默改指向另一个节点。
        """
        if not isinstance(ref, str) or not ref.strip():
            return _err("invalid_args", "ref 必须是非空字符串")
        options_error = self._validate_wait_options(timeout_ms, cancel_event)
        if options_error is not None:
            return options_error
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            backend_id = self._ref_to_backend_id.get(ref)
            if backend_id is None:
                return _err(
                    "invalid_ref",
                    f"ref {ref} 无效。请先调用 snapshot 获取当前页面的新 ref。",
                )

            def ref_is_visible() -> bool:
                return self._is_backend_node_visible_locked(backend_id)

            return self._wait_with_condition_locked(
                snapshot_id=snapshot_id,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
                condition=ref_is_visible,
                description=f"元素 {ref}",
            )

    def wait_for_load_state(
        self,
        state: str,
        snapshot_id: str,
        *,
        timeout_ms: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """等待 ``domcontentloaded``、``load`` 或 ``networkidle`` 状态。"""
        normalized_state = state.lower().strip() if isinstance(state, str) else ""
        if normalized_state not in {"domcontentloaded", "load", "networkidle"}:
            return _err(
                "invalid_args",
                "state 必须是 domcontentloaded、load 或 networkidle",
            )
        options_error = self._validate_wait_options(timeout_ms, cancel_event)
        if options_error is not None:
            return options_error
        with self._lock:
            return self._wait_with_condition_locked(
                snapshot_id=snapshot_id,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
                condition=lambda: self._is_load_state_ready_locked(normalized_state),
                description=f"加载状态 {normalized_state}",
            )

    def _wait_with_condition_locked(
        self,
        *,
        snapshot_id: str,
        timeout_ms: int | None,
        cancel_event: threading.Event | None,
        condition: Callable[[], bool],
        description: str,
    ) -> str:
        """把一次等待纳入统一的对话框策略生命周期。"""
        return self._execute_with_dialog_policy_locked(
            lambda: self._wait_with_condition_core_locked(
                snapshot_id=snapshot_id,
                timeout_ms=timeout_ms,
                cancel_event=cancel_event,
                condition=condition,
                description=description,
            )
        )

    def _wait_with_condition_core_locked(
        self,
        *,
        snapshot_id: str,
        timeout_ms: int | None,
        cancel_event: threading.Event | None,
        condition: Callable[[], bool],
        description: str,
    ) -> str:
        """在已持锁的会话中轮询条件，并统一处理成功、超时和取消。"""
        self._require_started_locked()
        stale_error = self._validate_snapshot_locked(snapshot_id)
        if stale_error is not None:
            return stale_error
        resolved_timeout = self._resolve_wait_timeout(timeout_ms)
        if resolved_timeout is None:
            return _err(
                "invalid_args",
                "timeout_ms 必须是大于 0 的整数或 None",
            )

        try:
            previous_url = self._page.url
            previous_marker = self._dom_marker_locked()
            before_pages = set(self._pages)
            try:
                previous_position = self._position_marker_locked()
            except Exception as exc:
                if self._is_permanent_browser_error(exc):
                    return _err(
                        "wait_failed",
                        f"开始等待时页面不可用: {exc}",
                    )
                if self._is_transient_wait_error(exc):
                    # 导航切换期间 history.state 可能短暂不可读；这不妨碍等待本身。
                    previous_position = None
                else:
                    return _err(
                        "wait_failed",
                        f"开始等待时读取页面位置失败: {exc}",
                    )
        except Exception as exc:
            return _err("wait_failed", f"开始等待时页面不可用: {exc}")
        # 条件等待期间 DOM、URL 都可能已变化；开始等待即废弃旧观察结果。
        self._invalidate_snapshot_locked()
        deadline = monotonic() + resolved_timeout / 1000
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return self._finish_wait_locked(
                    "wait_cancelled",
                    f"等待{description}已取消",
                    previous_url,
                    previous_position,
                    previous_marker,
                    before_pages,
                )
            try:
                if condition():
                    return self._finish_wait_locked(
                        None,
                        None,
                        previous_url,
                        previous_position,
                        previous_marker,
                        before_pages,
                    )
            except Exception as exc:
                if self._is_permanent_browser_error(exc):
                    return self._finish_wait_locked(
                        "wait_failed",
                        f"等待{description}时页面不可用: {exc}",
                        previous_url,
                        previous_position,
                        previous_marker,
                        before_pages,
                    )
                if not self._is_transient_wait_error(exc):
                    return self._finish_wait_locked(
                        "wait_failed",
                        f"等待{description}时发生未知错误: {exc}",
                        previous_url,
                        previous_position,
                        previous_marker,
                        before_pages,
                    )
                # 导航和框架重绘会暂时销毁执行上下文或 locator；视为本轮未命中。

            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                return self._finish_wait_locked(
                    "wait_timeout",
                    f"等待{description}超时({resolved_timeout}ms)",
                    previous_url,
                    previous_position,
                    previous_marker,
                    before_pages,
                )
            if cancel_event is not None and cancel_event.is_set():
                return self._finish_wait_locked(
                    "wait_cancelled",
                    f"等待{description}已取消",
                    previous_url,
                    previous_position,
                    previous_marker,
                    before_pages,
                )
            # 使用短轮询保持取消响应；同步 API 在这里阻塞的是 BrowserSession 所在线程。
            try:
                self._page.wait_for_timeout(min(100, remaining_ms))
            except Exception as exc:
                if self._is_permanent_browser_error(exc):
                    return self._finish_wait_locked(
                        "wait_failed",
                        f"等待{description}时页面不可用: {exc}",
                        previous_url,
                        previous_position,
                        previous_marker,
                        before_pages,
                    )
                if not self._is_transient_wait_error(exc):
                    return self._finish_wait_locked(
                        "wait_failed",
                        f"等待{description}时发生未知错误: {exc}",
                        previous_url,
                        previous_position,
                        previous_marker,
                        before_pages,
                    )
                # 有些导航窗口拒绝任何 page 调用；短暂退让后再次检查条件。
                sleep(min(0.05, max(0.0, remaining_ms / 1000)))

    def _validate_wait_options(
        self,
        timeout_ms: int | None,
        cancel_event: threading.Event | None,
    ) -> str | None:
        """在失效快照前验证等待选项，避免无效参数改变会话状态。"""
        if self._resolve_wait_timeout(timeout_ms) is None:
            return _err("invalid_args", "timeout_ms 必须是大于 0 的整数或 None")
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            return _err("invalid_args", "cancel_event 必须是 threading.Event 或 None")
        return None

    def _resolve_wait_timeout(self, timeout_ms: int | None) -> int | None:
        """解析单次等待的超时；None 继承会话默认值。"""
        resolved_timeout = self._timeout_ms if timeout_ms is None else timeout_ms
        if (
            isinstance(resolved_timeout, bool)
            or not isinstance(resolved_timeout, int)
            or resolved_timeout <= 0
        ):
            return None
        return resolved_timeout

    def _is_permanent_browser_error(self, exc: Exception) -> bool:
        """区分已关闭的浏览器资源与导航期间可恢复的短暂异常。"""
        if self._page is None or self._context is None or self._browser is None:
            return True
        message = str(exc).lower()
        permanent_markers = (
            "target page, context or browser has been closed",
            "target page has been closed",
            "page has been closed",
            "context has been closed",
            "browser has been closed",
            "browser is disconnected",
            "connection closed",
            "target closed",
        )
        return any(marker in message for marker in permanent_markers)

    def _is_transient_wait_error(self, exc: Exception) -> bool:
        """仅识别导航或重绘窗口内已知、可恢复的 Playwright 错误。"""
        message = str(exc).lower()
        transient_markers = (
            "execution context was destroyed",
            "execution context is not available",
            "most likely because of a navigation",
            "cannot find context with specified id",
            "frame was detached",
            "frame has been detached",
            "frame is detached",
            "frame was navigated",
            "frame is navigating",
            "element is not attached to the dom",
            "element is not attached",
            "node is detached",
            "no node with given id found",
            "could not find node with given id",
        )
        return any(marker in message for marker in transient_markers)

    def _is_networkidle_probe_timeout(self, exc: Exception) -> bool:
        """识别本模块主动发起的短 networkidle 探测超时。"""
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:
            return False
        return isinstance(exc, PlaywrightTimeoutError)

    def _is_backend_node_visible_locked(self, backend_id: int) -> bool:
        """通过 backendDOMNodeId 检查元素仍连接在页面中且可见。"""
        client = self._context.new_cdp_session(self._page)
        try:
            try:
                resolved = client.send("DOM.resolveNode", {"backendNodeId": backend_id})
                object_id = resolved.get("object", {}).get("objectId")
                if not object_id:
                    return False
                result = client.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": _JS_IS_VISIBLE,
                        "returnByValue": True,
                    },
                )
            except Exception as exc:
                if self._is_transient_wait_error(exc):
                    # 元素被框架重绘而脱离 DOM 时，本轮尚不能确认可见。
                    return False
                raise
            return result.get("result", {}).get("value") is True
        finally:
            try:
                client.detach()
            except Exception as exc:
                if not self._is_transient_wait_error(exc):
                    raise

    def _is_load_state_ready_locked(self, state: str) -> bool:
        """以非阻塞短探测检查加载状态，避免一次调用占满总超时。"""
        if state == "domcontentloaded":
            return self._page.evaluate("document.readyState") in {"interactive", "complete"}
        if state == "load":
            return self._page.evaluate("document.readyState") == "complete"
        try:
            # 给本次探测一个很短的窗口，既能接住即将发生的 networkidle，
            # 也不会让取消信号被一次长等待拖住。
            self._page.wait_for_load_state("networkidle", timeout=50)
            return True
        except Exception as exc:
            if self._is_networkidle_probe_timeout(exc) or self._is_transient_wait_error(exc):
                return False
            raise

    def _finish_wait_locked(
        self,
        error_type: str | None,
        error: str | None,
        previous_url: str,
        previous_position: str | None,
        previous_marker: str,
        before_pages: set[str],
    ) -> str:
        """等待专用收尾：共享事件识别，仍只使用短暂快照重试。"""
        return self._finalize_interaction_locked(
            previous_url,
            previous_position,
            "wait",
            False,
            before_pages,
            previous_marker,
            error_type=error_type,
            error=error,
            settle_navigation=False,
            snapshot_attempts=3,
        )

    # --- 高级读取(P3) ---
    # get_text 是纯读取,不改变页面状态,因此不失效旧 snapshot_id、不取新快照。
    # 调用方拿到文本后,手里的 ref 仍然有效,可继续 click/type。
    # console 执行任意 JS,可能改变 DOM/状态,因此按交互操作处理:失效旧 ref、取新快照。

    def get_text(
        self,
        ref: str | None,
        snapshot_id: str,
        max_chars: int = 5000,
    ) -> str:
        """读取元素或整页的连贯文本。

        - ``ref=None``:返回整页可见文本(``document.body.innerText``)。
        - 传 ``ref``:返回该元素的 ``textContent``(含后代文本)。

        纯读取,不改页面:不失效旧 snapshot_id、不取新快照。返回里携带的
        ``snapshot_id`` 与传入相同,调用方可继续用手里已有的 ref 操作。

        ``max_chars`` 限制返回文本长度,默认 5000。超长时截断并置
        ``truncated=True``,避免整页文本撑爆 tool result。
        """
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars <= 0
        ):
            return _err("invalid_args", f"max_chars 必须是正整数,收到: {max_chars!r}")
        if ref is not None and not isinstance(ref, str):
            return _err("invalid_args", "ref 必须是字符串或 None")

        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error

            if ref is None:
                # 整页可见文本。innerText 会反映渲染后的换行与可见性,
                # 比 textContent 更接近"人看到的内容"。
                try:
                    full_text = self._page.evaluate("document.body.innerText")
                except Exception as exc:
                    return _err("get_text_failed", f"读取整页文本失败: {exc}")
            else:
                # 解析 ref -> 调 textContent。和交互操作同一条 CDP 路径,
                # 但只读不改:不失效旧观察结果,执行后不取新快照。
                read_result = self._read_ref_text_locked(ref)
                if read_result.error_type is not None:
                    return _err(
                        read_result.error_type,
                        read_result.error or "读取元素文本失败",
                    )
                full_text = read_result.text or ""

            full_text = full_text if isinstance(full_text, str) else str(full_text or "")
            if len(full_text) <= max_chars:
                return _ok_text(full_text, False, snapshot_id, self._page.url)
            return _ok_text(
                full_text[:max_chars], True, snapshot_id, self._page.url
            )

    def _read_ref_text_locked(self, ref: str) -> _RefTextReadResult:
        """调用方已持锁时,解析 ref 并返回元素的 textContent。

        内部结果显式区分文本与错误，调用方不再从 JSON 字符串猜测失败。
        """
        backend_id = self._ref_to_backend_id.get(ref)
        if backend_id is None:
            return _RefTextReadResult(
                error_type="invalid_ref",
                error=(
                    f"ref {ref} 无效。ref 在每次 snapshot 之间失效,"
                    "请重新调 navigate 或 snapshot 取新 ref。"
                ),
            )
        try:
            client = self._context.new_cdp_session(self._page)
        except Exception as exc:
            return _RefTextReadResult(
                error_type="resolve_failed",
                error=f"创建 ref 解析会话失败: {exc}",
            )
        try:
            try:
                resolved = client.send(
                    "DOM.resolveNode",
                    {"backendNodeId": backend_id},
                )
            except Exception as exc:
                return _RefTextReadResult(
                    error_type="resolve_failed",
                    error=f"解析 ref 失败: {exc}",
                )
            if not isinstance(resolved, dict):
                return _RefTextReadResult(
                    error_type="resolve_failed",
                    error="DOM.resolveNode 返回了无效结果",
                )
            remote_obj = resolved.get("object", {})
            if not isinstance(remote_obj, dict):
                return _RefTextReadResult(
                    error_type="resolve_failed",
                    error="DOM.resolveNode 返回了无效 object 结构",
                )
            object_id = remote_obj.get("objectId")
            if not object_id:
                return _RefTextReadResult(
                    error_type="resolve_failed",
                    error="DOM.resolveNode 未返回 objectId",
                )
            try:
                call_result = client.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": object_id,
                        "functionDeclaration": _JS_GET_TEXT,
                        "returnByValue": True,
                    },
                )
            except Exception as exc:
                return _RefTextReadResult(
                    error_type="get_text_failed",
                    error=f"读取元素文本失败: {exc}",
                )
        finally:
            try:
                client.detach()
            except Exception:
                pass
        # CDP callFunctionOn 返回结构:{"result": {"type": "string", "value": "..."}}
        # 注意:这与 _interact 里取 result_val 的路径不同 -- _interact 只检查
        # 错误标志(subtype/exceptionDetails),不读返回值;这里要读 value。
        if not isinstance(call_result, dict):
            return _RefTextReadResult(
                error_type="get_text_failed",
                error="读取元素文本时收到无效 CDP 返回结构",
            )
        remote_result = call_result.get("result", {})
        if not isinstance(remote_result, dict):
            return _RefTextReadResult(
                error_type="get_text_failed",
                error="读取元素文本时未返回有效结果",
            )
        if remote_result.get("subtype") == "error" or "exceptionDetails" in call_result:
            exc_detail = call_result.get("exceptionDetails", {})
            if not isinstance(exc_detail, dict):
                exc_detail = {}
            exception = exc_detail.get("exception", {})
            if not isinstance(exception, dict):
                exception = {}
            exc_msg = exception.get("description", "未知 JS 错误")
            return _RefTextReadResult(
                error_type="get_text_failed",
                error=f"JS 执行错误: {exc_msg}",
            )
        # textContent 可能是 null(空元素),统一成空串。
        value = remote_result.get("value")
        if value is not None and not isinstance(value, str):
            return _RefTextReadResult(
                error_type="get_text_failed",
                error="元素文本返回值不是字符串",
            )
        return _RefTextReadResult(text=value or "")

    def console(
        self,
        expression: str,
        snapshot_id: str,
        max_chars: int = 5000,
    ) -> str:
        """执行 JavaScript，并让它遵守与其他页面操作相同的对话框规则。"""
        if not isinstance(expression, str) or not expression.strip():
            return _err("invalid_args", "expression 不能为空")
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars <= 0
        ):
            return _err("invalid_args", f"max_chars 必须是正整数,收到: {max_chars!r}")
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            return self._execute_with_dialog_policy_locked(
                lambda: self._console_locked(expression, snapshot_id, max_chars)
            )

    def _console_locked(
        self,
        expression: str,
        snapshot_id: str,
        max_chars: int = 5000,
    ) -> str:
        """在页面里执行任意 JavaScript 表达式,返回序列化结果。

        **逃生舱**:AX tree 看不到的元素、结构化数据、非文本状态,都能用
        console 读取或操作。

        **危险**:JS 能做任何事--读 cookie、发请求、改 DOM、导航。执行后
        旧 ref 可能失效(JS 可能改了 DOM),因此按交互操作处理:失效旧观察
        结果、取新快照。

        接 agent 时,console 必须 ``unknown_on_crash``(不能 retry_safe):
        JS 副作用不可逆,重跑可能重复提交表单或重复扣款。

        返回值用 JSON 序列化;非可序列化(Map/Set/循环引用/函数)兜底成
        ``"<unserializable>"`` 字符串,不报错。``max_chars`` 限制结果的
        序列化长度；超限时 ``result`` 返回安全截断的文本，同时带回
        ``truncated`` 与 ``original_length``。
        """
        if not isinstance(expression, str) or not expression.strip():
            return _err("invalid_args", "expression 不能为空")
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars <= 0
        ):
            return _err("invalid_args", f"max_chars 必须是正整数,收到: {max_chars!r}")

        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            previous_marker = self._dom_marker_locked()
            before_pages = set(self._pages)
            # JS 可能改 DOM,先失效旧观察结果。
            self._invalidate_snapshot_locked()
            # 页面侧先序列化并截断，避免超大结果完整穿过 CDP 传到 Python。
            wrapped = _JS_CONSOLE_WRAPPER.format(
                escaped_expr=_js_escape(expression),
                max_chars=max_chars,
            )
            try:
                call_result = self._page.evaluate(wrapped)
            except Exception as exc:
                return self._finish_dialog_operation_locked(
                    self._finalize_interaction_locked(
                        previous_url,
                        previous_position,
                        "console",
                        False,
                        before_pages,
                        previous_marker,
                        error_type="console_failed",
                        error=f"执行表达式失败: {exc}",
                        record_navigation=True,
                    )
                )
            # page.evaluate 返回的是 Python 对象(str/dict/None 等),
            # 因为 JS 侧已经序列化成 {ok, result/error} 结构。
            if not isinstance(call_result, dict):
                return self._finish_dialog_operation_locked(
                    self._finalize_interaction_locked(
                        previous_url, previous_position, "console", False,
                        before_pages, previous_marker,
                        error_type="console_failed",
                        error=f"返回结构异常: {call_result!r}",
                    )
                )
            if call_result.get("error"):
                return self._finish_dialog_operation_locked(
                    self._finalize_interaction_locked(
                        previous_url, previous_position, "console", False,
                        before_pages, previous_marker,
                        error_type="console_failed",
                        error=f"JS 抛出异常: {call_result.get('error')}",
                    )
                )
            js_result = call_result.get("result", "<unserializable>")
            truncated = call_result.get("truncated")
            original_length = call_result.get("original_length")
            if not isinstance(truncated, bool) or (
                isinstance(original_length, bool)
                or not isinstance(original_length, int)
                or original_length < 0
            ):
                return self._finish_dialog_operation_locked(
                    self._finalize_interaction_locked(
                        previous_url, previous_position, "console", False,
                        before_pages, previous_marker,
                        error_type="console_failed",
                        error="页面返回了无效的结果长度信息",
                    )
                )
            observation = self._finalize_interaction_locked(
                previous_url, previous_position, "console", False,
                before_pages, previous_marker,
            )
            try:
                payload = json.loads(observation)
            except json.JSONDecodeError:
                return self._finish_dialog_operation_locked(observation)
            if not payload.get("ok"):
                return self._finish_dialog_operation_locked(observation)
            payload.update(
                {
                    "result": js_result,
                    "truncated": truncated,
                    "original_length": original_length,
                }
            )
            return self._finish_dialog_operation_locked(
                json.dumps(payload, ensure_ascii=False)
            )

    # --- 文件传输与页面产物(P7) ---

    def upload_files(
        self,
        ref: str,
        paths: str | Path | list[str | Path],
        snapshot_id: str,
    ) -> str:
        """向当前快照中的 ``<input type=file>`` 选择工作区内的文件。"""
        if not isinstance(ref, str) or not ref:
            return _err("invalid_ref", "ref 必须是非空字符串")
        raw_paths = [paths] if isinstance(paths, (str, Path)) else paths
        if not isinstance(raw_paths, list) or not raw_paths:
            return _err("invalid_path", "paths 必须是单个路径或非空路径列表")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            resolved_paths: list[Path] = []
            for value in raw_paths:
                resolved, error_type = self._resolve_upload_path_locked(value)
                if error_type is not None or resolved is None:
                    messages = {
                        "invalid_path": "上传路径必须是工作区内的普通文件，且不能包含 '..' 或符号链接",
                        "file_not_found": "上传文件不存在",
                        "path_outside_workspace": "上传文件必须位于 workspace_root 内",
                    }
                    return self._current_observation_error_locked(
                        error_type or "invalid_path",
                        messages.get(error_type or "", "上传路径无效"),
                    )
                resolved_paths.append(resolved)
            target = self._locator_for_ref_locked(ref)
            if isinstance(target, str):
                return self._p7_ref_error_with_observation_locked(target)
            upload_target_ready = False
            try:
                details = target.locator.evaluate(
                    """(element) => ({
                        tagName: element.tagName.toLowerCase(),
                        type: String(element.type || '').toLowerCase(),
                        multiple: Boolean(element.multiple)
                    })"""
                )
                if not isinstance(details, dict) or (
                    details.get("tagName") != "input" or details.get("type") != "file"
                ):
                    return self._current_observation_error_locked(
                        "unsupported_upload_target",
                        "上传目标必须是 <input type=file> 元素",
                    )
                if len(resolved_paths) > 1 and not details.get("multiple"):
                    return self._current_observation_error_locked(
                        "multiple_files_not_supported",
                        "该文件输入框不支持同时选择多个文件",
                    )
                upload_target_ready = True
            except Exception as exc:
                return self._current_observation_error_locked(
                    self._upload_error_type(exc), f"检查上传目标失败: {exc}"
                )
            finally:
                # 校验失败尚未进入原生动作收尾，也必须恢复临时定位锚点。
                if not upload_target_ready:
                    self._clear_native_markers_locked([target])
            result = self._run_native_action_locked(
                "upload_files",
                lambda: target.locator.set_input_files([str(path) for path in resolved_paths]),
                targets=[target],
                error_mapper=self._upload_error_type,
            )
            metadata = [
                {"filename": path.name, "size_bytes": path.stat().st_size}
                for path in resolved_paths
            ]
            try:
                succeeded = bool(json.loads(result).get("ok"))
            except (TypeError, json.JSONDecodeError):
                return result
            return self._add_result_fields(
                result,
                **({"files": metadata} if succeeded else {"attempted_files": metadata}),
            )

    def download(
        self,
        ref: str,
        snapshot_id: str,
        timeout_ms: int | None = None,
        *,
        event_timeout_ms: int | None = None,
        completion_timeout_ms: int | None = None,
    ) -> str:
        """点击下载并保存为会话产物。

        ``timeout_ms`` 是兼容旧调用的下载事件等待上限，等同于
        ``event_timeout_ms``，不表示整个下载截止时间。同步 Playwright 无法
        安全中断正在执行的 ``failure()`` 或 ``save_as()``；
        ``completion_timeout_ms`` 因而是完成阶段的截止判定。若阻塞调用返回
        时已经越过该截止时间，产物会被删除并返回 ``download_timeout``。
        """
        if not isinstance(ref, str) or not ref:
            return _err("invalid_ref", "ref 必须是非空字符串")
        if timeout_ms is not None and event_timeout_ms is not None:
            return _err("invalid_args", "timeout_ms 与 event_timeout_ms 不能同时提供")
        event_timeout = (
            event_timeout_ms
            if event_timeout_ms is not None
            else timeout_ms if timeout_ms is not None else self._timeout_ms
        )
        completion_timeout = (
            self._timeout_ms if completion_timeout_ms is None else completion_timeout_ms
        )
        for name, value in (
            ("event_timeout_ms", event_timeout),
            ("completion_timeout_ms", completion_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                return _err("invalid_args", f"{name} 必须是正整数")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            target = self._locator_for_ref_locked(ref)
            if isinstance(target, str):
                return self._p7_ref_error_with_observation_locked(target)
            return self._execute_with_dialog_policy_locked(
                lambda: self._download_locked(
                    target, event_timeout, completion_timeout
                )
            )

    def _download_locked(
        self,
        target: _NativeTarget,
        event_timeout_ms: int,
        completion_timeout_ms: int,
    ) -> str:
        """按事件阶段和完成阶段的独立截止规则保存下载产物。"""
        before_pages = set(self._pages)
        previous_url = self._page.url
        previous_position = self._position_marker_locked()
        previous_marker = self._dom_marker_locked()
        self._invalidate_snapshot_locked()
        suggested_filename: str | None = None
        download_url: str | None = None
        failure: str | None = None
        try:
            with self._page.expect_download(timeout=event_timeout_ms) as download_info:
                target.locator.click()
            browser_download = download_info.value
            suggested_filename = browser_download.suggested_filename
            download_url = browser_download.url
            completion_deadline = monotonic() + completion_timeout_ms / 1000
            # sync API 没有可传入的下载完成/保存超时，且对象不能跨线程调用。
            # 因此只能在阻塞调用返回后检查截止时间，并删除迟到的本地产物。
            failure = browser_download.failure()
            if monotonic() > completion_deadline:
                result = self._interaction_failure_with_observation_locked(
                    "download_timeout",
                    "下载完成阶段超过 completion_timeout_ms",
                    previous_url,
                    previous_marker,
                    before_pages,
                )
                return self._add_result_fields(
                    result,
                    artifact=None,
                    suggested_filename=suggested_filename,
                    download_url=download_url,
                    failure="completion_timeout",
                    completed=False,
                )
            if failure:
                result = self._interaction_failure_with_observation_locked(
                    "download_cancelled" if "cancel" in failure.lower() else "download_failed",
                    f"下载未完成: {failure}",
                    previous_url,
                    previous_marker,
                    before_pages,
                )
                return self._add_result_fields(
                    result,
                    artifact=None,
                    suggested_filename=suggested_filename,
                    download_url=download_url,
                    failure=failure,
                    completed=False,
                )
            filename = self._safe_artifact_filename_locked(
                suggested_filename, prefix="download"
            )
            try:
                artifact = self._publish_artifact_locked(
                    "download",
                    filename,
                    lambda temporary_path: browser_download.save_as(str(temporary_path)),
                    page_id=self._current_page_id,
                    source_url=download_url or previous_url,
                )
            except Exception as exc:
                error_type = (
                    "download_timeout"
                    if monotonic() > completion_deadline
                    else "artifact_write_failed"
                )
                result = self._interaction_failure_with_observation_locked(
                    error_type,
                    (
                        "下载保存阶段超过 completion_timeout_ms"
                        if error_type == "download_timeout"
                        else f"保存下载产物失败: {exc}"
                    ),
                    previous_url,
                    previous_marker,
                    before_pages,
                )
                return self._add_result_fields(
                    result,
                    artifact=None,
                    suggested_filename=suggested_filename,
                    download_url=download_url,
                    failure=str(exc),
                    completed=False,
                )
            if monotonic() > completion_deadline:
                self._discard_artifact_locked(artifact)
                result = self._interaction_failure_with_observation_locked(
                    "download_timeout",
                    "下载保存阶段超过 completion_timeout_ms",
                    previous_url,
                    previous_marker,
                    before_pages,
                )
                return self._add_result_fields(
                    result,
                    artifact=None,
                    suggested_filename=suggested_filename,
                    download_url=download_url,
                    failure="completion_timeout",
                    completed=False,
                )
            result = self._finalize_interaction_locked(
                previous_url,
                previous_position,
                "download",
                False,
                before_pages,
                previous_marker,
            )
            return self._add_result_fields(
                result,
                artifact=self._artifact_payload_locked(artifact),
                suggested_filename=suggested_filename,
                download_url=download_url,
                failure=None,
                completed=True,
            )
        except Exception as exc:
            error_type = "page_closed" if self._is_permanent_browser_error(exc) else "download_timeout" if "timeout" in str(exc).lower() else "download_failed"
            result = self._interaction_failure_with_observation_locked(
                error_type,
                f"下载失败: {exc}",
                previous_url,
                previous_marker,
                before_pages,
            )
            return self._add_result_fields(
                result,
                artifact=None,
                suggested_filename=suggested_filename,
                download_url=download_url,
                failure=str(exc),
                completed=False,
            )
        finally:
            self._clear_native_markers_locked([target])

    def screenshot(self, snapshot_id: str, *, full_page: bool = False) -> str:
        """保存当前页面 PNG 截图；这是一项纯读取操作，不会失效快照。"""
        if not isinstance(full_page, bool):
            return _err("invalid_args", "full_page 必须是布尔值")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            artifact: _Artifact | None = None
            completed = False
            try:
                artifact = self._publish_artifact_locked(
                    "screenshot",
                    self._safe_artifact_filename_locked(
                        "page.png", prefix="page", suffix=".png"
                    ),
                    lambda temporary_path: self._page.screenshot(
                        path=str(temporary_path), type="png", full_page=full_page
                    ),
                    page_id=self._current_page_id,
                    source_url=self._page.url,
                )
                observation = self._current_observation_locked()
                if observation is None:
                    return _err("screenshot_failed", "页面截图完成后原快照已不可用")
                snapshot, active_snapshot_id = observation
                result = self._add_result_fields(
                    self._ok_snapshot_locked(
                        snapshot, active_snapshot_id, event_type="none"
                    ),
                    artifact=self._artifact_payload_locked(artifact),
                )
                result_payload = json.loads(result)
                if not isinstance(result_payload, dict) or not result_payload.get("ok"):
                    raise RuntimeError("页面截图结果构造失败")
                completed = True
                return result
            except Exception as exc:
                return self._current_observation_error_locked(
                    "page_closed" if self._is_permanent_browser_error(exc) else "screenshot_failed",
                    f"页面截图失败: {exc}",
                )
            finally:
                if not completed:
                    self._discard_new_artifact_on_failure_locked(artifact)

    def _passive_screenshot_state_locked(self) -> tuple[str, str, float, float, tuple[str, ...], str | None]:
        """读取截图前后必须保持不变的页面状态，不触发交互动作。"""
        if self._page is None or self._page.is_closed():
            raise RuntimeError("页面已关闭")
        self._ensure_dom_version_locked()
        state = self._page.evaluate(
            """() => ({
                domVersion: String(window.__browserDomVersion || 0),
                scrollX: window.scrollX,
                scrollY: window.scrollY
            })"""
        )
        if not isinstance(state, dict):
            raise RuntimeError("页面未返回有效的截图状态")
        scroll_x = state.get("scrollX")
        scroll_y = state.get("scrollY")
        dom_version = state.get("domVersion")
        if (
            isinstance(scroll_x, bool)
            or not isinstance(scroll_x, (int, float))
            or isinstance(scroll_y, bool)
            or not isinstance(scroll_y, (int, float))
            or not isinstance(dom_version, str)
        ):
            raise RuntimeError("页面返回了无效的截图状态")
        dialog = (
            json.dumps(self._last_dialog_event, ensure_ascii=False, sort_keys=True)
            if self._last_dialog_event is not None
            else None
        )
        return (
            self._page.url,
            dom_version,
            float(scroll_x),
            float(scroll_y),
            tuple(sorted(self._pages)),
            dialog,
        )

    def _visible_element_clip_locked(self, ref: str) -> dict[str, float] | str:
        """用 CDP 读取元素当前可见坐标，拒绝需要滚动才能截取的元素。"""
        backend_id = self._ref_to_backend_id.get(ref)
        if backend_id is None:
            return _err("invalid_ref", f"ref {ref} 无效或不属于当前页面快照")
        client = self._context.new_cdp_session(self._page)
        try:
            resolved = client.send("DOM.resolveNode", {"backendNodeId": backend_id})
            object_id = resolved.get("object", {}).get("objectId")
            if not object_id:
                return _err("invalid_ref", f"ref {ref} 对应节点已失效")
            evaluated = client.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": """function() {
                        if (!(this instanceof Element) || !this.isConnected) {
                            throw new Error('ref 对应节点已脱离 DOM');
                        }
                        const style = window.getComputedStyle(this);
                        const rect = this.getBoundingClientRect();
                        const visible = this.getClientRects().length > 0 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            style.visibility !== 'collapse' &&
                            Number(style.opacity) > 0 &&
                            rect.width > 0 && rect.height > 0;
                        const center = document.elementFromPoint(
                            rect.left + rect.width / 2,
                            rect.top + rect.height / 2
                        );
                        const obscured = Boolean(center) &&
                            center !== this && !this.contains(center);
                        return {
                            visible: visible,
                            obscured: obscured,
                            left: rect.left,
                            top: rect.top,
                            width: rect.width,
                            height: rect.height,
                            viewportWidth: window.innerWidth,
                            viewportHeight: window.innerHeight,
                            scrollX: window.scrollX,
                            scrollY: window.scrollY
                        };
                    }""",
                    "returnByValue": True,
                },
            )
            if "exceptionDetails" in evaluated:
                return _err("invalid_ref", f"ref {ref} 无法读取元素坐标")
            rect = evaluated.get("result", {}).get("value")
            if not isinstance(rect, dict):
                return _err("invalid_ref", f"ref {ref} 未返回有效元素坐标")
            visible = rect.get("visible")
            obscured = rect.get("obscured")
            if not isinstance(visible, bool) or not isinstance(obscured, bool):
                return _err("screenshot_failed", "元素可见性状态格式无效")
            values = [
                rect.get("left"),
                rect.get("top"),
                rect.get("width"),
                rect.get("height"),
                rect.get("viewportWidth"),
                rect.get("viewportHeight"),
                rect.get("scrollX"),
                rect.get("scrollY"),
            ]
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in values
            ):
                return _err("screenshot_failed", "元素坐标格式无效")
            left, top, width, height, viewport_width, viewport_height, scroll_x, scroll_y = (
                float(value) for value in values
            )
            if (
                not visible
                or obscured
                or width <= 0
                or height <= 0
                or left < 0
                or top < 0
                or left + width > viewport_width
                or top + height > viewport_height
            ):
                return _err("element_not_visible", "元素不完全位于当前可见区域，截图不会自动滚动页面")
            return {
                "x": left + scroll_x,
                "y": top + scroll_y,
                "width": width,
                "height": height,
            }
        except Exception as exc:
            error_type = "page_closed" if self._is_permanent_browser_error(exc) else "screenshot_failed"
            return _err(error_type, f"读取元素截图坐标失败: {exc}")
        finally:
            try:
                client.detach()
            except Exception:
                pass

    def screenshot_element(self, ref: str, snapshot_id: str) -> str:
        """保存当前快照中单个元素的 PNG 截图，不改变页面和快照。"""
        if not isinstance(ref, str) or not ref:
            return _err("invalid_ref", "ref 必须是非空字符串")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            target = self._locator_for_ref_locked(ref)
            if isinstance(target, str):
                return self._p7_ref_error_with_observation_locked(target)
            artifact: _Artifact | None = None
            completed = False
            try:
                # 截图不是会触发页面事件的操作；清除意外留下的临时对话框信息，
                # 但保留调用方为后续真实操作声明的 next policy。
                self._active_dialog_policy = None
                self._last_dialog_event = None
                before_state = self._passive_screenshot_state_locked()
                clip = self._visible_element_clip_locked(ref)
                if isinstance(clip, str):
                    self._last_dialog_event = None
                    return self._p7_ref_error_with_observation_locked(clip)
                artifact = self._publish_artifact_locked(
                    "element-screenshot",
                    self._safe_artifact_filename_locked(
                        "element.png", prefix="element", suffix=".png"
                    ),
                    lambda temporary_path: self._page.screenshot(
                        path=str(temporary_path), type="png", clip=clip
                    ),
                    page_id=self._current_page_id,
                    source_url=self._page.url,
                )
                after_state = self._passive_screenshot_state_locked()
                if after_state != before_state:
                    self._last_dialog_event = None
                    return self._current_observation_error_locked(
                        "screenshot_failed",
                        "截图期间页面状态发生变化，已丢弃图片产物",
                    )
                observation = self._current_observation_locked()
                if observation is None:
                    return _err("screenshot_failed", "元素截图完成后原快照已不可用")
                snapshot, active_snapshot_id = observation
                result = self._add_result_fields(
                    self._ok_snapshot_locked(
                        snapshot, active_snapshot_id, event_type="none"
                    ),
                    artifact=self._artifact_payload_locked(artifact),
                )
                result_payload = json.loads(result)
                if not isinstance(result_payload, dict) or not result_payload.get("ok"):
                    raise RuntimeError("元素截图结果构造失败")
                completed = True
                return result
            except Exception as exc:
                self._last_dialog_event = None
                return self._current_observation_error_locked(
                    "page_closed" if self._is_permanent_browser_error(exc) else "screenshot_failed",
                    f"元素截图失败: {exc}",
                )
            finally:
                self._clear_native_markers_locked([target])
                self._active_dialog_policy = None
                self._last_dialog_event = None
                if not completed:
                    self._discard_new_artifact_on_failure_locked(artifact)

    def _upload_error_type(self, exc: Exception) -> str:
        """上传失败优先保留可操作性错误，其余统一为上传失败。"""
        classified = self._classify_interaction_error(exc)
        if classified in {
            "page_closed",
            "invalid_ref",
            "element_disabled",
            "element_not_visible",
            "element_obscured",
            "interaction_timeout",
        }:
            return classified
        return "upload_failed"

    # --- 完整交互(P6) ---
    # 以下定义覆盖早期的 JS 交互实现：先把 backend DOM 节点临时标记为唯一属性，
    # 再交给 Playwright Locator 完成真实用户交互和可操作性检查。
    def click(self, ref: str, snapshot_id: str) -> str:
        return self._native_ref_action(ref, snapshot_id, "click", lambda locator: locator.click())

    def hover(self, ref: str, snapshot_id: str) -> str:
        return self._native_ref_action(ref, snapshot_id, "hover", lambda locator: locator.hover())

    def focus(self, ref: str, snapshot_id: str) -> str:
        return self._native_ref_action(ref, snapshot_id, "focus", lambda locator: locator.focus())

    def check(self, ref: str, snapshot_id: str) -> str:
        return self._native_ref_action(ref, snapshot_id, "check", lambda locator: locator.check())

    def uncheck(self, ref: str, snapshot_id: str) -> str:
        return self._native_ref_action(ref, snapshot_id, "uncheck", lambda locator: locator.uncheck())

    def type(
        self,
        ref: str,
        text: str,
        snapshot_id: str,
        clear: bool = True,
        *,
        mode: str = "fill",
        delay_ms: int = 0,
    ) -> str:
        """输入文本。mode 为 fill(一次写入)或 type(逐字符输入)。"""
        if not isinstance(text, str):
            return _err("invalid_args", "text 必须是字符串")
        if not isinstance(clear, bool):
            return _err("invalid_args", "clear 必须是布尔值")
        if mode not in {"fill", "type"}:
            return _err("invalid_args", "mode 必须是 fill 或 type")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
            return _err("invalid_args", "delay_ms 必须是非负整数")

        def action(locator: Any) -> None:
            if mode == "fill":
                if clear:
                    locator.fill(text)
                else:
                    locator.press("End")
                    locator.press_sequentially(text, delay=delay_ms)
                return
            if clear:
                locator.fill("")
            locator.press_sequentially(text, delay=delay_ms)

        return self._native_ref_action(ref, snapshot_id, "type", action)

    def select(self, ref: str, value: str | list[str], snapshot_id: str) -> str:
        """选择单个或多个 option；每项先按 value 匹配，再回退按可见文字匹配。"""
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            values = value
        else:
            return _err("invalid_args", "value 必须是非空字符串或非空字符串列表")

        def action(locator: Any) -> None:
            try:
                locator.select_option(value=values)
            except Exception as exc:
                message = str(exc).lower()
                # 只有 Playwright 明确说明指定 value 没有匹配 option 时，
                # 才把同一输入按可见文字重试；超时、关闭和未知错误不能掩盖。
                value_not_found = (
                    "did not find some options" in message
                    or re.search(r"options?\s+\[.*\]\s+not found", message) is not None
                )
                if not value_not_found:
                    raise
                locator.select_option(label=values)

        return self._native_ref_action(ref, snapshot_id, "select", action)

    def press(self, key: str, snapshot_id: str) -> str:
        """向当前页面发送键盘按键或快捷键组合，例如 Control+L。"""
        if not isinstance(key, str) or not key.strip():
            return _err("invalid_args", "key 必须是非空字符串")
        with self._lock:
            return self._native_page_action(snapshot_id, "keyboard", lambda: self._page.keyboard.press(key))

    def keyboard_shortcut(self, key: str, snapshot_id: str) -> str:
        """页面级键盘快捷键的语义别名。"""
        return self.press(key, snapshot_id)

    def drag_and_drop(self, source_ref: str, target_ref: str, snapshot_id: str) -> str:
        if not isinstance(source_ref, str) or not isinstance(target_ref, str):
            return _err("invalid_args", "source_ref 和 target_ref 必须是字符串")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            targets: list[_NativeTarget] = []
            source = self._locator_for_ref_locked(source_ref)
            if isinstance(source, str):
                return source
            targets.append(source)
            target = self._locator_for_ref_locked(target_ref)
            if isinstance(target, str):
                self._clear_native_markers_locked(targets)
                return target
            targets.append(target)
            return self._run_native_action_locked(
                "drag_and_drop",
                lambda: source.locator.drag_to(target.locator),
                targets=targets,
            )

    def _native_ref_action(
        self,
        ref: str,
        snapshot_id: str,
        name: str,
        action: Callable[[Any], None],
    ) -> str:
        if not isinstance(ref, str) or not ref:
            return _err("invalid_ref", "ref 必须是非空字符串")
        with self._lock:
            self._require_started_locked()
            no_page_error = self._require_current_page_locked()
            if no_page_error is not None:
                return no_page_error
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            target = self._locator_for_ref_locked(ref)
            if isinstance(target, str):
                return target
            return self._run_native_action_locked(
                name, lambda: action(target.locator), targets=[target]
            )

    def _native_page_action(
        self,
        snapshot_id: str,
        name: str,
        action: Callable[[], None],
    ) -> str:
        self._require_started_locked()
        no_page_error = self._require_current_page_locked()
        if no_page_error is not None:
            return no_page_error
        stale_error = self._validate_snapshot_locked(snapshot_id)
        if stale_error is not None:
            return stale_error
        return self._run_native_action_locked(name, action)

    def _locator_for_ref_locked(self, ref: str) -> _NativeTarget | str:
        """为当前快照的 backend DOM 节点创建只供本次操作使用的 Locator。"""
        backend_id = self._ref_to_backend_id.get(ref)
        if backend_id is None:
            return _err("invalid_ref", f"ref {ref} 无效或不属于当前页面快照")
        token = f"browser-{uuid4().hex}"
        client = self._context.new_cdp_session(self._page)
        try:
            resolved = client.send("DOM.resolveNode", {"backendNodeId": backend_id})
            object_id = resolved.get("object", {}).get("objectId")
            if not object_id:
                return _err("invalid_ref", f"ref {ref} 对应节点已失效")
            marked = client.send("Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": _JS_MARK_NATIVE_TARGET.format(token=_js_escape(token)),
                "returnByValue": True,
            })
            if "exceptionDetails" in marked or marked.get("result", {}).get("subtype") == "error":
                return _err("invalid_ref", f"ref {ref} 无法建立原生定位锚点")
            marker_state = marked.get("result", {}).get("value")
            if not isinstance(marker_state, dict):
                return _err("invalid_ref", f"ref {ref} 未返回定位锚点状态")
            marker_existed = marker_state.get("hadAttribute")
            previous_marker = marker_state.get("previousValue")
            if not isinstance(marker_existed, bool) or (
                previous_marker is not None and not isinstance(previous_marker, str)
            ):
                return _err("invalid_ref", f"ref {ref} 返回了无效定位锚点状态")
        except Exception as exc:
            return _err(self._classify_interaction_error(exc), f"解析 ref 失败: {exc}")
        finally:
            try:
                client.detach()
            except Exception:
                pass
        return _NativeTarget(
            page=self._page,
            locator=self._page.locator(f'[data-browser-native-ref="{token}"]'),
            token=token,
            marker_existed=marker_existed,
            previous_marker=previous_marker,
        )

    def _clear_native_markers_locked(self, targets: list[_NativeTarget]) -> None:
        """仅恢复本次锚点，绝不删除网页原有的同名属性。"""
        for target in targets:
            try:
                target.locator.evaluate(
                    """(element, state) => {
                        if (element.getAttribute('data-browser-native-ref') !== state.token) return;
                        if (state.markerExisted) {
                            element.setAttribute('data-browser-native-ref', state.previousMarker);
                        } else {
                            element.removeAttribute('data-browser-native-ref');
                        }
                    }""",
                    {
                        "token": target.token,
                        "markerExisted": target.marker_existed,
                        "previousMarker": target.previous_marker,
                    },
                )
            except Exception:
                # 清理问题不能覆盖已经得到的操作结果。
                pass

    def _begin_dialog_operation_locked(self) -> None:
        """消费下一次策略，并让本次事件与之后的操作隔离。"""
        self._last_dialog_event = None
        self._active_dialog_policy = self._next_dialog_policy
        self._next_dialog_policy = None

    def _finish_dialog_operation_locked(self, result: str) -> str:
        """把本次未声明策略的对话框转成错误，再清空临时状态。"""
        try:
            if self._last_dialog_event and self._last_dialog_event.get("result") == "unhandled":
                return _as_error_with_observation(
                    result,
                    "dialog_strategy_required",
                    "操作触发了对话框，但未预设 dialog_strategy；对话框已被拒绝以解除阻塞",
                )
            return result
        finally:
            self._active_dialog_policy = None
            self._last_dialog_event = None

    def _execute_with_dialog_policy_locked(
        self,
        operation: Callable[[], str],
    ) -> str:
        """为一次可能触发对话框的操作统一管理策略和事件。"""
        self._begin_dialog_operation_locked()
        try:
            result = operation()
        except Exception:
            self._active_dialog_policy = None
            self._last_dialog_event = None
            raise
        return self._finish_dialog_operation_locked(result)

    def _run_native_action_locked(
        self,
        name: str,
        action: Callable[[], None],
        *,
        targets: list[_NativeTarget] | None = None,
        error_mapper: Callable[[Exception], str] | None = None,
    ) -> str:
        return self._execute_with_dialog_policy_locked(
            lambda: self._run_native_action_core_locked(
                name, action, targets=targets, error_mapper=error_mapper
            )
        )

    def _run_native_action_core_locked(
        self,
        name: str,
        action: Callable[[], None],
        *,
        targets: list[_NativeTarget] | None = None,
        error_mapper: Callable[[Exception], str] | None = None,
    ) -> str:
        before_pages = set(self._pages)
        previous_url = self._page.url
        previous_position = self._position_marker_locked()
        previous_marker = self._dom_marker_locked()
        # 原生动作一旦发出，旧 ref 不能再被后续请求使用。
        self._invalidate_snapshot_locked()
        try:
            action()
        except Exception as exc:
            result = self._interaction_failure_with_observation_locked(
                error_mapper(exc) if error_mapper is not None else self._classify_interaction_error(exc),
                f"{name} 失败: {exc}",
                previous_url,
                previous_marker,
                before_pages,
            )
        else:
            result = self._finalize_interaction_locked(
                previous_url, previous_position, name, False, before_pages, previous_marker,
            )
        finally:
            self._clear_native_markers_locked(targets or [])
        return result

    def _interaction_failure_with_observation_locked(
        self,
        error_type: str,
        error: str,
        previous_url: str,
        previous_marker: str,
        before_pages: set[str],
        *,
        settle_navigation: bool = True,
        event_collection_timeout_ms: int = 250,
    ) -> str:
        """动作已发出但失败时，尽量返回当前的新观察结果。"""
        if error_type == "page_closed":
            return _err(error_type, error)
        return self._finalize_interaction_locked(
            previous_url,
            None,
            "失败操作",
            False,
            before_pages,
            previous_marker,
            error_type=error_type,
            error=error,
            record_navigation=False,
            settle_navigation=settle_navigation,
            event_collection_timeout_ms=event_collection_timeout_ms,
        )

    def _dom_marker_locked(self) -> str:
        try:
            self._ensure_dom_version_locked()
            return str(self._page.evaluate("window.__browserDomVersion || 0"))
        except Exception:
            return ""

    def _event_type_locked(
        self,
        previous_url: str,
        previous_marker: str,
        before_pages: set[str],
    ) -> str:
        """按页面事件的优先级识别本次操作已经造成的可见变化。"""
        if any(page_id not in before_pages for page_id in self._pages):
            return "popup"
        if self._last_dialog_event is not None:
            return "dialog"
        if self._page.url != previous_url:
            return "navigation"
        if self._dom_marker_locked() != previous_marker:
            return "dom_update"
        return "none"

    def _wait_for_popup_ready_locked(self, page_id: str) -> bool:
        """在短窗口内确认 popup 已离开空白初始页且可观察。"""
        state = self._pages.get(page_id)
        if state is None:
            return False
        popup = state.page
        try:
            popup.set_default_timeout(self._timeout_ms)
        except Exception:
            return False
        for _ in range(6):
            try:
                if popup.is_closed():
                    return False
                if popup.url != "about:blank":
                    try:
                        popup.wait_for_load_state("domcontentloaded", timeout=100)
                    except Exception:
                        # URL 已可用时允许仍在加载，快照会继续给出当前观察。
                        pass
                    return True
                popup.wait_for_timeout(50)
            except Exception:
                return False
        return False

    def _finalize_interaction_locked(
        self,
        previous_url: str,
        previous_position: str | None,
        action_name: str,
        used_fallback: bool,
        before_pages: set[str],
        previous_marker: str = "",
        *,
        error_type: str | None = None,
        error: str | None = None,
        record_navigation: bool = True,
        settle_navigation: bool = True,
        snapshot_attempts: int = 1,
        event_collection_timeout_ms: int = 250,
    ) -> str:
        """统一归并 popup、对话框、导航和普通 DOM 更新后的观察结果。"""
        # 在短而明确的窗口收集异步 popup/dialog，不把一次固定 sleep 当作事件事实。
        new_pages: list[str] = []
        event_deadline = monotonic() + max(0, event_collection_timeout_ms) / 1000.0
        while True:
            new_pages = [page_id for page_id in self._pages if page_id not in before_pages]
            if new_pages or self._last_dialog_event is not None:
                break
            remaining_ms = int((event_deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                self._page.wait_for_timeout(min(50, remaining_ms))
            except Exception as exc:
                return _err(self._classify_interaction_error(exc), f"{action_name} 后页面不可用: {exc}")
        event_type = "none"
        if new_pages:
            popup_page_id = new_pages[-1]
            if not self._wait_for_popup_ready_locked(popup_page_id):
                try:
                    snapshot, snapshot_id = self._snapshot_locked()
                    return self._err_observation_locked(
                        "popup_not_ready",
                        "新打开的页面尚未进入可观察状态",
                        snapshot,
                        snapshot_id,
                        event_type="popup",
                        extra={"popup_page_id": popup_page_id},
                    )
                except Exception as exc:
                    return _err("popup_not_ready", f"新页面未就绪且原页面不可观察: {exc}")
            self._select_page_locked(popup_page_id, invalidate=True)
            event_type = "popup"
        else:
            event_type = self._event_type_locked(
                previous_url, previous_marker, before_pages
            )
        # 导航场景需要等新页面就绪 -- 只等 100ms 会拿到 AJAX 半截快照
        # (JS 延迟渲染的内容还没注入)。和 _observe_after_action_locked 一致:
        # 等 load(覆盖子资源),失败退回 domcontentloaded,再给 AJAX 留时间。
        # 非 navigation(dialog/dom_update/popup)只等短窗口。
        if event_type == "navigation" and settle_navigation:
            try:
                self._page.wait_for_load_state("load", timeout=8000)
            except Exception:
                try:
                    self._page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
            self._page.wait_for_timeout(500)
        snapshot: str | None = None
        snapshot_id: str | None = None
        for attempt in range(snapshot_attempts):
            try:
                snapshot, snapshot_id = self._snapshot_locked()
                break
            except Exception as exc:
                if self._is_permanent_browser_error(exc):
                    return _err("page_closed", f"{action_name} 后页面不可用: {exc}")
                if not self._is_transient_wait_error(exc) or attempt == snapshot_attempts - 1:
                    if error_type is not None:
                        return _err(
                            error_type,
                            f"{error or f'{action_name}失败'}；获取恢复快照失败: {exc}",
                        )
                    return _err(
                        self._classify_interaction_error(exc),
                        f"{action_name} 后取快照失败: {exc}",
                    )
                sleep(0.04)
        if snapshot is None or snapshot_id is None:
            return _err("interaction_failed", f"{action_name} 后未能取得快照")
        if record_navigation:
            try:
                self._record_navigation_locked(previous_url, previous_position)
            except Exception:
                pass
        if error_type is not None:
            return self._err_observation_locked(
                error_type,
                error or f"{action_name}失败",
                snapshot,
                snapshot_id,
                event_type=event_type,
                used_fallback=used_fallback,
            )
        return self._ok_snapshot_locked(
            snapshot,
            snapshot_id,
            event_type=event_type,
            used_fallback=used_fallback,
        )

    def _classify_interaction_error(self, exc: Exception) -> str:
        """把 Playwright 的常见动作失败映射为稳定的工具错误类型。"""
        if self._is_permanent_browser_error(exc):
            return "page_closed"
        message = str(exc).lower()
        if "not attached" in message or "no node" in message or "ref 对应节点" in message:
            return "invalid_ref"
        if "disabled" in message or "not enabled" in message:
            return "element_disabled"
        if "editable" in message or "readonly" in message or "read-only" in message:
            return "element_not_editable"
        if "intercepts pointer" in message or "receives pointer events" in message or "obscures" in message:
            return "element_obscured"
        if "not visible" in message or "outside of the viewport" in message:
            return "element_not_visible"
        if "not a <select>" in message or "checkbox" in message or "radio" in message:
            return "unsupported_element"
        if "timeout" in message:
            return "interaction_timeout"
        return "interaction_failed"


# ---------------------------------------------------------------------------
# ref 映射构建。必须与 ``accessibility.py::format_snapshot`` 的 ref 分配
# 顺序完全一致 -- 那里按 INTERACTIVE_ROLES 在深度优先遍历中的出现顺序
# 从 e1 起编号,这里也按同一顺序填 _ref_to_backend_id。
# ---------------------------------------------------------------------------


def _build_ref_map(cdp_result: dict) -> dict[str, int]:
    """从 CDP AX tree 结果构建 ref -> backendDOMNodeId 映射。

    遍历顺序与 ``accessibility._format_node`` 一致:深度优先,按 childIds
    顺序。只给 INTERACTIVE_ROLES 里的角色分配 ref。
    """
    from browser.accessibility import INTERACTIVE_ROLES, _role_of

    if not cdp_result or not cdp_result.get("nodes"):
        return {}
    nodes = cdp_result["nodes"]
    nodes_by_id: dict[str, dict] = {
        str(n.get("nodeId", "")): n for n in nodes if isinstance(n, dict)
    }
    # 找根节点 -- 逻辑与 accessibility.format_snapshot 一致。
    root = None
    for n in nodes:
        if _role_of(n) == "rootWebArea":
            root = n
            break
    if root is None:
        root = nodes_by_id.get("0") or (nodes[0] if nodes else None)
    if root is None:
        return {}

    ref_map: dict[str, int] = {}
    counter = [0]
    _walk_ref_map(root, nodes_by_id, counter, ref_map, set())
    return ref_map


def _walk_ref_map(
    node: dict,
    nodes_by_id: dict[str, dict],
    counter: list[int],
    ref_map: dict[str, int],
    visited: set[str],
) -> None:
    """深度优先遍历,给交互角色分配 e1、e2...。与 _format_node 顺序对齐。"""
    from browser.accessibility import INTERACTIVE_ROLES, _role_of

    node_id = str(node.get("nodeId", ""))
    if node_id in visited:
        return
    visited.add(node_id)

    role = _role_of(node)
    backend_id = node.get("backendDOMNodeId")
    # 只给能解析回 DOM 的节点编号，必须与 format_snapshot 完全一致。
    if role in INTERACTIVE_ROLES and backend_id is not None:
        counter[0] += 1
        ref_map[f"e{counter[0]}"] = int(backend_id)

    for child_id in node.get("childIds", []) or []:
        child = nodes_by_id.get(str(child_id))
        if child is None:
            continue
        _walk_ref_map(child, nodes_by_id, counter, ref_map, visited)


# ---------------------------------------------------------------------------
# JS 片段。callFunctionOn 在元素上下文执行,``this`` 是元素本身。
# 约定:成功返回 null/undefined;失败抛 Error。
# ---------------------------------------------------------------------------

# get_text():返回元素 textContent(含后代文本)。null 安全(空元素返回空串)。
_JS_GET_TEXT = """function() {
    return this.textContent || '';
}"""

# 通过 CDP 解析旧 ref 时，以连接状态、计算样式和布局尺寸共同判断可见性。
_JS_IS_VISIBLE = """function() {
    if (!(this instanceof Element) || !this.isConnected) return false;
    const style = window.getComputedStyle(this);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        style.visibility === 'collapse' || Number(style.opacity) === 0) {
        return false;
    }
    const rect = this.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}"""

# 原生交互流程：backendDOMNodeId -> 临时原生定位锚点 -> Playwright Locator
# -> 清理锚点。锚点只用于本次动作，并保留网页原有同名属性以便准确恢复。
_JS_MARK_NATIVE_TARGET = """function() {{
    if (!(this instanceof Element) || !this.isConnected) {{
        throw new Error('ref 对应节点已脱离 DOM');
    }}
    const hadAttribute = this.hasAttribute('data-browser-native-ref');
    const previousValue = this.getAttribute('data-browser-native-ref');
    this.setAttribute('data-browser-native-ref', {token});
    return {{hadAttribute: hadAttribute, previousValue: previousValue}};
}}"""

# console():在页面侧执行、序列化并限制结果大小，避免完整大对象跨 CDP 返回。
# 用户表达式作为字符串注入,在函数体内 eval 执行(保留其作用域语义)。
# 返回的小结果保持原类型；超限结果返回截断后的序列化文本。
_JS_CONSOLE_WRAPPER = """(() => {{
    try {{
        var value = eval({escaped_expr});
        // 函数、undefined 和 Symbol 先转成可说明的文本，避免 JSON.stringify 丢值。
        if (typeof value === 'function' || value === undefined || typeof value === 'symbol') {{
            value = String(value);
        }}
        try {{
            var serialized = JSON.stringify(value, (key, val) => {{
                if (typeof val === 'function' || typeof val === 'symbol') {{
                    return val.toString();
                }}
                // 循环引用会让 JSON.stringify 抛错,落到 catch。
                return val;
            }});
            if (serialized === undefined) {{
                serialized = JSON.stringify('<unserializable>');
            }}
            var originalLength = serialized.length;
            var maxChars = {max_chars};
            if (originalLength > maxChars) {{
                var prefix = maxChars === 1 ? '' : serialized.slice(0, maxChars - 1);
                return {{
                    ok: true,
                    result: prefix + '…',
                    truncated: true,
                    original_length: originalLength,
                    error: null
                }};
            }}
            return {{
                ok: true,
                result: JSON.parse(serialized),
                truncated: false,
                original_length: originalLength,
                error: null
            }};
        }} catch (_) {{
            var fallback = JSON.stringify('<unserializable>');
            return {{
                ok: true,
                result: '<unserializable>',
                truncated: false,
                original_length: fallback.length,
                error: null
            }};
        }}
    }} catch (e) {{
        return {{
            ok: false,
            result: null,
            truncated: false,
            original_length: 0,
            error: String(e && e.message || e)
        }};
    }}
}})()"""


def _js_escape(text: str) -> str:
    """把 Python 字符串转成 JS 字符串字面量(含引号)。"""
    return json.dumps(text, ensure_ascii=False)


def _normalize_url(url: str) -> str:
    """补全命令行常见的裸域名，保留已有协议的地址。"""
    normalized = url.strip()
    if not normalized:
        raise ValueError("URL 不能为空")
    if urlsplit(normalized).scheme:
        return normalized
    if normalized.startswith("//"):
        return f"https:{normalized}"
    return f"https://{normalized}"


def _observation_result(
    snapshot: str,
    snapshot_id: str,
    url: str,
    *,
    page_id: str | None,
    frames: list[dict[str, Any]],
    event_type: str,
    used_fallback: bool,
    dialogs: list[dict[str, str]],
    error_type: str | None = None,
    error: str | None = None,
) -> str:
    """构造成功或可恢复失败共享的完整页面观察结构。"""
    payload: dict[str, Any] = {
        "ok": error_type is None,
        "snapshot_id": snapshot_id,
        "snapshot": snapshot,
        "url": url,
        "page_id": page_id,
        "frames": frames,
        "event_type": event_type,
        "used_fallback": used_fallback,
        "dialogs": dialogs,
    }
    if error_type is not None:
        payload["error_type"] = error_type
        payload["error"] = error or "操作失败"
    return json.dumps(payload, ensure_ascii=False)


def _err(error_type: str, error: str) -> str:
    """失败返回:JSON 字符串 ``{"ok": false, "error_type": ..., "error": ...}``。"""
    return json.dumps(
        {"ok": False, "error_type": error_type, "error": error},
        ensure_ascii=False,
    )


def _as_error_with_observation(result: str, error_type: str, error: str) -> str:
    """保留已有观察字段，把成功或失败结果统一改写为可恢复错误。"""
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return _err(error_type, error)
    payload["ok"] = False
    payload["error_type"] = error_type
    payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)


def _ok_text(text: str, truncated: bool, snapshot_id: str, url: str) -> str:
    """get_text 成功返回:文本 + 截断标记。snapshot_id 与传入相同(纯读取)。"""
    return json.dumps(
        {
            "ok": True,
            "text": text,
            "truncated": truncated,
            "snapshot_id": snapshot_id,
            "url": url,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 按 session_key 复用的单例池。结构照搬 hermes/backends/__init__.py:545-613。
# ---------------------------------------------------------------------------

_sessions: dict[str, BrowserSession] = {}
_sessions_lock = threading.Lock()


def get_session(
    session_key: str = "default",
    *,
    headless: bool = True,
    channel: str | None = "chrome",
    workspace_root: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> BrowserSession:
    """按 session_key 取或建 BrowserSession。

    第一次调用会启动 Chromium;后续调用直接返回缓存实例。启动完成后才发布
    到池中，避免其它线程取得尚未初始化的 session。同步 Playwright 不能
    跨线程复用；跨线程获取同一个 key 会得到明确错误，后续 BrowserWorker
    将负责把调用路由到固定线程。``workspace_root`` 与 ``artifact_dir`` 只在
    首次创建时确定；后续调用省略它们即可复用，提供不同目录会抛出
    ``session_configuration_conflict``，不会静默改写会话的文件授权边界。
    """
    with _sessions_lock:
        s = _sessions.get(session_key)
        if s is not None:
            s._assert_owner_thread()
            if workspace_root is not None:
                requested_workspace = s._resolve_workspace_root(workspace_root)
                if requested_workspace != s.workspace_root:
                    raise ValueError(
                        "session_configuration_conflict: "
                        "session_key 已绑定不同的 workspace_root"
                    )
            if artifact_dir is not None:
                requested_artifact_dir = s._configured_artifact_dir_locked(artifact_dir)
                if requested_artifact_dir != s.artifact_dir:
                    raise ValueError(
                        "session_configuration_conflict: "
                        "session_key 已绑定不同的 artifact_dir"
                    )
            return s
        s = BrowserSession(
            headless=headless,
            channel=channel,
            workspace_root=workspace_root,
            artifact_dir=artifact_dir,
        )
        # 启动与发布必须是一个原子步骤；启动很少发生，值得用全局锁换取
        # 正确性。若 start 抛错，实例不会进入池。
        s.start()
        _sessions[session_key] = s
        return s


def close_session(session_key: str = "default") -> bool:
    """关闭指定 session_key 的 BrowserSession。存在则返回 True。"""
    with _sessions_lock:
        s = _sessions.get(session_key)
    if s is None:
        return False
    s._assert_owner_thread()
    with _sessions_lock:
        _sessions.pop(session_key, None)
    s.close()
    return True


def close_all_sessions() -> None:
    """关闭所有缓存的 session。程序退出时调用。"""
    with _sessions_lock:
        items = list(_sessions.items())
    for _, s in items:
        s._assert_owner_thread()
    with _sessions_lock:
        _sessions.clear()
    for _, s in items:
        s.close()
