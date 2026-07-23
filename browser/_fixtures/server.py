"""
本地 fixture HTTP 服务器。

用标准库 ``http.server.ThreadingHTTPServer`` 在后台 daemon 线程运行,端口
由系统分配(传 0 避免冲突),随测试进程启停。提供 cookie、表单、延迟响应、
下载、iframe 等真实 HTTP 语义,让测试不依赖外网。

用法::

    site = start_fixture_server()
    try:
        s.navigate(site.base_url + "/")
        ...
    finally:
        site.stop()

延迟响应:``/slow?delay=1500`` 让服务器 sleep 1.5 秒再返回,测 wait_for_load_state /
wait 超时 / wait 取消。``/appear?delay=300`` 则是页面加载后 JS 延迟插入内容
(响应本身不慢),测 wait_for_text 的成功路径。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from browser._fixtures import pages


# cookie-set 页固定写入的 cookie,测试据此断言。
FIXTURE_COOKIE_NAME = "fixture_session"
FIXTURE_COOKIE_VALUE = "fixed-value"


class _FixtureHandler(BaseHTTPRequestHandler):
    """fixture 路由处理器。每个请求一个实例(由 HTTPServer 保证)。"""

    # 关掉默认 stderr 日志,测试输出保持干净。
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定的方法名
        """按 path + query 分发到对应 fixture 页面。"""
        parsed = urlsplit(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self._route(path, query)

    def _route(self, path: str, query: dict[str, str]) -> None:
        """根据 path 选页面,带特殊处理的路径单独走。"""
        if path == "/":
            self._send_html(pages.home_page())
        elif path == "/article":
            self._send_html(pages.article_page(query.get("name", "alpha")))
        elif path == "/search":
            self._send_html(pages.search_page(query.get("q")))
        elif path == "/form":
            self._send_html(pages.form_page())
        elif path == "/result":
            self._send_html(pages.result_page(query))
        elif path == "/cookie-set":
            # 写入固定 cookie,让 /cookie-check 能读到。
            self._send_html(
                pages.cookie_set_page(),
                extra_headers=[
                    (
                        "Set-Cookie",
                        f"{FIXTURE_COOKIE_NAME}={FIXTURE_COOKIE_VALUE}; Path=/",
                    )
                ],
            )
        elif path == "/cookie-check":
            self._send_html(pages.cookie_check_page())
        elif path == "/long":
            self._send_html(pages.long_page())
        elif path == "/same-url-state":
            self._send_html(pages.same_url_state_page())
        elif path == "/iframe":
            self._send_html(pages.iframe_page())
        elif path == "/controls":
            self._send_html(pages.controls_page())
        elif path == "/drag":
            self._send_html(pages.drag_page())
        elif path == "/popup":
            self._send_html(pages.popup_page())
        elif path == "/upload":
            self._send_html(pages.upload_page())
        elif path == "/dialog":
            self._send_html(pages.dialog_page())
        elif path == "/download":
            self._send_html(pages.download_page())
        elif path == "/download/file":
            # 真正的附件下载:Content-Disposition: attachment。
            self._send_download()
        elif path == "/slow":
            # 服务器侧延迟,模拟慢加载。delay 单位毫秒。
            delay_ms = _parse_int(query.get("delay"), default=0)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            self._send_html(pages.slow_page())
        elif path == "/appear":
            # 响应不慢,但页面 JS 延迟插入内容。
            delay_ms = _parse_int(query.get("delay"), default=300)
            self._send_html(pages.appear_page(delay_ms))
        elif path == "/slow-render":
            # AJAX 延迟渲染:domcontentloaded/load 时内容不全,JS 延迟注入。
            # 测 click/press 触发导航后能否拿到完整 AJAX 页面。
            delay_ms = _parse_int(query.get("delay"), default=500)
            self._send_html(pages.slow_render_page(delay_ms))
        else:
            self._send_html(pages.not_found_page(self.path), status=404)

    def _send_html(
        self,
        body: str,
        status: int = 200,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        """发送 HTML 响应。"""
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_download(self) -> None:
        """发送一个小的文本附件。"""
        content = b"fixture download content\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="fixture.txt"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _parse_int(value: str | None, *, default: int = 0) -> int:
    """安全解析 query 里的整数,失败返回 default。"""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class FixtureSite:
    """运行中的 fixture 站点句柄。"""

    base_url: str
    _server: ThreadingHTTPServer

    def stop(self) -> None:
        """关闭服务器,释放端口。重复调用安全。"""
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


def start_fixture_server() -> FixtureSite:
    """启动 fixture 服务器,返回带 base_url 和 stop 的句柄。

    端口 0 让系统分配,避免冲突;daemon 线程随主进程退出。启动是同步的 --
    返回时服务器已就绪可接受连接。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    # daemon 线程:主进程退出时自动收,不会阻塞退出。
    server.daemon_threads = True
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    return FixtureSite(base_url=base_url, _server=server)
