"""Review Driver 的显式注册表。"""

from __future__ import annotations

from hermes.review.contracts import ReviewDriver, ReviewKind


class ReviewDriverRegistry:
    """按 ReviewKind 保存已启用的 Review Driver。"""

    def __init__(self) -> None:
        self._drivers: dict[ReviewKind, ReviewDriver] = {}

    def register(self, driver: ReviewDriver) -> None:
        """注册一个 Driver；同一 ReviewKind 只能注册一次。"""
        kind = getattr(driver, "kind", None)
        if not isinstance(kind, ReviewKind):
            raise ValueError("review driver kind must be a ReviewKind")
        if kind in self._drivers:
            raise ValueError(
                f"review driver already registered for kind: {kind.value}"
            )
        self._drivers[kind] = driver

    def get(self, kind: ReviewKind) -> ReviewDriver | None:
        """返回指定类型的 Driver；无效或未注册类型均返回 None。"""
        if not isinstance(kind, ReviewKind):
            return None
        return self._drivers.get(kind)

    def enabled_drivers(self) -> tuple[ReviewDriver, ...]:
        """返回不可变的已注册 Driver 快照。"""
        return tuple(self._drivers.values())
