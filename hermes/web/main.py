"""启动本地只读 Web 管理 API。"""

from __future__ import annotations


def main() -> None:
    """仅绑定本机回环地址，避免意外暴露管理接口。"""
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "Web 管理 API 需要安装 FastAPI 和 Uvicorn。"
        ) from exc

    from hermes.web.app import app

    # 关闭访问日志，避免把查询参数或会话标识写入日志。
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_level="info",
        access_log=False,
    )
