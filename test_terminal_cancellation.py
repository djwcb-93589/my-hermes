from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

from hermes.backends.local import LocalBackend
from hermes.conversation import _dispatch_conversation_tool_call
from hermes.tools import terminal as terminal_tool


class _CancelledBackend:
    def __init__(self):
        self.cwd = "workspace"
        self.cancel_checker = None

    def execute(self, command, *, cancel_checker=None):
        self.cancel_checker = cancel_checker
        return {
            "output": "(cancelled)",
            "returncode": 130,
            "cancelled": True,
        }


class _CapturingRegistry:
    def __init__(self):
        self.kwargs = None

    def dispatch(self, name, args, **kwargs):
        self.kwargs = kwargs
        return json.dumps({"ok": True})


def test_terminal_forwards_cancel_checker_and_returns_cancelled(monkeypatch):
    backend = _CancelledBackend()
    checker = lambda: True  # noqa: E731
    monkeypatch.setattr(
        terminal_tool,
        "get_backend",
        lambda session_key: backend,
    )

    payload = json.loads(terminal_tool.run_terminal(
        {"command": "python long_running.py"},
        cancel_checker=checker,
    ))

    assert backend.cancel_checker is checker
    assert payload["ok"] is False
    assert payload["error_type"] == "cancelled"
    assert payload["exit_code"] == 130


def test_conversation_dispatch_injects_internal_cancel_checker():
    registry = _CapturingRegistry()
    checker = lambda: False  # noqa: E731
    loop = SimpleNamespace(
        allowed_tool_names=None,
        tool_context={},
        session_key="conversation-1",
        cancel_checker=checker,
        registry=registry,
    )
    tool_call = SimpleNamespace(function=SimpleNamespace(
        name="terminal",
        arguments=json.dumps({"command": "python long_running.py"}),
    ))

    _dispatch_conversation_tool_call(loop, tool_call)

    assert registry.kwargs["session_key"] == "conversation-1"
    assert registry.kwargs["cancel_checker"] is checker


def test_local_backend_cancel_interrupts_running_python(tmp_path):
    backend = LocalBackend(cwd=str(tmp_path), timeout=20)
    backend.init_session()
    cancelled = threading.Event()
    timer = threading.Timer(0.5, cancelled.set)
    started_at = time.monotonic()
    timer.start()
    try:
        result = backend.execute(
            'python -c "import time; time.sleep(30)"',
            cancel_checker=cancelled.is_set,
        )
    finally:
        timer.cancel()
        backend.cleanup()

    assert result["cancelled"] is True
    assert result["returncode"] == 130
    assert time.monotonic() - started_at < 8


def test_local_backend_cancel_force_stops_process_ignoring_interrupt(tmp_path):
    backend = LocalBackend(cwd=str(tmp_path), timeout=20)
    backend.init_session()
    cancelled = threading.Event()
    timer = threading.Timer(0.5, cancelled.set)
    signal_name = "signal.SIGBREAK" if sys.platform == "win32" else "signal.SIGINT"
    command = (
        'python -c "import signal,time; '
        f"signal.signal({signal_name}, signal.SIG_IGN); time.sleep(30)\""
    )
    started_at = time.monotonic()
    timer.start()
    try:
        result = backend.execute(
            command,
            cancel_checker=cancelled.is_set,
        )
    finally:
        timer.cancel()
        backend.cleanup()

    assert result["cancelled"] is True
    assert result["returncode"] == 130
    assert time.monotonic() - started_at < 8
