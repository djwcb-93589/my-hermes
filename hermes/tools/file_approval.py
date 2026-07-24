"""File 工具的审批 Binding 校验。"""

from __future__ import annotations

from hermes.file_state import normalize_file_state_snapshot


class FileApprovalHandler:
    def validate_request_binding(self, *, arguments: dict, binding: dict, session_key: str) -> bool:
        try:
            if not isinstance(arguments, dict) or not isinstance(binding.get("abs_path"), str):
                return False
            if not isinstance(binding.get("risk_level"), str) or not isinstance(binding.get("backend_risk"), dict):
                return False
            snapshot = binding.get("file_snapshot")
            if snapshot is not None:
                normalize_file_state_snapshot(snapshot)
            return bool(session_key)
        except (TypeError, ValueError):
            return False

    def validate_grant_binding(self, **kwargs) -> bool:
        return self.validate_request_binding(**kwargs)
