"""Gateway Outbox delivery kind 的共享分类合同。"""

from __future__ import annotations


CRON_SYSTEM_OUTBOX_DELIVERY_KIND_PREFIX = "cron_"
SYSTEM_NOTIFICATION_DELIVERY_KIND = "system_notification"
CLAUDE_CODE_WATCH_REGISTRATION_NOTICE_DELIVERY_KIND_PREFIX = (
    "claude_code_watch_registration_notice:"
)


def is_gateway_system_outbox_delivery_kind(delivery_kind: object) -> bool:
    """判断 delivery kind 是否只能走独立 system Outbox 投递路径。"""

    if not isinstance(delivery_kind, str):
        return False
    return (
        delivery_kind.startswith(CRON_SYSTEM_OUTBOX_DELIVERY_KIND_PREFIX)
        or delivery_kind == SYSTEM_NOTIFICATION_DELIVERY_KIND
        or delivery_kind.startswith(
            CLAUDE_CODE_WATCH_REGISTRATION_NOTICE_DELIVERY_KIND_PREFIX,
        )
    )


def non_system_gateway_outbox_sql_clause() -> tuple[str, tuple[str, str, str]]:
    """返回 SQL 中排除 system Outbox 的固定条件和绑定参数。"""

    return (
        """
        AND delivery_kind NOT GLOB ?
        AND delivery_kind<>?
        AND delivery_kind NOT GLOB ?
        """,
        (
            f"{CRON_SYSTEM_OUTBOX_DELIVERY_KIND_PREFIX}*",
            SYSTEM_NOTIFICATION_DELIVERY_KIND,
            f"{CLAUDE_CODE_WATCH_REGISTRATION_NOTICE_DELIVERY_KIND_PREFIX}*",
        ),
    )


__all__ = [
    "CLAUDE_CODE_WATCH_REGISTRATION_NOTICE_DELIVERY_KIND_PREFIX",
    "CRON_SYSTEM_OUTBOX_DELIVERY_KIND_PREFIX",
    "SYSTEM_NOTIFICATION_DELIVERY_KIND",
    "is_gateway_system_outbox_delivery_kind",
    "non_system_gateway_outbox_sql_clause",
]
