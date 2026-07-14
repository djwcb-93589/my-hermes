"""
飞书 adapter:基于 Webhook HTTP 回调,接收文本并发送 Markdown 富文本。

特性:
  - 内置有界线程 Webhook Server,限制路径、请求体、超时、并发和 IP 速率
  - 支持 URL challenge 校验
  - 私聊 / 群聊 / 话题路由
  - 群聊默认仅响应 @机器人
  - open_id + union_id
  - tenant_access_token 缓存
  - HTTP API 分类重试和 tenant token 失效刷新
  - Markdown 富文本和按请求体字节数安全分片
  - 消息回复 / 话题回复和按会话发送限速
  - allowed_users / allowed_chats / allow_all 白名单
  - MessageDeduplicator 防止飞书重复推送

不实现:加密事件解密、图片、文件、语音、卡片、流式回复。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import socket
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.adapters.webhook_security import (
    BoundedThreadingHTTPServer,
    SlidingWindowRateLimiter,
    TrustedProxyResolver,
    redact_ip,
)
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


FEISHU_POST_LIMIT_BYTES = 30 * 1024  # 飞书单条富文本请求体上限
FEISHU_POST_SAFETY_MARGIN_BYTES = 1024  # 为 receive_id 等外层字段预留空间
FEISHU_TOKEN_ERROR_CODES = frozenset({99991663, 99991665})
# 飞书回复 API 的降级条件必须与普通权限错误分开。只有目标消息或话题
# 本身不可用于回复时，才允许改成向 chat 直接发送；通用权限错误不能降级。
FEISHU_THREAD_REPLY_UNSUPPORTED_CODES = frozenset({230071, 230072})
FEISHU_REPLY_TARGET_MISSING_CODES = frozenset({
    230011,  # 原消息已撤回
    230019,  # 目标话题不存在
    230050,  # 原消息对当前机器人不可见，无法回复
    230054,  # 原消息类型不支持回复操作
    230110,  # 原消息已删除
    230111,  # 原消息即将自毁，不允许回复
})
FEISHU_RATE_LIMIT_ERROR_CODES = frozenset({230020, 99991400, 99991401})
FEISHU_TRANSIENT_SEND_ERROR_CODES = frozenset({230049})
FEISHU_PERMISSION_ERROR_CODES = frozenset({
    230002,  # 机器人不在目标群
    230006,  # 未启用机器人能力
    230013,  # 用户不在应用可用范围
    230018,  # 群设置禁止发送
    230027,  # 缺少 API 权限
    230035,  # 没有发送消息权限
    99991672,
})
FEISHU_BATCH_QUIET_SECONDS = 0.6  # 连续文本静默多久后提交
FEISHU_BATCH_MAX_WAIT_SECONDS = 2.0  # 单批消息最长累计等待时间
FEISHU_BATCH_SEPARATOR = "\n"
FEISHU_WEBHOOK_ACCEPT_TIMEOUT_SECONDS = 2.5
FEISHU_WEBHOOK_DEFAULT_PATH = "/feishu/webhook"
FEISHU_WEBHOOK_DEFAULT_MAX_BODY_BYTES = 1024 * 1024
FEISHU_WEBHOOK_READ_CHUNK_BYTES = 64 * 1024
FEISHU_DEDUP_TTL_SECONDS = 72 * 60 * 60  # 已处理消息保留 72 小时
FEISHU_DEDUP_CLEANUP_INTERVAL_SECONDS = 60 * 60
_IMMEDIATE_COMMANDS = frozenset({"/new", "/stop", "/status"})


class FeishuAdapter(BasePlatformAdapter):
    """飞书 Webhook 文本 adapter。"""

    PLATFORM = "feishu"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        db_path: str,
        webhook_host: str = "127.0.0.1",
        webhook_port: int = 8787,
        webhook_path: str = FEISHU_WEBHOOK_DEFAULT_PATH,
        webhook_max_body_bytes: int = FEISHU_WEBHOOK_DEFAULT_MAX_BODY_BYTES,
        webhook_read_timeout_seconds: float = 5.0,
        webhook_max_concurrent_requests: int = 32,
        webhook_rate_limit_window_seconds: float = 60.0,
        webhook_rate_limit_max_requests: int = 120,
        webhook_rate_limit_max_tracked_ips: int = 2048,
        webhook_trusted_proxies: list[str] | None = None,
        verification_token: str = "",
        encrypt_key: str = "",
        bot_open_id: str = "",
        is_lark: bool = False,
        dm_only: bool = True,
        require_mention: bool = True,
        allow_all: bool = False,
        allowed_users: list[str] | None = None,
        allowed_chats: list[str] | None = None,
        send_total_attempts: int | None = None,
        send_max_retries: int | None = None,
        send_retry_base_delay_seconds: float | None = None,
        send_retry_base_delay: float | None = None,
        send_retry_max_delay_seconds: float = 3.0,
        adapter_retry_after_max_seconds: float = 5.0,
        send_rate_limit_per_chat: int = 5,
        send_rate_limit_cache_idle_ttl_seconds: float = 600.0,
        send_rate_limit_max_tracked_chats: int = 1024,
    ):
        super().__init__("feishu")
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "")
        self.db_path = db_path
        self.webhook_host = str(webhook_host or "127.0.0.1").strip()
        self.webhook_port = int(webhook_port)
        self.webhook_path = self._validate_webhook_path(webhook_path)
        self.webhook_max_body_bytes = self._positive_int(
            "webhook_max_body_bytes",
            webhook_max_body_bytes,
        )
        self.webhook_read_timeout_seconds = self._positive_float(
            "webhook_read_timeout_seconds",
            webhook_read_timeout_seconds,
        )
        self.webhook_max_concurrent_requests = self._positive_int(
            "webhook_max_concurrent_requests",
            webhook_max_concurrent_requests,
        )
        self.webhook_rate_limit_window_seconds = self._positive_float(
            "webhook_rate_limit.window_seconds",
            webhook_rate_limit_window_seconds,
        )
        self.webhook_rate_limit_max_requests = self._positive_int(
            "webhook_rate_limit.max_requests",
            webhook_rate_limit_max_requests,
        )
        self.webhook_rate_limit_max_tracked_ips = self._positive_int(
            "webhook_rate_limit.max_tracked_ips",
            webhook_rate_limit_max_tracked_ips,
        )
        self.verification_token = str(verification_token or "")
        self.encrypt_key = str(encrypt_key or "")
        self.bot_open_id = str(bot_open_id or "").strip()
        self.is_lark = bool(is_lark)
        self.dm_only = bool(dm_only)
        self.require_mention = require_mention
        self.allow_all = allow_all
        self.allowed_users = set(allowed_users or [])
        self.allowed_chats = set(allowed_chats or [])
        configured_attempts = (
            send_total_attempts
            if send_total_attempts is not None
            else send_max_retries
        )
        if configured_attempts is None:
            configured_attempts = 3
        self.send_total_attempts = max(1, int(configured_attempts))
        # 旧属性只作为兼容别名；其值现在明确表示“总尝试次数”。
        self.send_max_retries = self.send_total_attempts
        configured_base_delay = (
            send_retry_base_delay_seconds
            if send_retry_base_delay_seconds is not None
            else send_retry_base_delay
        )
        if configured_base_delay is None:
            configured_base_delay = 0.5
        self.send_retry_base_delay_seconds = self._nonnegative_float(
            "send_retry_base_delay_seconds",
            configured_base_delay,
        )
        self.send_retry_base_delay = self.send_retry_base_delay_seconds
        self.send_retry_max_delay_seconds = max(
            self.send_retry_base_delay_seconds,
            self._nonnegative_float(
                "send_retry_max_delay_seconds",
                send_retry_max_delay_seconds,
            ),
        )
        self.adapter_retry_after_max_seconds = self._nonnegative_float(
            "adapter_retry_after_max_seconds",
            adapter_retry_after_max_seconds,
        )
        self.send_rate_limit_per_chat = max(1, int(send_rate_limit_per_chat))
        self.send_rate_limit_cache_idle_ttl_seconds = self._positive_float(
            "send_rate_limit_cache_idle_ttl_seconds",
            send_rate_limit_cache_idle_ttl_seconds,
        )
        self.send_rate_limit_max_tracked_chats = self._positive_int(
            "send_rate_limit_max_tracked_chats",
            send_rate_limit_max_tracked_chats,
        )

        self.api_base = (
            "https://open.larksuite.com/open-apis"
            if self.is_lark
            else "https://open.feishu.cn/open-apis"
        )
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._http = None
        self._server: BoundedThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._webhook_rate_limiter = SlidingWindowRateLimiter(
            window_seconds=self.webhook_rate_limit_window_seconds,
            max_requests=self.webhook_rate_limit_max_requests,
            max_tracked_keys=self.webhook_rate_limit_max_tracked_ips,
        )
        self._proxy_resolver = TrustedProxyResolver(webhook_trusted_proxies)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reliability_db: sqlite3.Connection | None = None
        self._last_dedup_cleanup = 0.0
        self._batch_buffers: dict[str, list[str]] = {}
        self._batch_events: dict[str, MessageEvent] = {}
        self._batch_message_ids: dict[str, list[str]] = {}
        self._batch_sources: dict[str, list[dict]] = {}
        self._batch_started_at: dict[str, float] = {}
        self._batch_tasks: dict[str, asyncio.Task] = {}
        self._batch_waiters: dict[str, list[asyncio.Future]] = {}
        self._inflight_messages: dict[str, asyncio.Future] = {}
        self._send_rate_locks: dict[str, asyncio.Lock] = {}
        self._send_timestamps: dict[str, deque[float]] = {}
        self._send_rate_last_used: dict[str, float] = {}
        self._send_rate_users: dict[str, int] = {}
        self._send_rate_next_cleanup_at = 0.0
        # 缓存满且所有 chat 都在使用时，新 chat 共用保守的溢出限速桶，
        # 从而不突破 key 上限，也不删除仍有 waiter 的 lock。
        self._send_rate_overflow_lock = asyncio.Lock()
        self._send_rate_overflow_timestamps: deque[float] = deque()
        self._initialized = False
        self._pending_restored = False

    @staticmethod
    def _positive_int(name: str, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be greater than 0")
        return parsed

    @staticmethod
    def _positive_float(name: str, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be greater than 0")
        return parsed

    @staticmethod
    def _nonnegative_float(name: str, value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{name} must be greater than or equal to 0")
        return parsed

    @staticmethod
    def _validate_webhook_path(value: Any) -> str:
        raw = str(value or "").strip()
        parsed = urlsplit(raw)
        if (
            not raw.startswith("/")
            or raw == "/"
            or len(raw) > 256
            or "\r" in raw
            or "\n" in raw
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path != raw
            or raw == "/healthz"
        ):
            raise ValueError("webhook_path must be a fixed non-root HTTP path")
        return raw

    @staticmethod
    def _safe_log_label(value: Any) -> str:
        text = str(value or "")[:64]
        return "".join(
            char for char in text
            if char.isalnum() or char in {".", "_", "-"}
        )

    def _log_webhook_result(
        self,
        *,
        status: int,
        client_ip: str,
        body_bytes: int = 0,
        event_type: str = "",
        reason: str = "",
        exception_type: str = "",
    ) -> None:
        fields = [
            f"status={int(status)}",
            f"ip={redact_ip(client_ip)}",
            f"bytes={max(0, int(body_bytes))}",
        ]
        safe_event = self._safe_log_label(event_type)
        safe_reason = self._safe_log_label(reason)
        safe_exception = self._safe_log_label(exception_type)
        if safe_event:
            fields.append(f"event={safe_event}")
        if safe_reason:
            fields.append(f"reason={safe_reason}")
        if safe_exception:
            fields.append(f"exception={safe_exception}")
        print(f"  [feishu:http] {' '.join(fields)}")

    def _log_webhook_overload(self, peer_ip: str) -> None:
        self._log_webhook_result(
            status=503,
            client_ip=peer_ip,
            reason="concurrency_limit",
        )

    # ===================== 生命周期 =====================

    async def initialize(self) -> bool:
        """初始化 Inbox 和发送客户端，但不创建或启动 Webhook Server。"""
        if self._initialized:
            return True
        try:
            import httpx
        except ImportError:
            print("  [feishu] httpx not installed, skipping")
            return False

        if not self.app_id or not self.app_secret:
            print("  [feishu] missing app_id / app_secret")
            return False
        if not self.db_path:
            print("  [feishu] missing db_path")
            return False
        if not self.verification_token:
            print("  [feishu] missing verification_token")
            return False
        if self.encrypt_key:
            # Encrypt Key 模式还需要签名校验和 AES 解密。当前不支持时
            # 必须拒绝启动,不能降级成明文 token 校验制造伪安全。
            print("  [feishu] encrypt_key mode is not supported")
            return False
        if not self.dm_only and self.require_mention and not self.bot_open_id:
            print(
                "  [feishu] bot_open_id is required when group messages "
                "require an exact mention"
            )
            return False
        if self.webhook_host in {"0.0.0.0", "::", "[::]"}:
            print(
                "  [feishu] SECURITY WARNING: webhook is bound to all network "
                "interfaces; place it behind a trusted reverse proxy and firewall"
            )

        try:
            self._open_reliability_store()
            self._loop = asyncio.get_running_loop()
            self._http = httpx.AsyncClient(timeout=15.0)
            self._initialized = True
            self._pending_restored = False
            return True
        except Exception as exc:
            print(f"  [feishu] initialization failed: {type(exc).__name__}")
            self._running = False
            await self._close_http_client()
            self._close_reliability_store()
            self._loop = None
            self._initialized = False
            return False

    async def restore_pending(self) -> None:
        """在 Gateway queue / Outbox 恢复后提交仍由飞书 Inbox 独占的事件。"""
        if not self._initialized:
            raise RuntimeError("feishu adapter is not initialized")
        await self._restore_pending_messages()
        self._pending_restored = True

    async def start_receiving(self) -> bool:
        """最后启动 Webhook Server，开放实时业务事件。"""
        if not self._initialized:
            return False
        if self._server_thread is not None and self._server_thread.is_alive():
            return True
        try:
            handler = self._make_webhook_handler()
            self._server = BoundedThreadingHTTPServer(
                (self.webhook_host, self.webhook_port),
                handler,
                max_concurrent_requests=self.webhook_max_concurrent_requests,
                reject_logger=self._log_webhook_overload,
            )
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="feishu-webhook",
                daemon=True,
            )
            self._running = True
            self._server_thread.start()

            # 端口为 0 时系统会自动分配端口,主要用于测试。
            actual_port = self._server.server_address[1]
            print(
                f"  [feishu] webhook listening on "
                f"http://{self.webhook_host}:{actual_port}{self.webhook_path}"
            )
            return True
        except Exception as exc:
            print(f"  [feishu] webhook start failed: {type(exc).__name__}")
            self._running = False
            if self._server:
                self._server.server_close()
            self._server = None
            self._server_thread = None
            return False

    async def connect(self) -> bool:
        """兼容旧接口，但只负责开放接收；初始化和恢复必须显式先完成。"""
        return await self.start_receiving()

    async def disconnect(self):
        self._running = False

        # 关闭时取消尚未提交的文本批次,避免 Adapter 停止后继续处理消息。
        for task in self._batch_tasks.values():
            task.cancel()
        self._batch_tasks.clear()
        self._batch_buffers.clear()
        self._batch_events.clear()
        self._batch_message_ids.clear()
        self._batch_sources.clear()
        self._batch_started_at.clear()
        for waiters in self._batch_waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(RuntimeError("feishu adapter stopped"))
        self._batch_waiters.clear()
        for waiter in self._inflight_messages.values():
            if not waiter.done():
                waiter.set_exception(RuntimeError("feishu adapter stopped"))
            # 主处理协程不等待此 Future,主动读取异常避免 asyncio 告警。
            waiter.exception()
        self._inflight_messages.clear()

        if self._server:
            server = self._server
            self._server = None
            try:
                await asyncio.to_thread(server.shutdown)
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

        if self._server_thread:
            self._server_thread.join(timeout=2.0)
            self._server_thread = None

        await self._close_http_client()
        self._close_reliability_store()
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._send_rate_locks.clear()
        self._send_timestamps.clear()
        self._send_rate_last_used.clear()
        self._send_rate_users.clear()
        self._send_rate_next_cleanup_at = 0.0
        self._send_rate_overflow_timestamps.clear()
        self._loop = None
        self._initialized = False
        self._pending_restored = False

    async def _close_http_client(self):
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    def _open_reliability_store(self) -> None:
        """在现有 Hermes 数据库中创建飞书入站可靠性表。"""
        from hermes.db import init_db

        self._reliability_db = init_db(self.db_path)
        self._reliability_db.executescript(
            """
            CREATE TABLE IF NOT EXISTS feishu_message_inbox (
                app_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at REAL NOT NULL,
                status TEXT NOT NULL,
                completed_at REAL,
                batch_message_id TEXT,
                PRIMARY KEY (app_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_feishu_inbox_completed
                ON feishu_message_inbox(app_id, completed_at);
            """
        )
        self._reliability_db.commit()
        self._prune_completed_messages(force=True)

    def _close_reliability_store(self) -> None:
        if self._reliability_db:
            self._reliability_db.close()
            self._reliability_db = None

    def _prune_completed_messages(self, *, force: bool = False) -> None:
        """按 TTL 清理已完成记录,未处理 inbox 永不自动删除。"""
        if not self._reliability_db:
            raise RuntimeError("feishu reliability store is unavailable")
        now = time.time()
        if (
            not force
            and now - self._last_dedup_cleanup
            < FEISHU_DEDUP_CLEANUP_INTERVAL_SECONDS
        ):
            return
        cutoff = now - FEISHU_DEDUP_TTL_SECONDS
        with self._reliability_db:
            self._reliability_db.execute(
                """
                DELETE FROM feishu_message_inbox
                WHERE app_id = ?
                  AND status != 'pending'
                  AND completed_at < ?
                """,
                (self.app_id, cutoff),
            )
        self._last_dedup_cleanup = now

    def _is_message_completed(self, message_id: str) -> bool:
        if not self._reliability_db:
            raise RuntimeError("feishu reliability store is unavailable")
        cutoff = time.time() - FEISHU_DEDUP_TTL_SECONDS
        row = self._reliability_db.execute(
            """
            SELECT 1
            FROM feishu_message_inbox
            WHERE app_id = ?
              AND message_id = ?
              AND status != 'pending'
              AND completed_at >= ?
            """,
            (self.app_id, message_id, cutoff),
        ).fetchone()
        return row is not None

    def _store_pending_message(self, message_id: str, payload: dict) -> None:
        """先持久化原始事件,成功交给 Runner 后再标记完成。"""
        if not self._reliability_db:
            raise RuntimeError("feishu reliability store is unavailable")
        self._prune_completed_messages()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._reliability_db:
            self._reliability_db.execute(
                """
                INSERT OR IGNORE INTO feishu_message_inbox
                    (app_id, message_id, payload, received_at, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (self.app_id, message_id, encoded, time.time()),
            )

    def _complete_messages(
        self,
        message_ids: list[str],
        *,
        status: str = "processed",
        batch_message_id: str | None = None,
    ) -> None:
        """原子标记整批消息完成,保留来源 ID 与拼接目标用于追踪。"""
        if not message_ids:
            return
        if not self._reliability_db:
            raise RuntimeError("feishu reliability store is unavailable")
        completed_at = time.time()
        with self._reliability_db:
            self._reliability_db.executemany(
                """
                UPDATE feishu_message_inbox
                SET status = ?, completed_at = ?, batch_message_id = ?
                WHERE app_id = ? AND message_id = ?
                """,
                [
                    (
                        status,
                        completed_at,
                        batch_message_id,
                        self.app_id,
                        message_id,
                    )
                    for message_id in message_ids
                ],
            )

    async def _restore_pending_messages(self) -> None:
        """启动时恢复上次未完成的飞书消息,按接收顺序重新提交。"""
        if not self._reliability_db:
            return
        rows = self._reliability_db.execute(
            """
            SELECT payload
            FROM feishu_message_inbox
            WHERE app_id = ? AND status = 'pending'
            ORDER BY received_at, message_id
            """,
            (self.app_id,),
        ).fetchall()
        tasks = []
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                print("  [feishu] invalid pending payload skipped")
                continue
            tasks.append(asyncio.create_task(self._handle_payload(payload)))
        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        failed = sum(isinstance(result, BaseException) for result in results)
        if failed:
            print(f"  [feishu] pending recovery failed: {failed}")
        else:
            print(f"  [feishu] restored pending messages: {len(tasks)}")

    # ===================== Webhook HTTP 服务 =====================

    def _make_webhook_handler(self) -> type[BaseHTTPRequestHandler]:
        adapter = self

        class FeishuWebhookHandler(BaseHTTPRequestHandler):
            """HTTP 线程只做有界解析，业务协程仍在 Gateway 事件循环运行。"""

            server_version = "HermesFeishuWebhook/0.2"
            sys_version = ""
            protocol_version = "HTTP/1.0"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(adapter.webhook_read_timeout_seconds)
                peer_ip = (
                    str(self.client_address[0])
                    if self.client_address
                    else "unknown"
                )
                self._client_ip = peer_ip
                self._request_body_bytes = 0
                self._request_event_type = ""
                self._response_reason = ""
                self._response_exception_type = ""
                self._header_deadline_lock = threading.Lock()
                self._headers_finished = False
                self._header_timeout_reached = False
                self._header_timer = threading.Timer(
                    adapter.webhook_read_timeout_seconds,
                    self._expire_header_read,
                )
                self._header_timer.daemon = True
                self._header_timer.start()

            def finish(self) -> None:
                self._headers_complete()
                try:
                    super().finish()
                except OSError:
                    pass

            def _expire_header_read(self) -> None:
                with self._header_deadline_lock:
                    if self._headers_finished:
                        return
                    self._header_timeout_reached = True
                adapter._log_webhook_result(
                    status=408,
                    client_ip=self._client_ip,
                    reason="header_timeout",
                )
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def _headers_complete(self) -> None:
                timer = getattr(self, "_header_timer", None)
                lock = getattr(self, "_header_deadline_lock", None)
                if lock is not None:
                    with lock:
                        self._headers_finished = True
                if timer is not None:
                    timer.cancel()

            def _path(self) -> str:
                try:
                    parsed = urlsplit(self.path)
                except ValueError:
                    return ""
                if (
                    parsed.scheme
                    or parsed.netloc
                    or parsed.query
                    or parsed.fragment
                ):
                    return ""
                return parsed.path

            def _resolve_client_ip(self) -> str:
                forwarded_values = self.headers.get_all("X-Forwarded-For") or []
                forwarded_for = (
                    forwarded_values[0]
                    if len(forwarded_values) == 1
                    else None
                )
                peer_ip = (
                    str(self.client_address[0])
                    if self.client_address
                    else "unknown"
                )
                self._client_ip = adapter._proxy_resolver.resolve(
                    peer_ip,
                    forwarded_for,
                )
                return self._client_ip

            def _send_json(
                self,
                status: int,
                payload: dict,
                *,
                reason: str = "",
            ) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._response_reason = reason
                self.close_connection = True
                try:
                    self.send_response(status)
                    self.send_header(
                        "Content-Type",
                        "application/json; charset=utf-8",
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Connection", "close")
                    if status == 429:
                        self.send_header("Retry-After", "1")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _send_head(self, status: int, *, reason: str = "") -> None:
                self._response_reason = reason
                self.close_connection = True
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _content_length(self) -> tuple[int | None, int | None]:
                if self.headers.get_all("Transfer-Encoding"):
                    return None, 400
                values = self.headers.get_all("Content-Length") or []
                if not values:
                    return None, 411
                if len(values) != 1:
                    return None, 400
                raw = values[0].strip()
                if not raw.isascii() or not raw.isdecimal():
                    return None, 400
                if len(raw) > 20:
                    return None, 413
                try:
                    length = int(raw, 10)
                except ValueError:
                    return None, 400
                if length < 0:
                    return None, 400
                if length > adapter.webhook_max_body_bytes:
                    return None, 413
                return length, None

            def _read_limited_body(self, length: int) -> bytes:
                deadline = (
                    time.monotonic()
                    + adapter.webhook_read_timeout_seconds
                )
                remaining = length
                chunks: list[bytes] = []
                total = 0
                read_cap = adapter.webhook_max_body_bytes + 1
                while remaining > 0:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        raise TimeoutError("webhook body read timed out")
                    self.connection.settimeout(timeout)
                    read_size = min(
                        FEISHU_WEBHOOK_READ_CHUNK_BYTES,
                        remaining,
                        read_cap - total,
                    )
                    if read_size <= 0:
                        break
                    chunk = self.rfile.read(read_size)
                    if not chunk:
                        raise EOFError("incomplete webhook body")
                    chunks.append(chunk)
                    total += len(chunk)
                    self._request_body_bytes = total
                    remaining -= len(chunk)
                    if total > adapter.webhook_max_body_bytes:
                        break
                self.connection.settimeout(
                    adapter.webhook_read_timeout_seconds
                )
                return b"".join(chunks)

            def _rate_limit_allowed(self) -> bool:
                client_ip = self._resolve_client_ip()
                return adapter._webhook_rate_limiter.allow(client_ip)

            def do_GET(self) -> None:
                self._headers_complete()
                if self._path() != "/healthz":
                    self._send_json(
                        404,
                        {"ok": False, "error": "not found"},
                        reason="path_not_found",
                    )
                    return
                self._send_json(200, {"ok": True, "channel": "feishu"})

            def do_HEAD(self) -> None:
                self._headers_complete()
                if self._path() != "/healthz":
                    self._send_head(404, reason="path_not_found")
                    return
                self._send_head(200)

            def do_POST(self) -> None:
                self._headers_complete()
                if self._path() != adapter.webhook_path:
                    self._send_json(
                        404,
                        {"ok": False, "error": "not found"},
                        reason="path_not_found",
                    )
                    return

                if not self._rate_limit_allowed():
                    self._send_json(
                        429,
                        {"ok": False, "error": "rate limited"},
                        reason="rate_limit",
                    )
                    return

                length, length_error = self._content_length()
                if length_error is not None:
                    messages = {
                        400: "invalid content length",
                        411: "content length required",
                        413: "request body too large",
                    }
                    self._send_json(
                        length_error,
                        {"ok": False, "error": messages[length_error]},
                        reason="content_length",
                    )
                    return
                assert length is not None

                try:
                    body = self._read_limited_body(length)
                except (TimeoutError, socket.timeout):
                    self.close_connection = True
                    self._send_json(
                        408,
                        {"ok": False, "error": "request timeout"},
                        reason="read_timeout",
                    )
                    return
                except EOFError:
                    self._send_json(
                        400,
                        {"ok": False, "error": "incomplete body"},
                        reason="incomplete_body",
                    )
                    return

                if len(body) > adapter.webhook_max_body_bytes:
                    self._send_json(
                        413,
                        {"ok": False, "error": "request body too large"},
                        reason="body_too_large",
                    )
                    return

                try:
                    payload = json.loads(body.decode("utf-8"))
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    RecursionError,
                ):
                    self._send_json(
                        400,
                        {"ok": False, "error": "invalid json"},
                        reason="invalid_json",
                    )
                    return

                if not isinstance(payload, dict):
                    self._send_json(
                        400,
                        {"ok": False, "error": "json object required"},
                        reason="json_object_required",
                    )
                    return

                token = adapter._extract_token(payload)
                if not adapter._token_allowed(token):
                    self._send_json(
                        403,
                        {"ok": False, "error": "forbidden"},
                        reason="token_verification",
                    )
                    return

                challenge = adapter._extract_challenge(payload)
                if challenge:
                    self._request_event_type = "url_verification"
                    self._send_json(200, {"challenge": challenge})
                    return

                if not adapter._app_allowed(payload):
                    self._send_json(
                        403,
                        {"ok": False, "error": "forbidden"},
                        reason="app_verification",
                    )
                    return

                event_type = adapter._extract_event_type(payload)
                self._request_event_type = event_type
                if not event_type:
                    self._send_json(
                        400,
                        {"ok": False, "error": "missing event_type"},
                        reason="missing_event_type",
                    )
                    return
                if event_type != "im.message.receive_v1":
                    # 合法但未订阅的事件返回 200，避免平台无意义重试。
                    self._send_json(200, {"ok": True, "ignored": True})
                    return

                # 只等待事件安全进入 Gateway，不等待模型最终回答。
                future = adapter._submit_payload(payload)
                if future is None:
                    self._send_json(
                        503,
                        {"ok": False, "error": "gateway unavailable"},
                        reason="gateway_unavailable",
                    )
                    return
                try:
                    future.result(
                        timeout=FEISHU_WEBHOOK_ACCEPT_TIMEOUT_SECONDS
                    )
                except FutureTimeoutError:
                    self._send_json(
                        503,
                        {"ok": False, "error": "gateway timeout"},
                        reason="gateway_timeout",
                    )
                    return
                except Exception as exc:
                    self._response_exception_type = type(exc).__name__
                    self._send_json(
                        500,
                        {"ok": False, "error": "processing failed"},
                        reason="processing_failed",
                    )
                    return

                self._send_json(200, {"ok": True})

            def _handle_unsupported_method(self) -> None:
                self._headers_complete()
                if self._path() not in {
                    adapter.webhook_path,
                    "/healthz",
                }:
                    self._send_json(
                        404,
                        {"ok": False, "error": "not found"},
                        reason="path_not_found",
                    )
                    return
                self._send_json(
                    405,
                    {"ok": False, "error": "method not allowed"},
                    reason="method_not_allowed",
                )

            do_DELETE = _handle_unsupported_method
            do_OPTIONS = _handle_unsupported_method
            do_PATCH = _handle_unsupported_method
            do_PUT = _handle_unsupported_method

            def send_error(
                self,
                code: int,
                message: str | None = None,
                explain: str | None = None,
            ) -> None:
                if code == 501:
                    self._handle_unsupported_method()
                    return
                super().send_error(code, message, explain)

            def log_request(
                self,
                code: int | str = "-",
                size: int | str = "-",
            ) -> None:
                try:
                    status = int(code)
                except (TypeError, ValueError):
                    status = 0
                adapter._log_webhook_result(
                    status=status,
                    client_ip=self._client_ip,
                    body_bytes=self._request_body_bytes,
                    event_type=self._request_event_type,
                    reason=self._response_reason,
                    exception_type=self._response_exception_type,
                )

            def log_error(self, fmt: str, *args: Any) -> None:
                if self._header_timeout_reached:
                    return
                if "timed out" in str(fmt).lower():
                    adapter._log_webhook_result(
                        status=408,
                        client_ip=self._client_ip,
                        body_bytes=self._request_body_bytes,
                        reason="header_timeout",
                    )
                    return
                adapter._log_webhook_result(
                    status=400,
                    client_ip=self._client_ip,
                    body_bytes=self._request_body_bytes,
                    reason="protocol_error",
                )

            def log_message(self, fmt: str, *args: Any) -> None:
                # 禁止 BaseHTTPRequestHandler 输出完整请求行或查询字符串。
                return

        return FeishuWebhookHandler

    @staticmethod
    def _extract_token(payload: dict) -> str:
        """按飞书明文事件结构提取 Verification Token。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            header = {}
        return str(
            payload.get("token")
            or header.get("token")
            or ""
        )

    @staticmethod
    def _extract_challenge(payload: dict) -> str:
        challenge = payload.get("challenge", "")
        if challenge:
            return str(challenge)
        event = payload.get("event", {})
        if isinstance(event, dict) and event.get("challenge"):
            return str(event["challenge"])
        return ""

    def _token_allowed(self, token: str) -> bool:
        """使用常量时间比较校验 Verification Token,缺失时默认拒绝。"""
        if not self.verification_token or not token:
            return False
        return hmac.compare_digest(
            token.encode("utf-8", errors="surrogatepass"),
            self.verification_token.encode("utf-8", errors="surrogatepass"),
        )

    def _app_allowed(self, payload: dict) -> bool:
        """校验 v2.0 事件属于当前飞书应用。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            return False
        event_app_id = str(header.get("app_id", "") or "")
        if not event_app_id:
            return False
        return hmac.compare_digest(
            event_app_id.encode("utf-8", errors="surrogatepass"),
            self.app_id.encode("utf-8", errors="surrogatepass"),
        )

    @staticmethod
    def _extract_event_type(payload: dict) -> str:
        """提取飞书 v2.0 事件类型。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            return ""
        return str(header.get("event_type", "") or "")

    def _submit_payload(self, payload: dict) -> Future | None:
        if not self._loop or not self._running:
            return None
        coroutine = self._handle_payload(payload)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            coroutine.close()
            return None

    # ===================== 消息入站 =====================

    async def _handle_payload(self, payload: dict) -> None:
        """飞书 webhook payload -> MessageEvent。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            header = {}
        event_type = self._extract_event_type(payload)
        if event_type != "im.message.receive_v1":
            return

        event = payload.get("event", {})
        if not isinstance(event, dict):
            return
        message = event.get("message", {})
        if not isinstance(message, dict):
            return
        sender = event.get("sender", {})
        if not isinstance(sender, dict):
            sender = {}

        # 单聊也可能收到机器人 / 应用侧消息。只接受真实用户消息,
        # 避免把机器人自己的回复再次送入会话形成自触发循环。
        sender_type = str(sender.get("sender_type", "") or "")
        if sender_type != "user":
            return

        sender_ids = sender.get("sender_id", {})
        if not isinstance(sender_ids, dict):
            sender_ids = {}

        msg_id = str(message.get("message_id", "") or "")
        chat_id = str(message.get("chat_id", "") or "")
        sender_id = str(
            sender_ids.get("open_id")
            or sender_ids.get("user_id")
            or ""
        )
        if not msg_id or not chat_id or not sender_id:
            return

        # 当前 Gateway 飞书链路严格只处理文本消息。
        msg_type = str(message.get("message_type") or message.get("msg_type") or "")
        if msg_type != "text":
            return
        text = self._parse_text(message)
        if not text.strip():
            return

        chat_type_raw = str(message.get("chat_type", "p2p") or "p2p")
        chat_type = "dm" if chat_type_raw == "p2p" else chat_type_raw

        # 单聊模式下忽略所有群聊和话题消息。
        if self.dm_only and chat_type != "dm":
            return

        mentioned_bot = self._bot_mentioned(message)
        if (
            chat_type in ("group", "topic")
            and self.require_mention
            and not mentioned_bot
        ):
            return

        if not self._is_allowed(sender_id, chat_id):
            return

        thread_id = message.get("thread_id") or message.get("root_id")
        event_obj = MessageEvent(
            message_id=msg_id,
            text=text,
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=self.PLATFORM,
                account_id=self.app_id,
                chat_id=chat_id,
                chat_type=chat_type,
                user_id=sender_id,
                user_id_alt=str(sender_ids.get("union_id", "") or ""),
                user_name=str(sender.get("sender_name", "") or ""),
                thread_id=str(thread_id) if thread_id else None,
            ),
            reply_to_message_id=message.get("parent_id"),
            metadata={
                "mentioned_bot": mentioned_bot,
                "mentioned_all": False,
                "raw_content_type": msg_type,
                "event_id": header.get("event_id", ""),
            },
        )

        # 同一消息在首个请求仍处理中被飞书重推时,共享首个请求的
        # 处理结果,不能因简单去重而提前向重推请求返回 200。
        inflight = self._inflight_messages.get(msg_id)
        if inflight:
            await asyncio.shield(inflight)
            return
        if self._is_message_completed(msg_id):
            return

        # Inbox 只负责尚未交给 Runner 的原始事件。数据库显示该消息已经
        # 进入 queue 或 Outbox 时，直接完成 Inbox，绝不再次提交模型任务。
        persisted = self.persisted_message_state(event_obj)
        if persisted is not None:
            self._complete_messages(
                [msg_id],
                status="processed",
                batch_message_id=msg_id,
            )
            print(
                "  [feishu] inbox already owned by gateway "
                f"(message_id={msg_id}, layer={persisted['layer']})"
            )
            return
        self._store_pending_message(msg_id, payload)

        accepted = asyncio.get_running_loop().create_future()
        self._inflight_messages[msg_id] = accepted
        try:
            if not self._on_message:
                raise RuntimeError("gateway runner is unavailable")

            command = text.strip().lower()
            if command in _IMMEDIATE_COMMANDS:
                batch_key = self._build_batch_key(event_obj)
                if command in ("/new", "/stop"):
                    # 新建或停止会话时取消尚未提交的普通文本,避免命令
                    # 执行后旧批次反而启动并进入错误的会话。
                    await self._discard_batch(batch_key)
                else:
                    # /status 前先提交旧批次,让状态包含刚发送的任务。
                    await self._flush_batch(batch_key)
                await self.handle_message(event_obj)
                self._complete_messages(
                    [msg_id],
                    batch_message_id=msg_id,
                )
            else:
                await self._enqueue_text(event_obj)
        except asyncio.CancelledError:
            if not accepted.done():
                accepted.cancel()
            raise
        except Exception as exc:
            if not accepted.done():
                accepted.set_exception(exc)
                # 没有并发重推等待时也要主动读取异常。
                accepted.exception()
            raise
        else:
            if not accepted.done():
                accepted.set_result(None)
        finally:
            if self._inflight_messages.get(msg_id) is accepted:
                self._inflight_messages.pop(msg_id, None)

    async def _enqueue_text(self, event: MessageEvent) -> None:
        """把同一来源短时间内的连续文本加入同一批次。"""
        batch_key = self._build_batch_key(event)
        now = time.monotonic()
        waiter = asyncio.get_running_loop().create_future()

        if batch_key not in self._batch_buffers:
            self._batch_buffers[batch_key] = []
            self._batch_message_ids[batch_key] = []
            self._batch_sources[batch_key] = []
            self._batch_waiters[batch_key] = []
            self._batch_started_at[batch_key] = now
        self._batch_buffers[batch_key].append(event.text)
        self._batch_message_ids[batch_key].append(event.message_id)
        self._batch_sources[batch_key].append({
            "message_id": event.message_id,
            "reply_to_message_id": event.reply_to_message_id,
            "event_id": event.metadata.get("event_id", ""),
        })
        self._batch_waiters[batch_key].append(waiter)
        # 使用最后一条消息的 message_id / reply 元数据回发。
        self._batch_events[batch_key] = event

        old_task = self._batch_tasks.get(batch_key)
        if old_task and not old_task.done():
            old_task.cancel()

        elapsed = now - self._batch_started_at[batch_key]
        remaining = max(0.0, FEISHU_BATCH_MAX_WAIT_SECONDS - elapsed)
        delay = min(FEISHU_BATCH_QUIET_SECONDS, remaining)
        task = asyncio.create_task(
            self._flush_batch_after(batch_key, delay)
        )
        task.add_done_callback(self._on_batch_task_done)
        self._batch_tasks[batch_key] = task
        await waiter

    async def _flush_batch_after(self, batch_key: str, delay: float) -> None:
        """等待静默窗口结束,合并并提交当前文本批次。"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # 只有当前最新计时任务可以提交,避免旧任务取消竞态重复发送。
        if self._batch_tasks.get(batch_key) is not asyncio.current_task():
            return

        await self._flush_batch(batch_key)

    def _pop_batch(self, batch_key: str) -> tuple:
        """原子取出一个批次,并取消对应的静默计时任务。"""
        task = self._batch_tasks.pop(batch_key, None)
        current_task = asyncio.current_task()
        if task and task is not current_task and not task.done():
            task.cancel()

        chunks = self._batch_buffers.pop(batch_key, [])
        event = self._batch_events.pop(batch_key, None)
        message_ids = self._batch_message_ids.pop(batch_key, [])
        sources = self._batch_sources.pop(batch_key, [])
        waiters = self._batch_waiters.pop(batch_key, [])
        self._batch_started_at.pop(batch_key, None)
        return chunks, event, message_ids, sources, waiters

    async def _flush_batch(self, batch_key: str) -> None:
        """立即合并并提交指定批次。"""
        chunks, event, message_ids, sources, waiters = self._pop_batch(batch_key)
        if not chunks or event is None:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)
            return

        event.text = FEISHU_BATCH_SEPARATOR.join(chunks)
        event.metadata["source_message_ids"] = message_ids
        event.metadata["source_messages"] = sources
        try:
            await self.handle_message(event)
            self._complete_messages(
                message_ids,
                batch_message_id=event.message_id,
            )
        except asyncio.CancelledError:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            raise
        except Exception as exc:
            # inbox 保持 pending,飞书重推或进程重启后会再次处理。
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(exc)
            raise
        else:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    async def _discard_batch(self, batch_key: str) -> None:
        """取消尚未提交的批次,用于 /new 和 /stop 保持命令语义。"""
        _, _, message_ids, _, waiters = self._pop_batch(batch_key)
        try:
            self._complete_messages(
                message_ids,
                status="cancelled",
                batch_message_id=message_ids[-1] if message_ids else None,
            )
        except Exception as exc:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(exc)
            raise
        else:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    @staticmethod
    def _on_batch_task_done(task: asyncio.Task) -> None:
        """读取后台拼接任务异常,避免错误被 asyncio 静默回收。"""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            print(f"  [feishu] batch processing failed: {type(exc).__name__}")

    @staticmethod
    def _build_batch_key(event: MessageEvent) -> str:
        """构建拼接隔离键,不同会话 / 用户 / 话题绝不混合。"""
        source = event.source
        return ":".join((
            source.platform,
            source.account_id,
            source.chat_type,
            source.chat_id,
            source.user_id,
            source.thread_id or "",
        ))

    @staticmethod
    def _parse_text(message: dict) -> str:
        raw = message.get("content", "{}")
        try:
            content = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return ""
        if not isinstance(content, dict):
            return ""
        return str(content.get("text", "") or "")

    def _bot_mentioned(self, message: dict) -> bool:
        mentions = message.get("mentions", [])
        if not isinstance(mentions, list):
            return False

        # 群聊 mention 必须精确匹配机器人 open_id。启动校验会阻止
        # require_mention 群聊模式在缺少 bot_open_id 时开放接收。
        if not self.bot_open_id:
            return False

        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("id", {})
            if (
                isinstance(mention_id, dict)
                and mention_id.get("open_id") == self.bot_open_id
            ):
                return True
            if isinstance(mention_id, str) and mention_id == self.bot_open_id:
                return True
        return False

    def _is_allowed(self, user_id: str, chat_id: str) -> bool:
        """白名单检查。"""
        if self.allow_all:
            return True
        if user_id and user_id in self.allowed_users:
            return True
        if chat_id and chat_id in self.allowed_chats:
            return True
        return False

    # ===================== 消息出站 =====================

    async def _refresh_token(self, *, force: bool = False) -> str:
        if (
            not force
            and self._tenant_token
            and time.time() < self._token_expires_at
        ):
            return self._tenant_token

        async with self._token_lock:
            if (
                not force
                and self._tenant_token
                and time.time() < self._token_expires_at
            ):
                return self._tenant_token
            if not self._http:
                return ""

            try:
                response = await self._http.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.app_id,
                        "app_secret": self.app_secret,
                    },
                )
                data = response.json()
            except Exception as exc:
                print(
                    "  [feishu] tenant token request failed "
                    f"({type(exc).__name__})"
                )
                return ""

            if data.get("code") != 0:
                print("  [feishu] tenant token rejected")
                return ""

            self._tenant_token = str(data.get("tenant_access_token", "") or "")
            expires = int(data.get("expire", 7200) or 7200)
            self._token_expires_at = time.time() + max(60, expires - 300)
            return self._tenant_token

    async def _invalidate_token(self, rejected_token: str) -> None:
        """只清除本次被拒绝的 token,避免覆盖并发刷新出的新 token。"""
        async with self._token_lock:
            if self._tenant_token == rejected_token:
                self._tenant_token = ""
                self._token_expires_at = 0.0

    def prepare_outbound(
        self,
        content: str,
        *,
        delivery_id: str,
    ) -> list[dict]:
        """把 Markdown 回复转换成可持久化的飞书富文本分片。"""
        chunks = self._split_markdown(content)
        payloads = []
        for index, chunk in enumerate(chunks):
            request_uuid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes:feishu:{delivery_id}:{index}",
            ))
            payloads.append({
                "msg_type": "post",
                "content": self._build_post_content(chunk),
                "request_uuid": request_uuid,
            })
        return payloads

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        if not self._http:
            return SendResult(
                success=False,
                error="adapter_unavailable",
                retryable=True,
            )

        payloads = self.prepare_outbound(
            content,
            delivery_id=str(uuid.uuid4()),
        )
        message_ids: list[str] = []
        for index, payload in enumerate(payloads):
            result = await self.send_prepared(
                chat_id,
                payload,
                reply_to_message_id=reply_to_message_id,
                thread_id=thread_id,
            )
            if not result.success:
                result.sent_chunks = index
                result.total_chunks = len(payloads)
                result.failed_chunk_index = index
                result.message_ids = message_ids
                return result
            if result.message_id:
                message_ids.append(result.message_id)
        return SendResult(
            success=True,
            message_id=message_ids[-1] if message_ids else None,
            sent_chunks=len(payloads),
            total_chunks=len(payloads),
            message_ids=message_ids,
        )

    async def send_prepared(
        self,
        chat_id: str,
        payload: dict,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        """发送一个已经确定格式和 UUID 的飞书消息分片。"""
        if not self._http:
            return SendResult(
                success=False,
                error="adapter_unavailable",
                retryable=True,
            )

        request_uuid = str(payload.get("request_uuid", "") or "")
        msg_type = str(payload.get("msg_type", "post") or "post")
        message_content = str(payload.get("content", "") or "")
        if reply_to_message_id and thread_id:
            delivery_mode = "thread_reply"
        elif reply_to_message_id:
            delivery_mode = "reply"
        else:
            delivery_mode = "direct"
        token_refreshed = False
        attempts_used = 0
        retry_after = None
        last_result = SendResult(
            success=False,
            error="internal_send_error",
            retryable=False,
        )

        while attempts_used < self.send_total_attempts:
            token = await self._refresh_token()
            if not token:
                # token 获取可能跨越较长故障窗口，不在 Adapter 内忙等，直接
                # 交给 Runner 记录 next_attempt_at 并持久化恢复。
                return SendResult(
                    success=False,
                    error="token_unavailable",
                    retryable=True,
                )

            try:
                await self._wait_send_slot(chat_id)
                if delivery_mode in {"thread_reply", "reply"}:
                    response = await self._http.post(
                        f"{self.api_base}/im/v1/messages/"
                        f"{reply_to_message_id}/reply",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "msg_type": msg_type,
                            "content": message_content,
                            "reply_in_thread": delivery_mode == "thread_reply",
                            "uuid": request_uuid,
                        },
                    )
                else:
                    response = await self._http.post(
                        f"{self.api_base}/im/v1/messages",
                        params={"receive_id_type": "chat_id"},
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "receive_id": chat_id,
                            "msg_type": msg_type,
                            "content": message_content,
                            "uuid": request_uuid,
                        },
                    )
                try:
                    data = response.json()
                except (TypeError, ValueError):
                    # 即使错误响应体不是 JSON,仍按 HTTP 状态分类，不记录
                    # 响应正文，避免平台响应携带敏感内容。
                    data = {}
                raw_code = data.get("code")
                code = self._normalize_error_code(raw_code)
                if code == 0:
                    message_id = (
                        data.get("data", {}).get("message_id")
                        if isinstance(data.get("data"), dict)
                        else None
                    )
                    return SendResult(
                        success=True,
                        message_id=message_id,
                        sent_chunks=1,
                        total_chunks=1,
                        message_ids=[message_id] if message_id else [],
                    )

                # 降级不是对同一故障做重试，因此不消耗瞬时重试次数。状态机
                # 只允许 thread_reply -> reply -> direct 单向前进，且全部复用
                # 当前分片已经持久化的 request UUID。
                if (
                    delivery_mode == "thread_reply"
                    and code in FEISHU_THREAD_REPLY_UNSUPPORTED_CODES
                ):
                    delivery_mode = "reply"
                    continue
                if (
                    delivery_mode in {"thread_reply", "reply"}
                    and code in FEISHU_REPLY_TARGET_MISSING_CODES
                ):
                    delivery_mode = "direct"
                    continue

                error, retryable, refresh_token = self._classify_send_error(
                    response.status_code,
                    code,
                )
                last_result = SendResult(
                    success=False,
                    error=error,
                    error_code=(
                        str(raw_code) if raw_code is not None else None
                    ),
                    retryable=retryable,
                )

                if refresh_token and not token_refreshed:
                    await self._invalidate_token(token)
                    token_refreshed = True
                    continue
                if not retryable:
                    return last_result
                (
                    retry_after,
                    defer_to_runner,
                    durable_retry_after,
                ) = self._bounded_retry_after(
                    response.headers,
                )
                last_result.retry_after_seconds = durable_retry_after
                attempts_used += 1
                if defer_to_runner:
                    return last_result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_result = self._classify_transport_exception(exc)
                print(f"  [feishu] send failed ({type(exc).__name__})")
                if not last_result.retryable:
                    return last_result
                retry_after = None
                attempts_used += 1

            if attempts_used < self.send_total_attempts:
                delay = self.send_retry_base_delay_seconds * (
                    2 ** max(0, attempts_used - 1)
                )
                if retry_after is not None:
                    delay = max(delay, retry_after)
                delay = min(self.send_retry_max_delay_seconds, delay)
                await asyncio.sleep(max(0.0, delay))

        return last_result

    def _cleanup_send_rate_cache(self, now: float, *, for_capacity: bool) -> None:
        """按 TTL 清理 chat 限速状态；容量不足时再淘汰空闲 LRU。"""
        if not for_capacity and now < self._send_rate_next_cleanup_at:
            return
        idle_ttl = self.send_rate_limit_cache_idle_ttl_seconds
        removable = [
            chat_key
            for chat_key, last_used in self._send_rate_last_used.items()
            if (
                self._send_rate_users.get(chat_key, 0) == 0
                and not self._send_rate_locks[chat_key].locked()
                and now - last_used >= idle_ttl
            )
        ]
        for chat_key in removable:
            self._remove_send_rate_key(chat_key)

        if for_capacity and len(self._send_rate_locks) >= (
            self.send_rate_limit_max_tracked_chats
        ):
            idle_keys = [
                chat_key
                for chat_key, lock in self._send_rate_locks.items()
                if self._send_rate_users.get(chat_key, 0) == 0
                and not lock.locked()
            ]
            if idle_keys:
                oldest = min(
                    idle_keys,
                    key=lambda key: self._send_rate_last_used.get(key, now),
                )
                self._remove_send_rate_key(oldest)

        self._send_rate_next_cleanup_at = now + min(60.0, idle_ttl)

    def _remove_send_rate_key(self, chat_key: str) -> None:
        self._send_rate_locks.pop(chat_key, None)
        self._send_timestamps.pop(chat_key, None)
        self._send_rate_last_used.pop(chat_key, None)
        self._send_rate_users.pop(chat_key, None)

    def _reserve_send_rate_state(
        self,
        chat_id: str,
        now: float,
    ) -> tuple[str | None, asyncio.Lock, deque[float]]:
        chat_key = str(chat_id)
        is_new = chat_key not in self._send_rate_locks
        self._cleanup_send_rate_cache(now, for_capacity=(
            is_new
            and len(self._send_rate_locks)
            >= self.send_rate_limit_max_tracked_chats
        ))
        if chat_key in self._send_rate_locks:
            self._send_rate_users[chat_key] = (
                self._send_rate_users.get(chat_key, 0) + 1
            )
            self._send_rate_last_used[chat_key] = now
            return (
                chat_key,
                self._send_rate_locks[chat_key],
                self._send_timestamps[chat_key],
            )

        if len(self._send_rate_locks) < self.send_rate_limit_max_tracked_chats:
            lock = asyncio.Lock()
            timestamps: deque[float] = deque()
            self._send_rate_locks[chat_key] = lock
            self._send_timestamps[chat_key] = timestamps
            self._send_rate_last_used[chat_key] = now
            self._send_rate_users[chat_key] = 1
            return chat_key, lock, timestamps

        # 所有缓存项都仍在使用时不强行删除 lock；未知 chat 共用一个有界
        # 溢出桶，代价只是暂时更保守地共享 QPS，而不是让字典继续增长。
        return (
            None,
            self._send_rate_overflow_lock,
            self._send_rate_overflow_timestamps,
        )

    async def _wait_send_slot(self, chat_id: str) -> None:
        """按会话限制发送速率,避免多路回复同时触发飞书限流。"""
        reserved_key, lock, timestamps = self._reserve_send_rate_state(
            chat_id,
            time.monotonic(),
        )
        try:
            async with lock:
                while True:
                    now = time.monotonic()
                    while timestamps and now - timestamps[0] >= 1.0:
                        timestamps.popleft()
                    if len(timestamps) < self.send_rate_limit_per_chat:
                        timestamps.append(now)
                        return
                    await asyncio.sleep(max(0.0, 1.0 - (now - timestamps[0])))
        finally:
            if reserved_key is not None:
                self._send_rate_users[reserved_key] = max(
                    0,
                    self._send_rate_users.get(reserved_key, 1) - 1,
                )
                self._send_rate_last_used[reserved_key] = time.monotonic()

    @staticmethod
    def _parse_retry_after(headers) -> float | None:
        value = headers.get("Retry-After") if headers else None
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return max(0.0, parsed)

    def _bounded_retry_after(
        self,
        headers,
    ) -> tuple[float | None, bool, float | None]:
        raw_delay = self._parse_retry_after(headers)
        if raw_delay is None:
            return None, False, None
        if raw_delay > self.adapter_retry_after_max_seconds:
            # 长 Retry-After 不在 Adapter 内等待；Runner 会以 durable
            # next_attempt_at 接管跨秒、跨分钟和跨重启的故障。
            return self.adapter_retry_after_max_seconds, True, raw_delay
        return raw_delay, False, raw_delay

    @staticmethod
    def _normalize_error_code(code: Any) -> int | None:
        try:
            return int(code)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classify_transport_exception(exc: Exception) -> SendResult:
        """只把明确的 httpx 传输异常归为可重试。"""
        try:
            import httpx
        except ImportError:
            return SendResult(
                success=False,
                error="internal_send_error",
                retryable=False,
            )
        if isinstance(exc, httpx.TimeoutException):
            return SendResult(
                success=False,
                error="send_timeout",
                retryable=True,
            )
        if isinstance(exc, (httpx.NetworkError, httpx.RemoteProtocolError)):
            return SendResult(
                success=False,
                error="network_error",
                retryable=True,
            )
        return SendResult(
            success=False,
            error="internal_send_error",
            retryable=False,
        )

    @staticmethod
    def _classify_send_error(
        status_code: int,
        code: Any,
    ) -> tuple[str, bool, bool]:
        """返回 ``错误类型、是否重试、是否刷新 tenant token``。"""
        normalized_code = FeishuAdapter._normalize_error_code(code)
        if (
            status_code == 429
            or normalized_code in FEISHU_RATE_LIMIT_ERROR_CODES
        ):
            return "rate_limited", True, False
        if status_code == 401 or normalized_code in FEISHU_TOKEN_ERROR_CODES:
            return "token_invalid", False, True
        if (
            status_code == 403
            or normalized_code in FEISHU_PERMISSION_ERROR_CODES
        ):
            return "permission_denied", False, False
        if normalized_code in FEISHU_REPLY_TARGET_MISSING_CODES:
            return "reply_target_missing", False, False
        if status_code == 408:
            return "send_timeout", True, False
        if normalized_code in FEISHU_TRANSIENT_SEND_ERROR_CODES:
            return "server_error", True, False
        if status_code >= 500:
            return "server_error", True, False
        if 400 <= status_code < 500:
            return "invalid_request", False, False
        if 200 <= status_code < 300:
            return "invalid_request", False, False
        return "internal_send_error", False, False

    @staticmethod
    def _build_post_content(text: str) -> str:
        content = {
            "zh_cn": {
                "content": [[{
                    "tag": "md",
                    "text": text,
                }]],
            },
        }
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _post_payload_size(cls, text: str) -> int:
        """估算完整回复请求体大小,为外层路由字段预留固定空间。"""
        body = {
            "msg_type": "post",
            "content": cls._build_post_content(text),
            "reply_in_thread": True,
            "uuid": "00000000-0000-0000-0000-000000000000",
        }
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return len(encoded) + FEISHU_POST_SAFETY_MARGIN_BYTES

    @classmethod
    def _split_markdown(cls, text: str) -> list[str]:
        """按富文本请求体字节数分片,优先保持段落和行边界。"""
        if cls._post_payload_size(text) <= FEISHU_POST_LIMIT_BYTES:
            return [text]

        chunks: list[str] = []
        remaining = text
        while remaining:
            if cls._post_payload_size(remaining) <= FEISHU_POST_LIMIT_BYTES:
                chunks.append(remaining)
                break

            low, high = 1, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if (
                    cls._post_payload_size(remaining[:middle])
                    <= FEISHU_POST_LIMIT_BYTES
                ):
                    low = middle
                else:
                    high = middle - 1

            split_at = low
            prefix = remaining[:split_at]
            minimum_boundary = max(1, split_at // 2)
            for separator in ("\n\n", "\n", " "):
                boundary = prefix.rfind(separator)
                if boundary >= minimum_boundary:
                    split_at = boundary + len(separator)
                    break

            # 即使单个字符也无法满足限制,也必须前进以避免死循环。
            split_at = max(1, split_at)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        return chunks or [text]
