from __future__ import annotations

from hermes.prompt import build_system_prompt


def _build_prompt(tmp_path, enabled_toolsets):
    return build_system_prompt(
        str(tmp_path),
        enabled_toolsets=enabled_toolsets,
        include_soul=False,
        include_memory=False,
        include_user_profile=False,
        include_project_context=False,
    )


def test_default_prompt_routes_future_work_to_cron_first(tmp_path):
    prompt = _build_prompt(tmp_path, None)

    assert "# Scheduled Task Routing" in prompt
    assert "your first tool call must be cron" in prompt
    assert "Do not call any other tool first" in prompt


def test_cron_prompt_defers_task_preparation_and_runtime_discovery(tmp_path):
    prompt = _build_prompt(tmp_path, ["cron", "file", "terminal"])

    assert "Do not perform or prepare the scheduled task body" in prompt
    assert "inspecting or discovering its runtime files or directories" in prompt
    assert "calculating runtime-dependent dates" in prompt
    assert "Put runtime discovery, file-selection criteria, and date calculations" in prompt
    assert "the cron prompt so they happen when the job runs" in prompt


def test_cron_prompt_only_clarifies_unsafe_schedule_or_capability(tmp_path):
    prompt = _build_prompt(tmp_path, ["cron"])

    assert "Build the schedule, workdir, and least-privilege capabilities" in prompt
    assert "Ask a clarifying question only when a safe schedule or capability scope" in prompt


def test_cron_prompt_uses_duration_for_one_time_relative_delay(tmp_path):
    prompt = _build_prompt(tmp_path, ["cron"])

    assert "use `5m`, `2h`, or `1d`" in prompt
    assert "Never convert a relative delay into a five-field" in prompt
    assert "requires `recurring: true`" in prompt


def test_prompt_omits_cron_routing_when_cron_is_unavailable(tmp_path):
    prompt = _build_prompt(tmp_path, ["file", "terminal"])
    no_tools_prompt = _build_prompt(tmp_path, [])

    assert "# Scheduled Task Routing" not in prompt
    assert "your first tool call must be cron" not in prompt
    assert "# Scheduled Task Routing" not in no_tools_prompt
