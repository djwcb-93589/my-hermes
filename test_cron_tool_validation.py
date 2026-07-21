from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes.cron.tool import (
    _prompt_absolute_paths,
    _validate_prompt_paths_within_workdir,
    handle_cron_tool,
)


def test_prompt_absolute_paths_extracts_quoted_paths_with_spaces():
    prompt = (
        'copy "E:\\reports folder\\source.docx" '
        'to "E:\\reports folder\\target.docx"'
    )

    assert _prompt_absolute_paths(prompt) == [
        "E:\\reports folder\\source.docx",
        "E:\\reports folder\\target.docx",
    ]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path semantics")
def test_prompt_git_bash_path_matches_windows_workdir():
    _validate_prompt_paths_within_workdir(
        "read /e/双周报/latest.docx",
        "E:\\双周报",
    )


def test_create_rejects_prompt_path_outside_workdir(tmp_path):
    workdir = tmp_path / "workdir"
    outside = tmp_path / "outside" / "report.docx"
    workdir.mkdir()
    args = {
        "action": "create",
        "schedule": "every 1h",
        "prompt": f'read "{outside}"',
        "toolsets": ["file"],
        "workdir": str(workdir),
    }

    result = json.loads(
        handle_cron_tool(args, gateway_db_path=str(tmp_path / "cron.db"))
    )

    assert result["ok"] is False
    assert result["error_type"] == "invalid_args"
    assert "outside workdir" in result["error"]


def test_create_duration_schedule_is_one_shot(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = json.loads(handle_cron_tool(
        {
            "action": "create",
            "schedule": "5m",
            "prompt": "list files at runtime",
            "toolsets": ["file"],
            "workdir": str(workdir),
        },
        gateway_db_path=str(tmp_path / "cron.db"),
    ))

    assert result["ok"] is True
    assert result["job"]["schedule"] == "5m"
    assert result["job"]["next_run_at"] is not None
    assert result["job"]["paused"] is False


def test_create_rejects_calendar_schedule_without_recurring_intent(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = json.loads(handle_cron_tool(
        {
            "action": "create",
            "schedule": "0 9 1 1 *",
            "prompt": "list files at runtime",
            "toolsets": ["file"],
            "workdir": str(workdir),
        },
        gateway_db_path=str(tmp_path / "cron.db"),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "invalid_args"
    assert "recurring=true" in result["error"]


def test_create_allows_explicit_calendar_recurrence(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = json.loads(handle_cron_tool(
        {
            "action": "create",
            "schedule": "0 9 1 1 *",
            "recurring": True,
            "prompt": "list files at runtime",
            "toolsets": ["file"],
            "workdir": str(workdir),
        },
        gateway_db_path=str(tmp_path / "cron.db"),
    ))

    assert result["ok"] is True
    assert result["job"]["schedule"] == "0 9 1 1 *"
    assert result["job"]["paused"] is False


def test_update_rejects_calendar_schedule_without_recurring_intent(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    database = str(tmp_path / "cron.db")
    created = json.loads(handle_cron_tool(
        {
            "action": "create",
            "schedule": "5m",
            "prompt": "list files at runtime",
            "toolsets": ["file"],
            "workdir": str(workdir),
        },
        gateway_db_path=database,
    ))

    result = json.loads(handle_cron_tool(
        {
            "action": "update",
            "job_id": created["job"]["job_id"],
            "schedule": "0 9 1 1 *",
        },
        gateway_db_path=database,
    ))

    assert result["ok"] is False
    assert result["error_type"] == "invalid_args"
    assert "recurring=true" in result["error"]


def test_update_rejects_prompt_path_outside_workdir(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    database = str(tmp_path / "cron.db")
    created = json.loads(handle_cron_tool(
        {
            "action": "create",
            "schedule": "every 1h",
            "prompt": f'read "{workdir / "report.docx"}"',
            "toolsets": ["file"],
            "workdir": str(workdir),
        },
        gateway_db_path=database,
    ))
    assert created["ok"] is True

    updated = json.loads(handle_cron_tool(
        {
            "action": "update",
            "job_id": created["job"]["job_id"],
            "prompt": f'read "{tmp_path / "outside" / "report.docx"}"',
        },
        gateway_db_path=database,
    ))

    assert updated["ok"] is False
    assert updated["error_type"] == "invalid_args"
    current = json.loads(handle_cron_tool(
        {"action": "get", "job_id": created["job"]["job_id"]},
        gateway_db_path=database,
    ))
    assert current["job"]["version"] == 1
    assert current["job"]["prompt"] == created["job"]["prompt"]
