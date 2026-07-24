"""Cron 工具的审批 Binding 校验。"""

from __future__ import annotations


class CronApprovalHandler:
    def validate_request_binding(self, *, arguments: dict, binding: dict, session_key: str) -> bool:
        return (
            isinstance(arguments, dict)
            and isinstance(binding.get("cron_action"), str)
            and isinstance(binding.get("scope_digest"), str)
            and isinstance(binding.get("prompt_digest"), str)
            and isinstance(binding.get("backend_risk"), dict)
            and isinstance(binding.get("risk_level"), str)
            and bool(session_key)
        )

    def validate_grant_binding(self, **kwargs) -> bool:
        return self.validate_request_binding(**kwargs)
