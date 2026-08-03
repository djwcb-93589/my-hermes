"""Claude Code Controller 的集中式有界策略。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ClaudeCodeControllerPolicy:
    """为同步 Controller 循环提供不可变且显式验证的限制。"""

    poll_interval: float = 1.0
    total_deadline: float = 900.0
    single_wait_limit: float = 5.0
    max_consecutive_empty_reads: int = 6
    max_observation_count: int = 1_000
    interrupt_observation_attempts: int = 3
    final_drain_attempts: int = 3
    cleanup_attempts: int = 3
    cleanup_retry_interval: float = 0.25
    observation_read_limit: int = 20_000
    final_event_limit: int = 64
    terminal_snapshot_limit: int = 64
    terminate_grace_period: float = 2.0
    terminal_observation_reserve: int = 4
    startup_observation_attempts: int = 4

    def __post_init__(self) -> None:
        self._require_seconds("poll_interval", self.poll_interval, 0.05, 30.0)
        self._require_seconds(
            "total_deadline",
            self.total_deadline,
            1.0,
            86_400.0,
        )
        self._require_seconds(
            "single_wait_limit",
            self.single_wait_limit,
            0.05,
            300.0,
        )
        self._require_count(
            "max_consecutive_empty_reads",
            self.max_consecutive_empty_reads,
            1,
            100,
        )
        self._require_count(
            "max_observation_count",
            self.max_observation_count,
            1,
            100_000,
        )
        self._require_count(
            "terminal_observation_reserve",
            self.terminal_observation_reserve,
            1,
            32,
        )
        self._require_count(
            "startup_observation_attempts",
            self.startup_observation_attempts,
            1,
            20,
        )
        self._require_count(
            "interrupt_observation_attempts",
            self.interrupt_observation_attempts,
            1,
            20,
        )
        self._require_count(
            "final_drain_attempts",
            self.final_drain_attempts,
            1,
            20,
        )
        self._require_count(
            "cleanup_attempts",
            self.cleanup_attempts,
            1,
            10,
        )
        self._require_seconds(
            "cleanup_retry_interval",
            self.cleanup_retry_interval,
            0.01,
            5.0,
        )
        self._require_count(
            "observation_read_limit",
            self.observation_read_limit,
            1,
            20_000,
        )
        self._require_count(
            "final_event_limit",
            self.final_event_limit,
            1,
            512,
        )
        self._require_count(
            "terminal_snapshot_limit",
            self.terminal_snapshot_limit,
            1,
            1_024,
        )
        self._require_seconds(
            "terminate_grace_period",
            self.terminate_grace_period,
            0.05,
            30.0,
        )
        if self.cleanup_retry_interval > self.single_wait_limit:
            raise ValueError(
                "cleanup_retry_interval must not exceed single_wait_limit"
            )
        if self.terminate_grace_period > self.single_wait_limit:
            raise ValueError(
                "terminate_grace_period must not exceed single_wait_limit"
            )

    @staticmethod
    def _require_seconds(
        name: str,
        value: float,
        minimum: float,
        maximum: float,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(
                f"{name} must be between {minimum} and {maximum} seconds"
            )

    @staticmethod
    def _require_count(
        name: str,
        value: int,
        minimum: int,
        maximum: int,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(
                f"{name} must be between {minimum} and {maximum}"
            )


__all__ = ["ClaudeCodeControllerPolicy"]
