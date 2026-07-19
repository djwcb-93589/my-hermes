"""Schedule expression parser.

Supports three forms:
  "30m"           → 30 min from now, one-shot
  "every 30m"     → every 30 min, recurring
  "0 9 * * 1-5"   → 5-field cron expression, recurring
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s: str) -> float:
    """Parse "30m", "2h", "1d" etc. into seconds."""
    s = s.strip()
    if not s:
        raise ValueError("empty duration")
    unit = s[-1].lower()
    if unit not in _DURATION_UNITS:
        raise ValueError(f"unknown duration unit: {unit!r}")
    return float(s[:-1]) * _DURATION_UNITS[unit]


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
        return lambda v, s=step: v % s == 0

    if "," in field_str:
        values = {int(x) for x in field_str.split(",")}
        return lambda v, vs=frozenset(values): v in vs

    if "-" in field_str:
        parts = field_str.split("-", 1)
        a, b = int(parts[0]), int(parts[1])
        return lambda v, lo=a, hi=b: lo <= v <= hi

    exact = int(field_str)
    return lambda v, e=exact: v == e


def _next_cron_fire(expr: str, *, after: float | None = None) -> float:
    """Find the next datetime matching a 5-field cron expression."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields, got {len(fields)}: {expr}")

    matchers = [
        _parse_cron_field(f, r) for f, r in
        zip(fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)])
    ]

    base = datetime.now() if after is None else datetime.fromtimestamp(after)
    t = base.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(366 * 24 * 60):
        # Python weekday: 0=Mon..6=Sun → cron weekday: 0=Sun..6=Sat
        cron_dow = (t.weekday() + 1) % 7
        if (matchers[0](t.minute) and matchers[1](t.hour)
                and matchers[2](t.day) and matchers[3](t.month)
                and matchers[4](cron_dow)):
            return t.timestamp()
        t += timedelta(minutes=1)

    raise ValueError(f"no match in 366 days for: {expr}")


def parse_schedule(expr: str) -> tuple[float, bool]:
    """
    Parse a schedule expression.

    Returns (next_fire_timestamp, one_shot).
    """
    expr = expr.strip()
    now = time.time()

    if expr.startswith("every "):
        seconds = _parse_duration(expr[6:])
        return now + seconds, False

    try:
        seconds = _parse_duration(expr)
        return now + seconds, True
    except ValueError:
        pass

    next_ts = _next_cron_fire(expr)
    return next_ts, False


def next_schedule_fire(expr: str, after: float) -> float | None:
    """从指定计划窗口之后计算下一次运行，不依赖调度器当前时钟。"""
    expression = expr.strip()
    if expression.startswith("every "):
        return float(after) + _parse_duration(expression[6:])
    try:
        _parse_duration(expression)
    except ValueError:
        return _next_cron_fire(expression, after=float(after))
    return None
