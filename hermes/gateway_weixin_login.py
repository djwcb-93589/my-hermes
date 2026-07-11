"""
个人微信二维码登录(独立脚本)。

用法:
    python -m hermes.gateway_weixin_login

或:
    python main.py --weixin-login

成功后凭据写入 ``<HERMES_HOME>/weixin_creds.json``,后续 Gateway 启动
WeixinAdapter 时自动读取。
"""

from __future__ import annotations

import asyncio

from hermes.gateway.adapters.weixin import login_with_qr


async def _main():
    print("=== Weixin QR Login ===")
    print("Scan the QR code with your WeChat app.\n")
    creds = await login_with_qr(display_qr=True)
    if creds:
        print(f"\nLogin successful. account_id={creds['account_id']}")
        print(f"Credentials saved. You can now enable weixin in config.yaml.")
    else:
        print("\nLogin failed.")
    return creds


def run():
    """命令行入口。"""
    asyncio.run(_main())


if __name__ == "__main__":
    run()
