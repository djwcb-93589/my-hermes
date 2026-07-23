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

导航(P2,操作后自动返回新快照):
- ``back(snapshot_id)`` -- 回退到上一页
- ``forward(snapshot_id)`` -- 前进到下一页
- ``reload(snapshot_id)`` -- 重新加载当前页
- ``scroll(direction, snapshot_id, amount=400)`` -- 滚动页面(up/down/left/right)

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
意外指向新页面的另一个元素。交互时用 CDP
``DOM.resolveNode`` 把 backendNodeId 转成 remote object,再用
``Runtime.callFunctionOn`` 在该元素上执行 JS(click / fill / 等)。
这比写 CSS selector 更可靠 -- backendDOMNodeId 直接来自浏览器内部,
不受页面结构变化影响。代价:ref 在 snapshot 之间失效,调用方必须重新
取快照才能拿到新 ref(文档原意如此)。

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
import sys
import threading
from typing import Any
from urllib.parse import urlsplit

from browser.accessibility import format_snapshot


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
    ):
        self._headless = headless
        self._timeout_ms = timeout_ms
        # channel="chrome" 用系统装的 Google Chrome,避免下载 Playwright 自带
        # Chromium(~150MB)。传 None 回退到 Playwright 自带 Chromium。
        # 常用值:"chrome"、"msedge"、None。
        self._channel = channel
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
        # 只记录本会话经由公开 API 到达的页面位置，不把浏览器初始的
        # about:blank 当作可回退页面。它用于在真正调用 go_back 前判断
        # 是否有可恢复的位置，避免为了判断历史而破坏当前页面。
        self._history_urls: list[str] = []
        # URL 相同的 history.pushState 位置仍是独立历史项。额外保存位置
        # 标记，避免把这种页面内回退再次误判成没有历史。
        self._history_markers: list[str] = []
        self._history_index = -1

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

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
                self._page = self._context.new_page()
                self._page.set_default_timeout(self._timeout_ms)
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
        self._history_urls.clear()
        self._history_markers.clear()
        self._history_index = -1

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
        if self._page is None:
            raise RuntimeError("BrowserSession 未启动;请用 `with` 或先 start()")

    def _invalidate_snapshot_locked(self) -> None:
        """使当前页面观察结果和 ref 映射失效。"""
        self._ref_to_backend_id.clear()
        self._active_snapshot_id = None

    # --- 读取操作 ---

    def navigate(self, url: str) -> str:
        """打开 URL 并返回带 ``snapshot_id`` 的观察结果 JSON。

        会等待 ``load`` 事件 + 一小段网络空闲,避免拿到 AJAX 半截页面。
        snapshot 内 ref 从 e1 起重新编号。
        """
        with self._lock:
            self._require_started_locked()
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            self._invalidate_snapshot_locked()
            self._page.goto(_normalize_url(url), wait_until="load")
            # 给 AJAX 一点喘息时间;不用 networkidle --对长轮询站点会一直等。
            self._page.wait_for_timeout(500)
            snapshot, snapshot_id = self._snapshot_locked()
            self._record_navigation_locked(previous_url, previous_position)
            return _ok(snapshot, snapshot_id, self._page.url)

    def snapshot(self) -> str:
        """对当前 page 取一次带 ``snapshot_id`` 的观察结果 JSON。"""
        with self._lock:
            self._require_started_locked()
            snapshot, snapshot_id = self._snapshot_locked()
            return _ok(snapshot, snapshot_id, self._page.url)

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
        self._snapshot_counter += 1
        self._active_snapshot_id = f"s{self._snapshot_counter}"
        return format_snapshot(cdp_result), self._active_snapshot_id

    def cookies(self) -> list[dict]:
        """返回当前 context 的 cookie 列表;测试用。"""
        with self._lock:
            self._require_started_locked()
            return self._context.cookies()

    # --- 交互操作(P1) ---
    # 所有交互操作都返回 ``{"ok": True, "snapshot": ...}`` 或
    # ``{"ok": False, "error_type": ..., "error": ...}``。操作成功后
    # 自动取新快照 -- 调用方拿到的就是操作后的页面状态,不用再单独调
    # snapshot。这也强制 ref 在每次交互后刷新,避免用旧 ref 操作失效元素。

    def click(self, ref: str, snapshot_id: str) -> str:
        """点击元素。返回操作后的新快照(JSON 包裹)。"""
        return self._interact(ref, snapshot_id, _JS_CLICK)

    def type(self, ref: str, text: str, snapshot_id: str, clear: bool = True) -> str:
        """在输入框填文字。``clear=True`` 先清空(默认)。返回新快照。"""
        # 用 JS 设置 value 并派发 input 事件 -- 比 Playwright 的 fill
        # 更直接,且能绕开 React 等框架对 value 属性的劫持。
        js = _JS_TYPE_TEMPLATE.format(
            clear_js="true" if clear else "false",
            escaped_text=_js_escape(text),
        )
        return self._interact(ref, snapshot_id, js)

    def press(self, key: str, snapshot_id: str) -> str:
        """按键盘键。``key`` 用 Playwright 键名(如 ``Enter``、``Tab``、
        ``Escape``、``ArrowDown``)。返回新快照。

        press 不针对特定元素 -- 它作用于当前焦点元素。所以不走 ref 解析,
        直接用 Playwright 的 ``page.keyboard.press``。
        """
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            # 按键可能已经被浏览器消费，即使后续报错也不能继续使用旧 ref。
            self._invalidate_snapshot_locked()
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            try:
                self._page.keyboard.press(key)
            except Exception as exc:
                return _err("press_failed", f"按键失败: {exc}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"按键后取快照失败: {exc}")
            return _ok(snapshot, new_snapshot_id, self._page.url)

    def select(self, ref: str, value: str, snapshot_id: str) -> str:
        """下拉选择。``value`` 匹配 ``<option value="...">`` 或 option 文本。
        返回新快照。"""
        js = _JS_SELECT_TEMPLATE.format(escaped_value=_js_escape(value))
        return self._interact(ref, snapshot_id, js)

    def _interact(self, ref: str, snapshot_id: str, js_fn: str) -> str:
        """通用交互流程:解析 ref -> callFunctionOn -> 等待 -> 取新快照。

        所有基于 ref 的交互(click/type/select)共用这条路径。press 不走
        这里因为它不针对特定元素。
        """
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            backend_id = self._ref_to_backend_id.get(ref)
            if backend_id is None:
                return _err(
                    "invalid_ref",
                    f"ref {ref} 无效。ref 在每次 snapshot 之间失效,"
                    "请重新调 navigate 或 snapshot 取新 ref。",
                )
            client = self._context.new_cdp_session(self._page)
            try:
                try:
                    resolved = client.send(
                        "DOM.resolveNode",
                        {"backendNodeId": backend_id},
                    )
                except Exception as exc:
                    return _err("resolve_failed", f"解析 ref 失败: {exc}")
                remote_obj = resolved.get("object", {})
                object_id = remote_obj.get("objectId")
                if not object_id:
                    return _err("resolve_failed", "DOM.resolveNode 未返回 objectId")
                # JS 调用一旦发出就无法判断页面是否已变，先废弃旧观察结果。
                self._invalidate_snapshot_locked()
                previous_url = self._page.url
                previous_position = self._position_marker_locked()
                try:
                    call_result = client.send(
                        "Runtime.callFunctionOn",
                        {
                            "objectId": object_id,
                            "functionDeclaration": js_fn,
                            "returnByValue": True,
                        },
                    )
                except Exception as exc:
                    return _err("interact_failed", f"操作执行失败: {exc}")
            finally:
                client.detach()
            # callFunctionOn 的返回值在 result.result.value。JS 侧约定:
            # 成功返回 null/undefined,失败抛 Error("...")。
            result_val = call_result.get("result", {}).get("result", {})
            if result_val.get("subtype") == "error" or "exceptionDetails" in call_result:
                exc_detail = call_result.get("exceptionDetails", {})
                exc_msg = exc_detail.get("exception", {}).get("description", "未知 JS 错误")
                return _err("interact_failed", f"JS 执行错误: {exc_msg}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"操作后取快照失败: {exc}")
            return _ok(snapshot, new_snapshot_id, self._page.url)

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
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
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
        if not self._history_urls:
            self._history_urls.append(current_url)
            self._history_markers.append(current_position)
            self._history_index = 0
            return
        if current_url == previous_url and current_position == previous_position:
            return
        # 在回退后打开新页面会形成新分支，旧的前进方向不再可用。
        self._history_urls = self._history_urls[: self._history_index + 1]
        self._history_markers = self._history_markers[: self._history_index + 1]
        self._history_urls.append(current_url)
        self._history_markers.append(current_position)
        self._history_index += 1

    # --- 导航操作(P2) ---
    # back / forward / reload / scroll 虽然不针对特定元素，但仍会改变当前
    # 页面。它们也必须携带 snapshot_id，避免晚到请求作用于新页面。

    def back(self, snapshot_id: str) -> str:
        """回退到上一页。没有工具可用的历史时保持当前页面不变。"""
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            if self._history_index <= 0:
                snapshot, snapshot_id = self._snapshot_locked()
                return _err_no_history(snapshot, snapshot_id, self._page.url)
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            self._invalidate_snapshot_locked()
            try:
                self._page.go_back(wait_until="domcontentloaded")
            except Exception as exc:
                return _err("back_failed", f"回退失败: {exc}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                    record_navigation=False,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"回退后取快照失败: {exc}")
            if self._position_marker_locked() == previous_position:
                return _err_no_history(snapshot, new_snapshot_id, self._page.url)
            self._history_index -= 1
            self._history_urls[self._history_index] = self._page.url
            self._history_markers[self._history_index] = self._position_marker_locked()
            return _ok(snapshot, new_snapshot_id, self._page.url)

    def forward(self, snapshot_id: str) -> str:
        """前进到下一页。没有历史时返回 ``no_history`` 错误,页面状态不变。"""
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            if self._history_index >= len(self._history_urls) - 1:
                snapshot, snapshot_id = self._snapshot_locked()
                return _err_no_history(snapshot, snapshot_id, self._page.url)
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            self._invalidate_snapshot_locked()
            try:
                self._page.go_forward(wait_until="domcontentloaded")
            except Exception as exc:
                return _err("forward_failed", f"前进失败: {exc}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                    record_navigation=False,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"前进后取快照失败: {exc}")
            if self._position_marker_locked() == previous_position:
                return _err_no_history(snapshot, new_snapshot_id, self._page.url)
            self._history_index += 1
            self._history_urls[self._history_index] = self._page.url
            self._history_markers[self._history_index] = self._position_marker_locked()
            return _ok(snapshot, new_snapshot_id, self._page.url)

    def reload(self, snapshot_id: str) -> str:
        """重新加载当前页。返回新快照。"""
        with self._lock:
            self._require_started_locked()
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            self._invalidate_snapshot_locked()
            try:
                self._page.reload(wait_until="load")
            except Exception as exc:
                return _err("reload_failed", f"刷新失败: {exc}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                    record_navigation=False,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"刷新后取快照失败: {exc}")
            return _ok(snapshot, new_snapshot_id, self._page.url)

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
            stale_error = self._validate_snapshot_locked(snapshot_id)
            if stale_error is not None:
                return stale_error
            previous_url = self._page.url
            previous_position = self._position_marker_locked()
            # 废弃旧观察:虽然滚动不改 URL,懒加载可能让新元素出现,
            # 旧 backendDOMNodeId 可能已失效。
            self._invalidate_snapshot_locked()
            try:
                self._page.evaluate(
                    f"window.scrollBy({dx}, {dy})"
                )
            except Exception as exc:
                return _err("scroll_failed", f"滚动失败: {exc}")
            try:
                snapshot, new_snapshot_id = self._observe_after_action_locked(
                    previous_url,
                    previous_position=previous_position,
                    record_navigation=False,
                )
            except Exception as exc:
                return _err("snapshot_failed", f"滚动后取快照失败: {exc}")
            return _ok(snapshot, new_snapshot_id, self._page.url)


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

# click():检查禁用状态、滚动并聚焦后点击。如果元素是 <a href>,会触发导航。
_JS_CLICK = """function() {
    if (!(this instanceof HTMLElement)) {
        throw new Error('ref 指向的不是 HTML 元素');
    }
    if (this.matches(':disabled') || this.getAttribute('aria-disabled') === 'true') {
        throw new Error('目标元素已禁用');
    }
    this.scrollIntoView({block: 'center', inline: 'center'});
    this.focus({preventScroll: true});
    this.click();
    return null;
}"""

# type():先 focus(保证后续 press 作用于该元素),再清空(可选),设 value,
# 派发 input 事件让框架感知。
# React 等框架会劫持 value 属性,必须用 Object.getOwnPropertyDescriptor
# 拿原生 setter 才能真正写入。
_JS_TYPE_TEMPLATE = """function() {{
    if (!(this instanceof HTMLElement)) {{
        throw new Error('ref 指向的不是 HTML 元素');
    }}
    this.scrollIntoView({{block: 'center', inline: 'center'}});
    this.focus({{preventScroll: true}});
    var clear = {clear_js};
    var text = {escaped_text};
    if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement) {{
        var prototype = this instanceof HTMLTextAreaElement
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        var setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
        setter.call(this, clear ? text : this.value + text);
    }} else if (this.isContentEditable) {{
        this.textContent = clear ? text : this.textContent + text;
    }} else {{
        throw new Error('ref 指向的不是输入框或 contenteditable 元素');
    }}
    this.dispatchEvent(new Event('input', {{ bubbles: true }}));
    this.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return null;
}}"""

# select():在 <select> 上找匹配的 option(value 或 text),设为 selected,
# 派发 change 事件。
_JS_SELECT_TEMPLATE = """function() {{
    if (this.tagName !== 'SELECT') {{
        throw new Error('ref 指向的不是 <select>,而是 ' + this.tagName);
    }}
    var target = {escaped_value};
    var matched = null;
    for (var i = 0; i < this.options.length; i++) {{
        var opt = this.options[i];
        if (opt.value === target || opt.text === target) {{
            matched = opt;
            break;
        }}
    }}
    if (!matched) {{
        throw new Error('未找到匹配的 option: ' + target);
    }}
    this.value = matched.value;
    this.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return null;
}}"""


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


def _ok(snapshot: str, snapshot_id: str, url: str) -> str:
    """成功返回含新 ``snapshot_id`` 的 JSON 字符串。"""
    return json.dumps(
        {"ok": True, "snapshot_id": snapshot_id, "snapshot": snapshot, "url": url},
        ensure_ascii=False,
    )


def _err(error_type: str, error: str) -> str:
    """失败返回:JSON 字符串 ``{"ok": false, "error_type": ..., "error": ...}``。"""
    return json.dumps(
        {"ok": False, "error_type": error_type, "error": error},
        ensure_ascii=False,
    )


def _err_no_history(snapshot: str, snapshot_id: str, url: str) -> str:
    """无历史错误:带新观察结果,让 agent 能继续操作当前页面。

    back/forward 在无历史时返回这个 -- 页面从未被离开，agent 拿到新的
    snapshot_id 可以继续交互，不需要重新调 snapshot。
    """
    return json.dumps(
        {
            "ok": False,
            "error_type": "no_history",
            "error": "没有浏览历史可回退/前进",
            "snapshot_id": snapshot_id,
            "snapshot": snapshot,
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
) -> BrowserSession:
    """按 session_key 取或建 BrowserSession。

    第一次调用会启动 Chromium;后续调用直接返回缓存实例。启动完成后才发布
    到池中，避免其它线程取得尚未初始化的 session。同步 Playwright 不能
    跨线程复用；跨线程获取同一个 key 会得到明确错误，后续 BrowserWorker
    将负责把调用路由到固定线程。
    """
    with _sessions_lock:
        s = _sessions.get(session_key)
        if s is not None:
            s._assert_owner_thread()
            return s
        s = BrowserSession(headless=headless, channel=channel)
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
