"""Dashboard 的正式 Uvicorn 启动入口。"""

from __future__ import annotations

from hermes.web.config import DashboardConfigurationError, load_dashboard_config


def run_dashboard() -> None:
    """按唯一装配路径启动 Dashboard，不加载 Agent、Gateway 或 Plugin 运行时。"""
    try:
        config = load_dashboard_config()
    except DashboardConfigurationError as exc:
        raise SystemExit(f"dashboard configuration error: {exc}") from exc

    try:
        import uvicorn
        from hermes.web.app import build_dashboard_app
    except ImportError as exc:
        raise SystemExit(
            "Dashboard requires FastAPI and Uvicorn."
        ) from exc

    application = build_dashboard_app(config)
    # 关闭访问日志，避免会话标识和查询条件进入进程日志。
    uvicorn.run(
        application,
        host=config.host,
        port=config.port,
        log_level="info",
        access_log=False,
    )


def main() -> None:
    """保留 python -m hermes.web 的兼容启动入口。"""
    run_dashboard()
