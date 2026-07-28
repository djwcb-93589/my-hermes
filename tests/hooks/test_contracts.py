"""契约不可变性与校验。"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from hermes.hooks import HookContext, HookEvent, HookRegistration
from hermes.hooks.contracts import _freeze_value


# ===================== HookContext 冻结 =====================

def test_payload_frozen_to_mappingproxy(ctx):
    assert isinstance(ctx.payload, MappingProxyType)
    with pytest.raises(TypeError):
        ctx.payload["new"] = "x"  # type: ignore[index]


def test_metadata_frozen(ctx):
    assert isinstance(ctx.metadata, MappingProxyType)


def test_nested_dict_frozen():
    c = HookContext(invocation_id="i", payload={"outer": {"inner": 1}})
    assert isinstance(c.payload["outer"], MappingProxyType)
    with pytest.raises(TypeError):
        c.payload["outer"]["inner"] = 999  # type: ignore[index]


def test_nested_list_becomes_tuple():
    c = HookContext(invocation_id="i", payload={"items": [1, 2, 3]})
    assert c.payload["items"] == (1, 2, 3)
    assert isinstance(c.payload["items"], tuple)


def test_set_becomes_frozenset():
    c = HookContext(invocation_id="i", payload={"s": {1, 2}})
    assert isinstance(c.payload["s"], frozenset)


def test_rejects_unsupported_type():
    class Custom:
        pass
    with pytest.raises(TypeError):
        HookContext(invocation_id="i", payload={"obj": Custom()})


def test_rejects_cyclic_container():
    d: dict = {}
    d["self"] = d
    with pytest.raises(TypeError):
        HookContext(invocation_id="i", payload=d)


def test_rejects_non_string_key():
    with pytest.raises(TypeError):
        HookContext(invocation_id="i", payload={1: "x"})  # type: ignore[dict-item]


def test_invocation_id_must_be_str_or_none():
    with pytest.raises(TypeError):
        HookContext(invocation_id=123)  # type: ignore[arg-type]
    c = HookContext(invocation_id=None, payload={})
    assert c.invocation_id is None


def test_default_factories_isolated():
    a = HookContext()
    b = HookContext()
    assert a is not b
    assert a.payload == {} == b.payload


# ===================== HookEvent 校验 =====================

def test_event_name_normalized(event):
    e = event(name="  pre_llm_call  ")
    assert e.name == "pre_llm_call"


def test_event_name_rejects_empty():
    with pytest.raises(ValueError):
        HookEvent(name="", context=HookContext())


def test_event_name_rejects_non_string():
    with pytest.raises(ValueError):
        HookEvent(name=123, context=HookContext())  # type: ignore[arg-type]


def test_event_context_must_be_hook_context():
    with pytest.raises(TypeError):
        HookEvent(name="post_tool_call", context="not a context")  # type: ignore[arg-type]


# ===================== HookRegistration =====================

def test_registration_is_frozen():
    reg = HookRegistration(
        event_name="pre_tool_call", hook_id="h1",
        callback=lambda ctx: None, timeout_seconds=1.0,
    )
    with pytest.raises(Exception):
        reg.hook_id = "h2"  # type: ignore[misc]
    assert reg.timeout_seconds == 1.0


def test_registration_callback_not_compared():
    r1 = HookRegistration(event_name="e", hook_id="h", callback=lambda ctx: None)
    r2 = HookRegistration(event_name="e", hook_id="h", callback=lambda ctx: None)
    assert r1 == r2  # callback compare=False


# ===================== _freeze_value 边界 =====================

def test_freeze_scalar_passthrough():
    assert _freeze_value(42, path="p", ancestors=set()) == 42
    assert _freeze_value("s", path="p", ancestors=set()) == "s"
    assert _freeze_value(True, path="p", ancestors=set()) is True
    assert _freeze_value(None, path="p", ancestors=set()) is None


def test_freeze_deeply_nested():
    value = {"a": {"b": {"c": [1, {"d": 2}]}}}
    frozen = _freeze_value(value, path="p", ancestors=set())
    assert frozen["a"]["b"]["c"][1]["d"] == 2
    assert isinstance(frozen["a"]["b"]["c"], tuple)
