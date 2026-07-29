"""Computer Use 底层实现必须遵守的 Backend 抽象。"""

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Literal, final

from .contracts import (
    ActionEffect,
    ActionResult,
    AppInfo,
    CaptureMode,
    CaptureResult,
    ComputerUseAction,
    DeliveryMode,
    WindowInfo,
)
from .errors import (
    BackendStartError,
    BackendUnavailableError,
    ComputerUseError,
)


type _BackendState = Literal["created", "started", "stopped"]


class ComputerUseBackend(ABC):
    """跨平台 Computer Use Backend 的统一抽象基类。

    子类实现 ``_start``、``_stop`` 以及 ``_capture``、``_click`` 等
    受保护操作。公共操作方法由基类统一检查生命周期后再调用受保护实现，
    子类不需要也不应重复调用 ``_ensure_started``。
    """

    def __init__(self) -> None:
        """创建一个尚未启动的 Backend。"""

        self._state: _BackendState = "created"

    @property
    def state(self) -> _BackendState:
        """返回当前生命周期状态。"""

        return self._state

    @final
    def start(self) -> None:
        """启动 Backend，并仅在成功后进入 ``started`` 状态。"""

        if self._state == "started":
            return
        try:
            self._start()
        except ComputerUseError:
            raise
        except Exception as exc:
            raise BackendStartError(
                "Failed to start Computer Use backend.",
                details={"exception_type": type(exc).__name__},
            ) from exc
        else:
            self._state = "started"

    @final
    def stop(self) -> None:
        """停止 Backend；未启动或重复停止均安全返回。"""

        if self._state != "started":
            self._state = "stopped"
            return
        try:
            self._stop()
        finally:
            self._state = "stopped"

    @abstractmethod
    def _start(self) -> None:
        """执行子类特定的启动逻辑。"""

        ...

    @abstractmethod
    def _stop(self) -> None:
        """执行子类特定的停止逻辑。"""

        ...

    @abstractmethod
    def is_available(self) -> bool:
        """返回 Backend 当前是否具备服务能力。"""

        ...

    def _ensure_started(self) -> None:
        """确保 Backend 已启动，否则抛出统一异常。"""

        if self._state != "started":
            raise BackendUnavailableError(
                "Computer Use backend is not started.",
                details={"state": self._state},
            )

    @final
    def capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """检查生命周期后捕获目标界面。"""

        self._ensure_started()
        return self._capture(
            mode=mode,
            app=app,
            pid=pid,
            window_id=window_id,
            max_elements=max_elements,
        )

    @abstractmethod
    def _capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """执行子类特定的界面捕获。"""

        ...

    @final
    def click(
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
        """检查生命周期后点击元素或坐标。"""

        self._ensure_started()
        return self._click(
            element=element,
            coordinate=coordinate,
            button=button,
            click_count=click_count,
            modifiers=modifiers,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
            capture_after=capture_after,
        )

    @abstractmethod
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
        """执行子类特定的点击操作。"""

        ...

    @final
    def drag(
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
        """检查生命周期后在元素或坐标之间拖动。"""

        self._ensure_started()
        return self._drag(
            from_element=from_element,
            to_element=to_element,
            from_coordinate=from_coordinate,
            to_coordinate=to_coordinate,
            button=button,
            modifiers=modifiers,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
            capture_after=capture_after,
        )

    @abstractmethod
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
        """执行子类特定的拖动操作。"""

        ...

    @final
    def scroll(
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
        """检查生命周期后在目标位置滚动。"""

        self._ensure_started()
        return self._scroll(
            direction=direction,
            amount=amount,
            element=element,
            coordinate=coordinate,
            modifiers=modifiers,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
            capture_after=capture_after,
        )

    @abstractmethod
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
        """执行子类特定的滚动操作。"""

        ...

    @final
    def type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """检查生命周期后输入文本。"""

        self._ensure_started()
        return self._type_text(
            text=text,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
            capture_after=capture_after,
        )

    @abstractmethod
    def _type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """执行子类特定的文本输入。"""

        ...

    @final
    def key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """检查生命周期后发送按键序列。"""

        self._ensure_started()
        return self._key(
            keys=keys,
            delivery_mode=delivery_mode,
            bring_to_front=bring_to_front,
            capture_after=capture_after,
        )

    @abstractmethod
    def _key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """执行子类特定的按键操作。"""

        ...

    @final
    def list_apps(self) -> list[AppInfo]:
        """检查生命周期后列出可访问应用。"""

        self._ensure_started()
        return self._list_apps()

    @abstractmethod
    def _list_apps(self) -> list[AppInfo]:
        """执行子类特定的应用枚举。"""

        ...

    @final
    def list_windows(self) -> list[WindowInfo]:
        """检查生命周期后列出可访问窗口。"""

        self._ensure_started()
        return self._list_windows()

    @abstractmethod
    def _list_windows(self) -> list[WindowInfo]:
        """执行子类特定的窗口枚举。"""

        ...

    @final
    def focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """检查生命周期后聚焦目标应用。"""

        self._ensure_started()
        return self._focus_app(
            app=app,
            raise_window=raise_window,
        )

    @abstractmethod
    def _focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """执行子类特定的应用聚焦。"""

        ...

    @final
    def set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """检查生命周期后直接设置界面元素值。"""

        self._ensure_started()
        return self._set_value(
            value=value,
            element=element,
            capture_after=capture_after,
        )

    @abstractmethod
    def _set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """执行子类特定的元素值设置。"""

        ...

    @final
    def wait(self, seconds: float) -> ActionResult:
        """检查生命周期并将受限等待时间交给 Backend 实现。"""

        self._ensure_started()
        bounded_seconds = min(max(float(seconds), 0.0), 30.0)
        return self._wait(bounded_seconds)

    def _wait(self, seconds: float) -> ActionResult:
        """执行真实等待并返回标准动作结果。"""

        time.sleep(seconds)
        return ActionResult(
            ok=True,
            action=ComputerUseAction.WAIT,
            message=f"Waited for {seconds:g} seconds.",
            verified=True,
            effect=ActionEffect.CONFIRMED,
        )
