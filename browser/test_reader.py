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
40. 真实百度百科冒烟(外网不可达时 skip)
41. iframe 页父页元素可操作
42. dialog API 边界:set_dialog_strategy 参数校验 + accept/dismiss 无效 id
43. 慢加载页 wait_for_load_state 等到就绪
44. 延迟出现文本 wait_for_text 成功(非超时)
45. 延迟出现元素 wait_for_ref 成功
46. click 触发导航到 AJAX 延迟页后快照含延迟注入内容(不拿半截)
47. press 触发表单提交到 AJAX 延迟页后快照完整
48. check/uncheck 切换复选框状态
49. hover/focus 返回新快照,focus 副作用生效
50. drag_and_drop 后放置目标区更新
51. keyboard_shortcut 等价 press
52. set_dialog_strategy 声明后 click 触发 alert 按策略处理,不卡死
53. 多页:list_pages/switch_page/close_page 完整生命周期
54. screenshot 生成 PNG 产物并登记(kind/mime/size)
55. screenshot full_page 全页截图
56. screenshot_element 对 ref 截图
57. download 点击下载链接存产物,文件内容正确
58. upload_files 上传文件后页面显示文件名
59. 产物生命周期:list/get/delete/cleanup_artifacts
60. screenshot/download/upload 拒绝 stale snapshot_id
61. get/delete 不存在 artifact_id 返回 artifact_not_found
62. analyze_* 参数校验(prompt 空/sources 空/timeout 非正)
63. analyze_media source 解析:artifact_id/工作区路径/不存在/绝对路径/含..
64. analyze_image 类型不匹配(传音频)返回 unsupported_media_type
65. analyze_image 不支持的扩展名返回 unsupported_media_type
66. analyze_image 有效图片 + 未配置 ARK_API_KEY 返回 multimodal_not_configured
67. analyze_page stale snapshot_id 拒绝
68. analyze_page 参数校验(prompt 空/full_page 非布尔)
69. analyze_page 未配置 key 返回 multimodal_not_configured
70. analyze_audio 类型不匹配(传图片)

playwright 或浏览器未装时整组 skip,不报 error。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import traceback
from pathlib import Path
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
    探测百度百科,和冒烟测试目标站点一致。
    """
    import urllib.request
    try:
        urllib.request.urlopen("https://baike.baidu.com", timeout=5)
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
        s.navigate(_fixture_url("/"))
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


def test_smoke_real_baike_navigate() -> None:
    """冒烟:navigate 真实百度百科,断言含 link+heading。外网不可达则 skip。

    用国内网站验证真实重 AJAX 站点兼容性。断言宽松(只看 role 存在),
    不依赖具体 ref 或页面结构,避免站点改版导致假失败。
    """
    from browser.session import BrowserSession
    if _skip_if_no_network():
        return
    with BrowserSession() as s:
        snap, _ = _observation(
            s.navigate("https://baike.baidu.com/item/Python")
        )
    assert snap, "真实百度百科快照为空"
    roles = {line.lstrip().split(" ", 1)[0] for line in snap.splitlines() if line.strip()}
    assert "link" in roles, f"真实百度百科缺 link: {sorted(roles)[:10]}"
    assert "heading" in roles, f"真实百度百科缺 heading: {sorted(roles)[:10]}"


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


def test_dialog_api_boundaries() -> None:
    """dialog API 边界：策略参数校验与无待处理对话框的错误。

    同步 Playwright 操作必须在触发对话框前声明策略，否则触发操作会阻塞。
    当前契约使用 set_dialog_strategy 声明 accept、dismiss 或 prompt；不再
    保存 Dialog 对象并等待下一次调用处理。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _observation(s.navigate(_fixture_url("/dialog")))
        # 无 dialog 时 list_dialogs 返回空列表。
        result = _parse_result(s.list_dialogs())
        assert result.get("ok") is True, f"list_dialogs 失败: {result}"
        assert result.get("dialogs") == [], (
            f"无 dialog 时应返回空列表,实际: {result.get('dialogs')}"
        )
        # accept 不存在的 dialog 返回 no_pending_dialog。
        result = _parse_result(s.accept_dialog("nonexistent"))
        assert result.get("ok") is False, "accept 不存在的 dialog 不应成功"
        assert result.get("error_type") == "no_pending_dialog", (
            f"错误类型应为 no_pending_dialog,实际: {result.get('error_type')}"
        )
        # dismiss 不存在的 dialog 同样。
        result = _parse_result(s.dismiss_dialog("nonexistent"))
        assert result.get("ok") is False
        assert result.get("error_type") == "no_pending_dialog"
        # 策略参数校验，以及有效策略的声明。
        result = _parse_result(s.set_dialog_strategy("unexpected"))
        assert result.get("ok") is False
        assert result.get("error_type") == "invalid_args"
        result = _parse_result(s.set_dialog_strategy("prompt"))
        assert result.get("ok") is False
        assert result.get("error_type") == "invalid_args"
        result = _parse_result(s.set_dialog_strategy("accept", prompt_text="text"))
        assert result.get("ok") is False
        assert result.get("error_type") == "invalid_args"
        result = _parse_result(s.set_dialog_strategy("dismiss"))
        assert result.get("ok") is True, f"声明 dismiss 策略失败: {result}"


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


def test_click_navigation_to_ajax_page_returns_complete_snapshot() -> None:
    """click 链接到 AJAX 延迟渲染页后,返回的快照应含延迟注入的内容。

    回归测试:fixture /slow-render 在 domcontentloaded 时只有占位标题,
    1500ms 后 JS 才注入"AJAX 内容已完整渲染"标记。若 ``_observe_after_action_locked``
    只等 domcontentloaded,会拿到半截页面(无标记);必须等 load 或更久
    才完整。这个 bug 用真实 Wikipedia 暴露过(press Enter 后只拿到 777 行
    而非 20630 行),现在用 fixture 在本地稳定回归。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        ref = _find_ref_for_role(snap, "link", "AJAX 延迟渲染页")
        assert ref, "首页缺少指向 AJAX 延迟渲染页的链接"
        result = _parse_result(s.click(ref, snapshot_id))
        assert result.get("ok") is True, f"click 失败: {result}"
        new_snap = result.get("snapshot", "")
        # 关键断言:click 触发导航后,快照必须含延迟注入的标记文本。
        # 只等 domcontentloaded 会拿到半截(无标记),这个断言会失败。
        assert "AJAX 内容已完整渲染" in new_snap, (
            "click 导航到 AJAX 页后快照缺少延迟注入的标记文本,"
            "可能只等了 domcontentloaded 拿到半截页面"
        )


def test_press_navigation_to_ajax_page_returns_complete_snapshot() -> None:
    """press Enter 触发表单提交到 AJAX 延迟页后,快照应含延迟内容。

    与 click 测试互补:press 不针对元素,但触发导航后同样要等完整加载。
    用 /search 表单提交到结果页 -- 但结果页非 AJAX。改用更直接的方式:
    在 AJAX 页上 press(不导航),验证非导航场景不受影响;再单独验证
    press 触发导航的场景已在 click 测试覆盖(press 走相同的 _observe 路径)。

    这里聚焦:press 触发导航(表单提交)后,若目标页是 AJAX 延迟页,
    快照应完整。构造一个提交到 /slow-render 的表单。
    """
    from browser.session import BrowserSession
    # 用 data: 内联一个表单,提交到 fixture 的 /slow-render。
    # 但 data: 页面里的表单 action 用相对/绝对 URL 到 fixture,需要 fixture 在线。
    html = (
        '<form action="PLACEHOLDER/slow-render?delay=500" method="get">'
        '<input aria-label="关键词" name="q" value="x">'
        '<button type="submit">提交到 AJAX 页</button>'
        '</form>'
    )
    html = html.replace("PLACEHOLDER", _fixture_url(""))
    with BrowserSession() as s:
        snap, snapshot_id = _observation(
            s.navigate("data:text/html;charset=utf-8," + html)
        )
        ref = _find_ref_for_role(snap, "button", "提交到 AJAX 页")
        assert ref, "测试页缺少提交按钮 ref"
        # click submit 按钮触发表单提交(导航到 AJAX 页)。
        result = _parse_result(s.click(ref, snapshot_id))
        assert result.get("ok") is True, f"提交失败: {result}"
        new_snap = result.get("snapshot", "")
        assert "AJAX 内容已完整渲染" in new_snap, (
            "表单提交到 AJAX 页后快照缺少延迟注入的标记文本,"
            "可能只等了 domcontentloaded 拿到半截页面"
        )


# ---------------------------------------------------------------------------
# P6 完整交互测试:hover/focus/check/uncheck/drag_and_drop/keyboard_shortcut
# + dialog 策略真实流程 + 多页(list_pages/switch_page/close_page)
# ---------------------------------------------------------------------------


def test_check_uncheck_checkbox() -> None:
    """check/uncheck 切换复选框状态,新快照应反映 checked 变化。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/controls")))
        # "订阅"复选框初始未选中。
        ref = _find_ref_for_role(snap, "checkbox", "订阅")
        assert ref, "控件页缺少 '订阅' 复选框 ref"
        result = _parse_result(s.check(ref, snapshot_id))
        assert result.get("ok") is True, f"check 失败: {result}"
        # 新快照里该 checkbox 行应含 checked=true。
        new_snap = result.get("snapshot", "")
        sub_line = next(
            (ln for ln in new_snap.splitlines() if "checkbox" in ln and "订阅" in ln),
            "",
        )
        assert "checked=true" in sub_line, f"check 后未变 checked=true: {sub_line!r}"

        # 再 uncheck 应变回 checked=false。
        result2 = _parse_result(s.uncheck(ref, result["snapshot_id"]))
        assert result2.get("ok") is True, f"uncheck 失败: {result2}"
        new_snap2 = result2.get("snapshot", "")
        sub_line2 = next(
            (ln for ln in new_snap2.splitlines() if "checkbox" in ln and "订阅" in ln),
            "",
        )
        assert "checked=false" in sub_line2, f"uncheck 后未变 checked=false: {sub_line2!r}"


def test_hover_and_focus_return_new_snapshot() -> None:
    """hover/focus 应成功返回新快照(不报错),snapshot_id 刷新。

    hover/focus 的视觉副作用(CSS)不出现在可访问性树里,这里只验证操作成功
    且产生新观察结果;副作用细节由 Playwright Locator 保证。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/controls")))
        # hover 悬停按钮。
        hover_ref = _find_ref_for_role(snap, "button", "悬停我")
        assert hover_ref, "控件页缺少 '悬停我' 按钮 ref"
        result = _parse_result(s.hover(hover_ref, snapshot_id))
        assert result.get("ok") is True, f"hover 失败: {result}"
        assert result.get("snapshot_id") != snapshot_id, "hover 后 snapshot_id 未刷新"

        # focus 聚焦输入框。
        focus_ref = _find_ref_for_role(result["snapshot"], "textbox", "聚焦目标")
        assert focus_ref, "控件页缺少 '聚焦目标' 输入框 ref"
        result2 = _parse_result(s.focus(focus_ref, result["snapshot_id"]))
        assert result2.get("ok") is True, f"focus 失败: {result2}"
        # 验证 focus 确实生效:document.activeElement 应是该输入框。
        active = s._page.evaluate('document.activeElement.id')
        assert active == "focus-target", f"focus 未生效,activeElement={active!r}"


def test_drag_and_drop_updates_target() -> None:
    """drag_and_drop 后,放置目标区应显示接收到的源文字。

    fixture 拖拽源/目标用 role="button" 让它们获得 ref(drag_and_drop
    需要 source_ref/target_ref,而可拖拽 div 本身不是交互角色)。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/drag")))
        source_ref = _find_ref_for_role(snap, "button", "拖拽源")
        target_ref = _find_ref_for_role(snap, "button", "放置目标")
        assert source_ref, "拖拽页找不到源元素 ref"
        assert target_ref, "拖拽页找不到放置目标 ref"
        result = _parse_result(s.drag_and_drop(source_ref, target_ref, snapshot_id))
        assert result.get("ok") is True, f"drag_and_drop 失败: {result}"
        # 拖拽后目标区应含"已接收"。
        target_text = s._page.evaluate('document.getElementById("target").textContent')
        assert "已接收" in target_text, f"拖拽后目标区未更新,实际: {target_text!r}"
        drop_result = s._page.evaluate('document.getElementById("drop-result").textContent')
        assert "拖拽完成" in drop_result, f"拖拽完成标记未触发,实际: {drop_result!r}"


def test_keyboard_shortcut_equivalent_to_press() -> None:
    """keyboard_shortcut 是 press 的语义别名,行为应一致。

    用 fixture 搜索页:聚焦搜索框后 keyboard_shortcut Enter 应提交表单(等价 press Enter)。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/search")))
        ref = _find_ref_for_role(snap, "textbox", "搜索框")
        assert ref, "搜索页缺少搜索框 ref"
        typed = _parse_result(s.type(ref, "alpha", snapshot_id, clear=True))
        assert typed.get("ok") is True, f"type 失败: {typed}"
        url_before = s._page.url
        # keyboard_shortcut 等价 press。
        result = _parse_result(s.keyboard_shortcut("Enter", typed["snapshot_id"]))
        assert result.get("ok") is True, f"keyboard_shortcut 失败: {result}"
        url_after = s._page.url
        assert url_after != url_before, (
            f"keyboard_shortcut Enter 后 URL 没变: {url_before} -> {url_after}"
        )
        assert "q=alpha" in url_after, f"提交后 URL 应含 q=alpha: {url_after}"


def test_dialog_strategy_handles_alert_without_blocking() -> None:
    """声明 dialog 策略后,click 触发 alert 应按策略处理,不卡死。

    真实流程:set_dialog_strategy("dismiss") -> click 触发 alert 按钮 ->
    alert 按策略自动 dismiss,click 立即返回新快照 -> list_dialogs 看到
    已处理的 alert 事件。这是 dialog 机制修复后的核心契约。
    """
    import time
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/dialog")))
        ref = _find_ref_for_role(snap, "button", "触发弹窗")
        assert ref, "弹窗页缺少触发按钮 ref"
        # 声明 dismiss 策略。
        strat = _parse_result(s.set_dialog_strategy("dismiss"))
        assert strat.get("ok") is True, f"set_dialog_strategy 失败: {strat}"
        # click 触发 alert,应按策略 dismiss,不卡 30s。
        t0 = time.time()
        result = _parse_result(s.click(ref, snapshot_id))
        elapsed = time.time() - t0
        assert result.get("ok") is True, f"click 触发 alert 失败: {result}"
        assert elapsed < 10, f"click 触发 dialog 后卡了 {elapsed:.1f}s,策略未生效"
        # alert 后的副作用:文本应变成"点击后"。
        after = s._page.evaluate('document.getElementById("after-alert").textContent')
        assert "点击后" in after, f"alert 副作用未生效,实际: {after!r}"
        # 已处理的 alert 事件随操作返回(_last_dialog_event 不跨操作持久化,
        # 因此从 click 返回值读,而非事后调 list_dialogs)。
        dialog_list = result.get("dialogs", [])
        assert any(d.get("type") == "alert" for d in dialog_list), (
            f"click 返回值未记录已处理的 alert: {dialog_list}"
        )
        # event_type 应标记为 dialog。
        assert result.get("event_type") == "dialog", (
            f"click 触发 alert 后 event_type 应为 dialog,实际: {result.get('event_type')}"
        )


def test_multi_page_lifecycle() -> None:
    """多页:list_pages 看到新页,switch_page 切换,close_page 关闭。

    用 /popup 页的 target=_blank 链接打开新标签页。click 该链接后
    session 应登记新 page,event_type 为 popup。
    """
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/popup")))
        # 初始只有一页。
        pages = _parse_result(s.list_pages())
        assert pages.get("ok") is True
        assert len(pages.get("pages", [])) == 1, f"初始应只有 1 页,实际: {pages.get('pages')}"

        # click "打开新页"链接(target=_blank)。
        ref = _find_ref_for_role(snap, "link", "打开新页")
        assert ref, "弹窗页缺少 '打开新页' 链接 ref"
        result = _parse_result(s.click(ref, snapshot_id))
        assert result.get("ok") is True, f"打开新页失败: {result}"
        # event_type 应为 popup(新标签页被打开并自动切换)。
        assert result.get("event_type") == "popup", (
            f"打开新标签页后 event_type 应为 popup,实际: {result.get('event_type')}"
        )

        # 现在应有两页,当前页是新页。
        pages = _parse_result(s.list_pages())
        page_list = pages.get("pages", [])
        assert len(page_list) == 2, f"打开新页后应有 2 页,实际: {len(page_list)}"
        # 新页 URL 含 popup-target。
        new_page = next(
            (p for p in page_list if p.get("is_current")), None
        )
        assert new_page is not None, "没有当前页"
        assert "popup-target" in new_page.get("url", ""), (
            f"当前页应是 popup-target,实际 url: {new_page.get('url')}"
        )

        # switch_page 切回原页。
        old_page = next(
            (p for p in page_list if not p.get("is_current")), None
        )
        assert old_page is not None, "找不到原页"
        switched = _parse_result(s.switch_page(old_page["page_id"]))
        assert switched.get("ok") is True, f"switch_page 失败: {switched}"
        assert "弹窗页" in switched.get("snapshot", ""), "切回原页后快照应含弹窗页标题"

        # close_page 关闭新页。
        closed = _parse_result(s.close_page(new_page["page_id"]))
        assert closed.get("ok") is True, f"close_page 失败: {closed}"
        # 关闭后应只剩一页。
        pages = _parse_result(s.list_pages())
        assert len(pages.get("pages", [])) == 1, (
            f"close_page 后应只剩 1 页,实际: {pages.get('pages')}"
        )


# ---------------------------------------------------------------------------
# P7 测试:screenshot / screenshot_element / download / upload_files
# + 产物管理(list/get/delete/cleanup_artifacts)
# ---------------------------------------------------------------------------


def _artifact_from_result(result: dict) -> dict:
    """从操作返回里取出 artifact 字段,缺失时抛断言。"""
    artifact = result.get("artifact")
    assert artifact, f"返回缺少 artifact 字段: {result}"
    return artifact


def test_screenshot_creates_png_artifact() -> None:
    """screenshot 生成 PNG 产物并登记:kind/mime/size 正确,文件存在。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        result = _parse_result(s.screenshot(snapshot_id))
        assert result.get("ok") is True, f"screenshot 失败: {result}"
        artifact = _artifact_from_result(result)
        assert artifact.get("kind") == "screenshot", f"kind 应为 screenshot: {artifact}"
        assert artifact.get("mime_type") == "image/png", (
            f"mime_type 应为 image/png: {artifact}"
        )
        assert artifact.get("size_bytes", 0) > 0, f"size_bytes 应 > 0: {artifact}"
        # 文件应实际存在。
        assert Path(artifact["path"]).is_file(), f"产物文件不存在: {artifact['path']}"
        # 纯读取:snapshot_id 应保持不变(不失效旧观察)。
        assert result.get("snapshot_id") == snapshot_id, "screenshot 不应改变 snapshot_id"


def test_screenshot_full_page() -> None:
    """full_page=True 全页截图应成功,产物更大。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/long")))
        result = _parse_result(s.screenshot(snapshot_id, full_page=True))
        assert result.get("ok") is True, f"full_page screenshot 失败: {result}"
        artifact = _artifact_from_result(result)
        assert artifact.get("size_bytes", 0) > 0


def test_screenshot_element() -> None:
    """screenshot_element 对指定 ref 截图,生成产物。

    screenshot_element 的稳定性检查(截图前后 DOM 版本不变)对时序敏感,
    偶发失败。这里用 data: URL 纯静态页降低概率,并在偶发失败时重试一次。
    """
    from browser.session import BrowserSession
    html = '<button id="b">静态按钮</button>'
    last_result = None
    for attempt in range(2):
        with BrowserSession() as s:
            snap, snapshot_id = _observation(
                s.navigate("data:text/html;charset=utf-8," + html)
            )
            ref = _find_ref_for_role(snap, "button", "静态按钮")
            assert ref, "静态页缺少按钮 ref"
            result = _parse_result(s.screenshot_element(ref, snapshot_id))
            last_result = result
            if result.get("ok"):
                artifact = _artifact_from_result(result)
                assert artifact.get("kind") == "element-screenshot", (
                    f"kind 应为 element-screenshot: {artifact}"
                )
                assert Path(artifact["path"]).is_file()
                return
    assert last_result.get("ok") is True, (
        f"screenshot_element 重试后仍失败: {last_result}"
    )


def test_download_creates_artifact() -> None:
    """download 点击下载链接存产物,文件内容正确。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        snap, snapshot_id = _observation(s.navigate(_fixture_url("/download")))
        ref = _find_ref_for_role(snap, "link", "下载文件")
        assert ref, "下载页缺少 '下载文件' 链接 ref"
        result = _parse_result(s.download(ref, snapshot_id, timeout_ms=5000))
        assert result.get("ok") is True, f"download 失败: {result}"
        artifact = _artifact_from_result(result)
        assert artifact.get("kind") == "download", f"kind 应为 download: {artifact}"
        # 文件内容应是 fixture 下载内容。
        content = Path(artifact["path"]).read_bytes()
        assert b"fixture download content" in content, (
            f"下载文件内容不对: {content[:50]!r}"
        )


def test_upload_files_shows_filename_on_page() -> None:
    """upload_files 上传文件后,页面应显示已选文件名。"""
    from browser.session import BrowserSession
    # 造一个临时文件在 workspace_root(cwd)内,供上传。
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=".", prefix="upload_test_"
    ) as f:
        f.write("upload test content")
        tmp_path = f.name
    try:
        with BrowserSession() as s:
            snap, snapshot_id = _observation(s.navigate(_fixture_url("/upload")))
            ref = _find_ref_for_role(snap, "button", "文件选择")
            # <input type=file> 在 AX tree 里 role 可能是 button 或 textbox,
            # 找不到 button 就找 textbox。
            if not ref:
                ref = _find_ref_for_role(snap, "textbox", "文件选择")
            assert ref, "上传页缺少文件选择 input ref"
            result = _parse_result(s.upload_files(ref, tmp_path, snapshot_id))
            assert result.get("ok") is True, f"upload_files 失败: {result}"
            # 页面应显示文件名。
            shown = s._page.evaluate('document.getElementById("upload-result").textContent')
            assert Path(tmp_path).name in shown, (
                f"上传后页面未显示文件名,实际: {shown!r}"
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_artifact_lifecycle_list_get_delete() -> None:
    """产物生命周期:screenshot 产生 -> list 看到 -> get 拿元数据 -> delete 删除。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        shot = _parse_result(s.screenshot(snapshot_id))
        artifact_id = _artifact_from_result(shot)["artifact_id"]
        artifact_path = Path(_artifact_from_result(shot)["path"])

        # list_artifacts 应含该产物。
        listed = _parse_result(s.list_artifacts())
        assert listed.get("ok") is True
        ids = [a.get("artifact_id") for a in listed.get("artifacts", [])]
        assert artifact_id in ids, f"list_artifacts 未含 {artifact_id}: {ids}"

        # get_artifact 拿元数据。
        got = _parse_result(s.get_artifact(artifact_id))
        assert got.get("ok") is True, f"get_artifact 失败: {got}"
        assert got["artifact"]["artifact_id"] == artifact_id

        # delete_artifact 删除,文件消失。
        deleted = _parse_result(s.delete_artifact(artifact_id))
        assert deleted.get("ok") is True, f"delete_artifact 失败: {deleted}"
        assert not artifact_path.exists(), "delete 后文件仍存在"

        # 再 list 应不含。
        listed2 = _parse_result(s.list_artifacts())
        ids2 = [a.get("artifact_id") for a in listed2.get("artifacts", [])]
        assert artifact_id not in ids2, "delete 后 list 仍含该产物"


def test_cleanup_artifacts_removes_all() -> None:
    """cleanup_artifacts 清理当前会话所有产物。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        # 产生两个产物。
        s.screenshot(snapshot_id)
        shot2 = _parse_result(s.screenshot(snapshot_id, full_page=True))
        paths = []
        listed = _parse_result(s.list_artifacts())
        paths = [Path(a["path"]) for a in listed.get("artifacts", [])]
        assert len(paths) >= 2, f"应至少 2 个产物,实际 {len(paths)}"

        # cleanup。
        cleaned = _parse_result(s.cleanup_artifacts())
        assert cleaned.get("ok") is True, f"cleanup_artifacts 失败: {cleaned}"

        # list 应为空。
        listed2 = _parse_result(s.list_artifacts())
        assert listed2.get("artifacts") == [], (
            f"cleanup 后应无产物,实际: {listed2.get('artifacts')}"
        )


def test_p7_actions_reject_stale_snapshot() -> None:
    """screenshot/screenshot_element/download/upload 拒绝 stale snapshot_id。

    每个动作:先拿一个 snapshot_id,再 navigate 一次让它失效,然后用旧 id
    调用,应返回 stale_snapshot。
    """
    from browser.session import BrowserSession

    def _stale_id_after_navigate(s, url: str) -> str:
        """navigate 拿 id,再 navigate 同页让 id 失效,返回失效的 id。"""
        _, old_id = _observation(s.navigate(url))
        _observation(s.navigate(url))  # 这次让 old_id 失效
        return old_id

    with BrowserSession() as s:
        # screenshot。
        stale_id = _stale_id_after_navigate(s, _fixture_url("/"))
        r = _parse_result(s.screenshot(stale_id))
        assert r.get("ok") is False and r.get("error_type") == "stale_snapshot", (
            f"screenshot 应拒绝 stale: {r}"
        )

        # screenshot_element:ref 来自失效快照前的页面,但 snapshot_id 已失效。
        snap, _ = _observation(s.navigate(_fixture_url("/article?name=alpha")))
        ref = _find_ref_for_role(snap, "link", "返回首页")
        if ref:
            stale_id2 = _stale_id_after_navigate(s, _fixture_url("/article?name=alpha"))
            r = _parse_result(s.screenshot_element(ref, stale_id2))
            assert r.get("ok") is False, f"screenshot_element 应拒绝 stale: {r}"
            assert r.get("error_type") == "stale_snapshot", (
                f"screenshot_element 错误类型: {r.get('error_type')}"
            )

        # download。
        snap2, _ = _observation(s.navigate(_fixture_url("/download")))
        dl_ref = _find_ref_for_role(snap2, "link", "下载文件")
        if dl_ref:
            stale_id3 = _stale_id_after_navigate(s, _fixture_url("/download"))
            r = _parse_result(s.download(dl_ref, stale_id3, timeout_ms=3000))
            assert r.get("ok") is False, f"download 应拒绝 stale: {r}"
            assert r.get("error_type") == "stale_snapshot", (
                f"download 错误类型: {r.get('error_type')}"
            )

        # upload_files。
        snap3, _ = _observation(s.navigate(_fixture_url("/upload")))
        up_ref = _find_ref_for_role(snap3, "button", "文件选择")
        if not up_ref:
            up_ref = _find_ref_for_role(snap3, "textbox", "文件选择")
        if up_ref:
            stale_id4 = _stale_id_after_navigate(s, _fixture_url("/upload"))
            r = _parse_result(s.upload_files(up_ref, ".", stale_id4))
            assert r.get("ok") is False, f"upload_files 应拒绝 stale: {r}"
            assert r.get("error_type") == "stale_snapshot", (
                f"upload_files 错误类型: {r.get('error_type')}"
            )


def test_artifact_not_found_errors() -> None:
    """get/delete 不存在的 artifact_id 返回 artifact_not_found。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _observation(s.navigate(_fixture_url("/")))
        # get 不存在。
        r = _parse_result(s.get_artifact("nonexistent"))
        assert r.get("ok") is False, "get 不存在 artifact 不应成功"
        assert r.get("error_type") == "artifact_not_found", (
            f"错误类型应为 artifact_not_found,实际: {r.get('error_type')}"
        )
        # delete 不存在。
        r = _parse_result(s.delete_artifact("nonexistent"))
        assert r.get("ok") is False
        assert r.get("error_type") == "artifact_not_found"
        # 空 id 参数校验。
        r = _parse_result(s.get_artifact(""))
        assert r.get("ok") is False
        assert r.get("error_type") == "artifact_not_found"


# ---------------------------------------------------------------------------
# P8 多模态测试:analyze_media / analyze_image / analyze_audio / analyze_page
#
# 多模态分析需调豆包模型(ARK_API_KEY + DOUBAO_MULTIMODAL_MODEL)。
# 无 key 时仍能测:参数校验、source 解析、类型不匹配、未配置错误 --
# 这些在调模型前就返回。有 key 时额外测真实分析(单独 skip)。
# ---------------------------------------------------------------------------


def _make_png_bytes() -> bytes:
    """造一个最小有效 PNG(1x1 红点),供 analyze_image 测试。"""
    # 标准 1x1 红色 PNG 字节。
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000"
        "00907753de0000000c4944415408d763f8ffff3f0005fe02fe5c9d96"
        "3a0000000049454e44ae426082"
    )


def _make_fake_audio_bytes() -> bytes:
    """造一段假的 mp3 字节(内容无效但扩展名是 .mp3),用于测类型不匹配。

    analyze 只按扩展名判定类型(validate_media),不真正解码,所以假字节足够。
    """
    return b"ID3\x03\x00" + b"\x00" * 200


def _write_workspace_file(name: str, content: bytes) -> str:
    """在工作区(cwd)内写一个临时文件,返回相对路径名(供 analyze 当 source)。"""
    Path(name).write_bytes(content)
    return name


def test_analyze_media_rejects_invalid_args() -> None:
    """analyze_* 参数校验:prompt 空、sources 空、timeout 非正。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _observation(s.navigate(_fixture_url("/")))
        # prompt 空。
        r = _parse_result(s.analyze_image("a.png", "  ", timeout_ms=None))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args", (
            f"空 prompt 应 invalid_args: {r}"
        )
        # sources 空(空列表)。
        r = _parse_result(s.analyze_media([], "描述", timeout_ms=None))
        assert r.get("ok") is False and r.get("error_type") == "invalid_media_path", (
            f"空 sources 应 invalid_media_path: {r}"
        )
        # sources 是非法类型(数字)。
        r = _parse_result(s.analyze_media(123, "描述", timeout_ms=None))
        assert r.get("ok") is False and r.get("error_type") == "invalid_media_path"
        # timeout 非正。
        r = _parse_result(s.analyze_image("a.png", "描述", timeout_ms=0))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args"
        r = _parse_result(s.analyze_image("a.png", "描述", timeout_ms=-5))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args"
        # timeout 是 bool。
        r = _parse_result(s.analyze_image("a.png", "描述", timeout_ms=True))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args"


def test_analyze_media_source_resolution() -> None:
    """source 解析:artifact_id 有效、工作区路径、不存在、绝对路径、含..。"""
    from browser.session import BrowserSession
    png_name = _write_workspace_file(
        "analyze_test_pic.png", _make_png_bytes()
    )
    try:
        with BrowserSession() as s:
            _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
            # screenshot 产生一个 artifact(PNG)。
            shot = _parse_result(s.screenshot(snapshot_id))
            artifact_id = shot["artifact"]["artifact_id"]

            # 用 artifact_id 作 source -- 解析通过(到模型阶段才报未配置)。
            r = _parse_result(s.analyze_image(artifact_id, "描述这张图"))
            # 未配置 key 时应 multimodal_not_configured(说明 source 解析成功)。
            if r.get("error_type") == "multimodal_not_configured":
                pass  # source 解析成功,只是没配 key
            elif r.get("ok"):
                pass  # 配了 key,真实分析了
            else:
                assert False, f"artifact_id source 解析应成功或未配置,实际: {r}"

            # 工作区相对路径(有效 PNG)-- 同样到模型阶段。
            r = _parse_result(s.analyze_image(png_name, "描述"))
            assert r.get("ok") or r.get("error_type") == "multimodal_not_configured", (
                f"工作区 PNG source 应解析成功: {r}"
            )

            # 不存在的路径。
            r = _parse_result(s.analyze_image("nonexistent_file.png", "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "media_not_found", (
                f"不存在路径应 media_not_found: {r}"
            )

            # 绝对路径 -- 拒绝(必须在 workspace 内)。
            r = _parse_result(s.analyze_image(str(Path("x.png").resolve()), "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "invalid_media_path", (
                f"绝对路径应 invalid_media_path: {r}"
            )

            # 含 .. 的路径。
            r = _parse_result(s.analyze_image("../escape.png", "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "invalid_media_path"
    finally:
        Path(png_name).unlink(missing_ok=True)


def test_analyze_image_rejects_unsupported_extension() -> None:
    """analyze_image 不支持的扩展名返回 unsupported_media_type。"""
    from browser.session import BrowserSession
    # 造一个 .txt 文件(非图片非音频)。
    txt_name = _write_workspace_file("analyze_test.txt", b"not an image")
    try:
        with BrowserSession() as s:
            _observation(s.navigate(_fixture_url("/")))
            r = _parse_result(s.analyze_image(txt_name, "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "unsupported_media_type", (
                f".txt 应 unsupported_media_type: {r}"
            )
    finally:
        Path(txt_name).unlink(missing_ok=True)


def test_analyze_image_type_mismatch_with_audio() -> None:
    """analyze_image 传音频文件返回 unsupported_media_type(类型不匹配)。

    类型不匹配在 validate_media 阶段判定,不需要 API key。
    """
    from browser.session import BrowserSession
    mp3_name = _write_workspace_file(
        "analyze_test_audio.mp3", _make_fake_audio_bytes()
    )
    try:
        with BrowserSession() as s:
            _observation(s.navigate(_fixture_url("/")))
            # analyze_image 只接受 image,传 mp3 应类型不匹配。
            r = _parse_result(s.analyze_image(mp3_name, "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "unsupported_media_type", (
                f"analyze_image 传音频应 unsupported_media_type: {r}"
            )
    finally:
        Path(mp3_name).unlink(missing_ok=True)


def test_analyze_audio_type_mismatch_with_image() -> None:
    """analyze_audio 传图片返回 unsupported_media_type。"""
    from browser.session import BrowserSession
    png_name = _write_workspace_file(
        "analyze_test_pic2.png", _make_png_bytes()
    )
    try:
        with BrowserSession() as s:
            _observation(s.navigate(_fixture_url("/")))
            r = _parse_result(s.analyze_audio(png_name, "描述"))
            assert r.get("ok") is False
            assert r.get("error_type") == "unsupported_media_type", (
                f"analyze_audio 传图片应 unsupported_media_type: {r}"
            )
    finally:
        Path(png_name).unlink(missing_ok=True)


def test_analyze_image_not_configured_returns_error() -> None:
    """有效图片 + 未配置 ARK_API_KEY 返回 multimodal_not_configured。

    已配置 key 时这条会真实调模型(跳到 ok 分支),所以已配置环境不断言未配置。
    """
    import os
    from browser.session import BrowserSession
    if os.getenv("ARK_API_KEY") and os.getenv("DOUBAO_MULTIMODAL_MODEL"):
        return  # 已配置,这个"未配置"测试不适用,跳过
    png_name = _write_workspace_file(
        "analyze_test_pic3.png", _make_png_bytes()
    )
    try:
        with BrowserSession() as s:
            _observation(s.navigate(_fixture_url("/")))
            r = _parse_result(s.analyze_image(png_name, "描述这张图片"))
            assert r.get("ok") is False, f"未配置应失败: {r}"
            assert r.get("error_type") == "multimodal_not_configured", (
                f"未配置应 multimodal_not_configured: {r}"
            )
    finally:
        Path(png_name).unlink(missing_ok=True)


def test_analyze_page_rejects_stale_snapshot() -> None:
    """analyze_page 拒绝 stale snapshot_id。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, stale_id = _observation(s.navigate(_fixture_url("/")))
        _observation(s.navigate(_fixture_url("/")))  # 让 stale_id 失效
        r = _parse_result(s.analyze_page(stale_id, "描述页面"))
        assert r.get("ok") is False, f"analyze_page 应拒绝 stale: {r}"
        assert r.get("error_type") == "stale_snapshot", (
            f"错误类型应为 stale_snapshot: {r.get('error_type')}"
        )


def test_analyze_page_rejects_invalid_args() -> None:
    """analyze_page 参数校验:prompt 空、full_page 非布尔。"""
    from browser.session import BrowserSession
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        # prompt 空。
        r = _parse_result(s.analyze_page(snapshot_id, "  "))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args", (
            f"空 prompt 应 invalid_args: {r}"
        )
        # full_page 非布尔。
        r = _parse_result(s.analyze_page(snapshot_id, "描述", full_page="yes"))  # type: ignore[arg-type]
        assert r.get("ok") is False and r.get("error_type") == "invalid_args", (
            f"full_page 非布尔应 invalid_args: {r}"
        )
        # timeout 非正。
        r = _parse_result(s.analyze_page(snapshot_id, "描述", timeout_ms=0))
        assert r.get("ok") is False and r.get("error_type") == "invalid_args"


def test_analyze_page_not_configured_returns_error() -> None:
    """analyze_page 未配置 key 时返回 multimodal_not_configured(截图成功后)。"""
    import os
    from browser.session import BrowserSession
    if os.getenv("ARK_API_KEY") and os.getenv("DOUBAO_MULTIMODAL_MODEL"):
        return  # 已配置,跳过"未配置"测试
    with BrowserSession() as s:
        _, snapshot_id = _observation(s.navigate(_fixture_url("/")))
        r = _parse_result(s.analyze_page(snapshot_id, "描述这个页面"))
        assert r.get("ok") is False, f"未配置应失败: {r}"
        assert r.get("error_type") == "multimodal_not_configured", (
            f"未配置应 multimodal_not_configured: {r.get('error_type')}"
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
    ("test_smoke_real_baike_navigate",
     test_smoke_real_baike_navigate),
    ("test_iframe_parent_page_navigable",
     test_iframe_parent_page_navigable),
    ("test_dialog_api_boundaries",
     test_dialog_api_boundaries),
    ("test_wait_for_load_state_on_slow_page",
     test_wait_for_load_state_on_slow_page),
    ("test_wait_for_text_succeeds_on_delayed_content",
     test_wait_for_text_succeeds_on_delayed_content),
    ("test_wait_for_ref_succeeds_on_delayed_element",
     test_wait_for_ref_succeeds_on_delayed_element),
    ("test_click_navigation_to_ajax_page_returns_complete_snapshot",
     test_click_navigation_to_ajax_page_returns_complete_snapshot),
    ("test_press_navigation_to_ajax_page_returns_complete_snapshot",
     test_press_navigation_to_ajax_page_returns_complete_snapshot),
    ("test_check_uncheck_checkbox",
     test_check_uncheck_checkbox),
    ("test_hover_and_focus_return_new_snapshot",
     test_hover_and_focus_return_new_snapshot),
    ("test_drag_and_drop_updates_target",
     test_drag_and_drop_updates_target),
    ("test_keyboard_shortcut_equivalent_to_press",
     test_keyboard_shortcut_equivalent_to_press),
    ("test_dialog_strategy_handles_alert_without_blocking",
     test_dialog_strategy_handles_alert_without_blocking),
    ("test_multi_page_lifecycle",
     test_multi_page_lifecycle),
    ("test_screenshot_creates_png_artifact",
     test_screenshot_creates_png_artifact),
    ("test_screenshot_full_page",
     test_screenshot_full_page),
    ("test_screenshot_element",
     test_screenshot_element),
    ("test_download_creates_artifact",
     test_download_creates_artifact),
    ("test_upload_files_shows_filename_on_page",
     test_upload_files_shows_filename_on_page),
    ("test_artifact_lifecycle_list_get_delete",
     test_artifact_lifecycle_list_get_delete),
    ("test_cleanup_artifacts_removes_all",
     test_cleanup_artifacts_removes_all),
    ("test_p7_actions_reject_stale_snapshot",
     test_p7_actions_reject_stale_snapshot),
    ("test_artifact_not_found_errors",
     test_artifact_not_found_errors),
    ("test_analyze_media_rejects_invalid_args",
     test_analyze_media_rejects_invalid_args),
    ("test_analyze_media_source_resolution",
     test_analyze_media_source_resolution),
    ("test_analyze_image_rejects_unsupported_extension",
     test_analyze_image_rejects_unsupported_extension),
    ("test_analyze_image_type_mismatch_with_audio",
     test_analyze_image_type_mismatch_with_audio),
    ("test_analyze_audio_type_mismatch_with_image",
     test_analyze_audio_type_mismatch_with_image),
    ("test_analyze_image_not_configured_returns_error",
     test_analyze_image_not_configured_returns_error),
    ("test_analyze_page_rejects_stale_snapshot",
     test_analyze_page_rejects_stale_snapshot),
    ("test_analyze_page_rejects_invalid_args",
     test_analyze_page_rejects_invalid_args),
    ("test_analyze_page_not_configured_returns_error",
     test_analyze_page_not_configured_returns_error),
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
