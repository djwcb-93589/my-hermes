"""不执行真实电脑操作的 Noop Backend。"""

from collections.abc import Sequence

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


class NoopBackend(ComputerUseBackend):
    """用于验证调用链且不产生真实电脑操作的 Backend。"""

    def _start(self) -> None:
        """完成纯内存启动。"""

        return None

    def _stop(self) -> None:
        """完成纯内存停止。"""

        return None

    def is_available(self) -> bool:
        """仅在 Backend 已启动时返回可用。"""

        return self.state == "started"

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

    def _action_result(
        self,
        action: ComputerUseAction,
        *,
        capture_after: bool = False,
    ) -> ActionResult:
        """创建明确标记为未真实执行的动作结果。"""

        capture = self._empty_capture() if capture_after else None
        return ActionResult(
            ok=True,
            action=action,
            message=(
                "No real computer operation was performed by NoopBackend "
                f"for {action.value}."
            ),
            capture=capture,
            verified=False,
            effect=ActionEffect.UNVERIFIABLE,
            path="noop",
            code="noop",
        )

    def _capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """返回保留请求模式的空捕获结果。"""

        return self._empty_capture(mode)

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
        """返回未执行真实点击的结果。"""

        action = self._click_action(button, click_count)
        return self._action_result(action, capture_after=capture_after)

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
        """返回未执行真实拖动的结果。"""

        return self._action_result(
            ComputerUseAction.DRAG,
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
        """返回未执行真实滚动的结果。"""

        return self._action_result(
            ComputerUseAction.SCROLL,
            capture_after=capture_after,
        )

    def _type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """返回未执行真实输入的结果。"""

        return self._action_result(
            ComputerUseAction.TYPE,
            capture_after=capture_after,
        )

    def _key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """返回未发送真实按键的结果。"""

        return self._action_result(
            ComputerUseAction.KEY,
            capture_after=capture_after,
        )

    def _list_apps(self) -> list[AppInfo]:
        """返回空应用列表。"""

        return []

    def _list_windows(self) -> list[WindowInfo]:
        """返回空窗口列表。"""

        return []

    def _focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """返回未聚焦真实应用的结果。"""

        return self._action_result(ComputerUseAction.FOCUS_APP)

    def _set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """返回未设置真实元素值的结果。"""

        return self._action_result(
            ComputerUseAction.SET_VALUE,
            capture_after=capture_after,
        )
