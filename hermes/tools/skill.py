"""
Skill system: discovery, view, manage.

Skills are directories under HERMES_HOME/skills/<name>/SKILL.md with YAML
frontmatter (name, description) and a Markdown body.
"""

from __future__ import annotations

import shutil

from hermes.config import HERMES_HOME


SKILLS_DIR = HERMES_HOME / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse SKILL.md frontmatter and body."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    metadata = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()

    return metadata, parts[2].strip()


def _render_skill(name: str, description: str, body: str) -> str:
    """Render frontmatter + body into SKILL.md content."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def discover_skills() -> list[dict]:
    """Scan the skills directory and return a list of skill summaries."""
    if not SKILLS_DIR.exists():
        return []

    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        metadata, _ = _parse_frontmatter(
            skill_file.read_text(encoding="utf-8")
        )
        skills.append({
            "name": metadata.get("name", skill_dir.name),
            "description": metadata.get("description", ""),
        })

    return skills


def handle_skill_view(args, **kwargs):
    """Load and return the full content of a skill by name."""
    skill_file = SKILLS_DIR / args.get("name", "") / "SKILL.md"
    if not skill_file.exists():
        return "(error: skill not found)"

    metadata, body = _parse_frontmatter(
        skill_file.read_text(encoding="utf-8")
    )
    return (
        f"=== Skill: {metadata.get('name', '')} ===\n"
        f"{metadata.get('description', '')}\n\n{body}"
    )


def handle_skill_manage(args, **kwargs):
    """Manage skills: create / edit / delete."""
    action = args.get("action", "")
    name = args.get("name", "")

    if not name:
        return "(error: name required)"

    skill_dir = SKILLS_DIR / name
    skill_file = skill_dir / "SKILL.md"

    if action == "create":
        if skill_file.exists():
            return "(error: exists)"
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = _render_skill(
            name,
            args.get("description", ""),
            args.get("body", ""),
        )
        skill_file.write_text(content, encoding="utf-8")
        return f"Created '{name}'."

    elif action == "edit":
        if not skill_file.exists():
            return "(error: not found)"
        metadata, old_body = _parse_frontmatter(
            skill_file.read_text(encoding="utf-8")
        )
        new_description = (
            args.get("description")
            or metadata.get("description", "")
        )
        new_body = args.get("body") or old_body
        content = _render_skill(name, new_description, new_body)
        skill_file.write_text(content, encoding="utf-8")
        return f"Updated '{name}'."

    elif action == "delete":
        if not skill_file.exists():
            return "(error: not found)"
        shutil.rmtree(skill_dir)
        return f"Deleted '{name}'."

    return "(error: unknown action)"


def register(registry):
    registry.register(
        name="skill_view",
        toolset="skill",
        schema={
            "name": "skill_view",
            "description": "Load full skill content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        handler=handle_skill_view,
    )
    registry.register(
        name="skill_manage",
        toolset="skill",
        schema={
            "name": "skill_manage",
            "description": "Manage skills: create/edit/delete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "edit", "delete"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["action", "name"],
            },
        },
        handler=handle_skill_manage,
    )
