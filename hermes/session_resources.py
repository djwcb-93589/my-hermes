"""按可信会话标识统一释放运行期资源。"""

from __future__ import annotations


def cleanup_session_resources(session_key: str) -> None:
    """清理一个会话的浏览器、审批状态和 backend，单项失败不阻断其余收尾。"""
    from browser.runtime import default_browser_manager
    from hermes.backends import cleanup_backend

    try:
        default_browser_manager.close_session(session_key)
    except Exception:
        pass
    try:
        cleanup_backend(session_key)
    except Exception:
        pass


def cleanup_all_session_resources() -> None:
    """在进程退出时统一关闭全部浏览器和 backend 缓存。"""
    from browser.runtime import default_browser_manager
    from hermes.backends import cleanup_all_backends

    try:
        default_browser_manager.close_all()
    except Exception:
        pass
    try:
        cleanup_all_backends()
    except Exception:
        pass
