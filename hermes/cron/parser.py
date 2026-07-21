"""Schedule expression parser.

Supports three forms:
  "30m"           → 30 min from now, one-shot
  "every 30m"     → every 30 min, recurring
  "0 9 * * 1-5"   → 5-field cron expression, recurring
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s: str) -> float:
    """Parse "30m", "2h", "1d" etc. into seconds."""
    s = s.strip()
    if not s:
        raise ValueError("empty duration")
    unit = s[-1].lower()
    if unit not in _DURATION_UNITS:
        raise ValueError(f"unknown duration unit: {unit!r}")
    try:
        seconds = float(s[:-1]) * _DURATION_UNITS[unit]
    except ValueError as exc:
        raise ValueError("duration value is invalid") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be a positive finite value")
    return seconds


def _parse_cron_field(field_str: str, value_range: tuple[int, int]):
    """
    Parse one cron field into a matcher function (int → bool).

    Supports: * (any), */N (step), N-M (range), N,M,... (list), N (exact).
    """
    lo, hi = value_range

    if field_str == "*":
        return lambda v: True

    if field_str.startswith("*/"):
        step = int(field_str[2:])
        if step <= 0 or step > hi - lo + 1:
            raise ValueError("cron step is invalid")
        return lambda v, s=step: v % s == 0

    if "," in field_str:
        values = {int(x) for x in field_str.split(",")}
        if not values or any(value < lo or value > hi for value in values):
            raise ValueError(f"cron value out of range: {field_str}")
        return lambda v, vs=frozenset(values): v in vs

    if "-" in field_str:
        parts = field_str.split("-", 1)
        a, b = int(parts[0]), int(parts[1])
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron range out of range: {field_str}")
        return lambda v, lo=a, hi=b: lo <= v <= hi

    exact = int(field_str)
    if exact < lo or exact > hi:
        raise ValueError(f"cron value out of range: {field_str}")
    return lambda v, e=exact: v == e


def validate_timezone(value: str) -> str:
    """校验 IANA 时区，并返回标准化后的名称。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a non-empty IANA timezone")
    try:
        return ZoneInfo(value.strip()).key
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid timezone: {value}") from exc


def _next_cron_fire(
    expr: str,
    *,
    after: float | None = None,
    timezone_name: str = "UTC",
) -> float:
    """按任务时区寻找下一条五字段 Cron 规则，并保存为 UTC 时间戳。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr}")

    matchers = [
        _parse_cron_field(f, r) for f, r in
        zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)])
    ]

    tz = ZoneInfo(validate_timezone(timezone_name))
    # 在 UTC 时间线上逐分钟推进，再投影到任务时区。这样夏令时跳变不会
    # 生成不存在的本地时间；回拨产生的第二个折叠窗口会被跳过，避免同一
    # 墙上时间触发两次。
    base_ts = time.time() if after is None else float(after)
    t = datetime.fromtimestamp(base_ts, datetime_timezone.utc).replace(
        second=0,
        microsecond=0,
    ) + timedelta(minutes=1)

    for _ in range(366 * 24 * 60):
        local = t.astimezone(tz)
        # Python weekday: 0=Mon..6=Sun → cron weekday: 0=Sun..6=Sat
        cron_dow = (local.weekday() + 1) % 7
        if (not local.fold
                and matchers[0](local.minute) and matchers[1](local.hour)
                and matchers[2](local.day) and matchers[3](local.month)
                and matchers[4](cron_dow)):
            return t.timestamp()
        t += timedelta(minutes=1)

    raise ValueError(f"no match in 366 days for: {expr}")


def parse_schedule(
    expr: str,
    *,
    timezone_name: str = "UTC",
    now: float | None = None,
) -> tuple[float, bool]:
    """
    Parse a schedule expression.

    Returns (next_fire_timestamp, one_shot).
    """
    expr = expr.strip()
    timestamp = time.time() if now is None else float(now)

    if expr.startswith("every "):
        seconds = _parse_duration(expr[6:])
        return timestamp + seconds, False

    try:
        seconds = _parse_duration(expr)
        return timestamp + seconds, True
    except ValueError:
        pass

    next_ts = _next_cron_fire(expr, after=timestamp, timezone_name=timezone_name)
    return next_ts, False


def next_schedule_fire(
    expr: str,
    after: float,
    *,
    timezone_name: str = "UTC",
) -> float | None:
    """从指定计划窗口之后计算下一次运行，不依赖调度器当前时钟。"""
    expression = expr.strip()
    if expression.startswith("every "):
        return float(after) + _parse_duration(expression[6:])
    try:
        _parse_duration(expression)
    except ValueError:
        return _next_cron_fire(
            expression,
            after=float(after),
            timezone_name=timezone_name,
        )
    return None
