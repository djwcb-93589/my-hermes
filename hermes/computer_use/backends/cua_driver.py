"""基于 cua-driver 的 P5 基础操作 Backend。"""

import base64
import binascii
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from ..backend import ComputerUseBackend
from ..contracts import (
    ActionEffect,
    ActionResult,
    AppInfo,
    CaptureMode,
    CaptureResult,
    ComputerUseAction,
    DeliveryMode,
    EscalationHint,
    UIElement,
    WindowInfo,
)
from ..errors import (
    BackendUnavailableError,
    ComputerUseError,
    InvalidArgumentsError,
    ProtocolError,
    StaleElementError,
    TargetNotFoundError,
)
from ..transport import CuaDriverClient, CuaDriverConfig


_REQUIRED_OBSERVATION_TOOLS = frozenset(
    {"list_apps", "list_windows", "wait"}
)
_REQUIRED_P5_ACTION_TOOLS = frozenset(
    {"click", "double_click", "type_text", "press_key", "hotkey"}
)
_CAPTURE_TOOLS = ("get_window_state", "screenshot")
_KEY_ALIASES = {
    "command": "cmd",
    "control": "ctrl",
    "alt": "option",
}
_MODIFIER_KEYS = frozenset({"cmd", "ctrl", "shift", "option", "fn"})
_LARGE_FIELD_MARKERS = (
    "base64",
    "image",
    "screenshot",
    "content",
    "elements",
)


class CuaDriverBackend(ComputerUseBackend):
    """将 cua-driver 的观察和基础操作转换为 Computer Use 正式契约。"""

    def __init__(
        self,
        config: CuaDriverConfig,
        *,
        client: CuaDriverClient | None = None,
    ) -> None:
        """创建独立 transport，并允许注入客户端。"""

        super().__init__()
        self._client = client or CuaDriverClient(config)
        self._session_id = f"myhermes-{uuid.uuid4().hex[:12]}"
        self._session_enabled = False
        self._active_pid: int | None = None
        self._active_window_id: int | None = None
        self._active_app = ""
        self._element_targets: dict[int, tuple[int, str | None]] = {}
        self._tool_names: set[str] = set()
        self._tool_argument_names: dict[str, set[str]] = {}
        self._capture_tool: str | None = None

    def _start(self) -> None:
        """启动 transport、发现工具并尝试建立独立 session。"""

        self._client.start()
        try:
            tools = self._client.list_tools()
            self._tool_names.clear()
            self._tool_argument_names.clear()
            for tool in tools:
                name = tool.get("name")
                if not isinstance(name, str) or not name:
                    continue
                self._tool_names.add(name)
                argument_names: set[str] = set()
                input_schema = tool.get("inputSchema")
                if isinstance(input_schema, Mapping):
                    properties = input_schema.get("properties")
                    if isinstance(properties, Mapping):
                        argument_names = {
                            key
                            for key in properties
                            if isinstance(key, str)
                        }
                self._tool_argument_names[name] = argument_names
            self._capture_tool = next(
                (
                    name
                    for name in _CAPTURE_TOOLS
                    if name in self._tool_names
                ),
                None,
            )

            missing = sorted(
                _REQUIRED_OBSERVATION_TOOLS - self._tool_names
            )
            if self._capture_tool is None:
                missing.extend(_CAPTURE_TOOLS)
            if missing:
                raise BackendUnavailableError(
                    "cua-driver does not provide required observation tools.",
                    details={
                        "reason": "missing_required_tools",
                        "tools": missing,
                    },
                )
            missing_actions = sorted(
                _REQUIRED_P5_ACTION_TOOLS - self._tool_names
            )
            if missing_actions:
                raise BackendUnavailableError(
                    "cua-driver does not provide required P5 action tools.",
                    details={
                        "reason": "missing_required_tools",
                        "tools": missing_actions,
                    },
                )
            self._try_start_session()
        except Exception:
            try:
                self._client.stop()
            except Exception:
                pass
            self._clear_runtime_state()
            raise

    def _stop(self) -> None:
        """尽力关闭 session，再停止 transport 并清空运行状态。"""

        try:
            if (
                self._session_enabled
                and "end_session" in self._tool_names
            ):
                try:
                    self._call_driver_tool("end_session")
                except Exception:
                    pass
        finally:
            try:
                self._client.stop()
            finally:
                self._clear_runtime_state()

    def is_available(self) -> bool:
        """仅在 Backend 已启动且 transport 存活时返回可用。"""

        return self.state == "started" and self._client.is_alive()

    def _capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """选择目标窗口并转换驱动捕获结果。"""

        self._clear_active_target()
        windows = self._list_windows()
        target = self._select_target_window(
            windows,
            app=app,
            pid=pid,
            window_id=window_id,
        )
        capture_tool = self._capture_tool
        if capture_tool is None:
            raise BackendUnavailableError(
                "cua-driver capture tool is unavailable.",
                details={"reason": "missing_capture_tool"},
            )

        capture_arguments = {
            "window_id": target.window_id,
        }
        if capture_tool == "get_window_state":
            capture_arguments = {
                "pid": target.pid,
                "window_id": target.window_id,
            }
        raw_result = self._call_driver_tool(
            capture_tool,
            capture_arguments,
        )
        payload = self._extract_tool_payload(
            raw_result,
            allow_image_only=True,
        )
        image_bytes, mime_type, width, height = self._parse_image(
            payload,
            raw_result,
        )
        elements = self._parse_elements(
            payload,
            target=target,
            mode=mode,
            max_elements=max_elements,
        )
        result = CaptureResult(
            mode=mode,
            width=width,
            height=height,
            image_bytes=image_bytes,
            mime_type=mime_type,
            elements=elements,
            app=target.app,
            window_title=target.title,
        )
        element_targets: dict[int, tuple[int, str | None]] = {}
        for captured_element in result.elements:
            driver_index = captured_element.attributes.get(
                "driver_element_index"
            )
            if type(driver_index) is not int or driver_index <= 0:
                driver_index = captured_element.index
            element_targets[captured_element.index] = (
                driver_index,
                captured_element.element_token,
            )
        self._active_pid = target.pid
        self._active_window_id = target.window_id
        self._active_app = target.app
        self._element_targets = element_targets
        return result

    def _list_apps(self) -> list[AppInfo]:
        """调用驱动并转换应用列表。"""

        raw_result = self._call_driver_tool("list_apps")
        payload = self._extract_tool_payload(raw_result)
        items = self._require_list(
            payload,
            ("apps", "applications", "result"),
            result_name="application list",
        )

        apps: list[AppInfo] = []
        excluded = {
            "name",
            "app_name",
            "pid",
            "identifier",
            "bundle_id",
            "executable",
            "window_count",
        }
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name_value = self._first_present(item, "name", "app_name")
            pid_value = item.get("pid")
            if name_value is None or type(pid_value) is not int:
                continue

            identifier_value = self._first_present(
                item,
                "identifier",
                "bundle_id",
                "executable",
            )
            window_count_value = item.get("window_count")
            window_count = (
                window_count_value
                if type(window_count_value) is int
                and window_count_value >= 0
                else 0
            )
            apps.append(
                AppInfo(
                    name=str(name_value),
                    pid=pid_value,
                    identifier=(
                        str(identifier_value)
                        if identifier_value is not None
                        else ""
                    ),
                    window_count=window_count,
                    metadata=self._small_metadata(item, excluded),
                )
            )
        return apps

    def _list_windows(self) -> list[WindowInfo]:
        """调用驱动并转换窗口列表。"""

        raw_result = self._call_driver_tool(
            "list_windows",
            {"on_screen_only": True},
        )
        payload = self._extract_tool_payload(raw_result)
        items = self._require_list(
            payload,
            ("windows", "result"),
            result_name="window list",
        )

        windows: list[WindowInfo] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            window_id = self._first_present(item, "window_id", "id")
            pid = item.get("pid")
            if type(window_id) is not int or type(pid) is not int:
                continue

            title = self._string_value(item.get("title"))
            app = self._string_value(
                self._first_present(
                    item,
                    "app",
                    "app_name",
                    "owner_name",
                )
            )
            visible_value = self._first_present(
                item,
                "is_visible",
                "visible",
            )
            is_on_screen = item.get("is_on_screen")
            is_visible = (
                is_on_screen
                if isinstance(is_on_screen, bool)
                else (
                    visible_value
                    if isinstance(visible_value, bool)
                    else True
                )
            )
            metadata = {
                key: item[key]
                for key in (
                    "z_index",
                    "minimized",
                    "focused",
                )
                if key in item and self._is_small_value(item[key])
            }
            off_screen = item.get("off_screen")
            if isinstance(off_screen, bool):
                metadata["off_screen"] = off_screen
                if isinstance(is_on_screen, bool):
                    is_visible = not off_screen
            elif isinstance(is_on_screen, bool):
                metadata["off_screen"] = not is_on_screen
            bounds_source = item.get("bounds", item)
            windows.append(
                WindowInfo(
                    window_id=window_id,
                    title=title,
                    app=app,
                    pid=pid,
                    bounds=self._parse_bounds(bounds_source),
                    is_visible=is_visible,
                    metadata=metadata,
                )
            )
        return windows

    def _wait(self, seconds: float) -> ActionResult:
        """通过驱动执行受限等待并转换正式动作结果。"""

        raw_result = self._call_driver_tool(
            "wait",
            {"seconds": seconds},
        )
        payload = self._extract_tool_payload(raw_result)

        verified_value = payload.get("verified")
        verified = (
            verified_value
            if isinstance(verified_value, bool)
            else True
        )
        effect = (
            self._parse_effect(payload["effect"])
            if "effect" in payload
            else ActionEffect.CONFIRMED
        )
        path_value = payload.get("path")
        degraded_value = payload.get("degraded")
        code_value = payload.get("code")
        message_value = payload.get("message")
        return ActionResult(
            ok=True,
            action=ComputerUseAction.WAIT,
            message=(
                message_value
                if isinstance(message_value, str)
                else f"cua-driver waited for {seconds:g} seconds."
            ),
            verified=verified,
            effect=effect,
            path=(
                path_value
                if isinstance(path_value, str)
                else "cua-driver"
            ),
            degraded=(
                degraded_value
                if isinstance(degraded_value, bool)
                else None
            ),
            code=code_value if isinstance(code_value, str) else None,
        )

    def _click(
        self,
        element: int | None = None,
        coordinate: tuple[int, int] | None = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Sequence[str] | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """通过活动窗口执行元素或坐标点击。"""

        has_element = element is not None
        has_coordinate = coordinate is not None
        if has_element == has_coordinate:
            raise InvalidArgumentsError(
                "Exactly one click target must be provided.",
                details={"reason": "exactly_one_click_target_required"},
            )
        if has_element and (
            type(element) is not int or element <= 0
        ):
            raise InvalidArgumentsError(
                "element must be a positive integer.",
                details={"reason": "invalid_element"},
            )
        if has_coordinate and (
            not isinstance(coordinate, tuple)
            or len(coordinate) != 2
            or any(type(value) is not int for value in coordinate)
        ):
            raise InvalidArgumentsError(
                "coordinate must contain exactly two integers.",
                details={"reason": "invalid_coordinate"},
            )
        if not isinstance(button, str):
            raise InvalidArgumentsError(
                "button must be left, right, or middle.",
                details={"reason": "invalid_button"},
            )
        normalized_button = button.casefold()
        if normalized_button not in {"left", "right", "middle"}:
            raise InvalidArgumentsError(
                "button must be left, right, or middle.",
                details={"reason": "invalid_button"},
            )
        if type(click_count) is not int or click_count not in {1, 2}:
            raise InvalidArgumentsError(
                "click_count must be 1 or 2.",
                details={"reason": "invalid_click_count"},
            )
        if click_count == 2 and normalized_button != "left":
            raise InvalidArgumentsError(
                "Double click only supports the left button.",
                details={"reason": "invalid_double_click_button"},
            )

        if normalized_button == "right":
            action = ComputerUseAction.RIGHT_CLICK
        elif normalized_button == "middle":
            action = ComputerUseAction.MIDDLE_CLICK
        elif click_count == 2:
            action = ComputerUseAction.DOUBLE_CLICK
        else:
            action = ComputerUseAction.CLICK

        pid, window_id = self._require_active_target()
        tool_name = "double_click" if click_count == 2 else "click"
        arguments: dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
        }
        if element is not None:
            target = self._element_targets.get(element)
            if target is None:
                raise StaleElementError(
                    "The element does not belong to the latest capture.",
                    details={"reason": "stale_element"},
                )
            driver_index, element_token = target
            arguments["element_index"] = driver_index
            if (
                element_token is not None
                and "element_token"
                in self._tool_argument_names.get(tool_name, set())
            ):
                arguments["element_token"] = element_token
        else:
            if coordinate is None:
                raise InvalidArgumentsError(
                    "coordinate is required for coordinate clicking.",
                    details={"reason": "invalid_coordinate"},
                )
            arguments["x"] = coordinate[0]
            arguments["y"] = coordinate[1]

        if normalized_button != "left":
            arguments["button"] = normalized_button
        if modifiers is not None:
            if (
                isinstance(modifiers, (str, bytes))
                or any(
                    not isinstance(modifier, str) or not modifier
                    for modifier in modifiers
                )
            ):
                raise InvalidArgumentsError(
                    "modifiers must contain non-empty strings.",
                    details={"reason": "invalid_modifiers"},
                )
            arguments["modifier"] = list(modifiers)

        unsupported = self._apply_delivery_options(
            tool_name,
            arguments,
            action=action,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
        )
        if unsupported is not None:
            return unsupported

        raw_result = self._call_driver_tool(tool_name, arguments)
        result = self._parse_action_result(
            raw_result,
            action=action,
            requested_delivery=delivery_mode,
        )
        return self._apply_capture_after(result, capture_after)

    def _drag(
        self,
        from_element: int | None = None,
        to_element: int | None = None,
        from_coordinate: tuple[int, int] | None = None,
        to_coordinate: tuple[int, int] | None = None,
        button: str = "left",
        modifiers: Sequence[str] | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """拒绝 P5 尚未实现的拖动动作。"""

        self._raise_action_unavailable(ComputerUseAction.DRAG)

    def _scroll(
        self,
        direction: str,
        amount: int,
        element: int | None = None,
        coordinate: tuple[int, int] | None = None,
        modifiers: Sequence[str] | None = None,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """拒绝 P5 尚未实现的滚动动作。"""

        self._raise_action_unavailable(ComputerUseAction.SCROLL)

    def _type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """通过活动窗口调用驱动文本输入工具。"""

        if not isinstance(text, str):
            raise InvalidArgumentsError(
                "text must be a string.",
                details={"reason": "invalid_text"},
            )
        pid, window_id = self._require_active_target()
        arguments: dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "text": text,
        }
        unsupported = self._apply_delivery_options(
            "type_text",
            arguments,
            action=ComputerUseAction.TYPE,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
        )
        if unsupported is not None:
            return unsupported

        raw_result = self._call_driver_tool("type_text", arguments)
        result = self._parse_action_result(
            raw_result,
            action=ComputerUseAction.TYPE,
            requested_delivery=delivery_mode,
        )
        return self._apply_capture_after(result, capture_after)

    def _key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """通过活动窗口调用单键或组合键工具。"""

        if isinstance(keys, (str, bytes)) or not keys:
            raise InvalidArgumentsError(
                "keys must contain at least one key.",
                details={"reason": "invalid_keys"},
            )

        normalized_keys: list[str] = []
        for key_value in keys:
            if not isinstance(key_value, str) or not key_value.strip():
                raise InvalidArgumentsError(
                    "keys must contain non-empty strings.",
                    details={"reason": "invalid_keys"},
                )
            normalized = key_value.strip().casefold()
            normalized_keys.append(
                _KEY_ALIASES.get(normalized, normalized)
            )
        if len(set(normalized_keys)) != len(normalized_keys):
            raise InvalidArgumentsError(
                "keys must not contain duplicates.",
                details={"reason": "duplicate_keys"},
            )

        ordinary_keys = [
            key
            for key in normalized_keys
            if key not in _MODIFIER_KEYS
        ]
        if len(ordinary_keys) != 1:
            raise InvalidArgumentsError(
                "keys must contain exactly one non-modifier key.",
                details={"reason": "invalid_key_combination"},
            )

        pid, window_id = self._require_active_target()
        if len(normalized_keys) == 1:
            tool_name = "press_key"
            arguments: dict[str, Any] = {
                "pid": pid,
                "window_id": window_id,
                "key": ordinary_keys[0],
            }
        else:
            tool_name = "hotkey"
            arguments = {
                "pid": pid,
                "window_id": window_id,
                "keys": normalized_keys,
            }

        unsupported = self._apply_delivery_options(
            tool_name,
            arguments,
            action=ComputerUseAction.KEY,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
        )
        if unsupported is not None:
            return unsupported

        raw_result = self._call_driver_tool(tool_name, arguments)
        result = self._parse_action_result(
            raw_result,
            action=ComputerUseAction.KEY,
            requested_delivery=delivery_mode,
        )
        return self._apply_capture_after(result, capture_after)

    def _focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """仅选择应用窗口，不改变真实桌面焦点。"""

        self._clear_active_target()
        if not isinstance(app, str) or not app:
            raise InvalidArgumentsError(
                "app must be a non-empty string.",
                details={"reason": "invalid_app"},
            )
        normalized_app = app.casefold()
        candidates = [
            window
            for window in self._list_windows()
            if window.is_visible
            and window.app.casefold() == normalized_app
        ]
        if not candidates:
            raise TargetNotFoundError(
                "No visible window was found for the requested app.",
                details={"reason": "target_window_not_found"},
            )

        target = self._prefer_highest_z_index(candidates)
        self._active_pid = target.pid
        self._active_window_id = target.window_id
        self._active_app = target.app
        return ActionResult(
            ok=True,
            action=ComputerUseAction.FOCUS_APP,
            message=(
                "Selected the target window without bringing it to front."
                if raise_window
                else "Selected the target window for later actions."
            ),
            verified=True,
            effect=ActionEffect.CONFIRMED,
            path="local-target-selection",
            degraded=True if raise_window else None,
            code="raise_window_not_applied" if raise_window else None,
        )

    def _set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """拒绝 P5 尚未实现的元素值设置。"""

        self._raise_action_unavailable(ComputerUseAction.SET_VALUE)

    def _try_start_session(self) -> None:
        """尽力启动独立 session，失败时降级为匿名调用。"""

        self._session_enabled = False
        if "start_session" not in self._tool_names:
            return
        try:
            raw_result = self._client.call_tool(
                "start_session",
                {"session": self._session_id},
            )
            payload = self._extract_action_payload(raw_result)
            if (
                raw_result.get("isError") is True
                or self._driver_reported_failure(payload)
            ):
                return
        except Exception:
            return
        self._session_enabled = True

    def _clear_runtime_state(self) -> None:
        """清空 transport 会话关联的所有运行状态。"""

        self._session_enabled = False
        self._clear_active_target()
        self._tool_names.clear()
        self._tool_argument_names.clear()
        self._capture_tool = None

    def _clear_active_target(self) -> None:
        """清空活动窗口及其最近一次元素映射。"""

        self._active_pid = None
        self._active_window_id = None
        self._active_app = ""
        self._element_targets.clear()

    def _call_driver_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """复制参数，并在启用时注入当前 session。"""

        call_arguments = dict(arguments or {})
        if self._session_enabled:
            call_arguments["session"] = self._session_id
        return self._client.call_tool(name, call_arguments)

    def _require_active_target(self) -> tuple[int, int]:
        """返回活动窗口，缺失时拒绝写操作。"""

        if (
            self._active_pid is None
            or self._active_window_id is None
        ):
            raise BackendUnavailableError(
                "No active window is selected.",
                details={"reason": "active_window_required"},
            )
        return self._active_pid, self._active_window_id

    def _apply_delivery_options(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        action: ComputerUseAction,
        delivery_mode: DeliveryMode,
        bring_to_front: bool,
    ) -> ActionResult | None:
        """按工具参数能力添加前台投递选项。"""

        argument_names = self._tool_argument_names.get(
            tool_name,
            set(),
        )
        if delivery_mode == DeliveryMode.FOREGROUND:
            if "delivery_mode" not in argument_names:
                return ActionResult(
                    ok=False,
                    action=action,
                    message=(
                        "cua-driver does not support foreground delivery "
                        "for this action."
                    ),
                    delivery_mode=DeliveryMode.FOREGROUND,
                    code="foreground_unsupported",
                )
            arguments["delivery_mode"] = "foreground"
        if bring_to_front:
            if "bring_to_front" not in argument_names:
                return ActionResult(
                    ok=False,
                    action=action,
                    message=(
                        "cua-driver does not support bring_to_front "
                        "for this action."
                    ),
                    delivery_mode=delivery_mode,
                    code="bring_to_front_unsupported",
                )
            arguments["bring_to_front"] = True
        return None

    def _parse_action_result(
        self,
        raw_result: Mapping[str, Any],
        *,
        action: ComputerUseAction,
        requested_delivery: DeliveryMode,
    ) -> ActionResult:
        """将驱动动作结果转换为正式 ActionResult。"""

        try:
            payload = self._extract_action_payload(raw_result)
        except ProtocolError:
            if raw_result.get("isError") is not True:
                raise
            payload = {}

        ok = not (
            raw_result.get("isError") is True
            or self._driver_reported_failure(payload)
        )
        verified_value = payload.get("verified")
        verified = (
            verified_value
            if isinstance(verified_value, bool)
            else None
        )
        effect = (
            self._parse_effect(payload["effect"])
            if "effect" in payload
            else None
        )

        escalation: EscalationHint | None = None
        escalation_value = payload.get("escalation")
        if isinstance(escalation_value, Mapping):
            recommended = escalation_value.get("recommended")
            reason = escalation_value.get("reason")
            if isinstance(recommended, str) and isinstance(reason, str):
                escalation = EscalationHint(
                    recommended=recommended,
                    reason=reason,
                )

        code: str | None = None
        for code_key in ("code", "reason_code"):
            code_value = payload.get(code_key)
            if isinstance(code_value, str):
                code = code_value
                break

        message_value = payload.get("message")
        path_value = payload.get("path")
        degraded_value = payload.get("degraded")
        excluded = {
            "ok",
            "success",
            "isError",
            "message",
            "verified",
            "effect",
            "escalation",
            "path",
            "degraded",
            "code",
            "reason_code",
            "error",
            "data",
            "delivery_mode",
        }
        return ActionResult(
            ok=ok,
            action=action,
            message=(
                message_value
                if isinstance(message_value, str)
                else (
                    "cua-driver action completed."
                    if ok
                    else "cua-driver action failed."
                )
            ),
            meta=self._small_metadata(payload, excluded),
            verified=verified,
            effect=effect,
            escalation=escalation,
            path=path_value if isinstance(path_value, str) else None,
            degraded=(
                degraded_value
                if isinstance(degraded_value, bool)
                else None
            ),
            delivery_mode=requested_delivery,
            code=code,
        )

    def _extract_action_payload(
        self,
        raw_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        """提取动作结果，但不把驱动失败转换为异常。"""

        structured = raw_result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise ProtocolError(
                    "cua-driver returned invalid structuredContent.",
                    details={"reason": "invalid_structured_content"},
                )
            return dict(structured)

        content = raw_result.get("content")
        plain_text: str | None = None
        if isinstance(content, list):
            for item in content:
                if (
                    not isinstance(item, Mapping)
                    or item.get("type") != "text"
                ):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    if plain_text is None:
                        plain_text = text
                    continue
                if isinstance(decoded, Mapping):
                    return dict(decoded)
                if plain_text is None:
                    plain_text = text

        if plain_text is not None:
            return {"message": plain_text}

        if raw_result.get("isError") is True:
            return {}
        raise ProtocolError(
            "cua-driver action result has no recognized structured data.",
            details={"reason": "unrecognized_action_result"},
        )

    def _apply_capture_after(
        self,
        result: ActionResult,
        requested: bool,
    ) -> ActionResult:
        """在成功动作后捕获当前活动窗口，失败时仅标记降级。"""

        if not requested or not result.ok:
            return result

        pid = self._active_pid
        window_id = self._active_window_id
        try:
            result.capture = self._capture(
                pid=pid,
                window_id=window_id,
            )
        except ComputerUseError as exc:
            result.capture = None
            result.degraded = True
            result.meta = dict(result.meta)
            result.meta["capture_after_error_code"] = exc.code
        except Exception:
            result.capture = None
            result.degraded = True
            result.meta = dict(result.meta)
            result.meta["capture_after_error_code"] = (
                "capture_after_failed"
            )
        return result

    def _extract_tool_payload(
        self,
        raw_result: Mapping[str, Any],
        *,
        allow_image_only: bool = False,
    ) -> dict[str, Any]:
        """从 structuredContent 或 JSON 文本提取结构化结果。"""

        if raw_result.get("isError") is True:
            raise ComputerUseError(
                "cua-driver tool call failed.",
                details={"reason": "mcp_tool_error"},
            )

        structured = raw_result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise ProtocolError(
                    "cua-driver returned invalid structuredContent.",
                    details={"reason": "invalid_structured_content"},
                )
            payload = dict(structured)
            self._raise_if_driver_failed(payload)
            return payload

        content = raw_result.get("content")
        if not isinstance(content, list):
            raise ProtocolError(
                "cua-driver tool result has no recognized content.",
                details={"reason": "missing_tool_content"},
            )

        has_image = False
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "image":
                has_image = True
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, Mapping):
                payload = dict(decoded)
                self._raise_if_driver_failed(payload)
                return payload

        if allow_image_only and has_image:
            return {}
        raise ProtocolError(
            "cua-driver tool result has no recognized structured data.",
            details={"reason": "unrecognized_tool_content"},
        )

    def _raise_if_driver_failed(
        self,
        payload: Mapping[str, Any],
    ) -> None:
        """将驱动明确报告的失败转换为统一异常。"""

        if not self._driver_reported_failure(payload):
            return

        details: dict[str, Any] = {
            "reason": "driver_reported_failure"
        }
        code = payload.get("code")
        if isinstance(code, (str, int)) and not isinstance(code, bool):
            details["driver_code"] = code
        raise ComputerUseError(
            "cua-driver reported an operation failure.",
            details=details,
        )

    @staticmethod
    def _driver_reported_failure(
        payload: Mapping[str, Any],
    ) -> bool:
        """判断驱动结果是否明确表示失败。"""

        status = payload.get("status")
        has_error = (
            "error" in payload
            and payload.get("error") not in (None, False, "")
        )
        return (
            payload.get("ok") is False
            or payload.get("success") is False
            or payload.get("isError") is True
            or (
                isinstance(status, str)
                and status.casefold() in {"error", "failed", "failure"}
            )
            or has_error
        )

    def _require_list(
        self,
        payload: Mapping[str, Any],
        keys: Sequence[str],
        *,
        result_name: str,
    ) -> list[Any]:
        """从已识别字段提取列表，否则抛出协议错误。"""

        for key in keys:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                return value
            raise ProtocolError(
                f"cua-driver returned an invalid {result_name}.",
                details={"reason": "invalid_list_result"},
            )
        raise ProtocolError(
            f"cua-driver did not return the requested {result_name}.",
            details={"reason": "missing_list_result"},
        )

    def _select_target_window(
        self,
        windows: Sequence[WindowInfo],
        *,
        app: str | None,
        pid: int | None,
        window_id: int | None,
    ) -> WindowInfo:
        """按固定优先级选择唯一目标窗口。"""

        if window_id is not None:
            candidates = [
                window
                for window in windows
                if window.window_id == window_id
            ]
        elif pid is not None:
            candidates = [
                window for window in windows if window.pid == pid
            ]
        elif app is not None:
            normalized_app = app.casefold()
            candidates = [
                window
                for window in windows
                if window.app.casefold() == normalized_app
            ]
        else:
            candidates = [
                window
                for window in windows
                if window.is_visible
                and window.metadata.get("off_screen") is not True
            ]
            if not candidates:
                candidates = list(windows)

        if not candidates:
            raise TargetNotFoundError(
                "No matching cua-driver window was found.",
                details={"reason": "target_window_not_found"},
            )
        return self._prefer_highest_z_index(candidates)

    @staticmethod
    def _prefer_highest_z_index(
        windows: Sequence[WindowInfo],
    ) -> WindowInfo:
        """优先选择具有最大 z_index 的候选窗口。"""

        selected: WindowInfo | None = None
        selected_z: int | float | None = None
        for window in windows:
            z_index = window.metadata.get("z_index")
            if (
                not isinstance(z_index, (int, float))
                or isinstance(z_index, bool)
                or not math.isfinite(float(z_index))
            ):
                continue
            if selected_z is None or z_index > selected_z:
                selected = window
                selected_z = z_index
        return selected if selected is not None else windows[0]

    def _parse_image(
        self,
        payload: Mapping[str, Any],
        raw_result: Mapping[str, Any],
    ) -> tuple[bytes | None, str | None, int, int]:
        """解析结构化图片或 MCP image content。"""

        nested_candidates: list[Mapping[str, Any]] = []
        for key in ("image", "screenshot"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                nested_candidates.append(value)
        candidates: list[Mapping[str, Any]] = []
        if self._looks_like_image(payload):
            candidates.append(payload)
        candidates.extend(nested_candidates)

        content = raw_result.get("content")
        if isinstance(content, list):
            candidates.extend(
                item
                for item in content
                if isinstance(item, Mapping)
                and item.get("type") == "image"
            )

        selected: Mapping[str, Any] | None = None
        encoded: str | None = None
        for candidate in candidates:
            for key in (
                "screenshot_png_b64",
                "png_b64",
                "image_base64",
                "imageBase64",
                "base64",
                "image",
                "screenshot",
            ):
                if key not in candidate:
                    continue
                value = candidate[key]
                if not isinstance(value, str):
                    if (
                        key in {"image", "screenshot"}
                        and isinstance(value, Mapping)
                    ):
                        continue
                    raise ProtocolError(
                        "cua-driver returned invalid image data.",
                        details={"reason": "invalid_image_data"},
                    )
                encoded = value
                selected = candidate
                break
            if (
                encoded is None
                and candidate.get("type") == "image"
                and "data" in candidate
            ):
                value = candidate["data"]
                if not isinstance(value, str):
                    raise ProtocolError(
                        "cua-driver returned invalid image data.",
                        details={"reason": "invalid_image_data"},
                    )
                encoded = value
                selected = candidate
            if encoded is not None:
                break

        sources = (
            [selected, payload]
            if selected is not None
            else [payload]
        )
        width = self._parse_dimension(sources, "width")
        height = self._parse_dimension(sources, "height")
        if encoded is None:
            return (
                None,
                None,
                width if width is not None else 0,
                height if height is not None else 0,
            )

        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProtocolError(
                "cua-driver returned invalid Base64 image data.",
                details={"reason": "invalid_image_base64"},
            ) from exc

        mime_type = self._first_string_from_sources(
            sources,
            (
                "mimeType",
                "mime_type",
                "media_type",
                "screenshot_mime_type",
            ),
        )
        if (
            mime_type is None
            and image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            mime_type = "image/png"
        parsed_width, parsed_height = self._image_dimensions_from_bytes(
            image_bytes
        )
        return (
            image_bytes,
            mime_type,
            width if width is not None else parsed_width,
            height if height is not None else parsed_height,
        )

    def _parse_elements(
        self,
        payload: Mapping[str, Any],
        *,
        target: WindowInfo,
        mode: CaptureMode,
        max_elements: int,
    ) -> list[UIElement]:
        """转换并限制驱动返回的无障碍元素。"""

        if mode is CaptureMode.VISION:
            return []

        raw_elements: list[Any] | None = None
        for key in (
            "elements",
            "accessibility_elements",
            "ax_elements",
        ):
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, list):
                raise ProtocolError(
                    "cua-driver returned invalid accessibility elements.",
                    details={"reason": "invalid_elements"},
                )
            raw_elements = value
            break
        if raw_elements is None:
            return []

        limit = max(0, max_elements)
        elements: list[UIElement] = []
        used_indexes: set[int] = set()
        next_index = 1
        for item in raw_elements:
            if len(elements) >= limit:
                break
            if not isinstance(item, Mapping):
                continue

            raw_index: int | None = None
            for index_key in ("element_index", "index"):
                candidate_index = item.get(index_key)
                if (
                    type(candidate_index) is int
                    and candidate_index > 0
                ):
                    raw_index = candidate_index
                    break
            if mode is CaptureMode.SOM:
                index = len(elements) + 1
            else:
                if (
                    raw_index is not None
                    and raw_index not in used_indexes
                ):
                    index = raw_index
                else:
                    while next_index in used_indexes:
                        next_index += 1
                    index = next_index
            used_indexes.add(index)

            pid_value = item.get("pid")
            window_id_value = item.get("window_id")
            attributes_value = item.get("attributes")
            attributes = (
                self._small_metadata(attributes_value, set())
                if isinstance(attributes_value, Mapping)
                else {}
            )
            if mode is CaptureMode.SOM and raw_index is not None:
                attributes["driver_element_index"] = raw_index
            token_value = self._first_present(
                item,
                "element_token",
                "token",
            )
            bounds_source = item.get("bounds")
            if bounds_source is None:
                bounds_source = item.get("frame", item)
            elements.append(
                UIElement(
                    index=index,
                    role=self._string_value(item.get("role")),
                    label=self._string_value(
                        self._first_present(
                            item,
                            "label",
                            "name",
                            "title",
                        )
                    ),
                    bounds=self._parse_bounds(bounds_source),
                    app=(
                        self._string_value(item.get("app"))
                        or target.app
                    ),
                    pid=(
                        pid_value
                        if type(pid_value) is int
                        else target.pid
                    ),
                    window_id=(
                        window_id_value
                        if type(window_id_value) is int
                        else target.window_id
                    ),
                    attributes=attributes,
                    element_token=(
                        token_value
                        if isinstance(token_value, str)
                        else None
                    ),
                )
            )
        return elements

    @staticmethod
    def _parse_bounds(value: Any) -> tuple[int, int, int, int]:
        """将常见 bounds 结构转换为整数四元组。"""

        raw_values: Sequence[Any]
        if isinstance(value, Mapping):
            raw_values = (
                value.get("x"),
                value.get("y"),
                value.get("width", value.get("w")),
                value.get("height", value.get("h")),
            )
        elif (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and len(value) == 4
        ):
            raw_values = value
        else:
            return (0, 0, 0, 0)

        converted: list[int] = []
        for item in raw_values:
            if isinstance(item, bool):
                return (0, 0, 0, 0)
            if isinstance(item, int):
                converted.append(item)
            elif isinstance(item, float) and math.isfinite(item):
                converted.append(int(item))
            else:
                return (0, 0, 0, 0)
        if converted[2] < 0 or converted[3] < 0:
            return (0, 0, 0, 0)
        return (
            converted[0],
            converted[1],
            converted[2],
            converted[3],
        )

    @staticmethod
    def _parse_dimension(
        sources: Sequence[Mapping[str, Any] | None],
        name: str,
    ) -> int | None:
        """从候选结构中读取非负整数尺寸。"""

        for source in sources:
            if source is None or name not in source:
                continue
            value = source[name]
            if type(value) is int and value >= 0:
                return value
            raise ProtocolError(
                "cua-driver returned an invalid image dimension.",
                details={"reason": f"invalid_{name}"},
            )
        return None

    @staticmethod
    def _image_dimensions_from_bytes(
        image_bytes: bytes,
    ) -> tuple[int, int]:
        """从 PNG 或 JPEG 文件头读取图片尺寸。"""

        png_signature = b"\x89PNG\r\n\x1a\n"
        if (
            len(image_bytes) >= 24
            and image_bytes.startswith(png_signature)
            and image_bytes[12:16] == b"IHDR"
        ):
            width = int.from_bytes(image_bytes[16:20], "big")
            height = int.from_bytes(image_bytes[20:24], "big")
            if width > 0 and height > 0:
                return width, height
            return 0, 0

        if not image_bytes.startswith(b"\xff\xd8"):
            return 0, 0

        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        offset = 2
        while offset < len(image_bytes):
            while (
                offset < len(image_bytes)
                and image_bytes[offset] != 0xFF
            ):
                offset += 1
            while (
                offset < len(image_bytes)
                and image_bytes[offset] == 0xFF
            ):
                offset += 1
            if offset >= len(image_bytes):
                break

            marker = image_bytes[offset]
            offset += 1
            if marker == 0xDA:
                break
            if marker in {0x01, *range(0xD0, 0xDA)}:
                continue
            if offset + 2 > len(image_bytes):
                break

            segment_length = int.from_bytes(
                image_bytes[offset : offset + 2],
                "big",
            )
            if (
                segment_length < 2
                or offset + segment_length > len(image_bytes)
            ):
                break
            if marker in sof_markers:
                if segment_length < 7:
                    return 0, 0
                height = int.from_bytes(
                    image_bytes[offset + 3 : offset + 5],
                    "big",
                )
                width = int.from_bytes(
                    image_bytes[offset + 5 : offset + 7],
                    "big",
                )
                if width > 0 and height > 0:
                    return width, height
                return 0, 0
            offset += segment_length
        return 0, 0

    @staticmethod
    def _looks_like_image(value: Mapping[str, Any]) -> bool:
        """判断结构是否包含常见图片字段。"""

        return (
            value.get("type") == "image"
            or any(
                key in value
                for key in (
                    "image",
                    "screenshot",
                    "image_base64",
                    "imageBase64",
                    "base64",
                    "screenshot_png_b64",
                    "png_b64",
                    "mimeType",
                    "mime_type",
                    "media_type",
                    "screenshot_mime_type",
                )
            )
        )

    @staticmethod
    def _first_string_from_sources(
        sources: Sequence[Mapping[str, Any] | None],
        keys: Sequence[str],
    ) -> str | None:
        """返回候选结构中的第一个字符串字段。"""

        for source in sources:
            if source is None:
                continue
            for key in keys:
                value = source.get(key)
                if isinstance(value, str):
                    return value
        return None

    @staticmethod
    def _first_present(
        source: Mapping[str, Any],
        *keys: str,
    ) -> Any:
        """返回第一个存在的字段值。"""

        for key in keys:
            if key in source:
                return source[key]
        return None

    @classmethod
    def _small_metadata(
        cls,
        source: Mapping[str, Any],
        excluded: set[str],
    ) -> dict[str, Any]:
        """仅保留非图片、非嵌套的小型扩展字段。"""

        metadata: dict[str, Any] = {}
        for key, value in source.items():
            if key in excluded or not isinstance(key, str):
                continue
            normalized = key.casefold()
            if any(marker in normalized for marker in _LARGE_FIELD_MARKERS):
                continue
            if not cls._is_small_value(value):
                continue
            if isinstance(value, str) and len(value) > 512:
                continue
            metadata[key] = value
        return metadata

    @staticmethod
    def _is_small_value(value: Any) -> bool:
        """判断值是否适合作为小型 metadata。"""

        return value is None or type(value) in (str, int, float, bool)

    @staticmethod
    def _string_value(value: Any) -> str:
        """将标量字段安全转换为字符串。"""

        if value is None:
            return ""
        if type(value) in (str, int, float, bool):
            return str(value)
        return ""

    @staticmethod
    def _parse_effect(value: Any) -> ActionEffect | None:
        """仅将可识别的 effect 转换为正式枚举。"""

        if isinstance(value, ActionEffect):
            return value
        if isinstance(value, str):
            try:
                return ActionEffect(value)
            except ValueError:
                pass
        return None

    @staticmethod
    def _raise_action_unavailable(
        action: ComputerUseAction,
    ) -> NoReturn:
        """统一拒绝 P5 尚未实现的写操作。"""

        raise BackendUnavailableError(
            "This Computer Use action is not available in the P5 backend.",
            details={
                "reason": "action_not_available_in_p5",
                "action": action.value,
            },
        )
