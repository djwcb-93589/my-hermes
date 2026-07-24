"""Browser 工具的审批 Binding 校验。"""

from __future__ import annotations


class BrowserApprovalHandler:
    def validate_request_binding(self, *, arguments: dict, binding: dict, session_key: str) -> bool:
        context = binding.get("browser_context")
        if not isinstance(arguments, dict) or not isinstance(context, dict) or not bool(session_key):
            return False
        if any(key in context for key in ("path", "abs_path", "artifact_dir", "workspace_root", "parent_abs_path")):
            return False
        snapshots = binding.get("media_snapshots")
        if snapshots is not None and (not isinstance(snapshots, list) or not snapshots):
            return False
        return isinstance(binding.get("backend_risk"), dict) and isinstance(binding.get("risk_level"), str)

    def validate_grant_binding(self, **kwargs) -> bool:
        return self.validate_request_binding(**kwargs)
