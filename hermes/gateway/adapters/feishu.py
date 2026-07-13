"""
飞书 adapter:基于 Webhook HTTP 回调,接收文本并发送 Markdown 富文本。

特性:
  - 内置 ThreadingHTTPServer,接收飞书事件回调
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
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


FEISHU_POST_LIMIT_BYTES = 30 * 1024  # 飞书单条富文本请求体上限
FEISHU_POST_SAFETY_MARGIN_BYTES = 1024  # 为 receive_id 等外层字段预留空间
FEISHU_TOKEN_ERROR_CODES = frozenset({99991663, 99991665})
FEISHU_BATCH_QUIET_SECONDS = 0.6  # 连续文本静默多久后提交
FEISHU_BATCH_MAX_WAIT_SECONDS = 2.0  # 单批消息最长累计等待时间
FEISHU_BATCH_SEPARATOR = "\n"
FEISHU_WEBHOOK_ACCEPT_TIMEOUT_SECONDS = 2.5
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
        webhook_host: str = "0.0.0.0",
        webhook_port: int = 8787,
        verification_token: str = "",
        encrypt_key: str = "",
        bot_open_id: str = "",
        is_lark: bool = False,
        dm_only: bool = True,
        require_mention: bool = True,
        allow_all: bool = False,
        allowed_users: list[str] | None = None,
        allowed_chats: list[str] | None = None,
        send_max_retries: int = 3,
        send_retry_base_delay: float = 1.0,
        send_rate_limit_per_chat: int = 5,
    ):
        super().__init__("feishu")
        self.app_id = app_id
        self.app_secret = app_secret
        self.db_path = db_path
        self.webhook_host = webhook_host
        self.webhook_port = int(webhook_port)
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.bot_open_id = bot_open_id
        self.is_lark = bool(is_lark)
        self.dm_only = bool(dm_only)
        self.require_mention = require_mention
        self.allow_all = allow_all
        self.allowed_users = set(allowed_users or [])
        self.allowed_chats = set(allowed_chats or [])
        self.send_max_retries = max(1, int(send_max_retries))
        self.send_retry_base_delay = max(0.0, float(send_retry_base_delay))
        self.send_rate_limit_per_chat = max(1, int(send_rate_limit_per_chat))

        self.api_base = (
            "https://open.larksuite.com/open-apis"
            if self.is_lark
            else "https://open.feishu.cn/open-apis"
        )
        self._tenant_token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._http = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
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

    # ===================== 生命周期 =====================

    async def connect(self) -> bool:
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

        try:
            self._open_reliability_store()
            self._loop = asyncio.get_running_loop()
            self._http = httpx.AsyncClient(timeout=15.0)
            handler = self._make_webhook_handler()
            self._server = ThreadingHTTPServer(
                (self.webhook_host, self.webhook_port),
                handler,
            )
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="feishu-webhook",
                daemon=True,
            )
            self._running = True
            await self._restore_pending_messages()
            self._server_thread.start()

            # 端口为 0 时系统会自动分配端口,主要用于测试。
            actual_port = self._server.server_address[1]
            print(
                f"  [feishu] webhook listening on "
                f"http://{self.webhook_host}:{actual_port}/"
            )
            return True
        except Exception as exc:
            print(f"  [feishu] webhook start failed: {type(exc).__name__}")
            self._running = False
            if self._server:
                self._server.server_close()
            await self._close_http_client()
            self._close_reliability_store()
            self._server = None
            self._server_thread = None
            self._loop = None
            return False

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
        self._loop = None

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
            """飞书回调处理器。HTTP 线程只解析请求,Agent 在主事件循环运行。"""

            server_version = "HermesFeishuWebhook/0.1"

            def _send_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send_json(200, {"ok": True, "channel": "feishu"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"ok": False, "error": "invalid json"})
                    return

                if not isinstance(payload, dict):
                    self._send_json(
                        400,
                        {"ok": False, "error": "json object required"},
                    )
                    return

                token = adapter._extract_token(payload)
                if not adapter._token_allowed(token):
                    print("  [feishu] token verification failed")
                    self._send_json(403, {"ok": False, "error": "forbidden"})
                    return

                challenge = adapter._extract_challenge(payload)
                if challenge:
                    print("  [feishu] webhook challenge verified")
                    self._send_json(200, {"challenge": challenge})
                    return

                if not adapter._app_allowed(payload):
                    print("  [feishu] app verification failed")
                    self._send_json(403, {"ok": False, "error": "forbidden"})
                    return

                event_type = adapter._extract_event_type(payload)
                if not event_type:
                    self._send_json(
                        400,
                        {"ok": False, "error": "missing event_type"},
                    )
                    return
                if event_type != "im.message.receive_v1":
                    # 未订阅的其它合法事件不应触发飞书重试,明确忽略即可。
                    self._send_json(200, {"ok": True, "ignored": True})
                    return

                # 等消息完成解析、拼接并成功交给 GatewayRunner 后再确认。
                # 不等待 LLM 最终回复,因此仍能满足飞书的快速响应要求。
                future = adapter._submit_payload(payload)
                if future is None:
                    self._send_json(
                        503,
                        {"ok": False, "error": "gateway unavailable"},
                    )
                    return
                try:
                    future.result(timeout=FEISHU_WEBHOOK_ACCEPT_TIMEOUT_SECONDS)
                except FutureTimeoutError:
                    print("  [feishu] webhook acceptance timed out")
                    self._send_json(
                        503,
                        {"ok": False, "error": "gateway timeout"},
                    )
                    return
                except Exception as exc:
                    print(
                        "  [feishu] webhook processing failed: "
                        f"{type(exc).__name__}"
                    )
                    self._send_json(
                        500,
                        {"ok": False, "error": "processing failed"},
                    )
                    return

                self._send_json(200, {"ok": True})

            def log_message(self, fmt: str, *args: Any) -> None:
                print(
                    f"  [feishu:http] {self.address_string()} - "
                    f"{fmt % args}"
                )

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
        return hmac.compare_digest(token, self.verification_token)

    def _app_allowed(self, payload: dict) -> bool:
        """校验 v2.0 事件属于当前飞书应用。"""
        header = payload.get("header", {})
        if not isinstance(header, dict):
            return False
        event_app_id = str(header.get("app_id", "") or "")
        if not event_app_id:
            return False
        return hmac.compare_digest(event_app_id, self.app_id)

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

        # 未配置 bot_open_id 时,接收消息事件中出现 mention 即视为 @机器人。
        # 配置后则做精确匹配,避免把 @其他成员误判为 @机器人。
        if not self.bot_open_id:
            return bool(mentions)

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
            if mention.get("key") == self.bot_open_id:
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
            except Exception:
                print("  [feishu] tenant token request failed")
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
        if not self._running or not self._http:
            return SendResult(
                success=False,
                error="not_connected",
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
                error="not_connected",
                retryable=True,
            )

        request_uuid = str(payload.get("request_uuid", "") or "")
        msg_type = str(payload.get("msg_type", "post") or "post")
        message_content = str(payload.get("content", "") or "")
        reply_in_thread = bool(thread_id)
        thread_fallback_used = False
        token_refreshed = False
        attempt = 0
        retry_after = None
        last_result = SendResult(
            success=False,
            error="unknown",
            retryable=False,
        )

        while attempt < self.send_max_retries:
            token = await self._refresh_token()
            if not token:
                last_result = SendResult(
                    success=False,
                    error="token_unavailable",
                    retryable=True,
                )
                attempt += 1
            else:
                try:
                    await self._wait_send_slot(chat_id)
                    if reply_to_message_id:
                        response = await self._http.post(
                            f"{self.api_base}/im/v1/messages/"
                            f"{reply_to_message_id}/reply",
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                "msg_type": msg_type,
                                "content": message_content,
                                "reply_in_thread": reply_in_thread,
                                "uuid": request_uuid,
                            },
                        )
                    else:
                        response = await self._http.post(
                            f"{self.api_base}/im/v1/messages",
                            params={
                                "receive_id_type": "chat_id",
                                "uuid": request_uuid,
                            },
                            headers={"Authorization": f"Bearer {token}"},
                            json={
                                "receive_id": chat_id,
                                "msg_type": msg_type,
                                "content": message_content,
                            },
                        )
                    try:
                        data = response.json()
                    except (TypeError, ValueError):
                        # 即使错误响应体不是 JSON,仍要按 HTTP 状态决定
                        # 401 刷新 token、403 停止、5xx 重试。
                        data = {}
                    code = data.get("code")
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

                    # 当前群不支持话题时降级为普通回复一次。同一次请求尚未
                    # 成功,所以可以继续复用分片 UUID。
                    if (
                        code == 230071
                        and reply_in_thread
                        and not thread_fallback_used
                    ):
                        reply_in_thread = False
                        thread_fallback_used = True
                        continue

                    error, retryable, refresh_token = self._classify_send_error(
                        response.status_code,
                        code,
                    )
                    last_result = SendResult(
                        success=False,
                        error=error,
                        error_code=str(code) if code is not None else None,
                        retryable=retryable,
                    )

                    if refresh_token and not token_refreshed:
                        await self._invalidate_token(token)
                        token_refreshed = True
                        continue
                    if not retryable:
                        return last_result
                    retry_after = self._parse_retry_after(response.headers)
                    attempt += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    last_result = SendResult(
                        success=False,
                        error="send_timeout",
                        retryable=True,
                    )
                    retry_after = None
                    attempt += 1

            if attempt < self.send_max_retries:
                delay = self.send_retry_base_delay * (2 ** (attempt - 1))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                await asyncio.sleep(max(0.0, delay))

        return last_result

    async def _wait_send_slot(self, chat_id: str) -> None:
        """按会话限制发送速率,避免多路回复同时触发飞书限流。"""
        lock = self._send_rate_locks.setdefault(chat_id, asyncio.Lock())
        timestamps = self._send_timestamps.setdefault(chat_id, deque())
        async with lock:
            while True:
                now = time.monotonic()
                while timestamps and now - timestamps[0] >= 1.0:
                    timestamps.popleft()
                if len(timestamps) < self.send_rate_limit_per_chat:
                    timestamps.append(now)
                    return
                await asyncio.sleep(max(0.0, 1.0 - (now - timestamps[0])))

    @staticmethod
    def _parse_retry_after(headers) -> float | None:
        value = headers.get("Retry-After") if headers else None
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _classify_send_error(
        status_code: int,
        code: Any,
    ) -> tuple[str, bool, bool]:
        """返回 ``错误类型、是否重试、是否刷新 tenant token``。"""
        if status_code == 429 or code in (99991400, 99991401):
            return "rate_limited", True, False
        if status_code == 401 or code in FEISHU_TOKEN_ERROR_CODES:
            return "token_invalid", False, True
        if status_code == 403 or code == 99991672:
            return "permission_denied", False, False
        if status_code >= 500:
            return "server_error", True, False
        if 400 <= status_code < 500:
            return "invalid_request", False, False
        return "unknown", False, False

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
