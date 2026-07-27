"""Skill 业务编排、风险扫描和治理授权。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from hermes.skill_security import get_skill_trust_state, scan_skill_content
from .governance import SkillActor, SkillDescriptor, SkillGovernance, SkillOwner, SkillSource
from .repository import SkillRepository


class SkillService:
    """组合 Repository、Governance 与既有的安全读取规则。"""

    def __init__(
        self,
        repository: SkillRepository | None = None,
        governance: SkillGovernance | None = None,
    ):
        self.repository = repository or SkillRepository()
        self.governance = governance or SkillGovernance(self.repository)

    def set_skills_dir(self, skills_dir: Path) -> None:
        """供工具适配层同步其兼容的 SKILLS_DIR 覆盖。"""
        self.repository.skills_dir = skills_dir

    def _actor(self, actor: SkillActor | str) -> SkillActor | dict:
        return self.governance.normalize_actor(actor)

    def _descriptor_fields(self, descriptor: SkillDescriptor) -> dict:
        return {
            "skill_id": descriptor.skill_id,
            "source": descriptor.source.value,
            "owner": descriptor.owner.value,
            "pinned": descriptor.pinned,
            "revision": descriptor.revision,
            "can_curate": self.governance.can_curate(descriptor),
        }

    def _describe_authorized(self, name: str, actor: SkillActor, action: str) -> SkillDescriptor | dict:
        descriptor = self.governance.describe(name)
        if isinstance(descriptor, dict):
            return descriptor
        authorization = self.governance.authorize(descriptor=descriptor, actor=actor, action=action)
        if not authorization["ok"]:
            return {**authorization, "name": name}
        return descriptor

    def list_skills(self, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        skills = self.repository.discover()
        for skill in skills:
            directory_name = skill["relative_path"].rsplit("/", 1)[-1]
            descriptor = self.governance.describe(directory_name)
            if isinstance(descriptor, dict):
                skill["governance_error"] = descriptor.get("error", "governance record is invalid")
                continue
            authorization = self.governance.authorize(descriptor=descriptor, actor=resolved_actor, action="read")
            if not authorization["ok"]:
                skill["governance_error"] = authorization["error"]
                continue
            skill.update(self._descriptor_fields(descriptor))
        return {"ok": True, "skills": skills, "count": len(skills)}

    def load_skill_body(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        payload = self.repository.load(name)
        if not payload.get("ok"):
            return payload
        descriptor = self._describe_authorized(name, resolved_actor, "read")
        governance_error = None
        if isinstance(descriptor, dict):
            if resolved_actor is not SkillActor.FOREGROUND or descriptor.get("error_type") != "governance_invalid":
                return descriptor
            governance_error = descriptor.get("error", "governance record is invalid")
        content = payload.pop("content")
        report = scan_skill_content(payload["body"])
        trust = get_skill_trust_state(name, content)
        payload.update(
            risk={"level": report.risk_level, "findings": [asdict(item) for item in report.findings]},
            trusted=trust.trusted,
            trust_stale=trust.trust_stale,
        )
        if governance_error is not None:
            payload["governance_error"] = governance_error
        else:
            payload.update(self._descriptor_fields(descriptor))
        return payload

    def view_skill(
        self,
        name: str,
        *,
        actor: SkillActor | str = SkillActor.FOREGROUND,
        interactive_approval: bool | None = None,
    ) -> dict:
        payload = self.load_skill_body(name, actor=actor)
        if not payload.get("ok"):
            return payload
        common = {
            key: payload[key]
            for key in (
                "name", "relative_path", "risk", "trusted", "trust_stale", "skill_id",
                "source", "owner", "pinned", "revision", "can_curate",
            )
            if key in payload
        }
        if interactive_approval is False:
            if payload["risk"]["level"] == "high":
                return {
                    "ok": False,
                    "error_type": "safety_blocked",
                    "error": "high-risk skill is blocked in unattended mode",
                    "status": "blocked",
                    "requires_confirmation": True,
                    **common,
                }
            if payload["risk"]["level"] == "medium" and not payload["trusted"]:
                return {
                    "ok": False,
                    "error_type": "permission_denied",
                    "error": "untrusted medium-risk skill requires interactive confirmation",
                    "status": "confirmation_required",
                    "requires_confirmation": True,
                    **common,
                }
        return payload

    def manage_skill(
        self,
        action: str,
        name: str,
        *,
        actor: SkillActor | str = SkillActor.FOREGROUND,
        **kwargs,
    ) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        if action == "create":
            authorization = self.governance.authorize_create(resolved_actor)
            if not authorization["ok"]:
                return authorization
            return self.repository.create(
                name,
                governance_record=self.governance.creation_record(resolved_actor),
                **kwargs,
            )
        if action in {"adopt", "release", "pin", "unpin"}:
            return getattr(self, f"{action}_skill")(name, actor=resolved_actor)
        if action not in {"edit", "patch", "delete"}:
            return {"ok": False, "error_type": "unknown_action", "error": f"unknown action: {action!r}"}
        descriptor = self._describe_authorized(name, resolved_actor, action)
        if isinstance(descriptor, dict):
            return descriptor
        if action == "edit":
            return self.repository.edit(name, **kwargs)
        if action == "patch":
            return self.repository.patch(name, **kwargs)
        return self.repository.delete(name)

    def _change_governance(self, name: str, *, actor: SkillActor | str, action: str) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        descriptor = self._describe_authorized(name, resolved_actor, action)
        if isinstance(descriptor, dict):
            return descriptor
        record_result = self.governance.load_record(name)
        if not record_result.get("ok"):
            return record_result
        record = record_result["record"]
        if action == "adopt":
            if descriptor.source is not SkillSource.LOCAL:
                return {"ok": False, "error_type": "permission_denied", "error": "only local skills can be adopted", "name": name}
            if descriptor.owner is SkillOwner.CURATOR:
                return {"ok": True, "name": name, "action": action, **self._descriptor_fields(descriptor)}
            if descriptor.owner is not SkillOwner.USER:
                return {"ok": False, "error_type": "permission_denied", "error": "only user-owned skills can be adopted", "name": name}
            record["owner"] = SkillOwner.CURATOR.value
            record["adopted_by"] = resolved_actor.value
        elif action == "release":
            if descriptor.owner is SkillOwner.USER:
                return {"ok": True, "name": name, "action": action, **self._descriptor_fields(descriptor)}
            if descriptor.owner is not SkillOwner.CURATOR:
                return {"ok": False, "error_type": "permission_denied", "error": "only curator-owned skills can be released", "name": name}
            record["owner"] = SkillOwner.USER.value
        elif action == "pin":
            if descriptor.pinned:
                return {"ok": True, "name": name, "action": action, **self._descriptor_fields(descriptor)}
            record["pinned"] = True
        elif action == "unpin":
            if not descriptor.pinned:
                return {"ok": True, "name": name, "action": action, **self._descriptor_fields(descriptor)}
            record["pinned"] = False
        result = self.repository.write_governance_record(name, record)
        if not result.get("ok"):
            return result
        updated = self.governance.describe(name)
        if isinstance(updated, dict):
            return updated
        return {"ok": True, "name": name, "action": action, **self._descriptor_fields(updated)}

    def adopt_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        return self._change_governance(name, actor=actor, action="adopt")

    def release_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        return self._change_governance(name, actor=actor, action="release")

    def pin_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        return self._change_governance(name, actor=actor, action="pin")

    def unpin_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        return self._change_governance(name, actor=actor, action="unpin")

    def render_skills_section(self) -> str | None:
        skills = self.list_skills()["skills"]
        if not skills:
            return None
        return "# Available Skills\n" + "\n".join(
            f"- **{item['name']}**: {item['description']}" for item in skills
        )
