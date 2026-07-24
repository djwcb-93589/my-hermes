"""Gateway 出站文件工具的审批 Binding 校验。"""

from __future__ import annotations


class GatewaySendFileApprovalHandler:
    def validate_request_binding(self, *, arguments: dict, binding: dict, session_key: str) -> bool:
        snapshot = binding.get("file_snapshot")
        target = binding.get("target_identity")
        return (
            isinstance(arguments, dict)
            and isinstance(snapshot, dict)
            and isinstance(snapshot.get("abs_path"), str)
            and isinstance(snapshot.get("sha256"), str)
            and isinstance(target, dict)
            and isinstance(binding.get("platform"), str)
            and isinstance(binding.get("backend_risk"), dict)
            and bool(session_key)
        )

    def validate_grant_binding(self, **kwargs) -> bool:
        return self.validate_request_binding(**kwargs)
