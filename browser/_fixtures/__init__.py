"""本地 fixture 测试网站。

提供 HTTP 语义(cookie、表单、延迟、下载、iframe)的本地站点,让 browser
模块测试不依赖外网。详见 ``server.py`` 和 ``pages.py``。
"""

from __future__ import annotations

from browser._fixtures.server import FixtureSite, start_fixture_server

__all__ = ["FixtureSite", "start_fixture_server"]
