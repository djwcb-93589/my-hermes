"""Skill 来源、所有权与治理授权。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .repository import SkillRepository


class SkillSource(str, Enum):
    LOCAL = "local"
    BUNDLED = "bundled"
    INSTALLED = "installed"
    EXTERNAL = "external"


class SkillOwner(str, Enum):
    USER = "user"
    CURATOR = "curator"
    SYSTEM = "system"
    EXTERNAL = "external"


class SkillActor(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND_REVIEW = "background_review"
    SYSTEM = "system"


@dataclass(frozen=True)
class SkillDescriptor:
    skill_id: str
    name: str
    source: SkillSource
    owner: SkillOwner
    pinned: bool
    relative_path: str
    revision: str


class SkillGovernance:
    """集中治理规则，存储细节仅通过 Repository 访问。"""

    def __init__(self, repository: SkillRepository):
        self.repository = repository

    @staticmethod
    def normalize_actor(actor: SkillActor | str) -> SkillActor | dict:
        if isinstance(actor, SkillActor):
            return actor
        try:
            return SkillActor(actor)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error_type": "invalid_context",
                "error": f"invalid skill actor: {actor!r}",
            }

    @staticmethod
    def _legacy_record() -> dict:
        return {
            "schema_version": 1,
            "source": SkillSource.LOCAL.value,
            "owner": SkillOwner.USER.value,
            "created_by": "legacy",
            "pinned": False,
        }

    @staticmethod
    def _validate_record(record: dict) -> dict:
        if record.get("schema_version") != 1:
            return {"ok": False, "error_type": "governance_invalid", "error": "unsupported governance schema version"}
        try:
            source = SkillSource(record["source"])
            owner = SkillOwner(record["owner"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error_type": "governance_invalid", "error": "governance source or owner is invalid"}
        if not isinstance(record.get("created_by"), str) or not record["created_by"]:
            return {"ok": False, "error_type": "governance_invalid", "error": "governance created_by is invalid"}
        if not isinstance(record.get("pinned"), bool):
            return {"ok": False, "error_type": "governance_invalid", "error": "governance pinned is invalid"}
        return {"ok": True, "source": source, "owner": owner}

    def load_record(self, name: str) -> dict:
        result = self.repository.load_governance_record(name)
        if not result.get("ok"):
            return result
        record = self._legacy_record() if result["legacy"] else result["record"]
        validation = self._validate_record(record)
        if not validation["ok"]:
            return {**validation, "name": name}
        return {"ok": True, "record": dict(record), "legacy": result["legacy"]}

    def describe(self, name: str) -> SkillDescriptor | dict:
        record_result = self.load_record(name)
        if not record_result.get("ok"):
            return record_result
        revision_result = self.repository.get_skill_revision(name)
        if not revision_result.get("ok"):
            return revision_result
        validation = self._validate_record(record_result["record"])
        return SkillDescriptor(
            skill_id=f"local:{name}",
            name=name,
            source=validation["source"],
            owner=validation["owner"],
            pinned=record_result["record"]["pinned"],
            relative_path=f"skills/{name}",
            revision=revision_result["revision"],
        )

    def authorize(self, *, descriptor: SkillDescriptor, actor: SkillActor, action: str) -> dict:
        if action == "read":
            return {"ok": True}
        if actor is SkillActor.SYSTEM:
            return {"ok": True}
        if action not in {"create", "edit", "patch", "delete", "adopt", "release", "pin", "unpin"}:
            return {"ok": False, "error_type": "permission_denied", "error": f"unsupported governance action: {action!r}"}
        if actor is SkillActor.BACKGROUND_REVIEW:
            if action in {"adopt", "release", "pin", "unpin", "create"}:
                return {"ok": False, "error_type": "permission_denied", "error": "background review cannot perform governance actions"}
            if descriptor.source is not SkillSource.LOCAL or descriptor.owner is not SkillOwner.CURATOR:
                return {"ok": False, "error_type": "permission_denied", "error": "background review may only modify local curator skills"}
            if descriptor.pinned:
                return {"ok": False, "error_type": "permission_denied", "error": "background review cannot modify pinned skills"}
            return {"ok": True}
        if descriptor.source is not SkillSource.LOCAL:
            return {"ok": False, "error_type": "permission_denied", "error": "foreground may only modify local skills"}
        if action == "delete" and descriptor.pinned:
            return {"ok": False, "error_type": "permission_denied", "error": "pinned skills must be unpinned before deletion"}
        return {"ok": True}

    def authorize_create(self, actor: SkillActor) -> dict:
        if actor in {SkillActor.FOREGROUND, SkillActor.BACKGROUND_REVIEW, SkillActor.SYSTEM}:
            return {"ok": True}
        return {"ok": False, "error_type": "permission_denied", "error": "actor cannot create skills"}

    def creation_record(self, actor: SkillActor) -> dict:
        if actor is SkillActor.BACKGROUND_REVIEW:
            owner = SkillOwner.CURATOR
        elif actor is SkillActor.SYSTEM:
            owner = SkillOwner.SYSTEM
        else:
            owner = SkillOwner.USER
        return {
            "schema_version": 1,
            "source": SkillSource.LOCAL.value,
            "owner": owner.value,
            "created_by": actor.value,
            "pinned": False,
        }

    def can_curate(self, descriptor: SkillDescriptor) -> bool:
        """表示后台策展 actor 是否可对该 Skill 执行写操作。"""
        return self.authorize(
            descriptor=descriptor,
            actor=SkillActor.BACKGROUND_REVIEW,
            action="edit",
        )["ok"]
