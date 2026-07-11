from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import hermes.tools.file as file_tool
import hermes.tools.memory as memory_tool
import hermes.tools.terminal as terminal_tool
from hermes.prompt import build_system_prompt


class _Backend:
    def __init__(self, root: Path):
        self.cwd = str(root / "workspace")
        self.file_root = str(root)
        Path(self.cwd).mkdir(parents=True)

    def execute(self, command: str) -> dict:
        return {"output": "done\n", "returncode": 0}

    def resolve_path(self, rel_path: str) -> str:
        path = Path(rel_path)
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return str(path)

    def stat_file(self, path: str) -> dict:
        stat = Path(path).stat()
        return {
            "size": stat.st_size,
            "is_dir": Path(path).is_dir(),
            "is_file": Path(path).is_file(),
            "mtime": stat.st_mtime,
        }

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(limit) if limit is not None else f.read()

    def list_dir(self, path: str) -> list[str]:
        return [entry.name for entry in Path(path).iterdir()]


def test_terminal_result_exposes_persisted_session_state(tmp_path, monkeypatch):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)

    result = json.loads(terminal_tool.run_terminal({"command": "echo done"}))

    assert result == {
        "ok": True,
        "command_succeeded": True,
        "output": "done",
        "exit_code": 0,
        "cwd": backend.cwd,
        "cwd_persisted": True,
        "environment_persisted": True,
    }


def test_file_stat_returns_explicit_utc_and_local_times(tmp_path, monkeypatch):
    backend = _Backend(tmp_path)
    target = Path(backend.cwd) / "report.md"
    target.write_text("test", encoding="utf-8")
    monkeypatch.setattr(file_tool, "get_backend", lambda session_key: backend)

    result = json.loads(file_tool.handle_file({
        "action": "stat",
        "path": "report.md",
    }))

    assert result["ok"] is True
    assert datetime.fromisoformat(result["mtime_utc"]).tzinfo is not None
    assert datetime.fromisoformat(result["mtime_local"]).tzinfo is not None
    assert result["mtime_timezone"]
    assert result["cwd"] == backend.cwd


def test_memory_initializes_directory_and_returns_unambiguous_counts(
    tmp_path, monkeypatch,
):
    memory_dir = tmp_path / "missing" / "memories"
    memory_file = memory_dir / "MEMORY.md"
    user_file = memory_dir / "USER.md"
    monkeypatch.setattr(memory_tool, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(memory_tool, "MEMORY_FILE", memory_file)
    monkeypatch.setattr(memory_tool, "USER_FILE", user_file)

    added = json.loads(memory_tool.handle_memory({
        "action": "add",
        "content": "remember this",
    }))
    removed = json.loads(memory_tool.handle_memory({
        "action": "remove",
        "content": "remember this",
    }))
    current = json.loads(memory_tool.handle_memory({"action": "read"}))

    assert added["ok"] is True
    assert added["action"] == "add"
    assert added["entry_count"] == 1
    assert removed["action"] == "remove"
    assert removed["entry_count"] == 0
    assert current["entry_count"] == 0
    for field in (
        "count", "added_count", "removed_count",
        "replaced_count", "remaining_count",
    ):
        assert field not in added
        assert field not in removed
        assert field not in current


def test_system_prompt_contains_only_compact_cross_tool_guidance(tmp_path):
    prompt = build_system_prompt(str(tmp_path))

    assert "Prefer the file tool for file content" in prompt
    assert "Relative file paths start at the current session cwd" in prompt
    assert "do not repeat that directory prefix" in prompt
    assert "never repaired by guessing paths in terminal" in prompt
    assert "Hermes home:" in prompt
