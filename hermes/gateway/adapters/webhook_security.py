"""Webhook HTTP 边界使用的轻量并发、代理 IP 和限流组件。"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


@dataclass
class _RateBucket:
    timestamps: deque[float]
    last_seen: float


class SlidingWindowRateLimiter:
    """线程安全且容量有界的每 key 滑动窗口限流器。"""

    def __init__(
        self,
        *,
        window_seconds: float,
        max_requests: int,
        max_tracked_keys: int,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be greater than 0")
        if max_requests <= 0:
            raise ValueError("max_requests must be greater than 0")
        if max_tracked_keys <= 0:
            raise ValueError("max_tracked_keys must be greater than 0")
        self.window_seconds = float(window_seconds)
        self.max_requests = int(max_requests)
        self.max_tracked_keys = int(max_tracked_keys)
        self._buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """允许请求时记录时间戳；容量满时拒绝新的 key，不扩张缓存。"""
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_seconds
        normalized_key = key or "unknown"
        with self._lock:
            self._prune_expired(cutoff)
            bucket = self._buckets.get(normalized_key)
            if bucket is None:
                if len(self._buckets) >= self.max_tracked_keys:
                    return False
                bucket = _RateBucket(timestamps=deque(), last_seen=current)
                self._buckets[normalized_key] = bucket
            else:
                while bucket.timestamps and bucket.timestamps[0] <= cutoff:
                    bucket.timestamps.popleft()
                bucket.last_seen = current
                self._buckets.move_to_end(normalized_key)

            if len(bucket.timestamps) >= self.max_requests:
                return False
            bucket.timestamps.append(current)
            bucket.last_seen = current
            return True

    def _prune_expired(self, cutoff: float) -> None:
        """OrderedDict 按最近访问排序，可从头部停止扫描。"""
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            if bucket.last_seen > cutoff:
                break
            self._buckets.pop(key, None)


class TrustedProxyResolver:
    """仅在 socket peer 属于可信网段时解析 X-Forwarded-For。"""

    _MAX_FORWARDED_HEADER_BYTES = 1024
    _MAX_FORWARDED_HOPS = 32

    def __init__(
        self,
        trusted_proxies: list[str] | tuple[str, ...] | None,
    ) -> None:
        if trusted_proxies is not None and not isinstance(
            trusted_proxies,
            (list, tuple),
        ):
            raise ValueError("trusted_proxies must be a list")
        networks = []
        for value in trusted_proxies or ():
            try:
                networks.append(
                    ipaddress.ip_network(str(value).strip(), strict=False)
                )
            except ValueError as exc:
                raise ValueError(
                    f"invalid trusted proxy network: {value!r}"
                ) from exc
        self._trusted_networks = tuple(networks)

    def resolve(self, peer_ip: str, forwarded_for: str | None) -> str:
        """从右向左剥离可信代理，避免直接相信客户端伪造的首项。"""
        peer = self._parse_address(peer_ip)
        if peer is None:
            return "unknown"
        if not forwarded_for or not self._is_trusted(peer):
            return str(peer)
        if (
            len(forwarded_for.encode("utf-8", errors="ignore"))
            > self._MAX_FORWARDED_HEADER_BYTES
        ):
            return str(peer)

        parts = [part.strip() for part in forwarded_for.split(",")]
        if not parts or len(parts) > self._MAX_FORWARDED_HOPS:
            return str(peer)
        forwarded = [self._parse_address(part) for part in parts]
        if any(address is None for address in forwarded):
            return str(peer)

        chain = [address for address in forwarded if address is not None]
        chain.append(peer)
        while len(chain) > 1 and self._is_trusted(chain[-1]):
            chain.pop()
        return str(chain[-1])

    def _is_trusted(self, address) -> bool:
        return any(address in network for network in self._trusted_networks)

    @staticmethod
    def _parse_address(value: str):
        raw = str(value or "").strip()
        if "%" in raw:
            raw = raw.split("%", 1)[0]
        try:
            return ipaddress.ip_address(raw)
        except ValueError:
            return None


def redact_ip(value: str) -> str:
    """保留排障所需网段信息，不输出完整客户端 IP。"""
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv4Address):
        parts = str(address).split(".")
        return f"{parts[0]}.{parts[1]}.x.x"
    groups = address.exploded.split(":")
    return f"{groups[0]}:{groups[1]}:{groups[2]}::/48"


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """在创建 handler 线程前取得槽位，过载连接由 accept 线程快速拒绝。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        request_handler: type[BaseHTTPRequestHandler],
        *,
        max_concurrent_requests: int,
        reject_logger: Callable[[str], None] | None = None,
    ) -> None:
        if max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be greater than 0")
        self.request_queue_size = int(max_concurrent_requests)
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._reject_logger = reject_logger
        super().__init__(server_address, request_handler)

    def process_request(self, request: socket.socket, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            self._reject_overloaded(request, client_address)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def _reject_overloaded(self, request: socket.socket, client_address) -> None:
        body = b'{"ok":false,"error":"server busy"}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Retry-After: 1\r\nConnection: close\r\n\r\n"
            + body
        )
        try:
            self._discard_request_headers(request)
            request.settimeout(0.1)
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)
        if self._reject_logger is not None:
            peer_ip = str(client_address[0]) if client_address else "unknown"
            self._reject_logger(peer_ip)

    @staticmethod
    def _discard_request_headers(request: socket.socket) -> None:
        """避免 Windows 因未读请求头发送 RST；总时间和读取量都严格有界。"""
        deadline = time.monotonic() + 0.02
        received = bytearray()
        while len(received) < 8192 and b"\r\n\r\n" not in received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            request.settimeout(remaining)
            try:
                chunk = request.recv(min(2048, 8192 - len(received)))
            except (OSError, socket.timeout):
                return
            if not chunk:
                return
            received.extend(chunk)
