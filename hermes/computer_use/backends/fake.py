"""可配置、可预测且完全内存化的 Fake Backend。"""

from collections import defaultdict, deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..backend import ComputerUseBackend
from ..contracts import (
    ActionEffect,
    ActionResult,
    AppInfo,
    CaptureMode,
    CaptureResult,
    ComputerUseAction,
    DeliveryMode,
    WindowInfo,
)


@dataclass(slots=True)
class FakeCall:
    """FakeBackend 记录的一次操作调用。"""

    action: ComputerUseAction
    arguments: dict[str, Any]


class FakeBackend(ComputerUseBackend):
    """提供调用记录和预设结果队列的内存 Backend。"""

    def __init__(
        self,
        *,
        apps: Sequence[AppInfo] | None = None,
        windows: Sequence[WindowInfo] | None = None,
        default_capture: CaptureResult | None = None,
    ) -> None:
        """复制并保存可配置的应用、窗口和默认捕获数据。"""

        super().__init__()
        self.calls: list[FakeCall] = []
        self._apps: list[AppInfo] = deepcopy(
            list(apps) if apps is not None else []
        )
        self._windows: list[WindowInfo] = deepcopy(
            list(windows) if windows is not None else []
        )
        self._default_capture: CaptureResult | None = deepcopy(
            default_capture
        )
        self._capture_results: deque[CaptureResult] = deque()
        self._action_results: defaultdict[
            ComputerUseAction,
            deque[ActionResult],
        ] = defaultdict(deque)
        self._failures: defaultdict[
            ComputerUseAction,
            deque[Exception],
        ] = defaultdict(deque)

    def _start(self) -> None:
        """完成纯内存启动。"""

        return None

    def _stop(self) -> None:
        """完成纯内存停止。"""

        return None

    def is_available(self) -> bool:
        """仅在 Backend 已启动时返回可用。"""

        return self.state == "started"

    def clear_calls(self) -> None:
        """清空调用历史。"""

        self.calls.clear()

    def queue_capture(self, result: CaptureResult) -> None:
        """将捕获结果副本加入先进先出队列。"""

        self._capture_results.append(deepcopy(result))

    def queue_action_result(
        self,
        action: ComputerUseAction,
        result: ActionResult,
    ) -> None:
        """为指定动作加入预设结果副本。"""

        self._action_results[action].append(deepcopy(result))

    def queue_failure(
        self,
        action: ComputerUseAction,
        error: Exception,
    ) -> None:
        """为指定动作加入预设异常。"""

        self._failures[action].append(error)

    def _record(
        self,
        action: ComputerUseAction,
        arguments: dict[str, Any],
    ) -> None:
        """以独立字典记录一次调用。"""

        self.calls.append(
            FakeCall(
                action=action,
                arguments=deepcopy(dict(arguments)),
            )
        )

    def _raise_next_failure(self, action: ComputerUseAction) -> None:
        """按先进先出顺序抛出指定动作的下一个异常。"""

        failures = self._failures.get(action)
        if failures:
            raise failures.popleft()

    @staticmethod
    def _empty_capture(
        mode: CaptureMode = CaptureMode.SOM,
    ) -> CaptureResult:
        """创建不含图片和元素的空捕获结果。"""

        return CaptureResult(mode=mode, width=0, height=0)

    @staticmethod
    def _click_action(
        button: str,
        click_count: int,
    ) -> ComputerUseAction:
        """根据点击参数确定正式动作名称。"""

        if button == "right":
            return ComputerUseAction.RIGHT_CLICK
        if button == "middle":
            return ComputerUseAction.MIDDLE_CLICK
        if click_count == 2:
            return ComputerUseAction.DOUBLE_CLICK
        return ComputerUseAction.CLICK

    def _current_capture(self) -> CaptureResult:
        """返回当前默认捕获数据的副本。"""

        if self._default_capture is None:
            return self._empty_capture()
        return deepcopy(self._default_capture)

    def _default_action_result(
        self,
        action: ComputerUseAction,
    ) -> ActionResult:
        """创建指定动作的默认成功结果。"""

        return ActionResult(
            ok=True,
            action=action,
            message=f"FakeBackend completed {action.value}.",
            verified=True,
            effect=ActionEffect.CONFIRMED,
            path="fake",
        )

    def _complete_action(
        self,
        action: ComputerUseAction,
        arguments: dict[str, Any],
        *,
        capture_after: bool = False,
    ) -> ActionResult:
        """记录动作并应用失败、结果和默认值队列。"""

        self._record(action, arguments)
        self._raise_next_failure(action)

        results = self._action_results.get(action)
        if results:
            result = deepcopy(results.popleft())
        else:
            result = self._default_action_result(action)

        result.action = action
        if capture_after and result.capture is None:
            result.capture = self._current_capture()
        return result

    def _capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """记录调用并返回排队或默认的捕获结果副本。"""

        self._record(
            ComputerUseAction.CAPTURE,
            {
                "mode": mode,
                "app": app,
                "pid": pid,
                "window_id": window_id,
                "max_elements": max_elements,
            },
        )
        self._raise_next_failure(ComputerUseAction.CAPTURE)

        if self._capture_results:
            result = deepcopy(self._capture_results.popleft())
        else:
            result = self._current_capture()
        result.mode = mode
        return result

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
        """记录并完成可配置的点击动作。"""

        action = self._click_action(button, click_count)
        return self._complete_action(
            action,
            {
                "element": element,
                "coordinate": coordinate,
                "button": button,
                "click_count": click_count,
                "modifiers": modifiers,
                "delivery_mode": delivery_mode,
                "bring_to_front": bring_to_front,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )

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
        """记录并完成可配置的拖动动作。"""

        return self._complete_action(
            ComputerUseAction.DRAG,
            {
                "from_element": from_element,
                "to_element": to_element,
                "from_coordinate": from_coordinate,
                "to_coordinate": to_coordinate,
                "button": button,
                "modifiers": modifiers,
                "delivery_mode": delivery_mode,
                "bring_to_front": bring_to_front,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )

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
        """记录并完成可配置的滚动动作。"""

        return self._complete_action(
            ComputerUseAction.SCROLL,
            {
                "direction": direction,
                "amount": amount,
                "element": element,
                "coordinate": coordinate,
                "modifiers": modifiers,
                "delivery_mode": delivery_mode,
                "bring_to_front": bring_to_front,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )

    def _type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """记录并完成可配置的文本输入动作。"""

        return self._complete_action(
            ComputerUseAction.TYPE,
            {
                "text": text,
                "delivery_mode": delivery_mode,
                "bring_to_front": bring_to_front,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )

    def _key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """记录并完成可配置的按键动作。"""

        return self._complete_action(
            ComputerUseAction.KEY,
            {
                "keys": keys,
                "delivery_mode": delivery_mode,
                "bring_to_front": bring_to_front,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )

    def _list_apps(self) -> list[AppInfo]:
        """记录调用并返回配置的应用副本。"""

        action = ComputerUseAction.LIST_APPS
        self._record(action, {})
        self._raise_next_failure(action)
        return deepcopy(self._apps)

    def _list_windows(self) -> list[WindowInfo]:
        """记录调用并返回配置的窗口副本。"""

        action = ComputerUseAction.LIST_WINDOWS
        self._record(action, {})
        self._raise_next_failure(action)
        return deepcopy(self._windows)

    def _focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """记录并完成可配置的应用聚焦动作。"""

        return self._complete_action(
            ComputerUseAction.FOCUS_APP,
            {
                "app": app,
                "raise_window": raise_window,
            },
        )

    def _set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """记录并完成可配置的元素值设置动作。"""

        return self._complete_action(
            ComputerUseAction.SET_VALUE,
            {
                "value": value,
                "element": element,
                "capture_after": capture_after,
            },
            capture_after=capture_after,
        )
