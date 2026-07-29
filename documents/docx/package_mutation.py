"""受控 DOCX package replacement、addition 与 deletion 计划。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DocxError
from .package import DocxPackage
from .parts import normalize_part_name


@dataclass(frozen=True)
class PackageMutation:
    """描述一次不向公共 API 暴露的全有或全无 package 修改。"""

    replacements: dict[str, bytes]
    additions: dict[str, bytes]
    deletions: frozenset[str] = field(default_factory=frozenset)


def validate_package_mutation(
    package: DocxPackage,
    mutation: PackageMutation,
) -> None:
    """根据源 package 校验修改集合、part 路径和存在性。"""

    if not isinstance(mutation, PackageMutation):
        raise DocxError("package_mutation_conflict", "PackageMutation 类型无效。")
    groups = (
        mutation.replacements,
        mutation.additions,
    )
    if not all(isinstance(group, dict) for group in groups) or not isinstance(
        mutation.deletions,
        frozenset,
    ):
        raise DocxError("package_mutation_conflict", "PackageMutation 集合类型无效。")

    replacements = set(mutation.replacements)
    additions = set(mutation.additions)
    deletions = set(mutation.deletions)
    if (
        replacements.intersection(additions)
        or replacements.intersection(deletions)
        or additions.intersection(deletions)
    ):
        raise DocxError(
            "package_mutation_conflict",
            "同一个 DOCX part 不能同时替换、添加或删除。",
        )

    for part_name, payload in (
        *mutation.replacements.items(),
        *mutation.additions.items(),
    ):
        if normalize_part_name(part_name) != part_name or not isinstance(
            payload,
            bytes,
        ):
            raise DocxError(
                "package_mutation_conflict",
                "PackageMutation 包含无效的 part 或 payload。",
            )
    for part_name in mutation.deletions:
        if normalize_part_name(part_name) != part_name:
            raise DocxError(
                "package_mutation_conflict",
                "PackageMutation 包含无效的删除目标。",
            )

    existing = set(package.part_names)
    if not replacements.issubset(existing):
        raise DocxError(
            "package_mutation_conflict",
            "replacement 目标在源 DOCX 中不存在。",
        )
    if additions.intersection(existing):
        raise DocxError(
            "package_mutation_conflict",
            "addition 目标已经存在于源 DOCX。",
        )
    if not deletions.issubset(existing):
        raise DocxError(
            "package_mutation_conflict",
            "deletion 目标在源 DOCX 中不存在。",
        )


def verify_package_mutation(
    source_package: DocxPackage,
    output_package: DocxPackage,
    mutation: PackageMutation,
) -> None:
    """逐 part 验证 replacement、addition 与未修改字节保真。"""

    validate_package_mutation(source_package, mutation)
    expected_names = (
        set(source_package.part_names) - set(mutation.deletions)
    ) | set(mutation.additions)
    if set(output_package.part_names) != expected_names:
        raise DocxError(
            "edit_verification_failed",
            "修改后的 DOCX part 集合与 PackageMutation 不一致。",
        )
    for part_name in expected_names:
        if part_name in mutation.additions:
            expected_payload = mutation.additions[part_name]
        elif part_name in mutation.replacements:
            expected_payload = mutation.replacements[part_name]
        else:
            expected_payload = source_package.read_part_bytes(part_name)
        if output_package.read_part_bytes(part_name) != expected_payload:
            raise DocxError(
                "edit_verification_failed",
                "修改后的 DOCX part 字节与 PackageMutation 不一致。",
            )
