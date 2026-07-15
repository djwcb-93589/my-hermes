"""Gateway 同步数据库 API 的异步执行边界。"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from hermes.db import init_db


class GatewayPersistence:
    """在线程池中为每次数据库操作创建独立连接。"""

    def __init__(self, db_path: str, *, max_workers: int = 4):
        workers = int(max_workers)
        if workers <= 0:
            raise ValueError("Gateway persistence max_workers must be positive")
        self.db_path = db_path
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="gateway-db",
        )
        self._pending: set[asyncio.Future] = set()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @staticmethod
    def _run_with_connection(
        db_path: str,
        operation: Callable[..., Any],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        """在线程内部完成连接创建、完整操作和连接关闭。"""
        conn = init_db(db_path)
        try:
            return operation(conn, *args, **kwargs)
        finally:
            conn.close()

    def _forget(self, future: asyncio.Future) -> None:
        """移除完成项并读取异常，兼容调用方在等待中被取消。"""
        self._pending.discard(future)
        if future.cancelled():
            return
        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def call(
        self,
        operation: Callable[..., Any],
        *args,
        **kwargs,
    ) -> Any:
        """异步执行一个保持原事务边界的同步数据库操作。"""
        if self._closing or self._closed:
            raise RuntimeError("Gateway persistence is closing")
        loop = asyncio.get_running_loop()
        invoke = functools.partial(
            self._run_with_connection,
            self.db_path,
            operation,
            args,
            kwargs,
        )
        future = loop.run_in_executor(self._executor, invoke)
        self._pending.add(future)
        future.add_done_callback(self._forget)
        # 调用协程取消时不取消已经进入线程的提交；shutdown 会统一 drain。
        return await asyncio.shield(future)

    async def close(self) -> None:
        """停止接收新操作，等待已提交操作完成后关闭线程池。"""
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            while self._pending:
                pending = tuple(self._pending)
                await asyncio.gather(
                    *(asyncio.shield(item) for item in pending),
                    return_exceptions=True,
                )
            await asyncio.to_thread(
                self._executor.shutdown,
                wait=True,
                cancel_futures=False,
            )
            self._closed = True

