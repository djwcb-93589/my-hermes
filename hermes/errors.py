"""Error classification and retry / fallback policy."""

from __future__ import annotations

import random

from openai import OpenAI

from hermes.config import (
    FALLBACK_API_KEY,
    FALLBACK_BASE_URL,
    FALLBACK_MODEL,
    MODEL_TIMEOUT_SECONDS,
)


# 网络 / 超时类异常关键字。命中时归为 retryable(没有 status_code,但
# 通常是瞬时网络抖动,值得重试)。
_NETWORK_MARKERS = (
    "timeout", "timed out", "connection",
    "connected", "reset by peer",
    "temporarily unavailable",
    "name or service not known",
    "getaddrinfo", "ssl eof",
)


def is_network_error_message(error_message: str) -> bool:
    """检查消息是否像网络 / 超时类异常(无 status_code 但应重试)。"""
    if not error_message:
        return False
    lower = error_message.lower()
    return any(marker in lower for marker in _NETWORK_MARKERS)


def classify_error(
    status_code: int | None,
    error_message: str,
) -> dict:
    """Classify an API error into an actionable decision.

    分类策略:
      - 429 rate_limit / 5xx server_error / 网络超时:retryable=True,先重试
      - 400 context_overflow:retryable + 触发压缩
      - 401 / 403 auth / 404 model_not_found:不可重试,直接 fallback
      - unknown:不重试(让调用方决定走 fallback 还是 abort)
    """
    # 网络 / 超时(无 status_code):通常瞬时,先重试
    if status_code is None and is_network_error_message(error_message):
        return {
            "reason": "network_or_timeout",
            "retryable": True,
            "should_compress": False,
            "should_fallback": False,
        }

    if status_code == 429:
        return {
            "reason": "rate_limit",
            "retryable": True,
            "should_compress": False,
            "should_fallback": False,
        }
    if status_code == 400 and "context" in error_message.lower():
        return {
            "reason": "context_overflow",
            "retryable": True,
            "should_compress": True,
            "should_fallback": False,
        }
    if status_code in (500, 502, 503):
        return {
            "reason": "server_error",
            "retryable": True,
            "should_compress": False,
            "should_fallback": False,
        }
    if status_code in (401, 403):
        return {
            "reason": "auth",
            "retryable": False,
            "should_compress": False,
            "should_fallback": True,
        }
    if status_code == 404:
        return {
            "reason": "model_not_found",
            "retryable": False,
            "should_compress": False,
            "should_fallback": True,
        }
    return {
        "reason": "unknown",
        "retryable": False,
        "should_compress": False,
        "should_fallback": False,
    }


def jittered_backoff(
    attempt: int,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
) -> float:
    """Calculate exponential backoff with random jitter."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay + random.uniform(0, delay * 0.5)


def switch_to_fallback():
    """Switch to the fallback model. Returns (client, model) or (None, None).

    没配置 FALLBACK_MODEL 时返 (None, None),调用方据此决定是 abort 还是 raise。
    """
    if not FALLBACK_MODEL:
        return None, None
    print(f"  [fallback] -> {FALLBACK_MODEL}")
    fallback_client = OpenAI(
        base_url=FALLBACK_BASE_URL,
        api_key=FALLBACK_API_KEY,
        timeout=MODEL_TIMEOUT_SECONDS,
    )
    return fallback_client, FALLBACK_MODEL
