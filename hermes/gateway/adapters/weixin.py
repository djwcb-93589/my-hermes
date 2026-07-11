"""
个人微信 Weixin adapter:iLink Bot API,第一阶段仅支持文本。

协议严格按照 iLink Bot 规范实现,不猜测字段。

特性:
  - 二维码登录(凭据持久化 + 原子写入)
  - Long polling(getupdates + sync_buf 持久化)
  - 文本消息收发
  - context_token 持久化 + 失效重试
  - message_id 去重
  - 错误码处理(-14 token 失效 / -2 限流/未知)

不实现:群聊、图片、文件、语音、typing、CDN、AES 加解密。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import struct
import time
import uuid
from pathlib import Path

from hermes.config import HERMES_HOME
from hermes.gateway.adapters import BasePlatformAdapter
from hermes.gateway.types import MessageEvent, MessageType, SendResult, SessionSource


# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8)

CREDS_FILE = HERMES_HOME / "weixin_creds.json"
CONTEXT_TOKENS_FILE = HERMES_HOME / "weixin_context_tokens.json"
SYNC_BUF_FILE = HERMES_HOME / "weixin_sync_buf.txt"

# 去重:最多保留最近 5000 条 message_id
_DEDUP_MAX = 5000


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _random_wechat_uin() -> str:
    """生成随机 X-WECHAT-UIN header 值。"""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _client_version_header() -> str:
    return str(ILINK_APP_CLIENT_VERSION)


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入 JSON 文件。tmp + os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass  # Windows: chmod 对 0o600 基本是 no-op


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# WeixinAdapter
# ---------------------------------------------------------------------------

class WeixinAdapter(BasePlatformAdapter):
    """个人微信 adapter,基于 iLink Bot API。"""

    PLATFORM = "weixin"

    def __init__(
        self,
        *,
        account_id: str = "",
        token: str = "",
        base_url: str = ILINK_BASE_URL,
        user_id: str = "",
        allow_all: bool = False,
        allowed_users: list[str] | None = None,
    ):
        super().__init__("weixin")
        # 优先用传入参数;没有就从持久化凭据文件读
        self.account_id = account_id
        self.token = token
        self.base_url = base_url or ILINK_BASE_URL
        self.user_id = user_id
        self.allow_all = allow_all
        self.allowed_users = set(allowed_users or [])

        self._session = None  # aiohttp.ClientSession
        self._poll_task: asyncio.Task | None = None
        self._sync_buf = ""
        self._dedup: dict[str, float] = {}  # message_id → timestamp
        self._context_tokens: dict[str, str] = {}  # key(account_id+peer) → token

    # ===================== 生命周期 =====================

    async def connect(self) -> bool:
        try:
            import aiohttp  # noqa: F401
            import certifi  # noqa: F401
        except ImportError:
            print("  [weixin] aiohttp / certifi not installed, skipping")
            return False

        # 从凭据文件恢复(如果构造参数没传 account_id/token)
        if not self.account_id or not self.token:
            creds = _load_json(CREDS_FILE)
            if creds:
                self.account_id = creds.get("account_id", "")
                self.token = creds.get("token", "")
                self.base_url = creds.get("base_url", ILINK_BASE_URL)
                self.user_id = creds.get("user_id", "")

        if not self.account_id or not self.token:
            print("  [weixin] no credentials; run gateway_weixin_login first")
            return False

        # 恢复 sync_buf 和 context_tokens
        self._sync_buf = _load_text(SYNC_BUF_FILE)
        ctx_data = _load_json(CONTEXT_TOKENS_FILE)
        if isinstance(ctx_data, dict):
            self._context_tokens = ctx_data

        import aiohttp
        import ssl
        import certifi

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(connector=connector)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        print(f"  [weixin] connected as {self.account_id}")
        return True

    async def disconnect(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None

    # ===================== HTTP 请求 =====================

    async def _post(self, endpoint: str, payload: dict) -> dict | None:
        """POST 请求,返回 JSON dict 或 None(请求失败)。"""
        if not self._session:
            return None
        body_dict = {**payload, "base_info": {"channel_version": CHANNEL_VERSION}}
        body = json.dumps(body_dict)
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self.token}",
            "Content-Length": str(len(body.encode("utf-8"))),
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": _client_version_header(),
        }
        try:
            async with self._session.post(
                url, data=body.encode("utf-8"), headers=headers, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return None
                return json.loads(text)
        except Exception:
            return None

    # ===================== Long polling =====================

    async def _poll_loop(self):
        """持续 long polling,直到 disconnect。"""
        import aiohttp
        while self._running:
            try:
                resp = await self._post(EP_GET_UPDATES, {
                    "get_updates_buf": self._sync_buf,
                })
                if resp is None:
                    await asyncio.sleep(5)
                    continue

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)
                if ret not in (0,) and errcode not in (0,):
                    # 错误:退避后重试
                    await asyncio.sleep(5)
                    continue

                # 更新 sync_buf(原子持久化)
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    self._sync_buf = new_buf
                    _atomic_write_text(SYNC_BUF_FILE, new_buf)

                timeout_ms = resp.get("longpolling_timeout_ms", 35000)

                # 处理消息
                messages = resp.get("msgs", [])
                for message in messages:
                    await self._handle_raw_message(message)

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    async def _handle_raw_message(self, message: dict):
        """处理单条原始消息。"""
        message_id = message.get("message_id", "")
        if not message_id:
            return

        # 去重(message_id 基准,TTL 清理)
        if self._is_duplicate(message_id):
            return

        sender_id = message.get("from_user_id", "")
        context_token = message.get("context_token")

        # 只处理文本(type==1)
        item_list = message.get("item_list", [])
        text = ""
        for item in item_list:
            if item.get("type") == 1:
                text = (item.get("text_item") or {}).get("text", "")
                break

        if not text:
            return

        # 持久化 context_token(account_id + peer_user_id)
        if context_token:
            key = f"{self.account_id}:{sender_id}"
            self._context_tokens[key] = context_token
            _atomic_write_json(CONTEXT_TOKENS_FILE, self._context_tokens)

        # 白名单
        if not self._is_allowed(sender_id):
            return

        event = MessageEvent(
            message_id=message_id,
            text=text,
            message_type=MessageType.TEXT,
            source=SessionSource(
                platform=self.PLATFORM,
                account_id=self.account_id,
                chat_id=sender_id,  # 第一阶段 chat_id = from_user_id
                chat_type="dm",
                user_id=sender_id,
                user_name="",
            ),
            metadata={"context_token": context_token or ""},
        )
        await self.handle_message(event)

    def _is_duplicate(self, message_id: str) -> bool:
        """message_id 去重 + TTL 清理。"""
        now = time.time()
        # 清理超过 1 小时的旧记录
        if len(self._dedup) > _DEDUP_MAX:
            cutoff = now - 3600
            self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
        if message_id in self._dedup:
            return True
        self._dedup[message_id] = now
        return False

    def _is_allowed(self, user_id: str) -> bool:
        if self.allow_all:
            return True
        if user_id and user_id in self.allowed_users:
            return True
        return False

    # ===================== 发送 =====================

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        if not self._session:
            return SendResult(success=False, error="not connected")

        # 取 context_token
        key = f"{self.account_id}:{chat_id}"
        context_token = self._context_tokens.get(key)

        result = await self._send_once(chat_id, content, context_token)

        # ret==-14 / errcode==-14:context_token 失效 → 清除 + 重试一次不带 token
        if not result.success and "token_invalid" in (result.error or ""):
            self._context_tokens.pop(key, None)
            _atomic_write_json(CONTEXT_TOKENS_FILE, self._context_tokens)
            result = await self._send_once(chat_id, content, None)

        return result

    async def _send_once(
        self,
        chat_id: str,
        content: str,
        context_token: str | None,
    ) -> SendResult:
        """单次发送(含错误码处理)。"""
        msg: dict = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": str(uuid.uuid4()),
            "message_type": 2,
            "message_state": 2,
            "item_list": [
                {"type": 1, "text_item": {"text": content}},
            ],
        }
        if context_token:
            msg["context_token"] = context_token

        resp = await self._post(EP_SEND_MESSAGE, {"msg": msg})
        if resp is None:
            return SendResult(success=False, error="http_error")

        ret = resp.get("ret", 0)
        errcode = resp.get("errcode", 0)
        errmsg = resp.get("errmsg", "")

        if ret == 0 and errcode == 0:
            return SendResult(success=True, message_id=resp.get("message_id"))

        # -14:context_token 失效
        if ret == -14 or errcode == -14:
            return SendResult(success=False, error="token_invalid")

        # -2 + "unknown error":会话失效
        if (ret == -2 or errcode == -2) and errmsg == "unknown error":
            return SendResult(success=False, error="session_invalid")

        # 其它 -2:限流
        if ret == -2 or errcode == -2:
            return SendResult(success=False, error="rate_limited")

        return SendResult(success=False, error=f"ret={ret} errcode={errcode} msg={errmsg}")


# ---------------------------------------------------------------------------
# QR 登录(独立函数,供 gateway_weixin_login 调用)
# ---------------------------------------------------------------------------

async def login_with_qr(display_qr: bool = True) -> dict | None:
    """二维码登录流程。成功后凭据原子写入 CREDS_FILE。

    返回凭据 dict 或 None(失败)。
    """
    import aiohttp

    base_url = ILINK_BASE_URL
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": _client_version_header(),
    }

    async with aiohttp.ClientSession() as session:
        # 1. 获取二维码
        async with session.get(
            f"{base_url}/{EP_GET_BOT_QR}?bot_type=3", headers=headers,
        ) as resp:
            if resp.status != 200:
                print(f"  [weixin] get_qr failed: HTTP {resp.status}")
                return None
            data = await resp.json()

        qrcode = data.get("qrcode", "")
        if not qrcode:
            print("  [weixin] no qrcode in response")
            return None

        qr_img = data.get("qrcode_img_content", "")
        if display_qr:
            if qr_img:
                try:
                    import qrcode
                    # qr_img 是 base64 编码的图片或原始 URL
                    # 显示二维码到终端
                    qr = qrcode.QRCode(box_size=1, border=1)
                    qr.add_data(f"ilink_login:{qrcode}")
                    qr.make(fit=True)
                    qr.print_ascii(invert=True)
                except ImportError:
                    print(f"  [weixin] qrcode: {qrcode}")
            else:
                print(f"  [weixin] qrcode: {qrcode}")

        print("  [weixin] Waiting for scan...")

        # 2. 轮询状态
        while True:
            async with session.get(
                f"{base_url}/{EP_GET_QR_STATUS}?qrcode={qrcode}", headers=headers,
            ) as resp:
                if resp.status != 200:
                    await asyncio.sleep(2)
                    continue
                status_data = await resp.json()

            status = status_data.get("status", "wait")
            print(f"  [weixin] status: {status}")

            if status == "wait":
                await asyncio.sleep(2)
                continue

            if status == "scaned":
                await asyncio.sleep(2)
                continue

            if status == "scaned_but_redirect":
                redirect_host = status_data.get("redirect_host", "")
                if redirect_host:
                    base_url = f"https://{redirect_host}"
                    print(f"  [weixin] redirecting to {redirect_host}")
                await asyncio.sleep(2)
                continue

            if status == "expired":
                print("  [weixin] QR expired")
                return None

            if status == "confirmed":
                account_id = status_data.get("ilink_bot_id", "")
                token = status_data.get("bot_token", "")
                baseurl = status_data.get("baseurl", base_url)
                user_id = status_data.get("ilink_user_id", "")

                if not account_id or not token:
                    print("  [weixin] confirmed but missing credentials")
                    return None

                creds = {
                    "account_id": account_id,
                    "token": token,
                    "base_url": baseurl,
                    "user_id": user_id,
                }
                _atomic_write_json(CREDS_FILE, creds)
                print(f"  [weixin] logged in as {account_id}")
                print(f"  [weixin] credentials saved to {CREDS_FILE}")
                return creds

            await asyncio.sleep(2)
