"""
模拟 agent 调用 browser 模块的 P2 导航操作演示脚本。

流程:
  1. navigate 到 Python 文章页
  2. scroll down 滚动查看更多内容
  3. navigate 到 Rust 文章页(建立浏览历史)
  4. back 回到 Python 文章页
  5. forward 再到 Rust 文章页
  6. reload 刷新当前页
  7. back 两次回到 Python -> 再 back 触发 no_history 错误

每步打印关键信息(快照截断,避免刷屏)。脚本不做 assert --
目的是演示 agent 真实调用流程,人工看输出判断对错。

运行::

    uv run python -m browser.demo_p2_flow
"""

from __future__ import annotations

import json
import sys

from browser.session import BrowserSession


# 快照打印时截断到这个行数,避免刷屏。
_SNAPSHOT_PREVIEW_LINES = 15


def _truncate_snapshot(snap: str, lines: int = _SNAPSHOT_PREVIEW_LINES) -> str:
    """快照太长,只打印前 N 行,末尾加 ...(共 X 行)。"""
    all_lines = snap.splitlines()
    if len(all_lines) <= lines:
        return snap
    return "\n".join(all_lines[:lines]) + f"\n... (共 {len(all_lines)} 行,只显示前 {lines} 行)"


def _print_step(step: int, title: str) -> None:
    """打印步骤分隔符。"""
    print(f"\n{'=' * 60}")
    print(f"步骤 {step}: {title}")
    print(f"{'=' * 60}")


def _print_result(result_json: str, show_snapshot: bool = True) -> None:
    """打印操作的 JSON 返回。成功时截断显示快照,失败时显示错误。"""
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        print(f"  [非 JSON 返回] {result_json[:200]}")
        return
    if not result.get("ok"):
        print(f"  [失败] error_type={result.get('error_type')}")
        print(f"         error={result.get('error')}")
        # no_history 等错误仍可能带 snapshot,显示一下。
        if show_snapshot and result.get("snapshot"):
            print(f"  (附带快照,共 {len(result['snapshot'].splitlines())} 行)")
        return
    print(f"  [成功] url={result.get('url')}")
    print(f"         snapshot_id={result.get('snapshot_id')}")
    if show_snapshot:
        print(f"  快照预览:")
        print(_truncate_snapshot(result.get("snapshot", "")))


def _snapshot_id(result_json: str) -> str:
    """从操作结果取下一步必须携带的页面版本号。"""
    result = json.loads(result_json)
    snapshot_id = result.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise RuntimeError(f"结果缺少 snapshot_id: {result}")
    return snapshot_id


def main() -> int:
    # Windows 终端 GBK 会乱码;reconfigure 成 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    url_python = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    url_rust = "https://en.wikipedia.org/wiki/Rust_(programming_language)"

    print("演示:用 browser 模块的 P2 导航操作浏览 Wikipedia")

    try:
        with BrowserSession(headless=True) as s:
            # --- 步骤 1:navigate 到 Python 文章页 ---
            _print_step(1, f"navigate 到 Python 文章页")
            result = s.navigate(url_python)
            _print_result(result, show_snapshot=False)
            print(f"  (快照共 {len(json.loads(result)['snapshot'].splitlines())} 行)")
            current_snapshot_id = _snapshot_id(result)

            # --- 步骤 2:scroll down 滚动查看更多内容 ---
            _print_step(2, "scroll down 800 像素")
            result = s.scroll("down", current_snapshot_id, 800)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)
            # 验证滚动确实发生了:用 JS 检查 scrollY。
            scroll_y = s._page.evaluate("window.scrollY")
            print(f"  验证: window.scrollY = {scroll_y}(应 > 0)")

            # --- 步骤 3:navigate 到 Rust 文章页(建立历史) ---
            _print_step(3, "navigate 到 Rust 文章页(建立浏览历史)")
            result = s.navigate(url_rust)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)

            # --- 步骤 4:back 回到 Python 文章页 ---
            _print_step(4, "back 回到上一页(Python 文章页)")
            result = s.back(current_snapshot_id)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)
            assert_check = "Python" in s._page.url
            print(f"  验证: URL 含 'Python' = {assert_check}")

            # --- 步骤 5:forward 再到 Rust 文章页 ---
            _print_step(5, "forward 前进到下一页(Rust 文章页)")
            result = s.forward(current_snapshot_id)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)
            assert_check = "Rust" in s._page.url
            print(f"  验证: URL 含 'Rust' = {assert_check}")

            # --- 步骤 6:reload 刷新当前页 ---
            _print_step(6, "reload 刷新当前页")
            url_before = s._page.url
            result = s.reload(current_snapshot_id)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)
            assert_check = s._page.url == url_before
            print(f"  验证: URL 不变 = {assert_check}({s._page.url})")

            # --- 步骤 7:连续 back 演示 no_history 错误处理 ---
            _print_step(7, "连续 back 两次,演示 no_history 错误")
            print(f"  第一次 back(应回到 Python 文章页):")
            result = s.back(current_snapshot_id)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)

            print(f"  第二次 back(应返回 no_history 错误,页面保持):")
            url_before = s._page.url
            result = s.back(current_snapshot_id)
            _print_result(result, show_snapshot=False)
            current_snapshot_id = _snapshot_id(result)
            assert_check = s._page.url == url_before
            print(f"  验证: 无历史 back 后 URL 保持不变 = {assert_check}")

            # --- 额外演示:非法 scroll 参数 ---
            _print_step(8, "演示非法 scroll 参数的错误处理")
            result = s.scroll("sideways", current_snapshot_id, 100)
            _print_result(result, show_snapshot=False)

    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 130
    except ImportError as exc:
        print(f"[error] {exc}")
        print("提示:先执行 `uv add playwright`")
        return 1

    print(f"\n{'=' * 60}")
    print("P2 演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
