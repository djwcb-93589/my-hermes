"""Skill 业务编排与读取安全策略。"""

from __future__ import annotations

from dataclasses import asdict

from hermes.skill_security import get_skill_trust_state, scan_skill_content
from .repository import SkillRepository


class SkillService:
    """组合存储、风险扫描和无人值守读取规则。"""

    def __init__(self, repository: SkillRepository | None = None):
        self.repository = repository or SkillRepository()

    def list_skills(self) -> dict:
        skills = self.repository.discover()
        return {"ok": True, "skills": skills, "count": len(skills)}

    def load_skill_body(self, name: str) -> dict:
        payload = self.repository.load(name)
        if not payload.get("ok"):
            return payload
        content = payload.pop("content")
        report = scan_skill_content(payload["body"])
        trust = get_skill_trust_state(name, content)
        payload.update(risk={"level": report.risk_level, "findings": [asdict(item) for item in report.findings]},
                       trusted=trust.trusted, trust_stale=trust.trust_stale)
        return payload

    def view_skill(self, name: str, *, interactive_approval: bool | None = None) -> dict:
        payload = self.load_skill_body(name)
        if not payload.get("ok"):
            return payload
        common = {key: payload[key] for key in ("name", "relative_path", "risk", "trusted", "trust_stale")}
        if interactive_approval is False:
            if payload["risk"]["level"] == "high":
                return {"ok": False, "error_type": "safety_blocked", "error": "high-risk skill is blocked in unattended mode",
                        "status": "blocked", "requires_confirmation": True, **common}
            if payload["risk"]["level"] == "medium" and not payload["trusted"]:
                return {"ok": False, "error_type": "permission_denied", "error": "untrusted medium-risk skill requires interactive confirmation",
                        "status": "confirmation_required", "requires_confirmation": True, **common}
        return payload

    def manage_skill(self, action: str, name: str, **kwargs) -> dict:
        operations = {"create": self.repository.create, "edit": self.repository.edit,
                      "delete": self.repository.delete, "patch": self.repository.patch}
        operation = operations.get(action)
        if operation is None:
            return {"ok": False, "error_type": "unknown_action", "error": f"unknown action: {action!r}"}
        return operation(name, **kwargs)

    def render_skills_section(self) -> str | None:
        skills = self.repository.discover()
        if not skills:
            return None
        return "# Available Skills\n" + "\n".join(f"- **{item['name']}**: {item['description']}" for item in skills)
