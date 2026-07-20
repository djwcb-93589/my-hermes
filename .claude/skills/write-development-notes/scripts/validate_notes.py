#!/usr/bin/env python3
"""Validate the problem-driven structure of a development note range."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SECTION_RE = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$")
SUBSECTION_RE = re.compile(r"^####\s+(\d+)\.(\d+)\s+(.+?)\s*$")
EXPECTED_SUBSECTIONS = (
    (1, "问题描述"),
    (2, "原因分析"),
    (3, "解决办法"),
)
FORBIDDEN_PATTERNS = (
    (re.compile(r"\b\d+\s+passed\b", re.IGNORECASE), "test result count"),
    (re.compile(r"\b(?:pytest|xfailed|deselected|compileall|ruff)\b", re.IGNORECASE),
     "test or tool process"),
    (re.compile(r"测试过程|测试结果|回归结果|验证结果"), "test/validation record"),
    (re.compile(r"修改范围|修改文件|本次实际修改"), "mechanical change list"),
    (re.compile(r"新增配置|配置说明|配置项|配置修改"), "configuration change record"),
)
LEARNING_AID_RE = re.compile(
    r"~~~|```|^\s*\|.+\|\s*$|例如|例子|示例|时序|场景|前后对比",
    re.MULTILINE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate numbered development-note sections."
    )
    parser.add_argument("path", type=Path, help="Markdown development note")
    parser.add_argument(
        "--start-section",
        type=int,
        required=True,
        help="First numbered section written or reorganized in this update",
    )
    return parser.parse_args()


def nonempty_body(lines: list[str]) -> bool:
    return any(line.strip() and not line.lstrip().startswith("<!--") for line in lines)


def validate(path: Path, start_section: int) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    sections: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match and int(match.group(1)) >= start_section:
            sections.append((int(match.group(1)), index, match.group(2)))

    errors: list[str] = []
    if not sections:
        return [f"no section found at or after {start_section}"]
    if sections[0][0] != start_section:
        errors.append(
            f"first selected section is {sections[0][0]}, expected {start_section}"
        )

    expected_numbers = list(range(sections[0][0], sections[0][0] + len(sections)))
    actual_numbers = [number for number, _, _ in sections]
    if actual_numbers != expected_numbers:
        errors.append(
            f"section numbers are not continuous: {actual_numbers}"
        )

    selected_start = sections[0][1]
    selected_text = "\n".join(lines[selected_start:])
    for pattern, label in FORBIDDEN_PATTERNS:
        match = pattern.search(selected_text)
        if match:
            line_number = selected_start + selected_text[: match.start()].count("\n") + 1
            errors.append(f"line {line_number}: forbidden {label}: {match.group(0)!r}")

    for position, (number, start, title) in enumerate(sections):
        end = sections[position + 1][1] if position + 1 < len(sections) else len(lines)
        block = lines[start:end]
        subheadings: list[tuple[int, int, str, int]] = []
        for relative_index, line in enumerate(block):
            if not line.startswith("####"):
                continue
            match = SUBSECTION_RE.match(line)
            if match:
                subheadings.append(
                    (
                        int(match.group(1)),
                        int(match.group(2)),
                        match.group(3),
                        relative_index,
                    )
                )
            else:
                errors.append(
                    f"line {start + relative_index + 1}: malformed fourth-level heading"
                )

        expected = [(number, index, label) for index, label in EXPECTED_SUBSECTIONS]
        actual = [(owner, index, label) for owner, index, label, _ in subheadings]
        if actual != expected:
            errors.append(
                f"section {number} ({title}) must have exactly {expected}; got {actual}"
            )
            continue

        for sub_position, (_, sub_number, label, relative_start) in enumerate(subheadings):
            relative_end = (
                subheadings[sub_position + 1][3]
                if sub_position + 1 < len(subheadings)
                else len(block)
            )
            if not nonempty_body(block[relative_start + 1 : relative_end]):
                errors.append(f"section {number}.{sub_number} {label} has no body")

        if not LEARNING_AID_RE.search("\n".join(block)):
            errors.append(
                f"section {number} ({title}) has no example, timeline, table, or comparison"
            )

    return errors


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    args = parse_args()
    if not args.path.is_file():
        print(f"ERROR: note does not exist: {args.path}", file=sys.stderr)
        return 2

    errors = validate(args.path, args.start_section)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"OK: development-note sections from {args.start_section} are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
