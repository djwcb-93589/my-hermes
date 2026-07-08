"""
skill 工具 LLM loop 端到端测试。

这个测试不直接调用 skill handler，而是启动 run_conversation()，
让模型根据自然语言提示词自行调用 skills_list / skill_manage / skill_view。

覆盖场景：
1. 临时重定向 SKILLS_DIR，避免污染真实 skills 目录。
2. 预置一个合法 skill 和一个非法目录名 skill。
3. 验证 skills_list 不返回非法目录名 skill。
4. 让 LLM 创建一个真实任务型 skill。
5. 让 LLM patch 该 skill。
6. 让 LLM view 该 skill 并确认内容。
7. 让 LLM delete 该 skill。
8. 验证测试结束后产生的 skill 文件已经删除。
9. finally 中清理临时 skills 目录和测试数据库。

用法：
    python skill_loop_e2e.py

要求：
    需要可用的 OPENAI_API_KEY / config.yaml API key。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from hermes.config import API_KEY
from hermes.conversation import run_conversation
from hermes.db import create_session, init_db
from hermes.prompt import build_system_prompt
from hermes.tools import register_all
from hermes.tools import skill as sk


class SkillLoopCtx:
    """skill loop e2e 测试上下文。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hermes-skill-loop-e2e-"))
        self.skills_dir = self.root / "skills"
        self.workdir = self.root / "workdir"
        self.db_path = self.root / "skill_loop_e2e.db"

        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)

        self.original_skills_dir = sk.SKILLS_DIR
        sk.SKILLS_DIR = self.skills_dir

        # 注册工具。registry 是 dict 覆盖式注册，重复调用不会产生重复 schema。
        register_all()

        self.conn = init_db(str(self.db_path))
        self.session_key = f"skill-loop-e2e-{uuid.uuid4().hex[:8]}"

    def seed_skills(self) -> None:
        """预置一个合法 skill 和一个非法目录名 skill。"""

        # 合法 skill：应该被 skills_list 发现。
        legal_dir = self.skills_dir / "existing_triage"
        legal_dir.mkdir(parents=True, exist_ok=True)
        (legal_dir / "SKILL.md").write_text(
            """---
name: existing_triage
description: Existing incident triage skill used as a legal discovery fixture.
version: 1.0.0
metadata:
  scenario: fixture
---

# Existing Triage

This is a legal fixture skill.
""",
            encoding="utf-8",
        )

        # 非法目录名 skill：目录名含点号，应该被 discover_skills / skills_list 跳过。
        # 注意：这里是手动写磁盘，模拟旧版本遗留目录或人为放错目录的真实情况。
        illegal_dir = self.skills_dir / "apache.skill"
        illegal_dir.mkdir(parents=True, exist_ok=True)
        (illegal_dir / "SKILL.md").write_text(
            """---
name: apache.skill
description: This illegal on-disk skill must not appear in skills_list.
version: 1.0.0
metadata:
  scenario: illegal-fixture
---

# Illegal Apache Skill

This should be ignored by discovery.
""",
            encoding="utf-8",
        )

    def run(self, prompt: str) -> dict[str, Any]:
        """启动真实 conversation loop。"""

        session_id = create_session(self.conn)

        # system prompt 要在 seed_skills 之后构建，
        # 这样 Available Skills 也会走当前 discover_skills 逻辑。
        system_prompt = build_system_prompt(str(self.workdir))

        return run_conversation(
            prompt,
            self.conn,
            session_id,
            system_prompt,
            session_key=self.session_key,
        )

    def cleanup(self) -> None:
        """还原全局变量并删除测试产生的所有文件。"""

        try:
            self.conn.close()
        except Exception:
            pass

        sk.SKILLS_DIR = self.original_skills_dir
        shutil.rmtree(self.root, ignore_errors=True)


def _tool_calls(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """从 run_conversation 返回值中提取 tool call 序列。"""

    calls: list[tuple[str, dict[str, Any]]] = []

    for msg in result.get("messages", []):
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments") or "{}"

            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"_raw": raw_args}

            calls.append((name, args))

    return calls


def _tool_outputs(result: dict[str, Any]) -> list[dict[str, Any]]:
    """从 run_conversation 返回值中提取 tool 输出 JSON。"""

    outputs: list[dict[str, Any]] = []

    for msg in result.get("messages", []):
        if msg.get("role") != "tool":
            continue

        content = msg.get("content") or ""
        try:
            outputs.append(json.loads(content))
        except json.JSONDecodeError:
            outputs.append({"_raw": content})

    return outputs


def _paired_tool_results(
    result: dict[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """按顺序配对 tool call 和 tool output。"""

    calls = _tool_calls(result)
    outputs = _tool_outputs(result)
    return [
        (name, args, output)
        for (name, args), output in zip(calls, outputs)
    ]


def assert_loop_skill_lifecycle() -> None:
    """真实 loop 场景：list → create → patch → view → delete → list。"""

    if not API_KEY:
        raise RuntimeError(
            "缺少 API key。请设置 OPENAI_API_KEY，或在 config.yaml 中配置 api_key。"
        )

    ctx = SkillLoopCtx()
    skill_name = f"loop_triage_{uuid.uuid4().hex[:8]}"

    try:
        ctx.seed_skills()

        prompt = f"""
你正在执行一个 skill 工具端到端测试。必须只使用 skill 工具，不要使用 terminal、file、memory、delegate、cron 工具。

请严格按顺序完成下面步骤：

1. 调用 skills_list，检查当前可用 skill。
   - existing_triage 应该可见。
   - apache.skill 不应该可见。

2. 调用 skill_manage 创建一个新 skill：
   - action: create
   - name: {skill_name}
   - description: Incident triage workflow created by loop e2e test.
   - version: 1.0.0
   - platforms: ["linux", "windows"]
   - metadata:
       scenario: skill-loop-e2e
       owner: test
       cleanup_required: true
   - body 必须包含下面这些原文片段：
       LOOP_SKILL_MARKER_CREATE
       Initial severity rubric: low/medium/high
       Required cleanup: delete this skill before finishing the test

3. 调用 skill_manage patch 这个 skill：
   - name: {skill_name}
   - old_text: Initial severity rubric: low/medium/high
   - new_text: Initial severity rubric: informational/low/medium/high/critical

4. 调用 skill_view 查看 {skill_name}。
   确认返回内容中同时包含：
   - LOOP_SKILL_MARKER_CREATE
   - informational/low/medium/high/critical

5. 调用 skill_manage 删除 {skill_name}。
   这是测试产生的临时 skill，必须删除。

6. 再次调用 skills_list，确认 {skill_name} 已经不存在。

最后回答时必须包含这一行：
SKILL_LOOP_E2E_DONE

同时简要报告：
- existing_triage 是否可见
- apache.skill 是否被跳过
- {skill_name} 是否已删除
"""

        result = ctx.run(prompt)
        final_response = result.get("final_response") or ""
        final_lower = final_response.lower()

        pairs = _paired_tool_results(result)
        calls = _tool_calls(result)

        print("\n=== Final response ===")
        print(final_response)

        print("\n=== Tool calls ===")
        for name, args in calls:
            print(f"- {name}: {json.dumps(args, ensure_ascii=False)[:300]}")

        # 1. 必须真的走了 tool call，而不是模型空口回答。
        if not calls:
            raise AssertionError("模型没有调用任何工具，测试无效。")

        # 2. 不允许借助其它工具完成任务。
        non_skill_tools = [
            name
            for name, _ in calls
            if name not in {"skills_list", "skill_manage", "skill_view"}
        ]
        if non_skill_tools:
            raise AssertionError(f"不应调用非 skill 工具: {non_skill_tools}")

        # 3. 必须调用核心 skill 工具。
        called_names = [name for name, _ in calls]
        for required_tool in ["skills_list", "skill_manage", "skill_view"]:
            if required_tool not in called_names:
                raise AssertionError(f"缺少必要工具调用: {required_tool}")

        # 4. 必须完成 create / patch / delete 三个 manage action。
        manage_actions = [
            args.get("action")
            for name, args in calls
            if name == "skill_manage"
        ]
        for required_action in ["create", "patch", "delete"]:
            if required_action not in manage_actions:
                raise AssertionError(
                    f"skill_manage 缺少 action={required_action}; "
                    f"实际 actions={manage_actions}"
                )

        # 5. 检查 skills_list 输出：非法目录 apache.skill 不应该出现。
        list_outputs = [
            output
            for name, _args, output in pairs
            if name == "skills_list" and output.get("ok") is True
        ]
        if not list_outputs:
            raise AssertionError("没有可解析的 skills_list 成功输出。")

        first_list = list_outputs[0]
        first_names = [
            item.get("name")
            for item in first_list.get("skills", [])
        ]

        if "existing_triage" not in first_names:
            raise AssertionError(
                f"合法 fixture skill existing_triage 未被发现: {first_names}"
            )

        if "apache.skill" in first_names:
            raise AssertionError(
                "非法目录名 skill apache.skill 出现在 skills_list 中，"
                "说明发现阶段没有复用 name 校验规则。"
            )

        # 6. 检查最后一次 skills_list：临时 skill 应该已删除。
        last_list = list_outputs[-1]
        last_names = [
            item.get("name")
            for item in last_list.get("skills", [])
        ]

        if skill_name in last_names:
            raise AssertionError(
                f"临时 skill 仍出现在最后一次 skills_list 中: {skill_name}"
            )

        # 7. 检查文件系统：LLM 创建的 skill 目录必须已经被删除。
        produced_skill_dir = ctx.skills_dir / skill_name
        if produced_skill_dir.exists():
            raise AssertionError(
                f"测试产生的 skill 目录未删除: {produced_skill_dir}"
            )

        # 8. 最终回答必须包含完成标记。
        if "skill_loop_e2e_done" not in final_lower:
            raise AssertionError(
                "最终回答缺少 SKILL_LOOP_E2E_DONE，"
                f"final_response={final_response!r}"
            )

        print("\n skill loop e2e ........ OK")

    finally:
        # 无论测试成功还是失败，都删除临时 skills 目录和测试 DB。
        ctx.cleanup()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    assert_loop_skill_lifecycle()


if __name__ == "__main__":
    main()