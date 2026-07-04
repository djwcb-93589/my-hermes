"""
Built-in unit tests (--test mode).

No pytest: each test asserts directly and prints OK / fails loudly. Covers
s13 (text utils), s14 (backends), and s15 (cron) utilities.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime

from hermes.backends.local import LocalBackend
from hermes.backends import create_backend
from hermes.cron.job import CronJob
from hermes.cron.parser import (
    parse_schedule,
    _parse_duration,
    _parse_cron_field,
)
from hermes.cron.scheduler import JobScheduler
from hermes.cron.store import JobStore, get_job_store, set_job_store
from hermes.cron.tool import handle_cron_tool
from hermes.gateway.text_utils import (
    utf16_len,
    truncate_utf16,
    MessageDeduplicator,
)


def run_unit_tests():
    """Quick self-tests for s13 + s14 + s15 utilities."""
    print("=== s15: Unit Tests ===\n")

    # --- s13 tests ---
    assert utf16_len("hello") == 5
    assert utf16_len("😀") == 2
    print("  utf16_len ........... OK")

    assert truncate_utf16("hello", 3) == "hel"
    assert truncate_utf16("a😀b", 3) == "a😀"
    print("  truncate_utf16 ...... OK")

    dedup = MessageDeduplicator(max_size=3)
    assert not dedup.is_duplicate("a")
    assert dedup.is_duplicate("a")
    assert not dedup.is_duplicate("b")
    assert not dedup.is_duplicate("c")
    assert not dedup.is_duplicate("d")
    assert not dedup.is_duplicate("a")
    print("  MessageDeduplicator . OK")

    # --- s14 tests ---
    backend = LocalBackend(cwd=os.getcwd())
    result = backend.execute("echo hello_from_s14")
    assert "hello_from_s14" in result["output"]
    assert result["returncode"] == 0
    print("  LocalBackend.execute  OK")

    backend.execute("export TEST_S14_VAR=persistent_value")
    result2 = backend.execute("echo $TEST_S14_VAR")
    assert "persistent_value" in result2["output"]
    print("  Session snapshot .... OK")

    backend.execute("cd /tmp")
    assert backend.cwd == "/tmp"
    print("  CWD tracking ........ OK")

    os.environ["OPENAI_API_KEY"] = "sk-test-secret-12345"
    result3 = backend.execute("echo $OPENAI_API_KEY")
    assert "sk-test-secret-12345" not in result3["output"]
    del os.environ["OPENAI_API_KEY"]
    print("  Secret filtering .... OK")

    backend.cleanup()
    print("  LocalBackend.cleanup  OK")

    test_config = {"terminal": {"backend": "local"}}
    b = create_backend(test_config)
    assert isinstance(b, LocalBackend)
    b.cleanup()
    print("  create_backend ...... OK")

    # --- s15 tests ---
    assert _parse_duration("30m") == 1800
    assert _parse_duration("2h") == 7200
    assert _parse_duration("1d") == 86400
    print("  _parse_duration ..... OK")

    now = time.time()
    ts, one_shot = parse_schedule("30m")
    assert abs(ts - (now + 1800)) < 2
    assert one_shot is True
    print("  parse_schedule(30m) . OK")

    ts2, one_shot2 = parse_schedule("every 2h")
    assert abs(ts2 - (now + 7200)) < 2
    assert one_shot2 is False
    print("  parse_schedule(every) OK")

    ts3, one_shot3 = parse_schedule("0 9 * * *")
    assert ts3 > now
    assert one_shot3 is False
    print("  parse_schedule(cron)  OK")

    m_star = _parse_cron_field("*", (0, 59))
    assert m_star(0) and m_star(30) and m_star(59)

    m_step = _parse_cron_field("*/15", (0, 59))
    assert m_step(0) and m_step(15) and m_step(30) and not m_step(7)

    m_range = _parse_cron_field("1-5", (0, 6))
    assert m_range(1) and m_range(3) and m_range(5) and not m_range(0) and not m_range(6)

    m_list = _parse_cron_field("1,3,5", (1, 31))
    assert m_list(1) and m_list(3) and m_list(5) and not m_list(2)
    print("  _parse_cron_field ... OK")

    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    store = JobStore(path=tmp.name)

    job = CronJob(
        job_id="test001",
        schedule="30m",
        prompt="test prompt",
        session_key="cli",
        created_at=datetime.now().isoformat(),
        next_fire=time.time() + 1800,
        one_shot=True,
    )
    store.add(job)
    assert len(store.list_all()) == 1
    assert store.list_all()[0].job_id == "test001"

    store2 = JobStore(path=tmp.name)
    assert len(store2.list_all()) == 1
    assert store2.list_all()[0].prompt == "test prompt"
    print("  JobStore CRUD ....... OK")

    store.advance(job)
    assert len(store.list_all()) == 0
    print("  JobStore advance .... OK")

    recurring_job = CronJob(
        job_id="test002",
        schedule="every 1h",
        prompt="recurring test",
        session_key="cli",
        created_at=datetime.now().isoformat(),
        next_fire=time.time() - 10,
        one_shot=False,
    )
    store.add(recurring_job)
    old_fire = recurring_job.next_fire
    store.advance(recurring_job)
    assert len(store.list_all()) == 1
    assert recurring_job.next_fire > old_fire
    print("  JobStore recurring .. OK")

    store.remove("test002")
    os.unlink(tmp.name)

    # JobScheduler: fires due jobs
    fired_jobs = []
    fire_store = JobStore(path=tmp.name + ".sched")

    due_job = CronJob(
        job_id="sched001",
        schedule="1s",
        prompt="fire me",
        session_key="cli",
        created_at=datetime.now().isoformat(),
        next_fire=time.time() - 1,
        one_shot=True,
    )
    fire_store.add(due_job)

    scheduler = JobScheduler(
        fire_store,
        fire_callback=lambda j: fired_jobs.append(j.job_id),
        interval=1,
    )
    scheduler.start()
    time.sleep(2)
    scheduler.stop()

    assert "sched001" in fired_jobs
    assert len(fire_store.list_all()) == 0
    print("  JobScheduler ........ OK")

    try:
        os.unlink(tmp.name + ".sched")
    except FileNotFoundError:
        pass

    # cron tool: create + list + delete (uses the global store)
    old_store = get_job_store()
    tmp2 = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp2.close()
    set_job_store(JobStore(path=tmp2.name))

    result_create = handle_cron_tool(
        {"action": "create", "schedule": "30m", "prompt": "test cron tool"},
        session_key="test_session",
    )
    assert "created" in result_create
    print("  cron_tool create .... OK")

    result_list = handle_cron_tool({"action": "list"})
    assert "test cron tool" in result_list
    print("  cron_tool list ...... OK")

    jobs = get_job_store().list_all()
    assert len(jobs) == 1
    jid = jobs[0].job_id

    result_delete = handle_cron_tool({"action": "delete", "job_id": jid})
    assert "deleted" in result_delete
    assert len(get_job_store().list_all()) == 0
    print("  cron_tool delete .... OK")

    set_job_store(old_store)
    os.unlink(tmp2.name)

    print("\nAll s15 unit tests passed.")
