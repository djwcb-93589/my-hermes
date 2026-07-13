"""
统一 Gateway 启动入口。

根据 ``config['gateway']['platforms']`` 创建 CLI / Feishu / Weixin adapter。
单个平台连接失败不阻止其他平台。关闭时停止全部 adapter + 清理 backend。
"""

from __future__ import annotations

import asyncio
import signal

from hermes.config import _config, DB_PATH, MODEL
from hermes.gateway.runner import GatewayRunner
from hermes.prompt import build_system_prompt


async def run_gateway():
    """根据配置启动多平台 Gateway。"""
    print(f"=== Hermes Gateway ===")
    print(f"Model: {MODEL}")

    # Gateway 仅保留基础会话能力,不向模型注册任何工具。

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
            runner.add_adapter(FeishuAdapter(
                app_id=feishu_cfg.get("app_id", ""),
                app_secret=feishu_cfg.get("app_secret", ""),
                db_path=DB_PATH,
                webhook_host=feishu_cfg.get("webhook_host", "0.0.0.0"),
                webhook_port=feishu_cfg.get("webhook_port", 8787),
                verification_token=feishu_cfg.get("verification_token", ""),
                encrypt_key=feishu_cfg.get("encrypt_key", ""),
                bot_open_id=feishu_cfg.get("bot_open_id", ""),
                is_lark=feishu_cfg.get("is_lark", False),
                dm_only=feishu_cfg.get("dm_only", True),
                require_mention=feishu_cfg.get("require_mention", True),
                allow_all=feishu_cfg.get("allow_all", False),
                allowed_users=feishu_cfg.get("allowed_users", []),
                allowed_chats=feishu_cfg.get("allowed_chats", []),
                send_max_retries=feishu_cfg.get("send_max_retries", 3),
                send_retry_base_delay=feishu_cfg.get("send_retry_base_delay", 1.0),
            ))
            print("  [gateway] Feishu adapter added")
        except Exception as exc:
            print(f"  [gateway] Feishu adapter failed to init: {exc!r}")

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

    await runner.start()

    # 等待所有 adapter 停止(CLI /quit 或信号)
    try:
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
