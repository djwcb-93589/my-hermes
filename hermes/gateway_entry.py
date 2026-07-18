"""
统一 Gateway 启动入口。

根据 ``config['gateway']['platforms']`` 创建 CLI / Feishu / Weixin adapter。
Runner 统一编排初始化、Gateway 恢复、Adapter Inbox 恢复和开放接收；单个平台
启动失败不阻止其他平台。关闭时停止全部 adapter + 清理 backend。
"""

from __future__ import annotations

import asyncio
import signal

from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.tools import register_all


async def run_gateway():
    """根据配置启动多平台 Gateway。"""
    print(f"=== Hermes Gateway ===")
    print(f"Model: {MODEL}")

    # Runner 会按平台配置把同一工具集同时用于 prompt、API schema 和
    # dispatch 白名单；全局 registry 本身不作为安全边界。
    # Runner 构造阶段统一校验 Gateway 配置；非法配置会在任何 Adapter
    # 初始化或 Webhook 监听开始前直接终止启动。
    register_all()
    runner = GatewayRunner(config=_config, db_path=DB_PATH)

    platforms = _config.get("gateway", {}).get("platforms", {})

    # CLI
    cli_cfg = platforms.get("cli", {})
    if cli_cfg.get("enabled", False):
        from hermes.gateway.adapters.cli import CLIAdapter
        runner.add_adapter(CLIAdapter())
        print("  [gateway] CLI adapter added")

    # 飞书
    feishu_cfg = platforms.get("feishu", {})
    if feishu_cfg.get("enabled", False):
        try:
            from hermes.gateway.adapters.feishu import FeishuAdapter
            rate_limit_cfg = feishu_cfg.get("webhook_rate_limit", {})
            if not isinstance(rate_limit_cfg, dict):
                raise ValueError("feishu webhook_rate_limit must be a mapping")
            runner.add_adapter(FeishuAdapter(
                app_id=feishu_cfg.get("app_id", ""),
                app_secret=feishu_cfg.get("app_secret", ""),
                db_path=DB_PATH,
                webhook_host=feishu_cfg.get("webhook_host", "127.0.0.1"),
                webhook_port=feishu_cfg.get("webhook_port", 8787),
                webhook_path=feishu_cfg.get(
                    "webhook_path", "/feishu/webhook",
                ),
                webhook_max_body_bytes=feishu_cfg.get(
                    "webhook_max_body_bytes", 1024 * 1024,
                ),
                webhook_read_timeout_seconds=feishu_cfg.get(
                    "webhook_read_timeout_seconds", 5.0,
                ),
                webhook_max_concurrent_requests=feishu_cfg.get(
                    "webhook_max_concurrent_requests", 32,
                ),
                webhook_rate_limit_window_seconds=rate_limit_cfg.get(
                    "window_seconds", 60.0,
                ),
                webhook_rate_limit_max_requests=rate_limit_cfg.get(
                    "max_requests", 120,
                ),
                webhook_rate_limit_max_tracked_ips=rate_limit_cfg.get(
                    "max_tracked_ips", 2048,
                ),
                webhook_trusted_proxies=feishu_cfg.get(
                    "webhook_trusted_proxies", [],
                ),
                verification_token=feishu_cfg.get("verification_token", ""),
                encrypt_key=feishu_cfg.get("encrypt_key", ""),
                bot_open_id=feishu_cfg.get("bot_open_id", ""),
                is_lark=feishu_cfg.get("is_lark", False),
                dm_only=feishu_cfg.get("dm_only", True),
                require_mention=feishu_cfg.get("require_mention", True),
                allow_all=feishu_cfg.get("allow_all", False),
                allowed_users=feishu_cfg.get("allowed_users", []),
                allowed_chats=feishu_cfg.get("allowed_chats", []),
                group_authorization_mode=feishu_cfg.get(
                    "group_authorization_mode",
                    "and",
                ),
                inbox_retry_max_attempts=feishu_cfg.get(
                    "inbox_retry_max_attempts", 5,
                ),
                inbox_retry_base_delay_seconds=feishu_cfg.get(
                    "inbox_retry_base_delay_seconds", 1.0,
                ),
                inbox_retry_max_delay_seconds=feishu_cfg.get(
                    "inbox_retry_max_delay_seconds", 60.0,
                ),
                inbox_retry_jitter_ratio=feishu_cfg.get(
                    "inbox_retry_jitter_ratio", 0.2,
                ),
                inbox_retry_poll_interval_seconds=feishu_cfg.get(
                    "inbox_retry_poll_interval_seconds", 1.0,
                ),
                inbox_retry_batch_size=feishu_cfg.get(
                    "inbox_retry_batch_size", 64,
                ),
                inbox_retention_seconds=(
                    runner.feishu_inbox_retention_seconds
                ),
                retention_cleanup_interval_seconds=(
                    runner.retention_cleanup_interval_seconds
                ),
                retention_cleanup_batch_size=(
                    runner.retention_cleanup_batch_size
                ),
                send_total_attempts=feishu_cfg.get(
                    "send_total_attempts",
                    feishu_cfg.get("send_max_retries", 3),
                ),
                send_retry_base_delay_seconds=feishu_cfg.get(
                    "send_retry_base_delay_seconds",
                    feishu_cfg.get("send_retry_base_delay", 0.5),
                ),
                send_retry_max_delay_seconds=feishu_cfg.get(
                    "send_retry_max_delay_seconds", 3.0,
                ),
                adapter_retry_after_max_seconds=feishu_cfg.get(
                    "adapter_retry_after_max_seconds", 5.0,
                ),
                send_rate_limit_per_chat=feishu_cfg.get(
                    "send_rate_limit_per_chat", 5,
                ),
                send_rate_limit_cache_idle_ttl_seconds=feishu_cfg.get(
                    "send_rate_limit_cache_idle_ttl_seconds", 600.0,
                ),
                send_rate_limit_max_tracked_chats=feishu_cfg.get(
                    "send_rate_limit_max_tracked_chats", 1024,
                ),
                file_transfer_config=runner.file_transfer_config,
            ))
            print("  [gateway] Feishu adapter added")
        except Exception as exc:
            print(
                "  [gateway] Feishu adapter failed to init: "
                f"{type(exc).__name__}"
            )

    # 个人微信
    weixin_cfg = platforms.get("weixin", {})
    if weixin_cfg.get("enabled", False):
        try:
            from hermes.gateway.adapters.weixin import WeixinAdapter
            runner.add_adapter(WeixinAdapter(
                account_id=weixin_cfg.get("account_id", ""),
                token=weixin_cfg.get("token", ""),
                base_url=weixin_cfg.get("base_url", ""),
                user_id=weixin_cfg.get("user_id", ""),
                allow_all=weixin_cfg.get("allow_all", False),
                allowed_users=weixin_cfg.get("allowed_users", []),
            ))
            print("  [gateway] Weixin adapter added")
        except Exception as exc:
            print(f"  [gateway] Weixin adapter failed to init: {exc!r}")

    if not runner.adapters:
        print("  [gateway] WARNING: no adapters enabled. Check config.yaml.")
        return

    try:
        # start() 内部严格分阶段；返回前飞书 Webhook 不会接收业务事件。
        await runner.start()

        # 等待所有 adapter 停止(CLI /quit 或信号)
        while runner.adapters:
            # CLI adapter 的 _should_quit 被设置后退出
            cli = runner.adapters.get("cli")
            if cli and hasattr(cli, "_should_quit") and cli._should_quit.is_set():
                break
            # 其他 adapter:检查 _running
            any_running = any(
                getattr(a, "_running", False) for a in runner.adapters.values()
            )
            if not any_running:
                break
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.stop()
        print("  [gateway] stopped")
