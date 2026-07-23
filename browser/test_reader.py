"""
browser 模块独立测试(无 pytest)。

直接运行::

    uv run python -m browser.test_reader

测试覆盖:
1. navigate 返回的快照含 link、heading 和 snapshot_id
2. ref 只出现在交互角色行上
3. 连续两次 navigate 后 cookie 仍存在(长驻 context 的核心价值)
4. 跨页 navigate 后 ref 从 e1 重新编号,且 snapshot 反映新页面内容
5. session close 后再调 snapshot 抛 RuntimeError(资源已释放)
6. click 触发导航后返回新快照(页内锚点跳转)
7. type 在 Wikipedia 搜索框填入文字,快照反映输入值
8. press Enter 提交搜索,URL 变成搜索结果页
9. select 在带下拉框的页面选项
10. 旧 snapshot_id 不能再次操作页面
11. contenteditable 元素可以输入文字
12. 跨线程访问会得到明确错误，而不是 Playwright 内部异常
13. 裸域名会补全为 HTTPS URL
14. back/forward 在两页之间来回切换
15. 无历史时 back 返回 no_history 错误,页面状态恢复
16. reload 后 URL 不变但 snapshot_id 刷新
17. scroll 不改 URL 但 snapshot_id 刷新
18. scroll 非法 direction 返回 invalid_args
19. scroll 零/负 amount 返回 invalid_args

playwright 或浏览器未装时整组 skip,不报 error。
"""

from __future__ import annotations

import json
import re
import sys
import traceback
from typing import Any

# ---------------------------------------------------------------------------
# skip 检查:playwright 未装时,打印原因并退出 0。
# ---------------------------------------------------------------------------

try:
    import playwright  # noqa: F401
except ImportError:
    print("[skip] playwright 未安装。请先执行:")
    print("  uv add playwright")
    print("  uv run playwright install chromium  # 走系统 Chrome 时不需要")
    sys.exit(0)


def _check_browser_installed() -> bool:
    """试启动一次浏览器;失败返回 False。

    用和测试一致的 ``channel="chrome"``(系统 Chrome)试,不是 Playwright
    自带 chromium。系统 Chrome 装了就过,不需要 `playwright install chromium`。
    """
    from playwright.sync_api import sync_playwright
    try:
        pw = sync_playwright().start()
        try:
            # 优先系统 Chrome;失败时退回 Playwright chromium(可能未下载)。
            try:
                browser = pw.chromium.launch(headless=True, channel="chrome")
            except Exception:
                browser = pw.chromium.launch(headless=True)
            browser.close()
        finally:
            pw.stop()
        return True
    except Exception as exc:
        print(f"[skip] 浏览器启动失败:")
        print(f"  {exc.__class__.__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# 测试用例。每个函数是独立的 test,失败抛 AssertionError。
# ---------------------------------------------------------------------------

_REF_PATTERN = re.compile(r"\[ref=e(\d+)[,\]]")
# 任何带 ref= 的行,其开头角色必须是交互角色之一。
# 这里直接复用 accessibility.py 的 INTERACTIVE_ROLES,避免两处维护。
from browser.accessibility import INTERACTIVE_ROLES  # noqa: E402


def _extract_ref_lines(snapshot: str) -> list[str]:
    """返回所有含 [ref=eN] 的行(已 rstrip)。"""
    return [line.rstrip() for line in snapshot.splitlines() if "[ref=e" in line]


def _role_of_line(line: str) -> str:
    """从缩进行里提取行首的 role 单词。"""
    stripped = line.lstrip()
    return stripped.split(" ", 1)[0] if stripped else ""


def _find_ref_for_role(snap: str, role: str, name_contains: str = "") -> str | None:
    """从快照文本里找指定 role(可选 name 子串)的 ref。

    返回 ``"e3"`` 这样的字符串;找不到返回 None。用于测试时动态定位
    元素,避免硬编码 ref 编号(每次快照 ref 会重新分配)。
    """
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


def _parse_result(result_json: str) -> dict:
    """解析交互操作的 JSON 返回。失败时抛 AssertionError。"""
    try:
        return json.loads(result_json)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"返回不是合法 JSON: {result_json!r}\n{exc}")


def _observation(result_json: str) -> tuple[str, str]:
    """解析成功观察结果，返回 ``(snapshot, snapshot_id)``。"""
    result = _parse_result(result_json)
    assert result.get("ok") is True, f"观察失败: {result}"
    snapshot = result.get("snapshot")
    snapshot_id = result.get("snapshot_id")
    assert isinstance(snapshot, str) and snapshot, f"观察结果缺少 snapshot: {result}"
    assert isinstance(snapshot_id, str) and snapshot_id, (
        f"观察结果缺少 snapshot_id: {result}"
    )
    return snapshot, snapshot_id


def test_navigate_returns_snapshot_with_link_and_heading() -> None:
    """navigate 返回的快照应含至少一个 link 和一个 heading。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, _ = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
    assert snap, "快照为空"
    # 去缩进后比较 role。
    roles = {line.lstrip().split(" ", 1)[0] for line in snap.splitlines() if line.strip()}
    assert "link" in roles, f"快照里找不到 link 角色;实际 roles: {sorted(roles)[:20]}"
    assert "heading" in roles, f"快照里找不到 heading 角色;实际 roles: {sorted(roles)[:20]}"


def test_refs_only_on_interactive_roles() -> None:
    """所有 [ref=eN] 必须出现在 INTERACTIVE_ROLES 的行上。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, _ = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
    ref_lines = _extract_ref_lines(snap)
    assert ref_lines, "快照里没有任何 ref"
    for line in ref_lines:
        role = _role_of_line(line)
        assert role in INTERACTIVE_ROLES, (
            f"非交互角色 {role!r} 却分配了 ref: {line!r}"
        )


def test_cookie_persists_across_navigates() -> None:
    """同一 session 连续 navigate 后 cookie 应保持。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        cookies_after_first = s.cookies()
        assert cookies_after_first, "第一次 navigate 后没有 cookie"

        s.navigate("https://en.wikipedia.org/wiki/Rust_(programming_language)")
        cookies_after_second = s.cookies()
        assert cookies_after_second, "第二次 navigate 后没有 cookie"

        # 至少有一个同名 cookie 跨两次 navigate 仍在 -- 证明 context 持久化。
        names_first = {c["name"] for c in cookies_after_first}
        names_second = {c["name"] for c in cookies_after_second}
        common = names_first & names_second
        assert common, (
            f"两次 navigate 没有同名 cookie;first={sorted(names_first)[:5]} "
            f"second={sorted(names_second)[:5]}"
        )


def test_refs_reset_across_pages() -> None:
    """跨页 navigate 后 ref 从 e1 重新编号,且 snapshot 反映新页面内容。

    不断言"e1 行内容不同" -- Wikipedia 全站共享"Jump to content"链接,
    两个页面的 e1 都会是它。改成断言两页 ref 行集合不同(两篇文章内容不同),
    这能抓住"navigate 后 snapshot 返回旧页面缓存"的 bug。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap_a, _ = _observation(s.navigate(
            "https://en.wikipedia.org/wiki/Python_(programming_language)"
        ))
        snap_b, _ = _observation(s.navigate(
            "https://en.wikipedia.org/wiki/Rust_(programming_language)"
        ))

    ref_lines_a = _extract_ref_lines(snap_a)
    ref_lines_b = _extract_ref_lines(snap_b)
    assert ref_lines_a and ref_lines_b, "某次快照没有 ref"

    # 两次都应从 e1 开始 -- 证明 ref 每次快照重新计数。
    first_a = _REF_PATTERN.search(ref_lines_a[0])
    first_b = _REF_PATTERN.search(ref_lines_b[0])
    assert first_a and first_a.group(1) == "1", f"A 第一次出现的 ref 不是 e1: {ref_lines_a[0]!r}"
    assert first_b and first_b.group(1) == "1", f"B 第一次出现的 ref 不是 e1: {ref_lines_b[0]!r}"

    # 两页 ref 行集合应不同 -- 抓"snapshot 缓存旧页面"的 bug。
    set_a = set(ref_lines_a)
    set_b = set(ref_lines_b)
    assert set_a != set_b, (
        "两篇文章的 ref 行集合完全相同,navigate 后 snapshot 可能返回了缓存\n"
        f"  共 {len(set_a)} 行"
    )
    # 进一步:每页都应有对方没有的元素(不只是顺序不同)。
    only_a = set_a - set_b
    only_b = set_b - set_a
    assert only_a and only_b, (
        f"两页 ref 行互相包含,可能内容太相似\n"
        f"  only_a 示例: {sorted(only_a)[:2]}\n"
        f"  only_b 示例: {sorted(only_b)[:2]}"
    )


def test_session_closed_raises_on_snapshot() -> None:
    """with 退出后再调 snapshot 应抛 RuntimeError。"""
    from browser.session import BrowserSession
    s = BrowserSession()
    with s:
        s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
    # 此时 session 已关闭。
    try:
        s.snapshot()
    except RuntimeError:
        return
    raise AssertionError("关闭后的 session 调 snapshot 没有抛 RuntimeError")


def test_click_triggers_navigation_and_returns_new_snapshot() -> None:
    """click "Jump to content" 应触发页内锚点跳转,返回新快照。

    Wikipedia 每个页面顶部都有 "Jump to content" 链接,点击后 URL 加 #bodyContent。
    验证:click 返回 ok=True,且 page.url 变化。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
        ref = _find_ref_for_role(snap, "link", "Jump to content")
        assert ref, "快照里找不到 'Jump to content' 链接"
        url_before = s._page.url
        result_json = s.click(ref, snapshot_id)
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"click 失败: {result}"
        url_after = s._page.url
        # URL 应变化(加 #bodyContent 锚点)。
        assert url_after != url_before, (
            f"click 后 URL 没变: before={url_before} after={url_after}"
        )
        # 返回的 snapshot 字段非空。
        assert result.get("snapshot"), "click 返回的 snapshot 为空"
        assert result.get("snapshot_id"), "click 返回的 snapshot_id 为空"


def test_type_fills_search_box() -> None:
    """type 在 Wikipedia 搜索框填入文字,新快照应反映输入值。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
        # Wikipedia 搜索框 role 是 searchbox,name 含 "Search"。
        ref = _find_ref_for_role(snap, "searchbox", "Search")
        assert ref, "快照里找不到搜索框(searchbox, name~Search)"
        result_json = s.type(ref, "python programming", snapshot_id, clear=True)
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"type 失败: {result}"
        # 新快照里应能看到 value='python programming'。
        new_snap = result.get("snapshot", "")
        assert "python programming" in new_snap, (
            "新快照里找不到输入的文字,可能 value 没写进去"
        )


def test_press_enter_submits_search() -> None:
    """先 type 填搜索框,再 press Enter,URL 应变成搜索结果页。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
        ref = _find_ref_for_role(snap, "searchbox", "Search")
        assert ref, "快照里找不到搜索框"
        typed = _parse_result(s.type(ref, "python programming", snapshot_id, clear=True))
        assert typed.get("ok") is True, f"type 失败: {typed}"
        url_before = s._page.url
        result_json = s.press("Enter", typed["snapshot_id"])
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"press 失败: {result}"
        url_after = s._page.url
        # Enter 提交搜索,URL 应变化(跳到 /w/index.php?search=... 或 /wiki/Python)。
        assert url_after != url_before, (
            f"press Enter 后 URL 没变: before={url_before} after={url_after}"
        )


def test_select_on_dropdown() -> None:
    """select 在 Wikipedia 设置页的语言下拉框选项。

    Wikipedia Special:Preferences 有大量 <select>。但那需要登录 --
    改用 example.com 的简单 HTML fixture 不现实(需要起 HTTP 服务器)。
    这里用 Wikipedia 首页的语言选择链接页 /wiki/Wikipedia:Contents,
    用一个已知含 select 的页面:Wikimedia 的 SiteMatrix 不行...
    退而求其次:用 data: URL 内联一个含 <select> 的 HTML,保证测试稳定。
    """
    from browser.session import BrowserSession
    html = """<!DOCTYPE html><html><body>
    <select id="s">
      <option value="a">Apple</option>
      <option value="b">Banana</option>
      <option value="c">Cherry</option>
    </select>
    </body></html>"""
    data_url = "data:text/html;charset=utf-8," + html.replace("\n", "")
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(data_url))
        ref = _find_ref_for_role(snap, "combobox")
        assert ref, "快照里找不到 combobox(<select>)"
        # 选 value=b
        result_json = s.select(ref, "b", snapshot_id)
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"select 失败: {result}"
        # 新快照里 combobox 行应反映选中值。CDP 的 combobox value
        # 显示的是 option 的 text(如 "Banana"),不是 option 的 value 属性(如 "b")。
        new_snap = result.get("snapshot", "")
        combobox_line = next(
            (line for line in new_snap.splitlines() if "combobox" in line),
            "",
        )
        assert "Banana" in combobox_line, (
            f"select 后快照里 combobox 行没有 Banana: {combobox_line!r}"
        )


def test_stale_snapshot_id_is_rejected() -> None:
    """一次操作后，旧 snapshot_id 不能再驱动其他 ref。"""
    from browser.session import BrowserSession
    html = '<button id="first">First</button><button id="second">Second</button>'
    with BrowserSession() as s:
        snapshot, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8," + html)
        )
        first_ref = _find_ref_for_role(snapshot, "button", "First")
        second_ref = _find_ref_for_role(snapshot, "button", "Second")
        assert first_ref and second_ref, "测试页缺少 button ref"
        first = _parse_result(s.click(first_ref, snapshot_id))
        assert first.get("ok") is True, f"首次点击失败: {first}"
        stale = _parse_result(s.click(second_ref, snapshot_id))
        assert stale.get("ok") is False, f"旧快照却被接受: {stale}"
        assert stale.get("error_type") == "stale_snapshot", f"错误类型不正确: {stale}"


def test_type_supports_contenteditable() -> None:
    """role=textbox 的 contenteditable 元素也应支持 type。"""
    from browser.session import BrowserSession
    html = '<div role="textbox" contenteditable="true"></div>'
    with BrowserSession() as s:
        snapshot, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8," + html)
        )
        ref = _find_ref_for_role(snapshot, "textbox")
        assert ref, "测试页缺少 contenteditable 的 textbox ref"
        result = _parse_result(s.type(ref, "可编辑文本", snapshot_id))
        assert result.get("ok") is True, f"contenteditable 输入失败: {result}"
        assert "可编辑文本" in result.get("snapshot", ""), (
            f"新快照未显示输入文字: {result}"
        )


def test_cross_thread_access_has_clear_error() -> None:
    """同步 Playwright 跨线程调用应在模块边界被明确拒绝。"""
    from concurrent.futures import ThreadPoolExecutor

    from browser.session import BrowserSession
    with BrowserSession() as s:
        _observation(s.navigate("data:text/html;charset=utf-8,<button>OK</button>"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(s.snapshot)
            try:
                future.result()
            except RuntimeError as exc:
                assert "BrowserSession 只能在创建它的线程中使用" in str(exc)
            else:
                raise AssertionError("跨线程 snapshot 没有抛 RuntimeError")


def test_ref_map_skips_nodes_without_backend_dom_id() -> None:
    """展示用 AX 节点不能占用 ref 编号或制造无法操作的 ref。"""
    from browser.accessibility import format_snapshot
    from browser.session import _build_ref_map

    cdp_result = {
        "nodes": [
            {
                "nodeId": "0",
                "role": {"value": "rootWebArea"},
                "childIds": ["1", "2"],
            },
            {
                "nodeId": "1",
                "role": {"value": "link"},
                "name": {"value": "展示链接"},
            },
            {
                "nodeId": "2",
                "role": {"value": "button"},
                "name": {"value": "可点击按钮"},
                "backendDOMNodeId": 42,
            },
        ]
    }
    snapshot = format_snapshot(cdp_result)
    assert 'link "展示链接" [ref=' not in snapshot, snapshot
    assert 'button "可点击按钮" [ref=e1]' in snapshot, snapshot
    assert _build_ref_map(cdp_result) == {"e1": 42}


def test_normalize_url_adds_https_for_bare_domain() -> None:
    """文档示例中的 ``www.baidu.com`` 应可直接用于 navigate。"""
    from browser.session import _normalize_url

    assert _normalize_url("www.baidu.com") == "https://www.baidu.com"
    assert _normalize_url("https://example.com") == "https://example.com"
    assert _normalize_url("data:text/html,hello") == "data:text/html,hello"


# ---------------------------------------------------------------------------
# P2 测试:back / forward / reload / scroll
# ---------------------------------------------------------------------------


def test_back_forward_navigation() -> None:
    """navigate A -> navigate B -> back 应回到 A,forward 应再到 B。

    用两篇不同 Wikipedia 文章验证 URL 来回切换。这是 back/forward 最核心
    的契约。
    """
    from browser.session import BrowserSession
    url_a = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    url_b = "https://en.wikipedia.org/wiki/Rust_(programming_language)"
    with BrowserSession() as s:
        s.navigate(url_a)
        _, snapshot_id_b = _observation(s.navigate(url_b))
        assert s._page.url == url_b, f"navigate B 后 URL 不对: {s._page.url}"

        # back 应回到 A。
        result = _parse_result(s.back(snapshot_id_b))
        assert result.get("ok") is True, f"back 失败: {result}"
        assert s._page.url == url_a, (
            f"back 后应在 A,实际: {s._page.url}"
        )

        # forward 应再到 B。
        result = _parse_result(s.forward(result["snapshot_id"]))
        assert result.get("ok") is True, f"forward 失败: {result}"
        assert s._page.url == url_b, (
            f"forward 后应在 B,实际: {s._page.url}"
        )


def test_back_without_history_returns_no_history_error() -> None:
    """没有可用历史时，back 不得通过重新加载页面破坏未提交输入。"""
    from browser.session import BrowserSession
    html = '<input aria-label="draft">'
    data_url = "data:text/html;charset=utf-8," + html
    with BrowserSession() as s:
        snapshot, snapshot_id = _observation(s.navigate(data_url))
        ref = _find_ref_for_role(snapshot, "textbox", "draft")
        assert ref, "测试页缺少输入框 ref"
        typed = _parse_result(s.type(ref, "unsaved text", snapshot_id))
        assert typed.get("ok") is True, f"输入草稿失败: {typed}"
        url_before = s._page.url
        result = _parse_result(s.back(typed["snapshot_id"]))
        assert result.get("ok") is False, (
            f"无历史 back 不应成功: {result}"
        )
        assert result.get("error_type") == "no_history", (
            f"错误类型应为 no_history,实际: {result.get('error_type')}"
        )
        # 页面没有离开当前 URL，更不能经由重新加载恢复。
        assert s._page.url == url_before, (
            f"无历史 back 后 URL 不应变化,实际: {url_before} -> {s._page.url}"
        )
        assert s._page.locator("input").input_value() == "unsaved text", (
            "无历史 back 清空了未提交输入"
        )
        # 应返回新观察结果,agent 可继续操作。
        assert result.get("snapshot"), "无历史 back 后快照为空"
        assert result.get("snapshot_id"), "无历史 back 后没有 snapshot_id"


def test_back_forward_handles_same_document_history() -> None:
    """仅 URL 片段变化的历史也应正常 back/forward，而非误报 no_history。"""
    from browser.session import BrowserSession

    base_url = "https://example.com/"
    fragment_url = "https://example.com/#two"
    with BrowserSession() as s:
        s.navigate(base_url)
        _, fragment_snapshot_id = _observation(s.navigate(fragment_url))
        assert s._page.url == fragment_url, f"片段导航失败: {s._page.url}"

        back = _parse_result(s.back(fragment_snapshot_id))
        assert back.get("ok") is True, f"页面内 back 被误判失败: {back}"
        assert s._page.url == base_url, f"back 未回到基础 URL: {s._page.url}"

        forward = _parse_result(s.forward(back["snapshot_id"]))
        assert forward.get("ok") is True, f"页面内 forward 被误判失败: {forward}"
        assert s._page.url == fragment_url, (
            f"forward 未回到片段 URL: {s._page.url}"
        )


def test_back_handles_same_url_history_state() -> None:
    """同 URL 的 history.pushState 位置也应可正常回退。"""
    from urllib.parse import quote

    from browser.session import BrowserSession

    html = (
        '<button onclick="history.pushState({step: 2}, \'\', location.href)">'
        'next</button>'
    )
    data_url = "data:text/html;charset=utf-8," + quote(html)
    with BrowserSession() as s:
        snapshot, snapshot_id = _observation(s.navigate(data_url))
        ref = _find_ref_for_role(snapshot, "button", "next")
        assert ref, "测试页缺少 pushState 按钮"
        moved = _parse_result(s.click(ref, snapshot_id))
        assert moved.get("ok") is True, f"pushState 点击失败: {moved}"
        assert s._page.evaluate("history.state.step") == 2

        back = _parse_result(s.back(moved["snapshot_id"]))
        assert back.get("ok") is True, f"同 URL 历史被误判失败: {back}"
        assert s._page.evaluate("history.state") is None, (
            "back 后 history.state 未恢复到初始值"
        )


def test_reload_refreshes_page() -> None:
    """reload 后 URL 不变,但 snapshot_id 应变化(新观察结果)。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id_before = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
        url_before = s._page.url
        result = _parse_result(s.reload(snapshot_id_before))
        assert result.get("ok") is True, f"reload 失败: {result}"
        # URL 不变。
        assert s._page.url == url_before, (
            f"reload 后 URL 变了: {url_before} -> {s._page.url}"
        )
        # snapshot_id 应是新的(每次观察递增)。
        assert result.get("snapshot_id") != snapshot_id_before, (
            "reload 后 snapshot_id 没变,可能没重新取快照"
        )
        assert result.get("snapshot"), "reload 后快照为空"


def test_scroll_changes_snapshot_id() -> None:
    """scroll 不改 URL,但应产生新的 snapshot_id(页面状态变化)。

    用 Wikipedia 长文章页验证 -- 滚动后懒加载内容可能变化,至少
    snapshot_id 必须刷新,否则后续 ref 操作会基于旧观察结果。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id_before = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
        url_before = s._page.url
        result = _parse_result(s.scroll("down", snapshot_id_before, 800))
        assert result.get("ok") is True, f"scroll 失败: {result}"
        # URL 不应变化。
        assert s._page.url == url_before, (
            f"scroll 后 URL 变了: {url_before} -> {s._page.url}"
        )
        # snapshot_id 必须刷新。
        assert result.get("snapshot_id") != snapshot_id_before, (
            "scroll 后 snapshot_id 没变,可能没重新取快照"
        )


def test_scroll_rejects_invalid_direction() -> None:
    """非法 direction 应返回 invalid_args,不触发浏览器操作。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        result = _parse_result(s.scroll("sideways", snapshot_id, 100))
        assert result.get("ok") is False, f"非法 direction 不应成功: {result}"
        assert result.get("error_type") == "invalid_args", (
            f"错误类型应为 invalid_args,实际: {result.get('error_type')}"
        )


def test_scroll_negative_amount_rejected() -> None:
    """负数或零 amount 应被拒绝。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        result = _parse_result(s.scroll("down", snapshot_id, 0))
        assert result.get("ok") is False, f"amount=0 不应成功: {result}"
        assert result.get("error_type") == "invalid_args"


def test_p2_actions_reject_stale_snapshot() -> None:
    """P2 操作也必须拒绝已被后续观察替代的页面版本。"""
    from browser.session import BrowserSession

    with BrowserSession() as s:
        _, stale_snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>first</p>")
        )
        _observation(s.navigate("data:text/html;charset=utf-8,<p>second</p>"))
        for name, result_json in (
            ("back", s.back(stale_snapshot_id)),
            ("forward", s.forward(stale_snapshot_id)),
            ("reload", s.reload(stale_snapshot_id)),
            ("scroll", s.scroll("down", stale_snapshot_id)),
        ):
            result = _parse_result(result_json)
            assert result.get("ok") is False, f"过期 {name} 不应成功: {result}"
            assert result.get("error_type") == "stale_snapshot", (
                f"过期 {name} 错误类型不正确: {result}"
            )


def test_scroll_rejects_non_numeric_amount_and_non_string_direction() -> None:
    """来自 JSON 参数的类型错误应返回 JSON 错误，而不是抛 TypeError。"""
    from browser.session import BrowserSession

    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        for direction, amount in ((None, 100), ("down", "100"), ("down", True)):
            result = _parse_result(s.scroll(direction, snapshot_id, amount))
            assert result.get("ok") is False, f"非法参数不应成功: {result}"
            assert result.get("error_type") == "invalid_args", (
                f"非法参数错误类型不正确: {result}"
            )


# ---------------------------------------------------------------------------
# 简单测试运行器:依次跑,统计 pass/fail,失败打印 traceback。
# ---------------------------------------------------------------------------

_TESTS: list[tuple[str, Any]] = [
    ("test_navigate_returns_snapshot_with_link_and_heading",
     test_navigate_returns_snapshot_with_link_and_heading),
    ("test_refs_only_on_interactive_roles",
     test_refs_only_on_interactive_roles),
    ("test_cookie_persists_across_navigates",
     test_cookie_persists_across_navigates),
    ("test_refs_reset_across_pages",
     test_refs_reset_across_pages),
    ("test_session_closed_raises_on_snapshot",
     test_session_closed_raises_on_snapshot),
    ("test_click_triggers_navigation_and_returns_new_snapshot",
     test_click_triggers_navigation_and_returns_new_snapshot),
    ("test_type_fills_search_box",
     test_type_fills_search_box),
    ("test_press_enter_submits_search",
     test_press_enter_submits_search),
    ("test_select_on_dropdown",
     test_select_on_dropdown),
    ("test_stale_snapshot_id_is_rejected",
     test_stale_snapshot_id_is_rejected),
    ("test_type_supports_contenteditable",
     test_type_supports_contenteditable),
    ("test_cross_thread_access_has_clear_error",
     test_cross_thread_access_has_clear_error),
    ("test_ref_map_skips_nodes_without_backend_dom_id",
     test_ref_map_skips_nodes_without_backend_dom_id),
    ("test_normalize_url_adds_https_for_bare_domain",
     test_normalize_url_adds_https_for_bare_domain),
    ("test_back_forward_navigation",
     test_back_forward_navigation),
    ("test_back_without_history_returns_no_history_error",
     test_back_without_history_returns_no_history_error),
    ("test_back_forward_handles_same_document_history",
     test_back_forward_handles_same_document_history),
    ("test_back_handles_same_url_history_state",
     test_back_handles_same_url_history_state),
    ("test_reload_refreshes_page",
     test_reload_refreshes_page),
    ("test_scroll_changes_snapshot_id",
     test_scroll_changes_snapshot_id),
    ("test_scroll_rejects_invalid_direction",
     test_scroll_rejects_invalid_direction),
    ("test_scroll_negative_amount_rejected",
     test_scroll_negative_amount_rejected),
    ("test_p2_actions_reject_stale_snapshot",
     test_p2_actions_reject_stale_snapshot),
    ("test_scroll_rejects_non_numeric_amount_and_non_string_direction",
     test_scroll_rejects_non_numeric_amount_and_non_string_direction),
]


def _run_all() -> int:
    if not _check_browser_installed():
        return 0
    passed = 0
    failed = 0
    for name, fn in _TESTS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except Exception:
            print(f"  [FAIL] {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n结果: {passed} passed, {failed} failed, 共 {len(_TESTS)} 个")
    return 0 if failed == 0 else 1


def main() -> int:
    # Windows 终端默认 GBK,Playwright 错误信息和中文输出会乱码。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    return _run_all()


if __name__ == "__main__":
    raise SystemExit(main())
