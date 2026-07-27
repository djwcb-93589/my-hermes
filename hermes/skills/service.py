"""Skill 业务编排、风险扫描、治理授权与 package 文件服务。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from hermes.skill_security import get_skill_trust_state, scan_skill_content
from .governance import SkillActor, SkillDescriptor, SkillGovernance, SkillManagedBy, SkillSource
from .repository import SkillRepository


class SkillService:
    """唯一业务入口：组合 Repository、Governance 和读取安全策略。"""

    def __init__(
        self,
        repository: SkillRepository | None = None,
        governance: SkillGovernance | None = None,
    ):
        self.repository = repository or SkillRepository()
        self.governance = governance or SkillGovernance(self.repository)

    def set_skills_dir(self, skills_dir: Path) -> None:
        """供工具适配层同步兼容的 SKILLS_DIR 覆盖。"""
        self.repository.skills_dir = skills_dir

    def _actor(self, actor: SkillActor | str) -> SkillActor | dict:
        return self.governance.normalize_actor(actor)

    @staticmethod
    def _revision_conflict(name: str) -> dict:
        return {"ok": False, "error_type": "revision_conflict", "error": "skill content changed concurrently", "name": name}

    @staticmethod
    def _governance_conflict(name: str) -> dict:
        return {"ok": False, "error_type": "governance_conflict", "error": "governance record changed concurrently", "name": name}

    def _descriptor_fields(self, descriptor: SkillDescriptor) -> dict:
        return {
            "skill_id": descriptor.skill_id,
            "source": descriptor.source.value,
            "managed_by": descriptor.managed_by.value,
            "pinned": descriptor.pinned,
            "revision": descriptor.revision,
            "governance_revision": descriptor.governance_revision,
            "can_curate": self.governance.can_curate(descriptor),
        }

    def _authorize_loaded(
        self,
        name: str,
        *,
        actor: SkillActor,
        action: str,
        expected_revision: str | None = None,
        expected_governance_revision: str | None = None,
    ) -> tuple[dict, SkillDescriptor] | dict:
        payload = self.repository.load(name)
        if not payload.get("ok"):
            return payload
        if expected_revision is not None and payload["revision"] != expected_revision:
            return self._revision_conflict(name)
        descriptor = self.governance.describe(name, revision=payload["revision"])
        if isinstance(descriptor, dict):
            return descriptor
        if expected_governance_revision is not None and descriptor.governance_revision != expected_governance_revision:
            return self._governance_conflict(name)
        authorization = self.governance.authorize(descriptor=descriptor, actor=actor, action=action)
        if not authorization["ok"]:
            return {**authorization, "name": name}
        return payload, descriptor

    def list_skills(self, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        skills = self.repository.discover()
        for skill in skills:
            directory_name = skill["relative_path"].rsplit("/", 1)[-1]
            descriptor = self.governance.describe(directory_name, revision=skill["revision"])
            if isinstance(descriptor, dict):
                skill["governance_error"] = descriptor.get("error", "governance record is invalid")
                skill["support_files"] = []
                continue
            authorization = self.governance.authorize(descriptor=descriptor, actor=resolved_actor, action="read")
            if not authorization["ok"]:
                skill["governance_error"] = authorization["error"]
                skill["support_files"] = []
                continue
            skill.update(self._descriptor_fields(descriptor))
            support_files = self.repository.list_support_files(directory_name)
            skill["support_files"] = support_files.get("support_files", [])
            if not support_files.get("ok"):
                skill["support_files_error"] = support_files.get("error", "support file discovery failed")
        return {"ok": True, "skills": skills, "count": len(skills)}

    def load_skill_body(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        """兼容入口：读取正文、风险和治理字段。"""
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        payload = self.repository.load(name)
        if not payload.get("ok"):
            return payload
        descriptor = self.governance.describe(name, revision=payload["revision"])
        governance_error = None
        if isinstance(descriptor, dict):
            if resolved_actor is not SkillActor.FOREGROUND or descriptor.get("error_type") != "governance_invalid":
                return descriptor
            governance_error = descriptor.get("error", "governance record is invalid")
        else:
            authorization = self.governance.authorize(descriptor=descriptor, actor=resolved_actor, action="read")
            if not authorization["ok"]:
                return {**authorization, "name": name}
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

    def read_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        return self.load_skill_body(name, actor=actor)

    def view_skill(
        self,
        name: str,
        *,
        actor: SkillActor | str = SkillActor.FOREGROUND,
        interactive_approval: bool | None = None,
    ) -> dict:
        payload = self.read_skill(name, actor=actor)
        if not payload.get("ok"):
            return payload
        common = {
            key: payload[key]
            for key in (
                "name", "relative_path", "risk", "trusted", "trust_stale", "skill_id", "source",
                "managed_by", "pinned", "revision", "governance_revision", "can_curate",
            )
            if key in payload
        }
        if interactive_approval is False:
            if payload["risk"]["level"] == "high":
                return {"ok": False, "error_type": "safety_blocked", "error": "high-risk skill is blocked in unattended mode",
                        "status": "blocked", "requires_confirmation": True, **common}
            if payload["risk"]["level"] == "medium" and not payload["trusted"]:
                return {"ok": False, "error_type": "permission_denied", "error": "untrusted medium-risk skill requires interactive confirmation",
                        "status": "confirmation_required", "requires_confirmation": True, **common}
        return payload

    def read_support_file(self, name: str, relative_path: str, *, actor: SkillActor | str = SkillActor.FOREGROUND) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        authorized = self._authorize_loaded(name, actor=resolved_actor, action="read")
        if isinstance(authorized, dict):
            return authorized
        _, descriptor = authorized
        result = self.repository.read_support_file(name, relative_path)
        if not result.get("ok"):
            return result
        governance_fields = self._descriptor_fields(descriptor)
        governance_fields.pop("revision")
        result.update(governance_fields)
        return result

    def create_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND, **kwargs) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        authorization = self.governance.authorize_create(resolved_actor)
        if not authorization["ok"]:
            return authorization
        return self.repository.create(name, governance_record=self.governance.creation_record(resolved_actor), **kwargs)

    def edit_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                   expected_revision: str | None = None, expected_governance_revision: str | None = None, **kwargs) -> dict:
        return self._content_operation(name, actor=actor, action="edit", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision, **kwargs)

    def patch_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                    expected_revision: str | None = None, expected_governance_revision: str | None = None, **kwargs) -> dict:
        return self._content_operation(name, actor=actor, action="patch", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision, **kwargs)

    def delete_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                     expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._content_operation(name, actor=actor, action="delete", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision)

    def write_support_file(self, name: str, relative_path: str, content: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                           expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._content_operation(name, actor=actor, action="write_file", relative_path=relative_path, content=content,
                                       expected_revision=expected_revision, expected_governance_revision=expected_governance_revision)

    def remove_support_file(self, name: str, relative_path: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                            expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._content_operation(name, actor=actor, action="remove_file", relative_path=relative_path,
                                       expected_revision=expected_revision, expected_governance_revision=expected_governance_revision)

    def _content_operation(self, name: str, *, actor: SkillActor | str, action: str,
                           expected_revision: str | None, expected_governance_revision: str | None, **kwargs) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        authorized = self._authorize_loaded(name, actor=resolved_actor, action=action,
                                            expected_revision=expected_revision,
                                            expected_governance_revision=expected_governance_revision)
        if isinstance(authorized, dict):
            return authorized
        _, descriptor = authorized
        common = {"expected_governance_revision": descriptor.governance_revision, "expected_revision": expected_revision}
        if action == "edit":
            return self.repository.edit(name, **common, **kwargs)
        if action == "patch":
            return self.repository.patch(name, **common, **kwargs)
        if action == "delete":
            return self.repository.delete(name, **common)
        if action == "write_file":
            return self.repository.write_support_file(name, kwargs["relative_path"], kwargs["content"], **common)
        return self.repository.remove_support_file(name, kwargs["relative_path"], **common)

    def manage_skill(self, action: str, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND, **kwargs) -> dict:
        if action == "create":
            return self.create_skill(name, actor=actor, **kwargs)
        if action == "edit":
            return self.edit_skill(name, actor=actor, **kwargs)
        if action == "patch":
            return self.patch_skill(name, actor=actor, **kwargs)
        if action == "delete":
            return self.delete_skill(name, actor=actor,
                                     expected_revision=kwargs.get("expected_revision"),
                                     expected_governance_revision=kwargs.get("expected_governance_revision"))
        if action == "write_file":
            return self.write_support_file(name, kwargs.get("relative_path", ""), kwargs.get("content", ""), actor=actor,
                                           expected_revision=kwargs.get("expected_revision"),
                                           expected_governance_revision=kwargs.get("expected_governance_revision"))
        if action == "remove_file":
            return self.remove_support_file(name, kwargs.get("relative_path", ""), actor=actor,
                                            expected_revision=kwargs.get("expected_revision"),
                                            expected_governance_revision=kwargs.get("expected_governance_revision"))
        return {"ok": False, "error_type": "unknown_action", "error": f"unknown action: {action!r}"}

    def _change_governance(self, name: str, *, actor: SkillActor | str, action: str,
                           expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        resolved_actor = self._actor(actor)
        if isinstance(resolved_actor, dict):
            return resolved_actor
        authorized = self._authorize_loaded(name, actor=resolved_actor, action=action,
                                            expected_revision=expected_revision,
                                            expected_governance_revision=expected_governance_revision)
        if isinstance(authorized, dict):
            return authorized
        payload, descriptor = authorized
        record_result = self.governance.load_record(name)
        if not record_result.get("ok"):
            return record_result
        record = record_result["record"]
        no_op = False
        if action == "adopt":
            if descriptor.source is not SkillSource.LOCAL:
                return {"ok": False, "error_type": "permission_denied", "error": "only local skills can be adopted", "name": name}
            if descriptor.managed_by is SkillManagedBy.CURATOR:
                no_op = True
            elif descriptor.managed_by is not SkillManagedBy.USER:
                return {"ok": False, "error_type": "permission_denied", "error": "only user-managed skills can be adopted", "name": name}
            else:
                record["managed_by"] = SkillManagedBy.CURATOR.value
                record["adopted_by"] = resolved_actor.value
        elif action == "pin":
            no_op = descriptor.pinned
            if not no_op:
                record["pinned"] = True
        else:
            no_op = not descriptor.pinned
            if not no_op:
                record["pinned"] = False
        if no_op:
            confirmed = self.repository.validate_governance_revision(
                name,
                expected_governance_revision=descriptor.governance_revision,
            )
            if not confirmed.get("ok"):
                return confirmed
            return {"ok": True, "name": name, "action": action, **self._descriptor_fields(descriptor)}
        result = self.repository.write_governance_record(name, record, expected_governance_revision=descriptor.governance_revision)
        if not result.get("ok"):
            return result
        updated = self.governance.describe(name, revision=payload["revision"])
        if isinstance(updated, dict):
            return updated
        return {"ok": True, "name": name, "action": action, **self._descriptor_fields(updated)}

    def adopt_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                    expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._change_governance(name, actor=actor, action="adopt", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision)

    def pin_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                  expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._change_governance(name, actor=actor, action="pin", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision)

    def unpin_skill(self, name: str, *, actor: SkillActor | str = SkillActor.FOREGROUND,
                    expected_revision: str | None = None, expected_governance_revision: str | None = None) -> dict:
        return self._change_governance(name, actor=actor, action="unpin", expected_revision=expected_revision,
                                       expected_governance_revision=expected_governance_revision)

    def render_skills_section(self) -> str | None:
        skills = self.list_skills()["skills"]
        if not skills:
            return None
        return "# Available Skills\n" + "\n".join(f"- **{item['name']}**: {item['description']}" for item in skills)
