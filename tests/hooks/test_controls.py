"""controls：控制值校验与脱敏。"""

from __future__ import annotations

import pytest

from hermes.hooks import AddContext, Allow, Block, HookContext, HookEvent
from hermes.hooks.controls import (
    HookControlError, control_error_message,
    control_failure_reason, normalize_control_value,
)


def _ctx():
    return HookContext(invocation_id="i", payload={})


# ===================== Allow =====================

def test_allow_equal():
    assert Allow() == Allow()


# ===================== Block =====================

def test_block_strips_reason():
    assert Block("  forbidden   ").reason == "forbidden"


def test_block_rejects_empty_reason():
    with pytest.raises(ValueError):
        Block("   ")


def test_block_rejects_non_string_reason():
    with pytest.raises(TypeError):
        Block(123)  # type: ignore[arg-type]


def test_block_truncates_long_reason():
    assert len(Block("x" * 1000).reason) <= 300


def test_block_redacts_secrets():
    b = Block("api_key=sk-1234567890abcdef")
    assert "sk-1234567890abcdef" not in b.reason


# ===================== AddContext =====================

def test_addcontext_rejects_empty():
    with pytest.raises(ValueError):
        AddContext("   ")


def test_addcontext_rejects_too_long():
    with pytest.raises(ValueError):
        AddContext("x" * 8001)


def test_addcontext_accepts_max_length():
    AddContext("x" * 8000)


def test_addcontext_rejects_non_string():
    with pytest.raises(ValueError):
        AddContext(123)  # type: ignore[arg-type]


# ===================== normalize_control_value =====================

def test_normalize_none_means_allow():
    e = HookEvent(name="pre_tool_call", context=_ctx())
    assert isinstance(normalize_control_value(e, None), Allow)


def test_normalize_allow_passthrough():
    e = HookEvent(name="pre_tool_call", context=_ctx())
    a = Allow()
    assert normalize_control_value(e, a) is a


def test_normalize_block_passthrough():
    e = HookEvent(name="pre_tool_call", context=_ctx())
    b = Block("x")
    assert normalize_control_value(e, b) is b


def test_normalize_addcontext_only_pre_llm():
    pre_llm = HookEvent(name="pre_llm_call", context=_ctx())
    pre_tool = HookEvent(name="pre_tool_call", context=_ctx())
    ac = AddContext("x")
    assert normalize_control_value(pre_llm, ac) is ac
    with pytest.raises(HookControlError):
        normalize_control_value(pre_tool, ac)


def test_normalize_non_control_event_raises():
    e = HookEvent(name="post_tool_call", context=_ctx())
    with pytest.raises(HookControlError):
        normalize_control_value(e, Allow())


def test_normalize_invalid_value_raises():
    e = HookEvent(name="pre_tool_call", context=_ctx())
    with pytest.raises(HookControlError):
        normalize_control_value(e, "invalid")  # type: ignore[arg-type]


# ===================== error helpers =====================

def test_control_failure_reason_is_generic():
    assert "failed" in control_failure_reason().lower()


def test_control_error_message_redacts():
    msg = control_error_message(ValueError("api_key=sk-secret"))
    assert "sk-secret" not in msg


def test_control_error_message_fallback_on_empty():
    assert control_error_message(ValueError("   ")) == control_failure_reason()
