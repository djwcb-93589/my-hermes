"""
CLI:``python -m browser <url> [--action ...]``。

示例::

    # 读取:navigate + 打印快照
    python -m browser https://en.wikipedia.org

    # 点击:--ref e1(适合页面结构稳定时的单次调试)
    python -m browser https://en.wikipedia.org --action click --ref e1

    # 填表单:--ref e2 --text "python"
    python -m browser https://en.wikipedia.org --action type --ref e2 --text "python"

    # 按键
    python -m browser https://en.wikipedia.org --action press --key Enter

    # 下拉选择
    python -m browser https://example.com --action select --ref e3 --value "option1"

    # P2 单页操作:reload / scroll
    python -m browser https://en.wikipedia.org --action reload
    python -m browser https://en.wikipedia.org --action scroll --direction down

    # P3 高级读取:get_text(整页或元素文本)/ console(执行 JS)
    python -m browser https://en.wikipedia.org --action get_text
    python -m browser https://en.wikipedia.org --action get_text --ref e1
    python -m browser https://en.wikipedia.org --action console --expression "document.title"

    # P4 条件等待
    python -m browser https://example.com --action wait_url --url-pattern "https://example.com/*"
    python -m browser https://example.com --action wait_text --wait-text "Example Domain"
    python -m browser https://example.com --action wait_ref --ref e1
    python -m browser https://example.com --action wait_load --load-state domcontentloaded

所有动作都先 navigate 到 url，再使用这次导航在当前进程内生成的
snapshot_id 执行动作。CLI 每次运行都是新会话，因此 back/forward 的
连续历史应使用同一个 ``BrowserSession`` 脚本或演示程序完成。
打印返回的 JSON
(``{"ok": true, "snapshot_id": ..., "snapshot": ..., "url": ...}``
或 ``{"ok": false, ...}``)。
"""

from __future__ import annotations

import argparse
import json
import sys

from browser.session import BrowserSession


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m browser",
        description="打开 URL,执行读取或交互动作,打印结果(独立测试用)",
    )
    parser.add_argument("url", help="要打开的 URL")
    parser.add_argument(
        "--action",
        default="navigate",
        choices=["navigate", "click", "type", "press", "select",
                 "back", "forward", "reload", "scroll",
                 "get_text", "console",
                 "wait_url", "wait_text", "wait_ref", "wait_load"],
        help="动作(默认 navigate)",
    )
    parser.add_argument("--ref", help="目标元素的 ref(如 e1),click/type/select/get_text(可选)必填")
    parser.add_argument("--text", help="要输入的文字,type 必填")
    parser.add_argument("--key", help="要按的键(如 Enter),press 必填")
    parser.add_argument("--value", help="要选的值,select 必填")
    parser.add_argument(
        "--expression",
        help="要执行的 JS 表达式,console 必填",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=5000,
        help="get_text 返回文本最大字符数(默认 5000)",
    )
    parser.add_argument(
        "--url-pattern",
        help="wait_url 的 URL glob 模式，例如 https://example.com/*",
    )
    parser.add_argument(
        "--wait-text",
        help="wait_text 要等待出现的可见文本",
    )
    parser.add_argument(
        "--load-state",
        default="domcontentloaded",
        help="wait_load 的状态:domcontentloaded/load/networkidle(默认 domcontentloaded)",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        help="单次等待超时毫秒数；省略时沿用 --timeout",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="type 时不先清空(默认清空)",
    )
    parser.add_argument(
        "--direction",
        default="down",
        help="scroll 方向:up/down/left/right(默认 down)",
    )
    parser.add_argument(
        "--amount",
        type=int,
        default=400,
        help="scroll 像素数(默认 400)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="显示浏览器窗口(默认 headless)",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help=(
            "Playwright channel:chrome 用系统 Google Chrome(默认),"
            "msedge 用系统 Edge,none 用 Playwright 自带 Chromium"
            "(需 `playwright install chromium`)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="页面操作超时(毫秒),默认 30000",
    )
    return parser.parse_args(argv)


def _missing_arg(field: str, action: str) -> str:
    """构造"缺少参数"的 JSON 错误返回。"""
    return json.dumps(
        {"ok": False, "error_type": "missing_arg",
         "error": f"{action} 需要 --{field}"},
        ensure_ascii=False,
    )


def _run_action(s: BrowserSession, args: argparse.Namespace) -> str:
    """先 navigate 到 url,再根据 action 执行。

    所有动作都先 navigate 一次(back/forward/reload/scroll 也不例外)--
    这样 CLI 单次调用就能演示完整流程:navigate 建立初始观察结果,
    再执行动作。navigate 只返回观察结果 JSON;交互动作返回 JSON 字符串。
    """
    # 先 navigate,拿到初始 snapshot_id 供后续交互使用。
    nav_result_json = s.navigate(args.url)
    nav_result = json.loads(nav_result_json)
    if not nav_result.get("ok"):
        return nav_result_json
    snapshot_id = nav_result.get("snapshot_id")

    if args.action == "navigate":
        return nav_result_json

    if args.action == "click":
        if not args.ref:
            return _missing_arg("ref", "click")
        return s.click(args.ref, snapshot_id)
    if args.action == "type":
        if not args.ref or args.text is None:
            return _missing_arg("ref/text", "type")
        return s.type(args.ref, args.text, snapshot_id, clear=not args.no_clear)
    if args.action == "press":
        if not args.key:
            return _missing_arg("key", "press")
        return s.press(args.key, snapshot_id)
    if args.action == "select":
        if not args.ref or args.value is None:
            return _missing_arg("ref/value", "select")
        return s.select(args.ref, args.value, snapshot_id)
    # P2 动作同样使用本次导航产生的页面版本。
    if args.action == "back":
        return s.back(snapshot_id)
    if args.action == "forward":
        return s.forward(snapshot_id)
    if args.action == "reload":
        return s.reload(snapshot_id)
    if args.action == "scroll":
        return s.scroll(args.direction, snapshot_id, args.amount)
    # P3 高级读取:get_text 可选 ref(None=整页),console 执行任意 JS。
    if args.action == "get_text":
        # ref=None 传 Python None 表示整页;CLI 用 --ref 缺省即整页。
        return s.get_text(args.ref, snapshot_id, max_chars=args.max_chars)
    if args.action == "console":
        if not args.expression:
            return _missing_arg("expression", "console")
        return s.console(args.expression, snapshot_id)
    # P4 等待动作会使原 snapshot_id 失效，并在成功、超时或取消时都返回新快照。
    if args.action == "wait_url":
        if not args.url_pattern:
            return _missing_arg("url-pattern", "wait_url")
        return s.wait_for_url(
            args.url_pattern,
            snapshot_id,
            timeout_ms=args.wait_timeout,
        )
    if args.action == "wait_text":
        if not args.wait_text:
            return _missing_arg("wait-text", "wait_text")
        return s.wait_for_text(
            args.wait_text,
            snapshot_id,
            timeout_ms=args.wait_timeout,
        )
    if args.action == "wait_ref":
        if not args.ref:
            return _missing_arg("ref", "wait_ref")
        return s.wait_for_ref(
            args.ref,
            snapshot_id,
            timeout_ms=args.wait_timeout,
        )
    if args.action == "wait_load":
        return s.wait_for_load_state(
            args.load_state,
            snapshot_id,
            timeout_ms=args.wait_timeout,
        )
    return json.dumps(
        {"ok": False, "error_type": "unknown_action",
         "error": f"未知动作: {args.action}"},
        ensure_ascii=False,
    )


def main(argv: list[str] | None = None) -> int:
    # Windows 终端默认 GBK,快照含 \xa0 等 Unicode 字符会 UnicodeEncodeError。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    channel: str | None = None if args.channel.lower() == "none" else args.channel
    try:
        with BrowserSession(
            headless=not args.headed,
            timeout_ms=args.timeout,
            channel=channel,
        ) as s:
            output = _run_action(s, args)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130
    except ImportError as exc:
        print(
            f"[error] {exc}\n"
            "提示:先执行 `uv add playwright`;走系统 Chrome 时不需要 "
            "`playwright install chromium`",
            file=sys.stderr,
        )
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
