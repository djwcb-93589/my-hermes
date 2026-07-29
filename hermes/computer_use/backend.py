"""Computer Use 底层实现必须遵守的 Backend 抽象。"""

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Literal

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
from .errors import BackendUnavailableError


type _BackendState = Literal["created", "started", "stopped"]


class ComputerUseBackend(ABC):
    """跨平台 Computer Use Backend 的统一抽象基类。

    子类通过 ``_start`` 和 ``_stop`` 实现具体生命周期逻辑。所有抽象操作
    的实现都必须在访问底层驱动前调用 ``_ensure_started``。
    """

    def __init__(self) -> None:
        """创建一个尚未启动的 Backend。"""

        self._state: _BackendState = "created"

    @property
    def state(self) -> _BackendState:
        """返回当前生命周期状态。"""

        return self._state

    def start(self) -> None:
        """启动 Backend，并仅在成功后进入 ``started`` 状态。"""

        if self._state == "started":
            return
        self._start()
        self._state = "started"

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

    @abstractmethod
    def capture(
        self,
        mode: CaptureMode = CaptureMode.SOM,
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
        max_elements: int = 100,
    ) -> CaptureResult:
        """捕获目标界面；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
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
        """点击元素或坐标；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
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
        """在元素或坐标之间拖动；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
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
        """在目标位置滚动；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def type_text(
        self,
        text: str,
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """输入文本；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def key(
        self,
        keys: Sequence[str],
        delivery_mode: DeliveryMode = DeliveryMode.BACKGROUND,
        bring_to_front: bool = False,
        capture_after: bool = False,
    ) -> ActionResult:
        """发送按键序列；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def list_apps(self) -> list[AppInfo]:
        """列出可访问应用；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]:
        """列出可访问窗口；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def focus_app(
        self,
        app: str,
        raise_window: bool = False,
    ) -> ActionResult:
        """聚焦目标应用；实现必须先检查 Backend 已启动。"""

        ...

    @abstractmethod
    def set_value(
        self,
        value: str,
        element: int | None = None,
        capture_after: bool = False,
    ) -> ActionResult:
        """直接设置界面元素值；实现必须先检查 Backend 已启动。"""

        ...

    def wait(self, seconds: float) -> ActionResult:
        """等待至多 30 秒并返回标准动作结果。"""

        self._ensure_started()
        bounded_seconds = min(max(float(seconds), 0.0), 30.0)
        time.sleep(bounded_seconds)
        return ActionResult(
            ok=True,
            action=ComputerUseAction.WAIT,
            message=f"Waited for {bounded_seconds:g} seconds.",
            verified=True,
            effect=ActionEffect.CONFIRMED,
        )
