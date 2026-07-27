"""Review Runtime 的通用数据契约。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hermes.tools import ToolPolicy


class ReviewKind(str, Enum):
    """Review 的业务类型。"""

    MEMORY = "memory"
    SKILL = "skill"


@dataclass(frozen=True)
class ReviewClaim:
    """一个已领取 Review 任务的通用身份与业务载荷。"""

    kind: ReviewKind
    session_id: str
    token: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(self.payload)),
        )


@dataclass(frozen=True)
class ForegroundReviewEvent:
    """由一次前台运行产生的最小 Review 事件。"""

    session_id: str
    completed: bool
    tool_batches: int


@dataclass(frozen=True)
class ReviewRunSpec:
    """启动一次 Review Loop 所需的通用输入。"""

    messages: list[dict]
    system_prompt: str
    instruction: str
    tool_policy: "ToolPolicy"
    max_iterations: int
    tool_context: Mapping[str, object]


class ReviewDriver(Protocol):
    """不同 Review 类型接入通用运行时所遵循的形状。"""

    kind: ReviewKind

    def record_progress(
        self,
        conn: sqlite3.Connection,
        event: ForegroundReviewEvent,
    ) -> None:
        ...

    def claim_due(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> ReviewClaim | None:
        ...

    def validate_claim(self, claim: ReviewClaim) -> bool:
        ...

    def claim_is_valid(
        self,
        conn: sqlite3.Connection,
        claim: ReviewClaim,
    ) -> bool:
        ...

    def prepare_run(
        self,
        conn: sqlite3.Connection,
        claim: ReviewClaim,
    ) -> ReviewRunSpec:
        ...

    def complete(
        self,
        conn: sqlite3.Connection,
        claim: ReviewClaim,
    ) -> bool:
        ...

    def fail(
        self,
        conn: sqlite3.Connection,
        claim: ReviewClaim,
        error: str,
    ) -> bool:
        ...
