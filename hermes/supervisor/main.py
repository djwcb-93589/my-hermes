"""独立 Gateway Supervisor 的正式进程入口。"""

from __future__ import annotations

import logging
import signal
import threading

from hermes.backend_control import SupervisorLeaseLost
from hermes.config_revision import FileConfigRevisionReader
from hermes.persistence.backend_control import SQLiteBackendControlRepository
from hermes.persistence.schema import init_db

from .config import SupervisorConfigurationError, load_supervisor_config
from .launcher import LocalGatewayProcessLauncher, gateway_launch_spec
from .lease import SQLiteSupervisorLease, SupervisorLeaseUnavailable
from .runner import BackendSupervisor


logger = logging.getLogger(__name__)


def run_supervisor() -> None:
    """迁移正式 persistence、争用 lease 并进入串行控制循环。"""
    try:
        config = load_supervisor_config()
    except SupervisorConfigurationError as exc:
        raise SystemExit(f"supervisor configuration error: {exc}") from exc

    connection = None
    lease = None
    shutdown = threading.Event()
    previous_handlers: dict[int, object] = {}
    try:
        connection = init_db(config.db_path)
        lease = SQLiteSupervisorLease(connection)
        if not lease.acquire():
            raise SupervisorLeaseUnavailable(
                "another supervisor already holds the profile lease"
            )
        _install_signal_handlers(shutdown, previous_handlers)
        launcher = LocalGatewayProcessLauncher(gateway_launch_spec(config))
        supervisor = BackendSupervisor(
            SQLiteBackendControlRepository(config.db_path),
            launcher,
            launcher,
            FileConfigRevisionReader(config.config_path),
            lease,
        )
        logger.info(
            "Gateway Supervisor started: supervisor_stage=start "
            "backend_type=gateway action=none request_status=none "
            "result_code=ready exception_type=none"
        )
        supervisor.run(shutdown)
    except (SupervisorLeaseUnavailable, SupervisorLeaseLost):
        raise SystemExit("supervisor lease is unavailable") from None
    except KeyboardInterrupt:
        shutdown.set()
    except Exception as exc:
        logger.error(
            "Gateway Supervisor failed: supervisor_stage=failed "
            "backend_type=gateway action=none request_status=none "
            "result_code=supervisor_failed exception_type=%s",
            type(exc).__name__,
        )
        raise SystemExit("supervisor failed") from None
    finally:
        _restore_signal_handlers(previous_handlers)
        if lease is not None:
            lease.release()
        if connection is not None:
            connection.close()
        logger.info(
            "Gateway Supervisor stopped: supervisor_stage=stop "
            "backend_type=gateway action=none request_status=none "
            "result_code=stopped exception_type=none"
        )


def _install_signal_handlers(
    shutdown: threading.Event,
    previous_handlers: dict[int, object],
) -> None:
    """信号只停止领取新请求，不直接控制 Gateway。"""
    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is None:
            continue
        previous_handlers[int(signal_value)] = signal.getsignal(signal_value)

        def handle_shutdown(_signum, _frame) -> None:
            shutdown.set()

        signal.signal(signal_value, handle_shutdown)


def _restore_signal_handlers(previous_handlers: dict[int, object]) -> None:
    for signal_number, handler in previous_handlers.items():
        try:
            signal.signal(signal_number, handler)
        except (OSError, RuntimeError, ValueError):
            pass


__all__ = ["run_supervisor"]
