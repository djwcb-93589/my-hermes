"""
飞书 adapter:基于 Webhook HTTP 回调,接收入站消息并发送 Markdown 富文本。

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
  - 文件传输开启时把资源标识解析成平台无关附件
  - 提供复用现有鉴权和 HTTP 客户端的安全流式资源下载能力
  - 在持久 Inbox route consumer 内物化附件，再交给 GatewayRunner
  - 把审批后的本地文件流式上传为普通 file，并交给持久 Outbox 发送

不实现:加密事件解密、内容识别、卡片、流式回复。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import random
import socket
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from hermes.db import (
    InvalidFeishuInboxPayloadError,
    build_feishu_inbox_route_key,
    claim_feishu_inbox_route_message,
    fail_feishu_inbox_message,
    get_feishu_inbox_dispatch_routes,
    get_feishu_inbox_payload,
    get_feishu_inbox_route_next,
    get_feishu_inbox_status,
    insert_feishu_inbox_message,
    prune_feishu_inbox_messages,
    release_feishu_inbox_processing_message,
    reset_feishu_inbox_processing,
    update_feishu_inbox_status,
)
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.adapters.feishu_files import (
    FeishuFileUploadResult,
    FeishuResourceDownloadResult,
    download_feishu_message_resource,
    upload_feishu_file,
)
from hermes.gateway.adapters.webhook_security import (
    BoundedThreadingHTTPServer,
    SlidingWindowRateLimiter,
    TrustedProxyResolver,
    redact_ip,
)
from hermes.gateway.file_transfer import GatewayFileTransferConfig
from hermes.gateway.files.cache import (
    CacheCleanupResult,
    cleanup_expired_cache,
)
from hermes.gateway.observability import (
    safe_message_digest,
    safe_route_digest,
)
from hermes.gateway.persistence import GatewayPersistence
from hermes.gateway.types import (
    Attachment,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
    validate_attachment,
)


FEISHU_POST_LIMIT_BYTES = 30 * 1024  # 飞书单条富文本请求体上限
FEISHU_POST_SAFETY_MARGIN_BYTES = 1024  # 为 receive_id 等外层字段预留空间
FEISHU_TOKEN_ERROR_CODES = frozenset({99991663, 99991665})
# 飞书回复 API 的降级条件必须与普通权限错误分开。只有目标消息或话题
# 本身不可用于回复时，才允许改成向 chat 直接发送；通用权限错误不能降级。
FEISHU_THREAD_REPLY_UNSUPPORTED_CODES = frozenset({230071, 230072})
# 以下错误码统一按“回复目标已失效或不可用”处理。平台在不同消息场景下
# 可能给出不同细分原因，因此不在这里写容易过时的过度具体解释。
FEISHU_REPLY_TARGET_MISSING_CODES = frozenset({
    230011,
    230019,
    230050,
    230054,
    230110,
    230111,
    231003,
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
# Token 接口只对明确的限流或平台内部错误开放重试；未知业务码默认拒绝。
FEISHU_TOKEN_REJECTED_ERROR_CODES = frozenset({
    10005,     # 应用鉴权信息无效
    10014,     # 应用状态不可用
    10015,     # App Secret 错误
    20002,     # app_id 与 app_secret 不匹配
    20009,     # 租户未安装应用
    20025,     # 缺少 app_id 或 app_secret
    20028,     # app_id 无效
    99991662,  # 应用已停用
    99991672,  # 应用缺少所需权限
})
FEISHU_TOKEN_TRANSIENT_ERROR_CODES = frozenset({
    1500,
    1503,
    1551,
    1557,
    4006,
    5000,
    10101,
    10105,
    20050,
    95001,
    96001,
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
FEISHU_DEDUP_CLEANUP_BATCH_SIZE = 200
FEISHU_READINESS_TIMEOUT_SECONDS = 2.0
FEISHU_PROCESSING_REACTION_CACHE_SIZE = 2048
FEISHU_FILE_DOWNLOAD_CONCURRENCY = 2
FEISHU_ATTACHMENT_PLACEHOLDER = "[Feishu attachment]"
FEISHU_ATTACHMENT_MESSAGE_TYPES = frozenset({
    "image",
    "file",
    "audio",
    "media",
})
FEISHU_SUPPORTED_INBOUND_MESSAGE_TYPES = (
    FEISHU_ATTACHMENT_MESSAGE_TYPES | {"text"}
)
FEISHU_REACTION_ALREADY_GONE_CODES = frozenset({
    230110,  # 原消息已经删除
    231003,  # 原消息不存在或已经撤回
    231004,  # 原会话不存在
    231005,  # 原话题已经删除
    231010,  # reaction 已不属于该消息
    231011,  # reaction_id 已无法定位
})
_IMMEDIATE_COMMANDS = frozenset({
    "/new",
    "/stop",
    "/status",
    "/sessions",
    "/resume",
})


def _immediate_command_name(text: str) -> str | None:
    """保留旧命令精确匹配，仅 /resume 接受后续参数。"""
    normalized = str(text or "").strip().lower()
    if normalized in _IMMEDIATE_COMMANDS - {"/resume"}:
        return normalized
    parts = normalized.split(maxsplit=1)
    if parts and parts[0] == "/resume":
        return "/resume"
    return None


@dataclass(frozen=True)
class TokenResult:
    """tenant access token 获取结果，不用空字符串承载错误语义。"""

    success: bool
    token: str = ""
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class InboxFailureDisposition:
    """Inbox 异常分类结果，只持久化安全分类码而不保存原始敏感文本。"""

    code: str
    permanent: bool = False


class FeishuInboxRetryableError(RuntimeError):
    """Inbox 消费链路中的明确瞬时错误。"""


class FeishuRunnerUnavailableError(FeishuInboxRetryableError):
    """Runner 回调尚未注入或暂时不可使用。"""


class FeishuGatewayLifecycleError(FeishuInboxRetryableError):
    """Gateway 正在启动、停止或已经失去运行资格。"""


class FeishuInboxBusinessDataError(ValueError):
    """已落库但无法解析为业务事件的永久数据错误。"""


class FeishuAttachmentDownloadError(RuntimeError):
    """把附件下载结果安全地传递给现有 Inbox 失败状态机。"""

    def __init__(
        self,
        error_code: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ):
        safe_code = "".join(
            char
            for char in str(error_code or "").strip().lower()
            if char.isalnum() or char == "_"
        )[:64]
        self.error_code = (
            f"attachment_download_{safe_code}"
            if safe_code
            else "attachment_download_failed"
        )
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self.error_code)


class FeishuAdapter(BasePlatformAdapter):
    """飞书 Webhook 消息 adapter。"""

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
        group_authorization_mode: str = "and",
        inbox_retry_max_attempts: int = 5,
        inbox_retry_base_delay_seconds: float = 1.0,
        inbox_retry_max_delay_seconds: float = 60.0,
        inbox_retry_jitter_ratio: float = 0.2,
        inbox_retry_poll_interval_seconds: float = 1.0,
        inbox_retry_batch_size: int = 64,
        inbox_retention_seconds: float = FEISHU_DEDUP_TTL_SECONDS,
        retention_cleanup_interval_seconds: float = (
            FEISHU_DEDUP_CLEANUP_INTERVAL_SECONDS
        ),
        retention_cleanup_batch_size: int = FEISHU_DEDUP_CLEANUP_BATCH_SIZE,
        send_total_attempts: int | None = None,
        send_max_retries: int | None = None,
        send_retry_base_delay_seconds: float | None = None,
        send_retry_base_delay: float | None = None,
        send_retry_max_delay_seconds: float = 3.0,
        adapter_retry_after_max_seconds: float = 5.0,
        send_rate_limit_per_chat: int = 5,
        send_rate_limit_cache_idle_ttl_seconds: float = 600.0,
        send_rate_limit_max_tracked_chats: int = 1024,
        file_transfer_config: GatewayFileTransferConfig | None = None,
    ):
        super().__init__("feishu")
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "")
        self.db_path = db_path
        # 配置由 GatewayRunner 在启动前集中校验；Adapter 只保存，不读取文件。
        self.file_transfer_config = file_transfer_config
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
        self.group_authorization_mode = str(
            group_authorization_mode or ""
        ).strip().lower()
        if self.group_authorization_mode not in {"and", "or"}:
            raise ValueError(
                "group_authorization_mode must be 'and' or 'or'"
            )
        self.inbox_retry_max_attempts = self._positive_int(
            "inbox_retry_max_attempts",
            inbox_retry_max_attempts,
        )
        self.inbox_retry_base_delay_seconds = self._nonnegative_float(
            "inbox_retry_base_delay_seconds",
            inbox_retry_base_delay_seconds,
        )
        self.inbox_retry_max_delay_seconds = max(
            self.inbox_retry_base_delay_seconds,
            self._nonnegative_float(
                "inbox_retry_max_delay_seconds",
                inbox_retry_max_delay_seconds,
            ),
        )
        self.inbox_retry_jitter_ratio = min(
            1.0,
            self._nonnegative_float(
                "inbox_retry_jitter_ratio",
                inbox_retry_jitter_ratio,
            ),
        )
        self.inbox_retry_poll_interval_seconds = self._positive_float(
            "inbox_retry_poll_interval_seconds",
            inbox_retry_poll_interval_seconds,
        )
        self.inbox_retry_batch_size = self._positive_int(
            "inbox_retry_batch_size",
            inbox_retry_batch_size,
        )
        self.inbox_retention_seconds = self._positive_float(
            "inbox_retention_seconds",
            inbox_retention_seconds,
        )
        self.retention_cleanup_interval_seconds = self._positive_float(
            "retention_cleanup_interval_seconds",
            retention_cleanup_interval_seconds,
        )
        self.retention_cleanup_batch_size = self._positive_int(
            "retention_cleanup_batch_size",
            retention_cleanup_batch_size,
        )
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
        self._owns_persistence = False
        self._last_dedup_cleanup = 0.0
        self._route_tasks: dict[str, asyncio.Task] = {}
        self._route_wakeups: dict[str, asyncio.Event] = {}
        # route task 保持会话内串行；该信号量只限制跨 route 的下载并发。
        self._file_download_semaphore = asyncio.Semaphore(
            FEISHU_FILE_DOWNLOAD_CONCURRENCY,
        )
        self._inbox_dispatcher_task: asyncio.Task | None = None
        self._inbox_dispatch_wakeup: asyncio.Event | None = None
        self._inbox_deferred_failures: dict[
            str,
            tuple[str, float | None, bool, str, int, str],
        ] = {}
        self._send_rate_locks: dict[str, asyncio.Lock] = {}
        self._send_timestamps: dict[str, deque[float]] = {}
        self._send_rate_last_used: dict[str, float] = {}
        self._send_rate_users: dict[str, int] = {}
        self._send_rate_next_cleanup_at = 0.0
        # 缓存满且所有 chat 都在使用时，新 chat 共用保守的溢出限速桶，
        # 从而不突破 key 上限，也不删除仍有 waiter 的 lock。
        self._send_rate_overflow_lock = asyncio.Lock()
        self._send_rate_overflow_timestamps: deque[float] = deque()
        # reaction 仅作为进程内用户体验状态，不进入 Inbox 或 Outbox。
        self._processing_reactions: OrderedDict[str, str] = OrderedDict()
        self._processing_attempts: OrderedDict[str, None] = OrderedDict()
        self._processing_outcomes: OrderedDict[str, str] = OrderedDict()
        self._processing_reaction_lock = asyncio.Lock()
        self._initialized = False
        self._pending_restored = False
        self._stopping = False

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
            or raw in {"/healthz", "/livez", "/readyz"}
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

    def readiness_snapshot(self) -> dict[str, bool]:
        """返回 Feishu 本地恢复、监听和 durable dispatcher 状态。"""
        dispatcher = self._inbox_dispatcher_task
        server_thread = self._server_thread
        return {
            "adapter_initialized": bool(self._initialized),
            "inbox_restored": bool(self._pending_restored),
            "adapter_receiving": bool(
                self._running
                and not self._stopping
                and self._server is not None
                and server_thread is not None
                and server_thread.is_alive()
            ),
            "durable_dispatcher": bool(
                dispatcher is not None and not dispatcher.done()
            ),
        }

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
            await self._open_reliability_store()
            self._loop = asyncio.get_running_loop()
            self._inbox_dispatch_wakeup = asyncio.Event()
            self._inbox_deferred_failures.clear()
            self._stopping = False
            self._http = httpx.AsyncClient(timeout=15.0)
            self._initialized = True
            self._pending_restored = False
            self._ensure_webhook_server()
            return True
        except Exception as exc:
            print(f"  [feishu] initialization failed: {type(exc).__name__}")
            self._running = False
            if self._server:
                try:
                    self._server.server_close()
                except Exception:
                    pass
            self._server = None
            self._server_thread = None
            await self._close_http_client()
            await self._close_reliability_store()
            self._loop = None
            self._inbox_dispatch_wakeup = None
            self._initialized = False
            return False

    async def restore_pending(self) -> None:
        """在 Gateway queue / Outbox 恢复后提交仍由飞书 Inbox 独占的事件。"""
        if not self._initialized:
            raise RuntimeError("feishu adapter is not initialized")
        if self._pending_restored:
            self._start_inbox_dispatcher()
            self._wake_inbox_dispatcher()
            return
        try:
            await self._restore_pending_messages()
        except Exception:
            self._pending_restored = False
            task = self._inbox_dispatcher_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._inbox_dispatcher_task = None
            raise

    async def start_receiving(self) -> bool:
        """恢复完成后开放实时业务事件；健康 HTTP 监听已提前启动。"""
        if not self._initialized or not self._pending_restored:
            return False
        try:
            self._ensure_webhook_server()
            self._running = True
            return True
        except Exception as exc:
            print(f"  [feishu] webhook start failed: {type(exc).__name__}")
            self._running = False
            if self._server:
                self._server.server_close()
            self._server = None
            self._server_thread = None
            return False

    def _ensure_webhook_server(self) -> None:
        """启动 HTTP 监听；业务 POST 是否开放仍由 ``_running`` 控制。"""
        if self._server_thread is not None and self._server_thread.is_alive():
            return
        handler = self._make_webhook_handler()
        server = BoundedThreadingHTTPServer(
            (self.webhook_host, self.webhook_port),
            handler,
            max_concurrent_requests=self.webhook_max_concurrent_requests,
            reject_logger=self._log_webhook_overload,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="feishu-webhook",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        self._server_thread = thread

        # 端口为 0 时系统会自动分配端口，主要用于测试。
        actual_port = server.server_address[1]
        print(
            f"  [feishu] HTTP listening on "
            f"http://{self.webhook_host}:{actual_port} "
            f"(webhook={self.webhook_path})"
        )

    async def connect(self) -> bool:
        """兼容旧接口，但只负责开放接收；初始化和恢复必须显式先完成。"""
        return await self.start_receiving()

    async def disconnect(self):
        self._stopping = True
        self._running = False
        self._pending_restored = False
        self._wake_inbox_dispatcher()

        # 关闭时统一取消 dispatcher 和 route consumer，随后完整等待收尾。
        managed_tasks = list({
            *(
                [self._inbox_dispatcher_task]
                if self._inbox_dispatcher_task is not None
                else []
            ),
            *self._route_tasks.values(),
        })
        for task in managed_tasks:
            task.cancel()
        if managed_tasks:
            await asyncio.gather(*managed_tasks, return_exceptions=True)
        self._inbox_dispatcher_task = None
        self._route_tasks.clear()
        self._route_wakeups.clear()
        await self._flush_deferred_inbox_failures()
        self._inbox_deferred_failures.clear()
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

        await self._clear_processing_reactions()
        await self._close_http_client()
        await self._close_reliability_store()
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._send_rate_locks.clear()
        self._send_timestamps.clear()
        self._send_rate_last_used.clear()
        self._send_rate_users.clear()
        self._send_rate_next_cleanup_at = 0.0
        self._send_rate_overflow_timestamps.clear()
        self._processing_reactions.clear()
        self._processing_attempts.clear()
        self._processing_outcomes.clear()
        self._loop = None
        self._inbox_dispatch_wakeup = None
        self._initialized = False
        self._pending_restored = False

    async def _close_http_client(self):
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def _open_reliability_store(self) -> None:
        """准备异步可靠性存储，不在事件循环中创建 SQLite 连接。"""
        if self._persistence is None or self._persistence.closed:
            self._persistence = GatewayPersistence(self.db_path)
            self._owns_persistence = True
        await self._prune_completed_messages(force=True)

    async def _close_reliability_store(self) -> None:
        if self._owns_persistence and self._persistence is not None:
            await self._persistence.close()
            self._persistence = None
            self._owns_persistence = False

    def _require_persistence(self) -> GatewayPersistence:
        persistence = self._persistence
        if persistence is None or persistence.closed:
            raise RuntimeError("feishu reliability store is unavailable")
        return persistence

    async def _prune_completed_messages(self, *, force: bool = False) -> None:
        """按保留期分批清理终态 Inbox；失败只记录并等待下轮。"""
        persistence = self._require_persistence()
        now = time.time()
        if (
            not force
            and now - self._last_dedup_cleanup
            < self.retention_cleanup_interval_seconds
        ):
            return
        cutoff = now - self.inbox_retention_seconds
        removed = 0
        try:
            for _ in range(4):
                batch_removed = await persistence.call(
                    prune_feishu_inbox_messages,
                    self.app_id,
                    completed_before=cutoff,
                    limit=self.retention_cleanup_batch_size,
                )
                removed += int(batch_removed)
                if int(batch_removed) < self.retention_cleanup_batch_size:
                    break
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "  [feishu:audit] event=retention_cleanup_failed "
                f"kind=inbox exception={type(exc).__name__}"
            )
        else:
            if removed:
                print(
                    "  [feishu:audit] event=retention_cleanup "
                    f"kind=inbox removed={removed}"
                )
        # 失败也遵守清理间隔，避免锁故障期间每条 Webhook 都触发写重试。
        self._last_dedup_cleanup = now

    async def _message_inbox_status(self, message_id: str) -> str | None:
        """读取 Inbox 状态；调用方据此区分终态与可恢复 pending。"""
        return await self._require_persistence().call(
            get_feishu_inbox_status,
            self.app_id,
            message_id,
        )

    async def _store_pending_message(
        self,
        message_id: str,
        route_key: str,
        payload: dict,
    ) -> None:
        """先持久化原始事件,成功交给 Runner 后再标记完成。"""
        persistence = self._require_persistence()
        await self._prune_completed_messages()
        await persistence.call(
            insert_feishu_inbox_message,
            self.app_id,
            message_id,
            payload,
            route_key=route_key,
        )

    async def _complete_messages(
        self,
        message_ids: list[str],
        *,
        status: str = "processed",
        batch_message_id: str | None = None,
    ) -> None:
        """原子标记整批消息完成,保留来源 ID 与拼接目标用于追踪。"""
        if not message_ids:
            return
        completed_at = time.time()
        await self._require_persistence().call(
            update_feishu_inbox_status,
            self.app_id,
            message_ids,
            status,
            completed_at=completed_at,
            batch_message_id=batch_message_id,
            updated_at=completed_at,
            expected_statuses=("pending", "processing"),
        )

    async def _restore_pending_messages(self) -> None:
        """收敛异常退出状态并启动受管理的 Inbox dispatcher。"""
        persistence = self._require_persistence()
        recovered = await persistence.call(
            reset_feishu_inbox_processing,
            self.app_id,
        )
        candidates = await persistence.call(
            get_feishu_inbox_dispatch_routes,
            self.app_id,
            limit=self.inbox_retry_batch_size,
        )
        self._pending_restored = True
        self._start_inbox_dispatcher()
        self._wake_inbox_dispatcher()
        # 仅让 dispatcher 完成首轮 route 注册，不等待静默批次或 Runner handoff。
        await asyncio.sleep(0)
        if recovered:
            print(f"  [feishu] recovered processing messages: {recovered}")
        if candidates:
            print(f"  [feishu] inbox dispatch routes: {len(candidates)}")

    def _start_inbox_dispatcher(self) -> None:
        """确保当前 Adapter 只有一个受管理 dispatcher Task。"""
        task = self._inbox_dispatcher_task
        if task is not None and not task.done():
            return
        if self._inbox_dispatch_wakeup is None:
            raise RuntimeError("feishu inbox dispatcher is unavailable")
        task = asyncio.create_task(
            self._run_inbox_dispatcher(),
            name="feishu-inbox-dispatcher",
        )
        self._inbox_dispatcher_task = task
        task.add_done_callback(self._on_inbox_dispatcher_done)

    def _wake_inbox_dispatcher(self, route_key: str | None = None) -> None:
        """显式唤醒 dispatcher 和指定 route consumer。"""
        if self._inbox_dispatch_wakeup is not None:
            self._inbox_dispatch_wakeup.set()
        if route_key:
            route_wakeup = self._route_wakeups.get(route_key)
            if route_wakeup is not None:
                route_wakeup.set()

    async def _run_inbox_dispatcher(self) -> None:
        """持续注册当前可执行路由，并受显式唤醒和短轮询共同驱动。"""
        while self._initialized and self._pending_restored and not self._stopping:
            wakeup = self._inbox_dispatch_wakeup
            if wakeup is None:
                return
            wakeup.clear()
            dispatched = 0
            try:
                await self._prune_completed_messages()
                await self._flush_deferred_inbox_failures()
                dispatched = await self._dispatch_inbox_candidates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "  [feishu] inbox dispatcher deferred: "
                    f"{type(exc).__name__}"
                )

            if dispatched >= self.inbox_retry_batch_size:
                # 有界批量之间主动让出事件循环，避免大量恢复记录饿死 ACK。
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    wakeup.wait(),
                    timeout=self.inbox_retry_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _dispatch_inbox_candidates(self) -> int:
        """读取一批可执行 route 队首，每个 route 只注册一个 consumer。"""
        active_tasks = sum(
            1 for task in self._route_tasks.values() if not task.done()
        )
        available_slots = max(0, self.inbox_retry_batch_size - active_tasks)
        if available_slots <= 0:
            return 0
        route_keys = await self._require_persistence().call(
            get_feishu_inbox_dispatch_routes,
            self.app_id,
            limit=available_slots,
        )
        dispatched = 0
        for route_key in route_keys:
            if self._register_route_consumer(route_key):
                dispatched += 1
        return dispatched

    async def _flush_deferred_inbox_failures(self) -> int:
        """重试此前因 SQLite 瞬时错误未能落库的失败状态。"""
        if self._persistence is None or self._persistence.closed:
            return 0
        persisted = 0
        for message_id, failure in list(
            self._inbox_deferred_failures.items()
        ):
            (
                last_error,
                next_attempt_at,
                permanent,
                route_key,
                attempt_number,
                failure_type,
            ) = failure
            try:
                result = await self._persistence.call(
                    fail_feishu_inbox_message,
                    self.app_id,
                    message_id,
                    last_error=last_error,
                    next_attempt_at=next_attempt_at,
                    max_attempts=self.inbox_retry_max_attempts,
                    permanent=permanent,
                )
            except Exception:
                continue
            self._inbox_deferred_failures.pop(message_id, None)
            if result is not None:
                self._log_inbox_failure_state(
                    route_key=route_key,
                    message_id=message_id,
                    attempt_count=int(result["attempt_count"]),
                    status=str(result["status"]),
                    failure_type=failure_type,
                    next_attempt_at=next_attempt_at,
                )
            persisted += 1
        return persisted

    @staticmethod
    def _is_retryable_sqlite_failure(exc: sqlite3.OperationalError) -> bool:
        """识别锁竞争、I/O 中断等可能自行恢复的 SQLite 错误。"""
        error_code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(error_code, int):
            primary_code = error_code & 0xFF
            retryable_codes = {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
                sqlite3.SQLITE_IOERR,
                sqlite3.SQLITE_INTERRUPT,
                sqlite3.SQLITE_PROTOCOL,
                sqlite3.SQLITE_CANTOPEN,
            }
            if primary_code in retryable_codes:
                return True
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "database is locked",
                "database table is locked",
                "database is busy",
                "disk i/o error",
                "temporarily unavailable",
            )
        )

    @classmethod
    def _classify_inbox_failure(cls, exc: BaseException) -> InboxFailureDisposition:
        """把消费异常分成可重试与永久失败，不持久化异常原文。"""
        if isinstance(exc, FeishuAttachmentDownloadError):
            return InboxFailureDisposition(
                exc.error_code,
                permanent=not exc.retryable,
            )
        if isinstance(exc, InvalidFeishuInboxPayloadError):
            return InboxFailureDisposition("invalid_payload", permanent=True)
        if isinstance(exc, FeishuInboxBusinessDataError):
            return InboxFailureDisposition("invalid_business_data", permanent=True)
        if isinstance(exc, FeishuRunnerUnavailableError):
            return InboxFailureDisposition("runner_unavailable")
        if isinstance(exc, FeishuGatewayLifecycleError):
            return InboxFailureDisposition("gateway_lifecycle")
        if isinstance(exc, FeishuInboxRetryableError):
            return InboxFailureDisposition("retryable_processing")
        if isinstance(exc, sqlite3.OperationalError):
            if cls._is_retryable_sqlite_failure(exc):
                return InboxFailureDisposition("sqlite_transient")
            return InboxFailureDisposition("sqlite_persistence", permanent=True)
        if isinstance(exc, sqlite3.DatabaseError):
            return InboxFailureDisposition("sqlite_persistence", permanent=True)
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            "gateway is not accepting messages during "
        ):
            return InboxFailureDisposition("gateway_lifecycle")
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return InboxFailureDisposition("transient_io")
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return InboxFailureDisposition("invalid_business_data", permanent=True)
        return InboxFailureDisposition("unexpected_processing")

    def _inbox_retry_delay(self, attempt_number: int) -> float:
        """计算带对称小幅 jitter 的有上限指数退避。"""
        exponent = max(0, min(30, int(attempt_number) - 1))
        delay = min(
            self.inbox_retry_max_delay_seconds,
            self.inbox_retry_base_delay_seconds * (2 ** exponent),
        )
        jitter = delay * self.inbox_retry_jitter_ratio
        if jitter <= 0:
            return delay
        return max(0.0, delay + random.uniform(-jitter, jitter))

    async def _record_inbox_failure(
        self,
        route_key: str,
        message_id: str,
        attempt_count: int,
        exc: BaseException,
    ) -> None:
        """持久化脱敏失败信息，并在需要时显式唤醒 dispatcher。"""
        if self._persistence is None or self._persistence.closed:
            return
        disposition = self._classify_inbox_failure(exc)
        attempt_number = int(attempt_count) + 1
        last_error = f"{disposition.code}:{type(exc).__name__}"[:256]
        now = time.time()
        next_attempt_at = None
        if (
            not disposition.permanent
            and attempt_number < self.inbox_retry_max_attempts
        ):
            retry_delay = self._inbox_retry_delay(attempt_number)
            if isinstance(exc, FeishuAttachmentDownloadError):
                suggested_delay = exc.retry_after_seconds
                if (
                    suggested_delay is not None
                    and math.isfinite(suggested_delay)
                    and suggested_delay >= 0
                ):
                    retry_delay = max(
                        retry_delay,
                        min(
                            self.inbox_retry_max_delay_seconds,
                            suggested_delay,
                        ),
                    )
            next_attempt_at = now + retry_delay
        try:
            result = await self._persistence.call(
                fail_feishu_inbox_message,
                self.app_id,
                message_id,
                last_error=last_error,
                next_attempt_at=next_attempt_at,
                max_attempts=self.inbox_retry_max_attempts,
                permanent=disposition.permanent,
                now=now,
            )
        except Exception:
            # processing 记录不能因失败状态暂时写不回就丢失本地责任。
            self._inbox_deferred_failures[message_id] = (
                last_error,
                next_attempt_at,
                disposition.permanent,
                route_key,
                attempt_number,
                disposition.code,
            )
            print(
                "  [feishu:audit] event=inbox_failure_deferred "
                f"{safe_route_digest(route_key)} "
                f"{safe_message_digest(message_id)} "
                f"attempt_count={attempt_number} "
                "retry_status=processing "
                f"lease_epoch={self.audit_context().get('lease_epoch')} "
                f"failure_type={disposition.code}"
            )
            self._wake_inbox_dispatcher()
            return
        if result is None:
            return
        self._log_inbox_failure_state(
            route_key=route_key,
            message_id=message_id,
            attempt_count=int(result["attempt_count"]),
            status=str(result["status"]),
            failure_type=disposition.code,
            next_attempt_at=next_attempt_at,
        )
        if result["status"] == "retry_wait":
            self._wake_inbox_dispatcher()

    def _log_inbox_failure_state(
        self,
        *,
        route_key: str,
        message_id: str,
        attempt_count: int,
        status: str,
        failure_type: str,
        next_attempt_at: float | None,
    ) -> None:
        """记录不含正文和外部原始标识的 Inbox 重试/永久失败审计。"""
        event = (
            "inbox_permanent_failure"
            if status == "permanent_failed"
            else "inbox_retry"
        )
        fields = [
            f"event={event}",
            safe_route_digest(route_key),
            safe_message_digest(message_id),
            f"attempt_count={int(attempt_count)}",
            f"retry_status={status}",
            f"lease_epoch={self.audit_context().get('lease_epoch')}",
            f"failure_type={failure_type}",
        ]
        if next_attempt_at is not None and status == "retry_wait":
            fields.append(f"next_attempt_at={float(next_attempt_at):.3f}")
        print(f"  [feishu:audit] {' '.join(fields)}")

    async def _release_cancelled_inbox_message(self, message_id: str) -> None:
        """shutdown 取消消费 Task 时把 processing 立即释放为可恢复状态。"""
        if self._persistence is None or self._persistence.closed:
            return
        try:
            await self._persistence.call(
                release_feishu_inbox_processing_message,
                self.app_id,
                message_id,
                last_error="gateway_stopping:CancelledError",
            )
        except Exception:
            # 若 shutdown 时数据库不可写，下一次启动会统一 reset processing。
            pass

    @staticmethod
    def _on_inbox_dispatcher_done(task: asyncio.Task) -> None:
        """读取 dispatcher 异常，避免后台 Task 错误被静默回收。"""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print(
                "  [feishu] inbox dispatcher stopped unexpectedly: "
                f"{type(exc).__name__}"
            )

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

            def _readiness_response(self) -> tuple[int, dict]:
                future = adapter._submit_readiness_status()
                if future is None:
                    return 503, {
                        "ok": False,
                        "ready": False,
                        "error": "gateway unavailable",
                    }
                try:
                    result = future.result(
                        timeout=FEISHU_READINESS_TIMEOUT_SECONDS
                    )
                except FutureTimeoutError:
                    return 503, {
                        "ok": False,
                        "ready": False,
                        "error": "readiness timeout",
                    }
                except Exception as exc:
                    self._response_exception_type = type(exc).__name__
                    return 503, {
                        "ok": False,
                        "ready": False,
                        "error": "readiness failed",
                    }
                if not isinstance(result, dict):
                    return 503, {
                        "ok": False,
                        "ready": False,
                        "error": "invalid readiness state",
                    }
                ready = bool(result.get("ready", False))
                payload = {
                    "ok": ready,
                    "ready": ready,
                    "channel": "feishu",
                    "checks": result.get("checks", {}),
                    "lease_epoch": result.get("lease_epoch"),
                }
                return (200 if ready else 503), payload

            def do_GET(self) -> None:
                self._headers_complete()
                path = self._path()
                if path == "/livez":
                    self._send_json(
                        200,
                        {"ok": True, "live": True, "channel": "feishu"},
                    )
                    return
                if path == "/readyz":
                    status, payload = self._readiness_response()
                    self._send_json(
                        status,
                        payload,
                        reason="" if status == 200 else "not_ready",
                    )
                    return
                if path not in {"/livez", "/readyz"}:
                    self._send_json(
                        404,
                        {"ok": False, "error": "not found"},
                        reason="path_not_found",
                    )

            def do_HEAD(self) -> None:
                self._headers_complete()
                path = self._path()
                if path == "/livez":
                    self._send_head(200)
                    return
                if path == "/readyz":
                    status, _payload = self._readiness_response()
                    self._send_head(
                        status,
                        reason="" if status == 200 else "not_ready",
                    )
                    return
                self._send_head(404, reason="path_not_found")

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

                if not adapter._running:
                    self._send_json(
                        503,
                        {"ok": False, "error": "gateway unavailable"},
                        reason="webhook_not_ready",
                    )
                    return

                # 只等待 Inbox 持久化和受管后台任务注册，不等待批处理或 Runner。
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
                except ValueError as exc:
                    self._response_exception_type = type(exc).__name__
                    self._send_json(
                        400,
                        {"ok": False, "error": "invalid event"},
                        reason="invalid_event",
                    )
                    return
                except Exception as exc:
                    self._response_exception_type = type(exc).__name__
                    self._send_json(
                        500,
                        {"ok": False, "error": "acceptance failed"},
                        reason="acceptance_failed",
                    )
                    return

                self._send_json(200, {"ok": True})

            def _handle_unsupported_method(self) -> None:
                self._headers_complete()
                if self._path() not in {
                    adapter.webhook_path,
                    "/livez",
                    "/readyz",
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
        coroutine = self._accept_payload(payload)
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            coroutine.close()
            return None

    def _submit_readiness_status(self) -> Future | None:
        """从 HTTP 线程向 Gateway 事件循环提交 readiness 聚合。"""
        if not self._loop:
            return None
        coroutine = self.gateway_readiness_status()
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except RuntimeError:
            coroutine.close()
            return None

    # ===================== 消息入站 =====================

    def _parse_message_event(self, payload: dict) -> MessageEvent | None:
        """校验业务事件并转换为 MessageEvent；明确忽略时返回 None。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            raise ValueError("Feishu event header must be an object")
        event_type = self._extract_event_type(payload)
        if event_type != "im.message.receive_v1":
            raise ValueError("unsupported Feishu Inbox event type")

        event = payload.get("event", {})
        if not isinstance(event, dict):
            raise ValueError("Feishu event must be an object")
        message = event.get("message", {})
        if not isinstance(message, dict):
            raise ValueError("Feishu message must be an object")
        sender = event.get("sender", {})
        if not isinstance(sender, dict):
            raise ValueError("Feishu sender must be an object")

        # 单聊也可能收到机器人 / 应用侧消息。只接受真实用户消息,
        # 避免把机器人自己的回复再次送入会话形成自触发循环。
        sender_type = str(sender.get("sender_type", "") or "")
        if not sender_type:
            raise ValueError("Feishu sender_type is required")
        if sender_type != "user":
            return

        sender_ids = sender.get("sender_id", {})
        if not isinstance(sender_ids, dict):
            raise ValueError("Feishu sender_id must be an object")

        msg_id = str(message.get("message_id", "") or "")
        chat_id = str(message.get("chat_id", "") or "")
        sender_id = str(
            sender_ids.get("open_id")
            or sender_ids.get("user_id")
            or ""
        )
        if not msg_id or not chat_id or not sender_id:
            raise ValueError("Feishu message identity is incomplete")

        msg_type = str(
            message.get("message_type")
            or message.get("msg_type")
            or ""
        )
        if not msg_type:
            raise ValueError("Feishu message_type is required")
        if msg_type not in FEISHU_SUPPORTED_INBOUND_MESSAGE_TYPES:
            return

        attachments: list[Attachment] = []
        if msg_type == "text":
            text = self._parse_text(message)
            message_type = MessageType.TEXT
            if not text.strip():
                return
        else:
            if (
                self.file_transfer_config is None
                or self.file_transfer_config.get("enabled") is not True
            ):
                return
            text = FEISHU_ATTACHMENT_PLACEHOLDER
            message_type = MessageType.DOCUMENT
            attachments = [self._parse_attachment(message, msg_type)]

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

        if not self._is_allowed(sender_id, chat_id, chat_type):
            return

        thread_id = message.get("thread_id") or message.get("root_id")
        event_obj = MessageEvent(
            message_id=msg_id,
            text=text,
            message_type=message_type,
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
            attachments=attachments,
            metadata={
                "mentioned_bot": mentioned_bot,
                "mentioned_all": False,
                "raw_content_type": msg_type,
                "event_id": header.get("event_id", ""),
            },
        )

        return event_obj

    async def _accept_payload(self, payload: dict) -> None:
        """校验事件、持久化 Inbox 并唤醒消费者，随后即可确认 HTTP。"""
        if not self._initialized or not self._running:
            raise RuntimeError("feishu adapter is not receiving")
        event = self._parse_message_event(payload)
        if event is None:
            return

        message_id = event.message_id
        route_key = self._build_inbox_route_key(event)
        status = await self._message_inbox_status(message_id)
        if status in {"processed", "cancelled", "permanent_failed"}:
            return
        if status not in {None, "pending", "processing", "retry_wait"}:
            raise RuntimeError("invalid Feishu Inbox status")
        if status is None:
            await self._store_pending_message(message_id, route_key, payload)

        # ACK 只等待持久化和 Event.set()，不等待 claim、批处理或 Runner。
        self._wake_inbox_dispatcher(route_key)

    async def _handle_payload(self, payload: dict) -> None:
        """兼容旧内部入口；其语义现为持久化并注册后台处理。"""
        await self._accept_payload(payload)

    def _register_route_consumer(self, route_key: str) -> bool:
        """为持久路由注册唯一消费者，不在 dispatcher 中提前 claim。"""
        existing = self._route_tasks.get(route_key)
        if existing is not None and not existing.done():
            return False
        wakeup = asyncio.Event()
        self._route_wakeups[route_key] = wakeup
        try:
            task = asyncio.create_task(
                self._consume_inbox_route(route_key, wakeup),
                name="feishu-inbox-route",
            )
        except Exception:
            self._route_wakeups.pop(route_key, None)
            raise
        self._route_tasks[route_key] = task
        task.add_done_callback(
            lambda completed, key=route_key: self._on_route_consumer_done(
                key,
                completed,
            )
        )
        return True

    def _on_route_consumer_done(
        self,
        route_key: str,
        task: asyncio.Task,
    ) -> None:
        """移除 route consumer 身份并读取异常。"""
        if self._route_tasks.get(route_key) is task:
            self._route_tasks.pop(route_key, None)
            self._route_wakeups.pop(route_key, None)
        self._wake_inbox_dispatcher()
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print(
                "  [feishu:audit] event=inbox_route_failed "
                f"{safe_route_digest(route_key)} "
                f"exception={type(exc).__name__}"
            )

    async def _claim_route_head(
        self,
        route_key: str,
        *,
        allow_existing_processing: bool,
    ) -> dict | None:
        """查看并条件 claim route 队首；未到期重试由数据库拒绝。"""
        persistence = self._require_persistence()
        head = await persistence.call(
            get_feishu_inbox_route_next,
            self.app_id,
            route_key,
        )
        if head is None:
            return None
        return await persistence.call(
            claim_feishu_inbox_route_message,
            self.app_id,
            route_key,
            str(head["message_id"]),
            allow_existing_processing=allow_existing_processing,
        )

    @staticmethod
    def _is_plain_text_event(event: MessageEvent) -> bool:
        """只有没有附件的 TEXT 才属于命令与连续文本批处理域。"""
        return (
            event.message_type is MessageType.TEXT
            and not event.attachments
        )

    async def _consume_inbox_route(
        self,
        route_key: str,
        wakeup: asyncio.Event,
    ) -> None:
        """按持久顺序串行消费一个 route，route 之间保持并行。"""
        processing: dict[str, int] = {}
        try:
            while self._initialized and not self._stopping:
                try:
                    claimed = await self._claim_route_head(
                        route_key,
                        allow_existing_processing=False,
                    )
                    if claimed is None:
                        return
                    message_id = str(claimed["message_id"])
                    processing[message_id] = int(claimed["attempt_count"])
                    event = await self._load_inbox_event(
                        message_id,
                        route_key,
                        status="processing",
                    )
                    if (
                        event is None
                        or await self.persisted_message_state(event) is not None
                    ):
                        await self._complete_messages(
                            [message_id],
                            batch_message_id=message_id,
                        )
                        processing.pop(message_id, None)
                        continue

                    if not self._is_plain_text_event(event):
                        await self._materialize_event_attachments(event)
                        await self._handoff_to_runner(event)
                        await self._complete_messages(
                            [message_id],
                            batch_message_id=message_id,
                        )
                        processing.pop(message_id, None)
                        continue

                    command = _immediate_command_name(event.text)
                    if command is not None:
                        await self._handoff_to_runner(event)
                        await self._complete_messages(
                            [message_id],
                            batch_message_id=message_id,
                        )
                        processing.pop(message_id, None)
                        continue

                    await self._consume_adjacent_text_batch(
                        route_key,
                        wakeup,
                        event,
                        processing,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not processing:
                        raise
                    for failed_id, attempt_count in list(processing.items()):
                        await self._record_inbox_failure(
                            route_key,
                            failed_id,
                            attempt_count,
                            exc,
                        )
                        processing.pop(failed_id, None)
                    return
        except asyncio.CancelledError:
            for message_id in list(processing):
                await self._release_cancelled_inbox_message(message_id)
                processing.pop(message_id, None)
            raise

    async def _load_inbox_event(
        self,
        message_id: str,
        route_key: str,
        *,
        status: str | None,
    ) -> MessageEvent | None:
        """读取并验证持久事件与 message_id、route_key 的一致性。"""
        payload = await self._require_persistence().call(
            get_feishu_inbox_payload,
            self.app_id,
            message_id,
            status=status,
        )
        if payload is None:
            return None
        try:
            event = self._parse_message_event(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise FeishuInboxBusinessDataError(
                "Feishu Inbox business payload cannot be parsed"
            ) from exc
        if event is None:
            return None
        if event.message_id != message_id:
            raise FeishuInboxBusinessDataError(
                "Feishu Inbox message_id mismatch"
            )
        if self._build_inbox_route_key(event) != route_key:
            raise FeishuInboxBusinessDataError(
                "Feishu Inbox route_key mismatch"
            )
        return event

    async def _materialize_event_attachments(
        self,
        event: MessageEvent,
    ) -> MessageEvent:
        """在 processing route task 内把 pending 附件变成本地 ready 事实。"""
        if not event.attachments:
            return event

        materialized: list[Attachment] = []
        for raw_attachment in event.attachments:
            try:
                attachment = validate_attachment(raw_attachment)
            except ValueError as exc:
                raise FeishuAttachmentDownloadError(
                    "invalid_attachment",
                    retryable=False,
                ) from exc

            status = attachment["status"]
            if status == "failed":
                raise FeishuAttachmentDownloadError(
                    attachment.get("error_code") or "attachment_failed",
                    retryable=False,
                )
            if status == "pending":
                async with self._file_download_semaphore:
                    result = await self.download_message_resource(
                        event.message_id,
                        attachment,
                    )
                if not result.success:
                    raise FeishuAttachmentDownloadError(
                        result.error_code or "download_failed",
                        retryable=(
                            result.status == "retry_wait"
                            and result.retryable
                        ),
                        retry_after_seconds=result.retry_after_seconds,
                    )
                attachment.update({
                    "local_path": result.local_path,
                    "mime_type": result.mime_type,
                    "size_bytes": result.size_bytes,
                    "sha256": result.sha256,
                    "status": "ready",
                    "error_code": None,
                })

            if (
                not attachment.get("local_path")
                or attachment.get("size_bytes") is None
                or not attachment.get("sha256")
            ):
                raise FeishuAttachmentDownloadError(
                    "invalid_ready_metadata",
                    retryable=False,
                )
            materialized.append(validate_attachment(attachment))

        event.attachments = materialized
        event.text = self._format_materialized_attachment_text(materialized)
        return event

    @staticmethod
    def _format_materialized_attachment_text(
        attachments: list[Attachment],
    ) -> str:
        """生成不含资源凭据、也不暗示已识别正文的稳定 Agent 文本。"""
        if len(attachments) == 1:
            lines = ["用户发送了一个文件。"]
        else:
            lines = [f"用户发送了 {len(attachments)} 个文件。"]
        for index, attachment in enumerate(attachments, start=1):
            if len(attachments) > 1:
                lines.append(f"附件 {index}：")
            lines.extend([
                f"文件名：{attachment.get('original_name') or '未提供'}",
                f"文件类型：{attachment['source_type']}",
                f"文件大小：{attachment['size_bytes']} bytes",
                f"本地路径：{attachment['local_path']}",
            ])
        return "\n".join(lines)

    async def _handoff_to_runner(self, event: MessageEvent) -> None:
        """把 Runner 不可用和生命周期拒绝转换成明确瞬时分类。"""
        if not self._on_message:
            raise FeishuRunnerUnavailableError("gateway runner is unavailable")
        try:
            await self.handle_message(event)
        except RuntimeError as exc:
            if str(exc).startswith((
                "gateway is not accepting messages during ",
                "gateway runtime lease is invalid during ",
            )):
                raise FeishuGatewayLifecycleError(
                    "gateway lifecycle temporarily rejects messages"
                ) from exc
            raise

    async def _consume_adjacent_text_batch(
        self,
        route_key: str,
        wakeup: asyncio.Event,
        first_event: MessageEvent,
        processing: dict[str, int],
    ) -> None:
        """在单 route consumer 内等待静默窗口并合并相邻普通文本。"""
        if not self._is_plain_text_event(first_event):
            raise FeishuInboxBusinessDataError(
                "text batch requires an attachment-free TEXT event"
            )
        events = [first_event]
        started_at = time.monotonic()
        last_message_at = started_at

        while True:
            now = time.monotonic()
            max_remaining = FEISHU_BATCH_MAX_WAIT_SECONDS - (now - started_at)
            quiet_remaining = FEISHU_BATCH_QUIET_SECONDS - (
                now - last_message_at
            )
            if max_remaining <= 0 or quiet_remaining <= 0:
                await self._finish_text_batch(events, processing)
                return

            # 先清 Event 再查库：查询前后的新写入要么可见，要么会保留唤醒。
            wakeup.clear()
            persistence = self._require_persistence()
            head = await persistence.call(
                get_feishu_inbox_route_next,
                self.app_id,
                route_key,
            )
            if head is None:
                try:
                    await asyncio.wait_for(
                        wakeup.wait(),
                        timeout=min(max_remaining, quiet_remaining),
                    )
                except asyncio.TimeoutError:
                    await self._finish_text_batch(events, processing)
                    return
                continue

            status = str(head["status"])
            next_attempt_at = head["next_attempt_at"]
            if (
                status == "retry_wait"
                and (
                    next_attempt_at is None
                    or float(next_attempt_at) > time.time()
                )
            ):
                await self._finish_text_batch(events, processing)
                return

            message_id = str(head["message_id"])
            try:
                next_event = await self._load_inbox_event(
                    message_id,
                    route_key,
                    status=None,
                )
            except (
                InvalidFeishuInboxPayloadError,
                FeishuInboxBusinessDataError,
            ):
                # 损坏队首是批次边界；先提交前批，再由下一轮 claim 后落终态。
                await self._finish_text_batch(events, processing)
                return

            if (
                next_event is None
                or await self.persisted_message_state(next_event) is not None
            ):
                await self._finish_text_batch(events, processing)
                return

            if not self._is_plain_text_event(next_event):
                await self._finish_text_batch(events, processing)
                return

            command = _immediate_command_name(next_event.text)
            if command is not None:
                if command in {"/new", "/stop"}:
                    await self._finish_text_batch(
                        events,
                        processing,
                        cancelled=True,
                    )
                else:
                    await self._finish_text_batch(events, processing)
                return

            claimed = await persistence.call(
                claim_feishu_inbox_route_message,
                self.app_id,
                route_key,
                message_id,
                allow_existing_processing=True,
            )
            if claimed is None:
                await self._finish_text_batch(events, processing)
                return
            processing[message_id] = int(claimed["attempt_count"])
            events.append(next_event)
            last_message_at = time.monotonic()
            if len(events) % self.inbox_retry_batch_size == 0:
                await asyncio.sleep(0)

    async def _finish_text_batch(
        self,
        events: list[MessageEvent],
        processing: dict[str, int],
        *,
        cancelled: bool = False,
    ) -> None:
        """提交或取消已经由当前 route consumer claim 的完整文本批次。"""
        if not all(self._is_plain_text_event(event) for event in events):
            raise FeishuInboxBusinessDataError(
                "text batch contains a non-text or attachment event"
            )
        message_ids = [event.message_id for event in events]
        if cancelled:
            await self._complete_messages(
                message_ids,
                status="cancelled",
                batch_message_id=message_ids[-1],
            )
        else:
            event = events[-1]
            event.text = FEISHU_BATCH_SEPARATOR.join(
                item.text for item in events
            )
            event.metadata["source_message_ids"] = message_ids
            event.metadata["source_messages"] = [
                {
                    "message_id": item.message_id,
                    "reply_to_message_id": item.reply_to_message_id,
                    "event_id": item.metadata.get("event_id", ""),
                }
                for item in events
            ]
            await self._handoff_to_runner(event)
            await self._complete_messages(
                message_ids,
                batch_message_id=event.message_id,
            )
        for message_id in message_ids:
            processing.pop(message_id, None)

    @staticmethod
    def _build_inbox_route_key(event: MessageEvent) -> str:
        """构建持久路由键，不同应用、会话、用户和话题绝不混合。"""
        source = event.source
        return build_feishu_inbox_route_key(
            source.account_id,
            source.chat_type,
            source.chat_id,
            source.user_id,
            source.thread_id,
        )

    @staticmethod
    def _parse_message_content(message: dict, msg_type: str) -> dict:
        """把字符串或对象 content 收敛为普通 dict，不暴露原始正文。"""
        if "content" not in message:
            raise ValueError(f"Feishu {msg_type} content is required")
        raw = message.get("content")
        if isinstance(raw, str):
            try:
                content = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid Feishu {msg_type} content"
                ) from exc
        elif isinstance(raw, dict):
            content = raw
        else:
            raise ValueError(
                f"Feishu {msg_type} content must be an object"
            )
        if not isinstance(content, dict):
            raise ValueError(
                f"Feishu {msg_type} content must be an object"
            )
        return dict(content)

    @staticmethod
    def _required_content_string(
        content: dict,
        msg_type: str,
        field_name: str,
    ) -> str:
        """读取必须的非空字符串字段，错误中不包含实际字段值。"""
        value = content.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Feishu {msg_type} content.{field_name} is required"
            )
        return value.strip()

    @staticmethod
    def _optional_content_string(
        content: dict,
        msg_type: str,
        field_name: str,
    ) -> str | None:
        """读取可选字符串字段；存在但类型错误时拒绝事件。"""
        value = content.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                f"Feishu {msg_type} content.{field_name} must be a string"
            )
        return value.strip() or None

    @classmethod
    def _parse_attachment(
        cls,
        message: dict,
        msg_type: str,
    ) -> Attachment:
        """只提取平台资源身份和安全元数据，不执行任何网络操作。"""
        content = cls._parse_message_content(message, msg_type)
        resource_field = "image_key" if msg_type == "image" else "file_key"
        resource_key = cls._required_content_string(
            content,
            msg_type,
            resource_field,
        )

        original_name = None
        if msg_type == "file":
            original_name = cls._required_content_string(
                content,
                msg_type,
                "file_name",
            )
        elif msg_type == "media":
            original_name = cls._optional_content_string(
                content,
                msg_type,
                "file_name",
            )

        attachment: dict[str, object] = {
            "source_type": msg_type,
            "resource_key": resource_key,
            "resource_type": "image" if msg_type == "image" else "file",
            "original_name": original_name,
            "status": "pending",
        }
        duration = content.get("duration")
        if (
            msg_type in {"audio", "media"}
            and not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and math.isfinite(duration)
            and duration >= 0
        ):
            # duration 是平台提供的非敏感标量，保留原字段语义供后续阶段使用。
            attachment["duration"] = duration
        return validate_attachment(attachment)

    @classmethod
    def _parse_text(cls, message: dict) -> str:
        content = cls._parse_message_content(message, "text")
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

    def _is_allowed(
        self,
        user_id: str,
        chat_id: str,
        chat_type: str = "dm",
    ) -> bool:
        """私聊保留 OR；群聊和话题默认要求用户与会话同时授权。"""
        if self.allow_all:
            return True
        user_allowed = bool(user_id and user_id in self.allowed_users)
        chat_allowed = bool(chat_id and chat_id in self.allowed_chats)
        normalized_chat_type = str(chat_type or "").strip().lower()
        if normalized_chat_type in {"group", "topic"}:
            if self.group_authorization_mode == "and":
                return user_allowed and chat_allowed
            return user_allowed or chat_allowed
        return user_allowed or chat_allowed

    # ===================== 处理状态 reaction =====================

    def _log_reaction_failure(
        self,
        operation: str,
        message_id: str,
        error_type: str,
        *,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """只记录 reaction 故障的安全分类，不记录响应正文或凭据。"""
        fields = [
            f"operation={self._safe_log_label(operation)}",
            f"error={self._safe_log_label(error_type) or 'unknown'}",
            safe_message_digest(message_id),
        ]
        if http_status is not None:
            fields.append(f"http_status={int(http_status)}")
        safe_code = self._safe_log_label(error_code)
        if safe_code:
            fields.append(f"feishu_code={safe_code}")
        print(f"  [feishu:reaction] {' '.join(fields)}")

    @staticmethod
    def _reaction_operation(emoji_type: str) -> str:
        if emoji_type == "Typing":
            return "add_typing"
        if emoji_type == "CrossMark":
            return "add_crossmark"
        return "add_reaction"

    async def _add_message_reaction(
        self,
        message_id: str,
        emoji_type: str,
    ) -> str | None:
        """单次添加 reaction；失败只写脱敏日志并返回 ``None``。"""
        operation = self._reaction_operation(emoji_type)
        if not self._http:
            self._log_reaction_failure(
                operation,
                message_id,
                "adapter_unavailable",
            )
            return None

        token_refreshed = False
        force_refresh = False
        while True:
            token_result = await self._refresh_token(force=force_refresh)
            force_refresh = False
            if not token_result.success:
                self._log_reaction_failure(
                    operation,
                    message_id,
                    token_result.error or "token_unavailable",
                    error_code=token_result.error_code,
                )
                return None
            token = token_result.token
            try:
                response = await self._http.post(
                    f"{self.api_base}/im/v1/messages/{message_id}/reactions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "reaction_type": {"emoji_type": emoji_type},
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                classified = self._classify_transport_exception(exc)
                self._log_reaction_failure(
                    operation,
                    message_id,
                    classified.error or "reaction_transport_error",
                )
                return None

            try:
                status_code = int(response.status_code)
            except (AttributeError, TypeError, ValueError):
                status_code = 0
            try:
                data = response.json()
            except (TypeError, ValueError):
                data = {}
            raw_code = data.get("code") if isinstance(data, dict) else None
            code = self._normalize_error_code(raw_code)
            if code == 0:
                reaction_data = data.get("data", {})
                reaction_id = (
                    reaction_data.get("reaction_id")
                    if isinstance(reaction_data, dict)
                    else None
                )
                if reaction_id:
                    return str(reaction_id)
                self._log_reaction_failure(
                    operation,
                    message_id,
                    "invalid_response",
                    http_status=status_code,
                    error_code="0",
                )
                return None

            if (
                not token_refreshed
                and (
                    status_code == 401
                    or code in FEISHU_TOKEN_ERROR_CODES
                )
            ):
                await self._invalidate_token(token)
                token_refreshed = True
                force_refresh = True
                continue

            error, _, _ = self._classify_send_error(status_code, code)
            self._log_reaction_failure(
                operation,
                message_id,
                error,
                http_status=status_code,
                error_code=(str(raw_code) if raw_code is not None else None),
            )
            return None

    async def _delete_message_reaction(
        self,
        message_id: str,
        reaction_id: str,
    ) -> bool:
        """单次删除 reaction；已不存在时按幂等清理成功处理。"""
        operation = "delete_typing"
        if not self._http:
            self._log_reaction_failure(
                operation,
                message_id,
                "adapter_unavailable",
            )
            return False

        token_refreshed = False
        force_refresh = False
        while True:
            token_result = await self._refresh_token(force=force_refresh)
            force_refresh = False
            if not token_result.success:
                self._log_reaction_failure(
                    operation,
                    message_id,
                    token_result.error or "token_unavailable",
                    error_code=token_result.error_code,
                )
                return False
            token = token_result.token
            try:
                response = await self._http.delete(
                    f"{self.api_base}/im/v1/messages/{message_id}/reactions/"
                    f"{reaction_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                classified = self._classify_transport_exception(exc)
                self._log_reaction_failure(
                    operation,
                    message_id,
                    classified.error or "reaction_transport_error",
                )
                return False

            try:
                status_code = int(response.status_code)
            except (AttributeError, TypeError, ValueError):
                status_code = 0
            try:
                data = response.json()
            except (TypeError, ValueError):
                data = {}
            raw_code = data.get("code") if isinstance(data, dict) else None
            code = self._normalize_error_code(raw_code)
            if (
                code == 0
                or status_code == 404
                or code in FEISHU_REACTION_ALREADY_GONE_CODES
            ):
                return True

            if (
                not token_refreshed
                and (
                    status_code == 401
                    or code in FEISHU_TOKEN_ERROR_CODES
                )
            ):
                await self._invalidate_token(token)
                token_refreshed = True
                force_refresh = True
                continue

            error, _, _ = self._classify_send_error(status_code, code)
            self._log_reaction_failure(
                operation,
                message_id,
                error,
                http_status=status_code,
                error_code=(str(raw_code) if raw_code is not None else None),
            )
            return False

    def _remember_processing_outcome(
        self,
        message_id: str,
        outcome: str,
    ) -> None:
        self._processing_outcomes[message_id] = outcome
        self._processing_outcomes.move_to_end(message_id)
        while len(self._processing_outcomes) > (
            FEISHU_PROCESSING_REACTION_CACHE_SIZE
        ):
            self._processing_outcomes.popitem(last=False)

    async def mark_processing(self, event: MessageEvent) -> None:
        """在原消息上幂等添加 Typing；任何平台错误都不影响主流程。"""
        message_id = str(event.message_id or "")
        if not message_id:
            return
        try:
            async with self._processing_reaction_lock:
                if message_id in self._processing_outcomes:
                    return
                if message_id in self._processing_reactions:
                    self._processing_reactions.move_to_end(message_id)
                    return
                if message_id in self._processing_attempts:
                    self._processing_attempts.move_to_end(message_id)
                    return
                # 请求结果可能因网络中断而未知；先记录尝试可避免重推或恢复
                # 再次添加第二个无法关联 reaction_id 的 Typing。
                self._processing_attempts[message_id] = None
                while len(self._processing_attempts) > (
                    FEISHU_PROCESSING_REACTION_CACHE_SIZE
                ):
                    self._processing_attempts.popitem(last=False)
                reaction_id = await self._add_message_reaction(
                    message_id,
                    "Typing",
                )
                if not reaction_id:
                    return
                self._processing_reactions[message_id] = reaction_id
                self._processing_reactions.move_to_end(message_id)
                while len(self._processing_reactions) > (
                    FEISHU_PROCESSING_REACTION_CACHE_SIZE
                ):
                    old_message_id, old_reaction_id = (
                        self._processing_reactions.popitem(last=False)
                    )
                    await self._delete_message_reaction(
                        old_message_id,
                        old_reaction_id,
                    )
                    self._remember_processing_outcome(
                        old_message_id,
                        "evicted",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_reaction_failure(
                "add_typing",
                message_id,
                type(exc).__name__,
            )

    async def finish_processing(
        self,
        event: MessageEvent,
        outcome: str,
    ) -> None:
        """幂等结束 Typing；失败才额外添加 CrossMark。"""
        message_id = str(event.message_id or "")
        normalized_outcome = str(outcome or "").strip().lower()
        if not message_id or normalized_outcome not in {
            "success",
            "failed",
            "cancelled",
        }:
            return
        try:
            async with self._processing_reaction_lock:
                previous_outcome = self._processing_outcomes.get(message_id)
                reaction_id = self._processing_reactions.get(message_id)
                if reaction_id:
                    deleted = await self._delete_message_reaction(
                        message_id,
                        reaction_id,
                    )
                    if deleted:
                        self._processing_reactions.pop(message_id, None)

                # 第一个真实终态获胜：success/cancelled 不能被旧 worker
                # 改成失败；容量淘汰只表示 Typing 已清理，不吞掉后续失败。
                if (
                    previous_outcome is not None
                    and previous_outcome != "evicted"
                ):
                    self._processing_outcomes.move_to_end(message_id)
                    return
                self._remember_processing_outcome(
                    message_id,
                    normalized_outcome,
                )
                if normalized_outcome == "failed":
                    await self._add_message_reaction(
                        message_id,
                        "CrossMark",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_reaction_failure(
                f"finish_{normalized_outcome}",
                message_id,
                type(exc).__name__,
            )

    async def _clear_processing_reactions(self) -> None:
        """正常断开时尽最大努力清理当前进程已知的 Typing。"""
        try:
            async with self._processing_reaction_lock:
                pending = list(self._processing_reactions.items())
                for message_id, reaction_id in pending:
                    await self._delete_message_reaction(
                        message_id,
                        reaction_id,
                    )
                self._processing_reactions.clear()
                self._processing_attempts.clear()
                self._processing_outcomes.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_reaction_failure(
                "shutdown_cleanup",
                "",
                type(exc).__name__,
            )

    # ===================== 文件资源 =====================

    async def download_message_resource(
        self,
        message_id: str,
        attachment: Attachment,
    ) -> FeishuResourceDownloadResult:
        """复用 Adapter 的 token、失效刷新和 HTTP 客户端下载资源。"""
        return await download_feishu_message_resource(
            http_client=self._http,
            api_base=self.api_base,
            message_id=message_id,
            attachment=attachment,
            file_transfer_config=self.file_transfer_config,
            refresh_token=self._refresh_token,
            invalidate_token=self._invalidate_token,
            classify_error=self._classify_send_error,
        )

    async def cleanup_file_cache(self) -> CacheCleanupResult:
        """按集中配置清理过期缓存；普通失败不影响 Gateway 生命周期。"""
        config = self.file_transfer_config
        if config is None:
            return CacheCleanupResult(
                failed_files=1,
                error_code="file_transfer_config_unavailable",
            )
        return await cleanup_expired_cache(
            config["download_dir"],
            config["cache_retention_seconds"],
        )

    async def upload_file_delivery(
        self,
        *,
        local_path: str,
        display_name: str,
        size_bytes: int,
        sha256: str,
        database_path: str,
    ) -> FeishuFileUploadResult:
        """复用当前 token 与 HTTP client，把本地文件上传为普通 file。"""
        return await upload_feishu_file(
            http_client=self._http,
            api_base=self.api_base,
            local_path=local_path,
            display_name=display_name,
            expected_size_bytes=size_bytes,
            expected_sha256=sha256,
            database_path=database_path,
            file_transfer_config=self.file_transfer_config,
            refresh_token=self._refresh_token,
            invalidate_token=self._invalidate_token,
            classify_error=self._classify_send_error,
        )

    # ===================== 消息出站 =====================

    async def _refresh_token(self, *, force: bool = False) -> TokenResult:
        if (
            not force
            and self._tenant_token
            and time.time() < self._token_expires_at
        ):
            return TokenResult(success=True, token=self._tenant_token)

        async with self._token_lock:
            if (
                not force
                and self._tenant_token
                and time.time() < self._token_expires_at
            ):
                return TokenResult(success=True, token=self._tenant_token)
            if not self._http:
                return TokenResult(
                    success=False,
                    error="token_unavailable",
                    retryable=True,
                )

            try:
                response = await self._http.post(
                    f"{self.api_base}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.app_id,
                        "app_secret": self.app_secret,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "  [feishu] token request failed "
                    f"({type(exc).__name__})"
                )
                return self._classify_token_transport_exception(exc)

            try:
                status_code = int(response.status_code)
            except (AttributeError, TypeError, ValueError):
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    retryable=False,
                )

            retry_after = self._parse_retry_after(
                getattr(response, "headers", None),
            )
            if status_code == 429:
                return TokenResult(
                    success=False,
                    error="token_rate_limited",
                    error_code=str(status_code),
                    retryable=True,
                    retry_after_seconds=retry_after,
                )
            if 500 <= status_code < 600:
                return TokenResult(
                    success=False,
                    error="token_server_error",
                    error_code=str(status_code),
                    retryable=True,
                )
            if status_code in {401, 403}:
                print("  [feishu] token rejected")
                return TokenResult(
                    success=False,
                    error="token_rejected",
                    error_code=str(status_code),
                    retryable=False,
                )
            if 400 <= status_code < 500:
                print("  [feishu] token rejected")
                return TokenResult(
                    success=False,
                    error="token_request_invalid",
                    error_code=str(status_code),
                    retryable=False,
                )
            if not 200 <= status_code < 300:
                print("  [feishu] token rejected")
                return TokenResult(
                    success=False,
                    error="token_request_invalid",
                    error_code=str(status_code),
                    retryable=False,
                )

            try:
                data = response.json()
            except Exception:
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    retryable=False,
                )
            if not isinstance(data, dict):
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    retryable=False,
                )

            raw_code = data.get("code")
            code = self._normalize_error_code(raw_code)
            error_code = str(raw_code) if raw_code is not None else None
            if code is None:
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    error_code=error_code,
                    retryable=False,
                )
            if code != 0:
                if code in FEISHU_RATE_LIMIT_ERROR_CODES:
                    return TokenResult(
                        success=False,
                        error="token_rate_limited",
                        error_code=error_code,
                        retryable=True,
                        retry_after_seconds=retry_after,
                    )
                if code in FEISHU_TOKEN_TRANSIENT_ERROR_CODES:
                    return TokenResult(
                        success=False,
                        error="token_server_error",
                        error_code=error_code,
                        retryable=True,
                    )
                print("  [feishu] token rejected")
                return TokenResult(
                    success=False,
                    error=(
                        "token_rejected"
                        if code in FEISHU_TOKEN_REJECTED_ERROR_CODES
                        else "token_request_invalid"
                    ),
                    error_code=error_code,
                    retryable=False,
                )

            token = data.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    error_code=error_code,
                    retryable=False,
                )
            try:
                expires = int(data.get("expire", 7200) or 7200)
            except (TypeError, ValueError):
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    error_code=error_code,
                    retryable=False,
                )
            if expires <= 0:
                print("  [feishu] token response invalid")
                return TokenResult(
                    success=False,
                    error="token_invalid_response",
                    error_code=error_code,
                    retryable=False,
                )

            self._tenant_token = token
            self._token_expires_at = time.time() + max(60, expires - 300)
            return TokenResult(success=True, token=token)

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

    @staticmethod
    def prepare_file_outbound(
        platform_file_key: str,
        *,
        delivery_id: str,
    ) -> list[dict]:
        """生成不参与 Markdown 分片的单片普通 file payload。"""
        file_key = str(platform_file_key or "").strip()
        if not file_key:
            raise ValueError("platform_file_key must not be empty")
        request_uuid = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"hermes:feishu:file:{delivery_id}",
        ))
        return [{
            "msg_type": "file",
            "content": json.dumps(
                {"file_key": file_key},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "request_uuid": request_uuid,
        }]

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
        if msg_type == "file":
            try:
                file_content = json.loads(message_content)
            except (TypeError, ValueError):
                return SendResult(
                    success=False,
                    error="invalid_outbox_payload",
                    retryable=False,
                )
            if (
                not isinstance(file_content, dict)
                or not isinstance(file_content.get("file_key"), str)
                or not file_content["file_key"].strip()
            ):
                return SendResult(
                    success=False,
                    error="invalid_outbox_payload",
                    retryable=False,
                )
        elif msg_type != "post":
            return SendResult(
                success=False,
                error="invalid_outbox_payload",
                retryable=False,
            )
        if reply_to_message_id and thread_id:
            delivery_mode = "thread_reply"
        elif reply_to_message_id:
            delivery_mode = "reply"
        else:
            delivery_mode = "direct"
        token_refreshed = False
        force_token_refresh = False
        attempts_used = 0
        retry_after = None
        last_result = SendResult(
            success=False,
            error="internal_send_error",
            retryable=False,
        )

        while attempts_used < self.send_total_attempts:
            token_result = await self._refresh_token(
                force=force_token_refresh,
            )
            force_token_refresh = False
            if not token_result.success:
                # token 获取可能跨越较长故障窗口，不在 Adapter 内忙等，直接
                # 交给 Runner 记录 next_attempt_at 并持久化恢复。
                return SendResult(
                    success=False,
                    error=token_result.error or "token_unavailable",
                    error_code=token_result.error_code,
                    retryable=token_result.retryable,
                    retry_after_seconds=token_result.retry_after_seconds,
                )
            token = token_result.token

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
                can_downgrade_reply = (
                    response.status_code not in {401, 403, 408, 429}
                    and response.status_code < 500
                )
                if (
                    can_downgrade_reply
                    and delivery_mode == "thread_reply"
                    and code in FEISHU_THREAD_REPLY_UNSUPPORTED_CODES
                ):
                    delivery_mode = "reply"
                    continue
                if (
                    can_downgrade_reply
                    and delivery_mode in {"thread_reply", "reply"}
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
                    force_token_refresh = True
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
    def _classify_token_transport_exception(exc: Exception) -> TokenResult:
        """Token 获取只对明确的 httpx 传输故障开放重试。"""
        try:
            import httpx
        except ImportError:
            return TokenResult(
                success=False,
                error="token_request_invalid",
                retryable=False,
            )
        if isinstance(
            exc,
            (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ),
        ):
            return TokenResult(
                success=False,
                error="token_unavailable",
                retryable=True,
            )
        return TokenResult(
            success=False,
            error="token_request_invalid",
            retryable=False,
        )

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
