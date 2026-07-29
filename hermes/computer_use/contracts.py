"""Computer Use 的稳定公开数据契约。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


class ComputerUseAction(StrEnum):
    """Computer Use 执行器支持的动作名称。"""

    CAPTURE = "capture"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MIDDLE_CLICK = "middle_click"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE = "type"
    KEY = "key"
    SET_VALUE = "set_value"
    WAIT = "wait"
    LIST_APPS = "list_apps"
    LIST_WINDOWS = "list_windows"
    FOCUS_APP = "focus_app"


class CaptureMode(StrEnum):
    """界面捕获结果的表示方式。"""

    SOM = "som"
    VISION = "vision"
    AX = "ax"


class DeliveryMode(StrEnum):
    """动作向目标应用投递的前后台模式。"""

    BACKGROUND = "background"
    FOREGROUND = "foreground"


class ActionEffect(StrEnum):
    """动作是否真实生效的验证结论。"""

    CONFIRMED = "confirmed"
    UNVERIFIABLE = "unverifiable"
    SUSPECTED_NOOP = "suspected_noop"


@dataclass(slots=True)
class UIElement:
    """一次界面捕获中可供定位的无障碍元素。"""

    index: int
    role: str
    label: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    app: str = ""
    pid: int = 0
    window_id: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    element_token: str | None = None

    def center(self) -> tuple[int, int]:
        """返回元素相对于目标窗口的中心点。"""

        x, y, width, height = self.bounds
        return x + width // 2, y + height // 2


@dataclass(slots=True)
class CaptureResult:
    """一次界面捕获返回的原始图片和界面元素。"""

    mode: CaptureMode
    width: int
    height: int
    image_bytes: bytes | None = None
    mime_type: str | None = None
    elements: list[UIElement] = field(default_factory=list)
    app: str = ""
    window_title: str = ""


@dataclass(slots=True)
class AppInfo:
    """可供 Computer Use 定位的应用信息。"""

    name: str
    pid: int
    identifier: str = ""
    window_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WindowInfo:
    """应用拥有的原生窗口信息。"""

    window_id: int
    title: str
    app: str
    pid: int
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    is_visible: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EscalationHint:
    """操作未可靠生效时由底层实现提供的下一步建议。"""

    recommended: str
    reason: str


@dataclass(slots=True)
class ActionResult:
    """一次动作调用的结构化结果。

    ``ok`` 只表示调用过程是否成功完成；``ok=True`` 不代表操作一定真实
    生效，是否生效由 ``verified`` 和 ``effect`` 表示。``capture`` 保存
    ``capture_after`` 或 Backend 主动返回的操作后界面。``code`` 是稳定的
    机器可读错误或拒绝代码。``meta`` 只保存非核心扩展信息，不能替代已经
    定义的正式字段。
    """

    ok: bool
    action: ComputerUseAction
    message: str = ""
    capture: CaptureResult | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    effect: ActionEffect | None = None
    escalation: EscalationHint | None = None
    path: str | None = None
    degraded: bool | None = None
    delivery_mode: DeliveryMode | None = None
    code: str | None = None


# Computer Use 执行器允许返回的正式结果类型。
type ComputerUseResult = (
    CaptureResult
    | ActionResult
    | list[AppInfo]
    | list[WindowInfo]
)


@runtime_checkable
class ComputerUseExecutor(Protocol):
    """所有 Computer Use 调用方共同依赖的执行器协议。"""

    def execute(
        self,
        action: ComputerUseAction | str,
        arguments: Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> ComputerUseResult:
        """执行一个动作并返回正式结果类型。"""

        ...
