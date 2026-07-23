"""
browser 模块独立测试(无 pytest)。

直接运行::

    uv run python -m browser.test_reader

绝大多数用例跑在本地 fixture 网站(browser/_fixtures),不依赖外网 --
避免网络波动假失败、真实页面结构变化导致 ref 测试失效。fixture 提供
cookie/表单/延迟/下载/iframe/弹窗等真实 HTTP 语义,覆盖已实现的全部功能。
真实网站只保留 1 个冒烟用例,外网不可达时 skip 不 fail。

需要 HTTP 语义的用例(cookie 持久、表单提交、慢加载、延迟出现、iframe、
弹窗)走 fixture;极简单页(单元素、stale ref、参数校验)仍用 data: URL,
各取所长。

测试覆盖:
1. navigate 返回的快照含 link、heading 和 snapshot_id
2. ref 只出现在交互角色行上
3. cookie-set 写入后跨 navigate 保持(context 持久化)
4. 跨页 navigate 后 ref 从 e1 重新编号,且 snapshot 反映新页面内容
5. session close 后再调 snapshot 抛 RuntimeError(资源已释放)
6. click 链接触发跳转,返回新快照
7. type 在搜索框填入文字,快照反映输入值
8. press Enter 提交表单,URL 带查询参数
9. select 在带下拉框的页面选项
10. 旧 snapshot_id 不能再次操作页面
11. contenteditable 元素可以输入文字
12. 跨线程访问会得到明确错误，而不是 Playwright 内部异常
13. 裸域名会补全为 HTTPS URL
14. back/forward 在两页之间来回切换
15. 无历史时 back 返回 no_history 错误,页面状态恢复
16. 同文档片段历史 back/forward 正常(不误报 no_history)
17. 同 URL 的 pushState 历史可回退
18. reload 后 URL 不变但 snapshot_id 刷新
19. scroll 不改 URL 但 snapshot_id 刷新
20. scroll 非法 direction 返回 invalid_args
21. scroll 零/负 amount 返回 invalid_args
22. get_text 整页返回连贯 innerText(子标签文本拼好)
23. get_text(ref) 返回元素 textContent,含后代、不含 box 外文本
24. get_text 纯读取:不失效旧 snapshot_id,ref 仍可操作
25. get_text 超长文本截断 + truncated 标记
26. get_text 拒绝 stale snapshot_id
27. console 返回序列化的结构化结果
28. console 返回复杂对象(dict/list)
29. console 不可序列化返回值(循环引用)兜底成 '<unserializable>'
30. console JS 抛异常返回 console_failed 错误
31. console 改 DOM 后旧 ref 失效(stale_snapshot)
32. console 拒绝 stale snapshot_id
33. console 空表达式返回 invalid_args
34-39. wait_for_url/text/ref/load_state 成功 + 超时 + 取消 + stale 拒绝
40. 真实 Wikipedia 冒烟(外网不可达时 skip)
41. iframe 页父页元素可操作
42. alert 弹窗不阻塞,副作用生效
43. 慢加载页 wait_for_load_state 等到就绪
44. 延迟出现文本 wait_for_text 成功(非超时)
45. 延迟出现元素 wait_for_ref 成功

playwright 或浏览器未装时整组 skip,不报 error。
"""

from __future__ import annotations

import json
import re
import sys
import threading
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


# ---------------------------------------------------------------------------
# 本地 fixture 网站:提供 cookie/表单/延迟/下载等 HTTP 语义,测试不依赖外网。
# 由 _run_all 启停,测试通过 _fixture_url("/path") 拿 URL。
# ---------------------------------------------------------------------------

from browser._fixtures import FixtureSite, start_fixture_server  # noqa: E402

_FIXTURE: FixtureSite | None = None


def _fixture_url(path: str = "/") -> str:
    """拼出 fixture 站点的完整 URL。未启动时给清晰错误。"""
    if _FIXTURE is None:
        raise RuntimeError("fixture 服务器未启动;应在 _run_all 中启动")
    return _FIXTURE.base_url + path


def _skip_if_no_network() -> bool:
    """冒烟用例:探测外网可达性,不可达时打印 skip 并返回 True。

    返回 True 表示调用方应跳过(不 fail)。只在真实网站冒烟测试里用。
    """
    import urllib.request
    try:
        urllib.request.urlopen("https://en.wikipedia.org", timeout=5)
    except Exception as exc:
        print(f"  [skip] 外网不可达,跳过真实网站冒烟: {exc.__class__.__name__}")
        return True
    return False


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
        snap, _ = _observation(s.navigate(_fixture_url("/")))
    assert snap, "快照为空"
    # 去缩进后比较 role。
    roles = {line.lstrip().split(" ", 1)[0] for line in snap.splitlines() if line.strip()}
    assert "link" in roles, f"快照里找不到 link 角色;实际 roles: {sorted(roles)[:20]}"
    assert "heading" in roles, f"快照里找不到 heading 角色;实际 roles: {sorted(roles)[:20]}"


def test_refs_only_on_interactive_roles() -> None:
    """所有 [ref=eN] 必须出现在 INTERACTIVE_ROLES 的行上。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, _ = _observation(s.navigate(_fixture_url("/")))
    ref_lines = _extract_ref_lines(snap)
    assert ref_lines, "快照里没有任何 ref"
    for line in ref_lines:
        role = _role_of_line(line)
        assert role in INTERACTIVE_ROLES, (
            f"非交互角色 {role!r} 却分配了 ref: {line!r}"
        )


def test_cookie_persists_across_navigates() -> None:
    """同一 session 访问 cookie-set 后,cookie 在后续 navigate 仍保持。

    用 fixture 的 /cookie-set 写入固定 cookie,再 navigate 到别的页面,
    cookies() 仍应含该 cookie -- 证明 context 持久化。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        s.navigate(_fixture_url("/cookie-set"))
        cookies_after_first = s.cookies()
        assert cookies_after_first, "cookie-set 后没有 cookie"
        # 应含 fixture 写入的 cookie。
        names_first = {c["name"] for c in cookies_after_first}
        assert "fixture_session" in names_first, (
            f"cookie-set 后没有 fixture_session: {sorted(names_first)}"
        )

        # navigate 到另一页面,cookie 应仍在。
        s.navigate(_fixture_url("/"))
        cookies_after_second = s.cookies()
        names_second = {c["name"] for c in cookies_after_second}
        assert "fixture_session" in names_second, (
            f"第二次 navigate 后 fixture_session 丢失: {sorted(names_second)}"
        )


def test_refs_reset_across_pages() -> None:
    """跨页 navigate 后 ref 从 e1 重新编号,且 snapshot 反映新页面内容。

    用 fixture 两篇不同文章(alpha/beta)验证。断言两页 ref 行集合不同,
    能抓住"navigate 后 snapshot 返回旧页面缓存"的 bug。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap_a, _ = _observation(s.navigate(_fixture_url("/article?name=alpha")))
        snap_b, _ = _observation(s.navigate(_fixture_url("/article?name=beta")))

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
    """click 首页的"文章 Alpha"链接应跳转到 article 页,返回新快照。

    用 fixture 首页的导航链接验证:click 后 URL 变化到 /article,且返回新快照。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        ref = _find_ref_for_role(snap, "link", "文章 Alpha")
        assert ref, "首页快照里找不到 '文章 Alpha' 链接"
        url_before = s._page.url
        result_json = s.click(ref, snapshot_id)
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"click 失败: {result}"
        url_after = s._page.url
        # URL 应变化(跳到 /article?name=alpha)。
        assert url_after != url_before, (
            f"click 后 URL 没变: before={url_before} after={url_after}"
        )
        assert "article" in url_after, f"click 后应跳到 article 页: {url_after}"
        # 返回的 snapshot 字段非空。
        assert result.get("snapshot"), "click 返回的 snapshot 为空"
        assert result.get("snapshot_id"), "click 返回的 snapshot_id 为空"


def test_type_fills_search_box() -> None:
    """type 在搜索框填入文字,新快照应反映输入值。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/search")))
        # fixture 搜索框 aria-label="搜索框"。
        ref = _find_ref_for_role(snap, "textbox", "搜索框")
        assert ref, "快照里找不到搜索框(textbox, name~搜索框)"
        result_json = s.type(ref, "alpha", snapshot_id, clear=True)
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"type 失败: {result}"
        # 新快照里应能看到输入的文字。
        new_snap = result.get("snapshot", "")
        assert "alpha" in new_snap, (
            "新快照里找不到输入的文字,可能 value 没写进去"
        )


def test_press_enter_submits_search() -> None:
    """先 type 填搜索框,再 press Enter,URL 应变成搜索结果页。

    fixture /search 是 GET 表单,Enter 提交后 URL 带 ?q=alpha。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/search")))
        ref = _find_ref_for_role(snap, "textbox", "搜索框")
        assert ref, "快照里找不到搜索框"
        typed = _parse_result(s.type(ref, "alpha", snapshot_id, clear=True))
        assert typed.get("ok") is True, f"type 失败: {typed}"
        url_before = s._page.url
        result_json = s.press("Enter", typed["snapshot_id"])
        result = _parse_result(result_json)
        assert result.get("ok") is True, f"press 失败: {result}"
        url_after = s._page.url
        # Enter 提交搜索,URL 应变化并带查询参数。
        assert url_after != url_before, (
            f"press Enter 后 URL 没变: before={url_before} after={url_after}"
        )
        assert "q=alpha" in url_after, f"提交后 URL 应含 q=alpha: {url_after}"


def test_select_on_dropdown() -> None:
    """select 在 data: URL 内联的 <select> 上选项。

    select 不需要 HTTP 语义,用 data: URL 内联 HTML 最简单稳定。
    验证:select 成功,新快照里 combobox 反映选中值。
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

    用 fixture 两篇不同文章验证 URL 来回切换。这是 back/forward 最核心
    的契约。
    """
    from browser.session import BrowserSession
    url_a = _fixture_url("/article?name=alpha")
    url_b = _fixture_url("/article?name=beta")
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
    """仅 URL 片段变化的历史也应正常 back/forward，而非误报 no_history。

    用 fixture 长页面的锚点跳转验证:#top -> #p100 是同文档历史项。
    """
    from browser.session import BrowserSession

    base_url = _fixture_url("/long")
    fragment_url = _fixture_url("/long#p100")
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
        _, snapshot_id_before = _observation(s.navigate(_fixture_url("/")))
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

    用 fixture 长页面验证 -- 滚动后可见内容变化,至少 snapshot_id 必须刷新,
    否则后续 ref 操作会基于旧观察结果。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id_before = _observation(s.navigate(_fixture_url("/long")))
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
# P3 测试:get_text / console
# ---------------------------------------------------------------------------


def test_get_text_whole_page_returns_inner_text() -> None:
    """get_text(ref=None) 返回整页可见文本,把子标签文本拼成连贯字符串。"""
    from browser.session import BrowserSession
    # <p>Hello <b>World</b></p> -- innerText 应是 "Hello World",不是碎片。
    html = '<div id="main"><p>Hello <b>World</b></p></div>'
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        result = _parse_result(s.get_text(None, snapshot_id))
        assert result.get("ok") is True, f"get_text 失败: {result}"
        assert "Hello World" in result.get("text", ""), (
            f"整页文本应含 'Hello World',实际: {result.get('text')!r}"
        )
        assert result.get("truncated") is False


def test_get_text_by_ref_returns_element_textcontent() -> None:
    """get_text(ref) 返回该交互元素的 textContent。

    ref 体系只给交互元素(link/button/textbox 等)分配编号,所以 get_text(ref)
    主要用于读这些元素的文字(如按钮文字、链接文字)。读非交互容器的内容
    是 console 的职责(用 document.querySelector 定位)。

    这里用 button 验证:它的文字就是 ref 指向元素的 textContent。
    """
    from browser.session import BrowserSession
    html = '<button id="b">提交<b>表单</b></button><button id="other">其他</button>'
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        ref = _find_ref_for_role(snap, "button", "提交")
        assert ref, "快照里找不到 '提交' 按钮"
        result = _parse_result(s.get_text(ref, snapshot_id))
        assert result.get("ok") is True, f"get_text(ref) 失败: {result}"
        text = result.get("text", "")
        # textContent 含后代 <b> 的文字,拼成"提交表单"。
        assert "提交" in text and "表单" in text, (
            f"元素文本应含 '提交表单',实际: {text!r}"
        )
        assert "其他" not in text, "不应包含另一个按钮的文字"


def test_get_text_preserves_snapshot_id_and_ref_still_valid() -> None:
    """get_text 是纯读取:不失效旧 snapshot_id,之后 ref 仍能操作。

    这是 get_text 与交互操作的关键差异 -- 读完文本不破坏页面观察结果,
    agent 能继续用手里的 ref click/type。
    """
    from browser.session import BrowserSession
    html = '<input id="i" value="hello"><button id="b">btn</button>'
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        # 整页 get_text。
        result = _parse_result(s.get_text(None, snapshot_id))
        assert result.get("ok") is True, f"get_text 失败: {result}"
        # snapshot_id 应保持不变(纯读取)。
        assert result.get("snapshot_id") == snapshot_id, (
            "get_text 不应改变 snapshot_id(纯读取)"
        )
        # 旧 snapshot_id 仍有效 -- 用它操作 ref 应成功而非 stale。
        ref = _find_ref_for_role(snap, "button", "btn")
        assert ref, "快照里找不到 button"
        click_result = _parse_result(s.click(ref, snapshot_id))
        assert click_result.get("ok") is True, (
            f"get_text 后用旧 snapshot_id 操作应成功,实际: {click_result}"
        )


def test_get_text_truncates_long_text() -> None:
    """整页文本超过 max_chars 时截断,并置 truncated=True。"""
    from browser.session import BrowserSession
    long_text = "A" * 1000
    html = f'<div>{long_text}</div>'
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        result = _parse_result(s.get_text(None, snapshot_id, max_chars=50))
        assert result.get("ok") is True, f"get_text 失败: {result}"
        assert result.get("truncated") is True, "长文本应被截断"
        assert len(result.get("text", "")) == 50, (
            f"截断后应正好 50 字符,实际 {len(result.get('text', ''))}"
        )


def test_get_text_rejects_stale_snapshot() -> None:
    """get_text 也必须拒绝已被替代的 snapshot_id。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, stale_id = _observation(s.navigate("data:text/html;charset=utf-8,<p>a</p>"))
        _observation(s.navigate("data:text/html;charset=utf-8,<p>b</p>"))
        result = _parse_result(s.get_text(None, stale_id))
        assert result.get("ok") is False, f"过期 snapshot_id 不应成功: {result}"
        assert result.get("error_type") == "stale_snapshot"


def test_console_returns_serialized_result() -> None:
    """console 读取结构化数据,返回 JSON 可解析的结果。"""
    from browser.session import BrowserSession
    html = '<div><h2>标题</h2><h2>副标题</h2></div>'
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        result = _parse_result(s.console('document.querySelectorAll("h2").length', snapshot_id))
        assert result.get("ok") is True, f"console 失败: {result}"
        assert result.get("result") == 2, f"应返回 2,实际: {result.get('result')}"
        # console 改 DOM 后产生新 snapshot_id。
        assert result.get("snapshot_id") != snapshot_id
        assert result.get("snapshot"), "console 后应返回新快照"


def test_console_returns_complex_object() -> None:
    """console 返回的对象/数组应正确序列化成 Python dict/list。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        result = _parse_result(
            s.console('({name: "abc", count: 3, list: [1, 2, 3]})', snapshot_id)
        )
        assert result.get("ok") is True, f"console 失败: {result}"
        obj = result.get("result")
        assert obj == {"name": "abc", "count": 3, "list": [1, 2, 3]}, (
            f"对象序列化错误,实际: {obj}"
        )


def test_console_handles_unserializable() -> None:
    """函数/循环引用等不可序列化返回值应兜底成 '<unserializable>',不报错。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        # 循环引用对象 -- JSON.stringify 会抛错,应兜底。
        result = _parse_result(
            s.console('var a = {}; a.self = a; a', snapshot_id)
        )
        assert result.get("ok") is True, f"循环引用不应导致失败: {result}"
        assert result.get("result") == "<unserializable>", (
            f"循环引用应兜底成 '<unserializable>',实际: {result.get('result')!r}"
        )


def test_console_handles_js_exception() -> None:
    """JS 抛异常时返回 console_failed 错误,不崩溃。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        result = _parse_result(s.console('undefinedFunc()', snapshot_id))
        assert result.get("ok") is False, f"JS 异常不应成功: {result}"
        assert result.get("error_type") == "console_failed"
        # 错误信息应含 JS 异常内容。
        assert "undefinedFunc" in result.get("error", ""), (
            f"错误信息应含异常名,实际: {result.get('error')!r}"
        )


def test_console_invalidates_ref_after_dom_change() -> None:
    """console 用 JS 改 DOM 后,旧 ref 应失效(stale_snapshot)。

    这是 console 按交互操作处理的核心:JS 可能改了 DOM,旧 backendDOMNodeId
    对应的观察结果必须作废,否则 agent 会用旧 ref 操作新页面。
    """
    from browser.session import BrowserSession
    html = '<button id="b">btn</button>'
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate("data:text/html;charset=utf-8," + html))
        ref = _find_ref_for_role(snap, "button", "btn")
        assert ref, "快照里找不到 button"
        # 用 console 删除该按钮,改变 DOM。
        result = _parse_result(
            s.console('document.getElementById("b").remove()', snapshot_id)
        )
        assert result.get("ok") is True, f"console 删元素失败: {result}"
        # 旧 snapshot_id 应已失效。
        assert result.get("snapshot_id") != snapshot_id
        # 用旧 snapshot_id 操作 ref 应被拒。
        stale = _parse_result(s.click(ref, snapshot_id))
        assert stale.get("ok") is False, "DOM 改变后旧 ref 不应可用"
        assert stale.get("error_type") == "stale_snapshot"


def test_console_rejects_stale_snapshot() -> None:
    """console 也必须拒绝已被替代的 snapshot_id。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, stale_id = _observation(s.navigate("data:text/html;charset=utf-8,<p>a</p>"))
        _observation(s.navigate("data:text/html;charset=utf-8,<p>b</p>"))
        result = _parse_result(s.console('1+1', stale_id))
        assert result.get("ok") is False, f"过期 snapshot_id 不应成功: {result}"
        assert result.get("error_type") == "stale_snapshot"


def test_console_rejects_empty_expression() -> None:
    """空表达式应返回 invalid_args。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>hello</p>")
        )
        result = _parse_result(s.console("   ", snapshot_id))
        assert result.get("ok") is False
        assert result.get("error_type") == "invalid_args"


# ---------------------------------------------------------------------------
# P4 测试:条件等待
# ---------------------------------------------------------------------------


def test_wait_for_url_returns_new_snapshot() -> None:
    """当前 URL 已匹配时，wait_for_url 应立即成功并换发快照。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>ready</p>")
        )
        result = _parse_result(
            s.wait_for_url("data:text/html*", snapshot_id, timeout_ms=500)
        )
        assert result.get("ok") is True, f"wait_for_url 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_for_text_returns_new_snapshot() -> None:
    """页面已有可见文本时，wait_for_text 应成功。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>visible target</p>")
        )
        result = _parse_result(s.wait_for_text("visible target", snapshot_id, timeout_ms=500))
        assert result.get("ok") is True, f"wait_for_text 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_for_ref_returns_new_snapshot() -> None:
    """快照中仍可见的元素应能通过 wait_for_ref 确认。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snapshot, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<button>Continue</button>")
        )
        ref = _find_ref_for_role(snapshot, "button", "Continue")
        assert ref, "测试页缺少 button ref"
        result = _parse_result(s.wait_for_ref(ref, snapshot_id, timeout_ms=500))
        assert result.get("ok") is True, f"wait_for_ref 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_for_load_state_returns_new_snapshot() -> None:
    """已完成的 data 页面应满足 load 状态。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>loaded</p>")
        )
        result = _parse_result(s.wait_for_load_state("load", snapshot_id, timeout_ms=500))
        assert result.get("ok") is True, f"wait_for_load_state 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_timeout_returns_current_observation() -> None:
    """条件超时时应保留当前页面的新快照，供调用方继续操作。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>available</p>")
        )
        result = _parse_result(s.wait_for_text("never appears", snapshot_id, timeout_ms=100))
        assert result.get("ok") is False, f"未出现文本不应成功: {result}"
        assert result.get("error_type") == "wait_timeout"
        assert result.get("snapshot"), "超时时应返回当前快照"
        assert result.get("snapshot_id") and result.get("snapshot_id") != snapshot_id


def test_wait_cancelled_returns_current_observation() -> None:
    """取消等待应立刻返回 wait_cancelled 及当前页面的新快照。"""
    from browser.session import BrowserSession
    cancel_event = threading.Event()
    cancel_event.set()
    with BrowserSession() as s:
        _, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8,<p>available</p>")
        )
        result = _parse_result(
            s.wait_for_text(
                "never appears",
                snapshot_id,
                timeout_ms=500,
                cancel_event=cancel_event,
            )
        )
        assert result.get("ok") is False, f"已取消等待不应成功: {result}"
        assert result.get("error_type") == "wait_cancelled"
        assert result.get("snapshot"), "取消时应返回当前快照"


def test_wait_rejects_stale_snapshot() -> None:
    """所有等待入口都必须拒绝过期的 snapshot_id。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, stale_id = _observation(s.navigate("data:text/html;charset=utf-8,<p>a</p>"))
        _observation(s.navigate("data:text/html;charset=utf-8,<p>b</p>"))
        result = _parse_result(s.wait_for_text("b", stale_id, timeout_ms=500))
        assert result.get("ok") is False, f"过期 snapshot_id 不应成功: {result}"
        assert result.get("error_type") == "stale_snapshot"


# ---------------------------------------------------------------------------
# 真实网站冒烟用例:验证真实网页兼容性,外网不可达时 skip 不 fail。
# ---------------------------------------------------------------------------


def test_smoke_real_wikipedia_navigate() -> None:
    """冒烟:navigate 真实 Wikipedia,断言含 link+heading。外网不可达则 skip。"""
    from browser.session import BrowserSession
    if _skip_if_no_network():
        return
    with BrowserSession() as s:
        snap, _ = _observation(
            s.navigate("https://en.wikipedia.org/wiki/Python_(programming_language)")
        )
    assert snap, "真实 Wikipedia 快照为空"
    roles = {line.lstrip().split(" ", 1)[0] for line in snap.splitlines() if line.strip()}
    assert "link" in roles, f"真实 Wikipedia 缺 link: {sorted(roles)[:10]}"
    assert "heading" in roles, f"真实 Wikipedia 缺 heading: {sorted(roles)[:10]}"


# ---------------------------------------------------------------------------
# fixture 边界测试:iframe / 弹窗 / 慢加载 / 延迟出现 -- 之前外网无法稳定测。
# ---------------------------------------------------------------------------


def test_iframe_parent_page_navigable() -> None:
    """含 iframe 的页面本身可正常 navigate 和操作父页元素。

    iframe 内的内容未必进主可访问性树,但父页元素必须正常。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/iframe")))
        # 父页按钮应能找到并操作。
        ref = _find_ref_for_role(snap, "button", "父页按钮")
        assert ref, "iframe 页缺少父页按钮 ref"
        result = _parse_result(s.click(ref, snapshot_id))
        assert result.get("ok") is True, f"点击父页按钮失败: {result}"
        # iframe 应存在于 DOM。
        iframe_count = s._page.evaluate('document.querySelectorAll("iframe").length')
        assert iframe_count == 1, f"应含 1 个 iframe,实际 {iframe_count}"


def test_dialog_alert_does_not_block() -> None:
    """点击触发 alert 的按钮不应卡死,alert 被 Playwright 自动 dismiss。

    验证:click 成功返回新快照,且 alert 副作用(改元素文本)生效。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        # alert 需要先注册 dialog handler,否则 Playwright 默认 dismiss 但
        # 可能报错。session 未暴露 handler,这里靠 click 的 JS 路径:alert
        # 触发后 Playwright 自动 accept,onclick 继续执行改文本。
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/dialog")))
        ref = _find_ref_for_role(snap, "button", "触发弹窗")
        assert ref, "弹窗页缺少触发按钮 ref"
        result = _parse_result(s.click(ref, snapshot_id))
        assert result.get("ok") is True, f"触发 alert 的 click 失败: {result}"
        # alert 后的副作用:文本应变成"点击后"。
        after = s._page.evaluate('document.getElementById("after-alert").textContent')
        assert "点击后" in after, f"alert 副作用未生效,实际: {after!r}"


def test_wait_for_load_state_on_slow_page() -> None:
    """慢加载页面(服务器延迟)wait_for_load_state 应等到就绪。

    fixture /slow?delay=800 延迟 800ms 响应。navigate 已等 load,
    wait_for_load_state 应立即成功。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/slow?delay=800")))
        result = _parse_result(
            s.wait_for_load_state("load", snapshot_id, timeout_ms=3000)
        )
        assert result.get("ok") is True, f"慢加载页 wait_for_load_state 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_for_text_succeeds_on_delayed_content() -> None:
    """页面延迟插入文本时,wait_for_text 应等到文本出现而非超时。

    fixture /appear?delay=400 加载后 400ms 插入"延迟目标已出现"。
    wait_for_text 超时设 2000ms,应在文本出现后成功。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/appear?delay=400")))
        result = _parse_result(
            s.wait_for_text("延迟目标已出现", snapshot_id, timeout_ms=2000)
        )
        assert result.get("ok") is True, f"延迟文本等待失败: {result}"
        assert result.get("snapshot_id") != snapshot_id


def test_wait_for_ref_succeeds_on_delayed_element() -> None:
    """页面延迟插入按钮时,wait_for_ref 应等到它出现。

    fixture /appear?delay=400 加载后 400ms 插入"延迟按钮"。
    但 wait_for_ref 基于快照里的 backendDOMNodeId -- 延迟插入的元素不在
    初始快照里,没有 ref。所以这个测试改为:先 wait_for_text 等文本出现,
    拿到新快照里的按钮 ref,再 wait_for_ref 应立即成功。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/appear?delay=400")))
        # 等延迟文本出现,拿到含延迟按钮的新快照。
        waited = _parse_result(
            s.wait_for_text("延迟目标已出现", snapshot_id, timeout_ms=2000)
        )
        assert waited.get("ok") is True, f"等待延迟文本失败: {waited}"
        new_snap = waited["snapshot"]
        new_sid = waited["snapshot_id"]
        ref = _find_ref_for_role(new_snap, "button", "延迟按钮")
        assert ref, "延迟按钮出现后的快照里找不到它"
        # 此时按钮已可见,wait_for_ref 应立即成功。
        result = _parse_result(s.wait_for_ref(ref, new_sid, timeout_ms=1000))
        assert result.get("ok") is True, f"wait_for_ref 失败: {result}"


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
    ("test_get_text_whole_page_returns_inner_text",
     test_get_text_whole_page_returns_inner_text),
    ("test_get_text_by_ref_returns_element_textcontent",
     test_get_text_by_ref_returns_element_textcontent),
    ("test_get_text_preserves_snapshot_id_and_ref_still_valid",
     test_get_text_preserves_snapshot_id_and_ref_still_valid),
    ("test_get_text_truncates_long_text",
     test_get_text_truncates_long_text),
    ("test_get_text_rejects_stale_snapshot",
     test_get_text_rejects_stale_snapshot),
    ("test_console_returns_serialized_result",
     test_console_returns_serialized_result),
    ("test_console_returns_complex_object",
     test_console_returns_complex_object),
    ("test_console_handles_unserializable",
     test_console_handles_unserializable),
    ("test_console_handles_js_exception",
     test_console_handles_js_exception),
    ("test_console_invalidates_ref_after_dom_change",
     test_console_invalidates_ref_after_dom_change),
    ("test_console_rejects_stale_snapshot",
     test_console_rejects_stale_snapshot),
    ("test_console_rejects_empty_expression",
     test_console_rejects_empty_expression),
    ("test_wait_for_url_returns_new_snapshot",
     test_wait_for_url_returns_new_snapshot),
    ("test_wait_for_text_returns_new_snapshot",
     test_wait_for_text_returns_new_snapshot),
    ("test_wait_for_ref_returns_new_snapshot",
     test_wait_for_ref_returns_new_snapshot),
    ("test_wait_for_load_state_returns_new_snapshot",
     test_wait_for_load_state_returns_new_snapshot),
    ("test_wait_timeout_returns_current_observation",
     test_wait_timeout_returns_current_observation),
    ("test_wait_cancelled_returns_current_observation",
     test_wait_cancelled_returns_current_observation),
    ("test_wait_rejects_stale_snapshot",
     test_wait_rejects_stale_snapshot),
    ("test_smoke_real_wikipedia_navigate",
     test_smoke_real_wikipedia_navigate),
    ("test_iframe_parent_page_navigable",
     test_iframe_parent_page_navigable),
    ("test_dialog_alert_does_not_block",
     test_dialog_alert_does_not_block),
    ("test_wait_for_load_state_on_slow_page",
     test_wait_for_load_state_on_slow_page),
    ("test_wait_for_text_succeeds_on_delayed_content",
     test_wait_for_text_succeeds_on_delayed_content),
    ("test_wait_for_ref_succeeds_on_delayed_element",
     test_wait_for_ref_succeeds_on_delayed_element),
]


def _run_all() -> int:
    global _FIXTURE
    if not _check_browser_installed():
        return 0
    # 启动本地 fixture 服务器,所有 fixture 测试通过它拿 URL。
    _FIXTURE = start_fixture_server()
    passed = 0
    failed = 0
    try:
        for name, fn in _TESTS:
            try:
                fn()
                print(f"  [PASS] {name}")
                passed += 1
            except Exception:
                print(f"  [FAIL] {name}")
                traceback.print_exc()
                failed += 1
    finally:
        _FIXTURE.stop()
        _FIXTURE = None
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
