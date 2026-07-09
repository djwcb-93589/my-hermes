from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def _saw_tool_call(messages: list[dict], tool_name: str) -> bool:
    for m in messages:
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            if fn.get("name") == tool_name:
                return True
    return False


def main() -> int:
    if os.getenv("RUN_LIVE_LLM_TEST") != "1":
        print(
            "SKIP: this script calls the real LLM. "
            "Run with RUN_LIVE_LLM_TEST=1 python delegate_live_smoke.py"
        )
        return 0

    workdir = Path(tempfile.mkdtemp(prefix="myhermes_delegate_live_"))
    old_cwd = Path.cwd()

    try:
        os.chdir(workdir)

        from hermes.config import API_KEY, MODEL, BASE_URL
        from hermes.tools import register_all
        from hermes.db import init_db, create_session
        from hermes.backends import cleanup_all_backends
        import hermes.conversation as conversation

        if not API_KEY:
            print("SKIP: no API key configured in OPENAI_API_KEY or config.yaml")
            return 0

        register_all()

        conn = init_db(str(workdir / "live_smoke.sqlite"))
        session_id = create_session(conn)

        cached_prompt = """
你是 my-hermes live smoke test 主 agent。

本次测试目标是验证 delegate 工具真实链路是否可用。
当用户要求委托时，你必须调用 delegate_task 工具，而不是自己直接调用 terminal 或 file 完成。
delegate 子任务完成后，给出简短总结。
""".strip()

        prompt = """
请使用 delegate_task 工具完成一个很小的子任务：
在当前工作目录下创建 reports/live_delegate_smoke.txt，
写入固定内容 live delegate ok，
然后验证文件内容。

要求：
- 必须委托给 subagent。
- 不要由主 agent 直接调用 terminal 或 file 完成。
- 子任务可以使用 terminal 和 file。
""".strip()

        print(f"[live] MODEL={MODEL}")
        print(f"[live] BASE_URL={BASE_URL}")
        print(f"[live] workdir={workdir}")

        result = conversation.run_conversation(
            prompt,
            conn,
            session_id,
            cached_prompt,
            session_key=session_id,
        )

        target = workdir / "reports" / "live_delegate_smoke.txt"
        assert target.exists(), (
            "live smoke failed: reports/live_delegate_smoke.txt was not created"
        )
        content = target.read_text(encoding="utf-8")
        assert "live delegate ok" in content, (
            f"live smoke failed: unexpected file content: {content!r}"
        )

        assert _saw_tool_call(result["messages"], "delegate_task"), (
            "live smoke failed: main agent did not call delegate_task"
        )

        print("[PASS] live delegate smoke")
        print("final_response:")
        print(result.get("final_response", ""))

        conn.close()
        cleanup_all_backends()
        return 0

    finally:
        os.chdir(old_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())