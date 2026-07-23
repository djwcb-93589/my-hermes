"""
browser -- 独立的浏览器读取与交互模块。

参考 s17 文档(``D:\\learn-Hermes\\docs\\zh\\s17-browser-automation.md``):
agent 通过 accessibility tree 看网页,通过元素引用(@e1、@e2)操作网页。
本模块只做独立测试,不接入 agent。

公开 API::

    import json
    from browser import BrowserSession, get_session, close_session

    with BrowserSession() as s:
        observation = json.loads(s.navigate("https://example.com"))
        print(observation["snapshot"])
        # 交互操作返回 {"ok": True, "snapshot_id": ..., "snapshot": ...} 或
        # {"ok": False, "error_type": ..., "error": ...}
        result = s.click("e1", observation["snapshot_id"])
        print(result)

依赖:``uv add playwright``。走系统 Chrome 时不需要
``playwright install chromium``。
"""

from __future__ import annotations

from browser.accessibility import INTERACTIVE_ROLES, format_snapshot
from browser.multimodal import (
    DoubaoMultimodalProvider,
    MultimodalAnalyzer,
    MultimodalError,
)
from browser.session import (
    BrowserSession,
    close_all_sessions,
    close_session,
    get_session,
)

__all__ = [
    "BrowserSession",
    "DoubaoMultimodalProvider",
    "INTERACTIVE_ROLES",
    "MultimodalAnalyzer",
    "MultimodalError",
    "close_all_sessions",
    "close_session",
    "format_snapshot",
    "get_session",
]
