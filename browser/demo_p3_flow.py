"""
模拟 agent 调用 browser 模块的高级读取演示脚本。

流程:
  1. navigate 到 Wikipedia 文章页
  2. get_text 整页:读连贯正文(对比 snapshot 的碎片文本)
  3. get_text(ref):读某个链接的完整文字
  4. get_text 截断:max_chars 限制返回长度
  5. console 读结构化数据:页面标题、链接数量
  6. console 读复杂对象:所有二级标题的文本数组
  7. console 改 DOM:删除一个元素,验证旧 ref 失效
  8. get_text 纯读取后 ref 仍可用:读完文本继续 click

每步打印关键信息(文本/结果截断显示)。脚本不做 assert --
目的是演示 agent 真实调用流程,人工看输出判断对错。

运行::

    uv run python -m browser.demo_p3_flow
"""

from __future__ import annotations

import json
import re
import sys

from browser.session import BrowserSession


# 文本/快照打印时截断到这个字符/行数,避免刷屏。
_TEXT_PREVIEW_CHARS = 300
_SNAPSHOT_PREVIEW_LINES = 15
_REF_PATTERN = re.compile(r"\[ref=e(\d+)[,\]]")


def _truncate(text: str, limit: int) -> str:
    """文本截断,超长加 ...(共 N 字符)。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (共 {len(text)} 字符,只显示前 {limit})"


def _truncate_snapshot(snap: str, lines: int = _SNAPSHOT_PREVIEW_LINES) -> str:
    """快照截断到前 N 行。"""
    all_lines = snap.splitlines()
    if len(all_lines) <= lines:
        return snap
    return "\n".join(all_lines[:lines]) + f"\n... (共 {len(all_lines)} 行,只显示前 {lines} 行)"


def _print_step(step: int, title: str) -> None:
    """打印步骤分隔符。"""
    print(f"\n{'=' * 60}")
    print(f"步骤 {step}: {title}")
    print(f"{'=' * 60}")


def _find_ref(snap: str, role: str, name_contains: str = "") -> str | None:
    """从快照里找指定 role(可选 name 子串)的 ref。"""
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


def main() -> int:
    # Windows 终端 GBK 会乱码;reconfigure 成 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    print("演示：用 browser 模块的高级读取（get_text + console）")

    try:
        with BrowserSession(headless=True) as s:
            # --- 步骤 1:navigate ---
            _print_step(1, "navigate 到 Wikipedia 文章页")
            nav_result = json.loads(s.navigate(url))
            snap = nav_result["snapshot"]
            snapshot_id = nav_result["snapshot_id"]
            print(f"  url={nav_result['url']}")
            print(f"  snapshot_id={snapshot_id}(快照共 {len(snap.splitlines())} 行)")

            # --- 步骤 2:get_text 整页 ---
            _print_step(2, "get_text 整页(读连贯正文,对比 snapshot 碎片)")
            result = json.loads(s.get_text(None, snapshot_id))
            print(f"  ok={result['ok']}  truncated={result['truncated']}")
            print(f"  snapshot_id 仍是 {result['snapshot_id']}(纯读取不变)")
            print(f"  整页文本预览:")
            print("    " + _truncate(result["text"], _TEXT_PREVIEW_CHARS).replace("\n", "\n    "))

            # --- 步骤 3:get_text(ref) 读链接文字 ---
            _print_step(3, "get_text(ref) 读某个链接的完整文字")
            ref = _find_ref(snap, "link", "Log in")
            if ref:
                result = json.loads(s.get_text(ref, snapshot_id))
                print(f"  ref={ref}  ok={result['ok']}")
                print(f"  链接文字: {result['text']!r}")
            else:
                print("  没找到 'Log in' 链接(跳过)")

            # --- 步骤 4:get_text 截断 ---
            _print_step(4, "get_text 整页 max_chars=100(演示截断)")
            result = json.loads(s.get_text(None, snapshot_id, max_chars=100))
            print(f"  ok={result['ok']}  truncated={result['truncated']}")
            print(f"  截断后文本长度: {len(result['text'])}")
            print(f"  文本: {result['text']!r}")

            # --- 步骤 5:console 读结构化数据 ---
            _print_step(5, "console 读结构化数据(页面标题、链接数量)")
            result = json.loads(
                s.console('document.title', snapshot_id)
            )
            print(f"  document.title = {result.get('result')!r}")
            print(f"  执行后新 snapshot_id = {result['snapshot_id']}")
            # 注意:console 后 snapshot_id 变了,后续要用新的。
            current_sid = result["snapshot_id"]

            # --- 步骤 6:console 读复杂对象 ---
            _print_step(6, "console 读复杂对象(所有 h2 标题文本)")
            expr = 'JSON.stringify([...document.querySelectorAll("h2")].map(h => h.textContent))'
            result = json.loads(s.console(expr, current_sid))
            print(f"  ok={result['ok']}")
            titles = result.get("result")
            if isinstance(titles, str):
                import json as _json
                try:
                    titles_list = _json.loads(titles)
                    print(f"  共 {len(titles_list)} 个 h2 标题:")
                    for t in titles_list[:8]:
                        print(f"    - {t}")
                except Exception:
                    print(f"  result: {titles[:200]}")
            else:
                print(f"  result: {titles}")
            current_sid = result["snapshot_id"]

            # --- 步骤 7:console 改 DOM,验证旧 ref 失效 ---
            _print_step(7, "console 改 DOM(删一个元素),验证旧 ref 失效")
            # 重新 navigate 一个简单页面做这个演示,DOM 变化更直观。
            html = '<div id="target">要删的元素</div><button id="btn">按钮</button>'
            nav = json.loads(s.navigate("data:text/html;charset=utf-8," + html))
            snap2 = nav["snapshot"]
            sid2 = nav["snapshot_id"]
            btn_ref = _find_ref(snap2, "button", "按钮")
            print(f"  按钮 ref={btn_ref}(基于 snapshot_id={sid2})")
            # 用 console 删除 target div,改变 DOM。
            result = json.loads(
                s.console('document.getElementById("target").remove()', sid2)
            )
            print(f"  console 删元素: ok={result['ok']}  新 snapshot_id={result['snapshot_id']}")
            # 用旧 sid2 操作 btn_ref 应被拒(stale_snapshot)。
            stale = json.loads(s.click(btn_ref, sid2))
            print(f"  用旧 snapshot_id 操作 ref: ok={stale['ok']}")
            print(f"    error_type={stale.get('error_type')}")
            print(f"    (说明:console 改了 DOM,旧观察结果作废,ref 失效)")

            # --- 步骤 8:get_text 纯读取后 ref 仍可用 ---
            _print_step(8, "get_text 纯读取后 ref 仍可用(读完继续操作)")
            html2 = '<button id="go">继续</button>'
            nav = json.loads(s.navigate("data:text/html;charset=utf-8," + html2))
            snap3 = nav["snapshot"]
            sid3 = nav["snapshot_id"]
            go_ref = _find_ref(snap3, "button", "继续")
            print(f"  按钮 ref={go_ref}(snapshot_id={sid3})")
            # get_text 读取。
            t = json.loads(s.get_text(go_ref, sid3))
            print(f"  get_text: ok={t['ok']}  text={t['text']!r}  sid 仍={t['snapshot_id']}")
            # 旧 sid3 仍能操作。
            click = json.loads(s.click(go_ref, sid3))
            print(f"  用同一 snapshot_id click: ok={click['ok']}")
            print(f"    (说明:get_text 不失效观察结果,ref 仍可用)")

    except KeyboardInterrupt:
        print("\n[interrupted]")
        return 130
    except ImportError as exc:
        print(f"[error] {exc}")
        print("提示:先执行 `uv add playwright`")
        return 1

    print(f"\n{'=' * 60}")
    print("高级读取演示完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
