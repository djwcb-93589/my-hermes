"""
模拟 agent 调用 browser 模块完成 Wikipedia 搜索的演示脚本。

流程:
  1. navigate 到 Wikipedia 首页,看快照
  2. 从快照找搜索框 ref
  3. type 填入搜索词
  4. press Enter 提交搜索
  5. 从搜索结果页找第一个结果 link
  6. click 进入文章页
  7. 确认最终 URL

每步打印关键信息(快照截断显示,避免刷屏)。脚本不做 assert --
目的是演示 agent 真实调用流程,人工看输出判断对错。

运行::

    uv run python -m browser.demo_agent_flow
"""

from __future__ import annotations

import json
import re
import sys

from browser.session import BrowserSession


# 快照打印时截断到这个行数,避免刷屏。
_SNAPSHOT_PREVIEW_LINES = 25
_REF_PATTERN = re.compile(r"\[ref=e(\d+)[,\]]")


def _truncate_snapshot(snap: str, lines: int = _SNAPSHOT_PREVIEW_LINES) -> str:
    """快照太长,只打印前 N 行,末尾加 ...(共 X 行)。"""
    all_lines = snap.splitlines()
    if len(all_lines) <= lines:
        return snap
    return "\n".join(all_lines[:lines]) + f"\n... (共 {len(all_lines)} 行,只显示前 {lines} 行)"


def _find_ref(snap: str, role: str, name_contains: str = "") -> str | None:
    """从快照里找指定 role(可选 name 子串)的 ref。返回 "e3" 或 None。"""
    for line in snap.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(role + " "):
            continue
        if name_contains and name_contains not in stripped:
            continue
        m = _REF_PATTERN.search(stripped)
        if m:
            return f"e{m.group(1)}"
    return None


def _print_step(step: int, title: str) -> None:
    """打印步骤分隔符。"""
    print(f"\n{'=' * 60}")
    print(f"步骤 {step}: {title}")
    print(f"{'' * 60}")


def _print_snapshot(snap: str) -> None:
    """打印截断后的快照。"""
    print(_truncate_snapshot(snap))


def _print_result(result_json: str) -> None:
    """打印交互操作的 JSON 返回(快照字段截断)。"""
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        print(f"  [非 JSON 返回] {result_json[:200]}")
        return
    if not result.get("ok"):
        print(f"  [失败] {result}")
        return
    print("  [成功] 返回新快照:")
    print(_truncate_snapshot(result.get("snapshot", "")))


def _successful_observation(result_json: str) -> tuple[str, str]:
    """解析成功观察结果，返回快照和供下一步操作使用的版本号。"""
    result = json.loads(result_json)
    if not result.get("ok"):
        raise RuntimeError(f"观察失败: {result}")
    return result["snapshot"], result["snapshot_id"]


def main() -> int:
    # Windows 终端 GBK 会乱码;reconfigure 成 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    search_term = "python programming language"
    print(f"演示:用 browser 模块在 Wikipedia 搜索 '{search_term}'")

    try:
        with BrowserSession(headless=True) as s:
            # --- 步骤 1:navigate 到 Wikipedia 首页 ---
            _print_step(1, "navigate 到 Wikipedia 首页")
            snap, snapshot_id = _successful_observation(
                s.navigate("https://en.wikipedia.org/")
            )
            _print_snapshot(snap)

            # --- 步骤 2:从快照找搜索框 ref ---
            _print_step(2, "从快照找搜索框(searchbox)")
            search_ref = _find_ref(snap, "searchbox", "Search")
            if not search_ref:
                print("  [错误] 快照里找不到搜索框。流程中止。")
                return 1
            print(f"  找到搜索框 ref = {search_ref}")

            # --- 步骤 3:type 填入搜索词 ---
            _print_step(3, f"type 在 {search_ref} 填入 '{search_term}'")
            result = s.type(search_ref, search_term, snapshot_id, clear=True)
            _print_result(result)
            type_data = json.loads(result)
            if not type_data.get("ok"):
                return 1

            # --- 步骤 4:press Enter 提交搜索 ---
            _print_step(4, "press Enter 提交搜索")
            url_before = s._page.url
            result = s.press("Enter", type_data["snapshot_id"])
            url_after = s._page.url
            _print_result(result)
            print(f"  URL 变化: {url_before}")
            print(f"        ->  {url_after}")

            # --- 步骤 5:检查搜索后落地哪里 ---
            _print_step(5, "检查搜索后落地哪里")
            # Wikipedia 搜索行为:精确匹配会直接跳文章页,否则去搜索结果列表页。
            # 两种情况都要处理 -- 演示 agent 如何根据当前状态决定下一步。
            if "/wiki/" in url_after and "search" not in url_after:
                # 直接跳到文章页了(精确匹配)
                print(f"  Wikipedia 精确匹配,直接跳到文章页:")
                print(f"    {url_after}")
                article_ref = None  # 不需要再 click
            else:
                # 在搜索结果列表页,找第一个文章 link
                print(f"  在搜索结果列表页,找第一个文章 link")
                result_data = json.loads(result)
                if not result_data.get("ok"):
                    print("  [错误] press 没返回有效快照。流程中止。")
                    return 1
                result_snap = result_data["snapshot"]

                article_ref = None
                for line in result_snap.splitlines():
                    stripped = line.lstrip()
                    if not stripped.startswith("link "):
                        continue
                    m = _REF_PATTERN.search(stripped)
                    if not m:
                        continue
                    # 跳过导航 link
                    name_lower = stripped.lower()
                    if any(skip in name_lower for skip in (
                        "log in", "create account", "donate", "help",
                        "about", "disclaimers", "main page", "contents",
                        "featured", "current events", "random article",
                        "special", "upload", "contributions", "talk",
                    )):
                        continue
                    article_ref = f"e{m.group(1)}"
                    print(f"  找到候选文章 link: {stripped[:80]}")
                    print(f"  ref = {article_ref}")
                    break

                if not article_ref:
                    print("  [错误] 搜索结果里找不到文章 link。流程中止。")
                    print("  快照前 40 行供检查:")
                    print(_truncate_snapshot(result_snap, 40))
                    return 1

            # --- 步骤 6:如果还在列表页,click 进入文章页 ---
            if article_ref is None:
                _print_step(6, "已在文章页,跳过 click 步骤")
            else:
                _print_step(6, f"click {article_ref} 进入文章页")
                url_before = s._page.url
                result = s.click(article_ref, result_data["snapshot_id"])
                url_after = s._page.url
                _print_result(result)
                print(f"  URL 变化: {url_before}")
                print(f"        ->  {url_after}")

            # --- 步骤 7:确认最终 URL ---
            _print_step(7, "最终状态确认")
            print(f"  最终 URL: {s._page.url}")
            # 验证跳到了文章页(URL 含 /wiki/)。
            if "/wiki/" in s._page.url:
                print(f"  [OK] 成功跳转到文章页")
            else:
                print(f"  [警告] URL 不含 /wiki/,可能没跳到文章页")

            # --- 额外演示:snapshot 取当前页快照 ---
            _print_step(8, "snapshot 取文章页快照(看标题)")
            final_snap, _ = _successful_observation(s.snapshot())
            print(f"  快照共 {len(final_snap.splitlines())} 行")
            # 找第一个含 "Python" 的 heading 行(不限行数)
            for line in final_snap.splitlines():
                if "heading" in line.lower() and "Python" in line:
                    print(f"  文章标题行: {line.strip()}")
                    break
            else:
                print("  [未找到含 Python 的 heading 行]")

    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 130
    except ImportError as exc:
        print(f"[error] {exc}")
        print("提示:先执行 `uv add playwright`")
        return 1

    print(f"\n{'=' * 60}")
    print("演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
