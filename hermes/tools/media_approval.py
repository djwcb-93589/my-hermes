"""Media 工具的审批 Binding 校验。"""

from __future__ import annotations

from hermes.file_state import normalize_file_state_snapshot


class MediaApprovalHandler:
    def validate_request_binding(self, *, arguments: dict, binding: dict, session_key: str) -> bool:
        try:
            snapshots = binding.get("media_snapshots")
            if not isinstance(arguments, dict) or not isinstance(snapshots, list) or not snapshots:
                return False
            for snapshot in snapshots:
                normalized = normalize_file_state_snapshot(snapshot)
                if not normalized.get("exists") or normalized.get("file_type") != "file":
                    return False
            return all(isinstance(binding.get(key), str) and binding[key] for key in ("provider", "model", "risk_level")) and isinstance(binding.get("backend_risk"), dict) and bool(session_key)
        except (TypeError, ValueError):
            return False

    def validate_grant_binding(self, **kwargs) -> bool:
        return self.validate_request_binding(**kwargs)
