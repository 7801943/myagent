"""File-backed Skill definitions and registry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SkillContext:
    """Context provided by an activated Skill."""

    name: str
    description: str
    instructions: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class SkillDefinition:
    """A Skill loaded from prompts/skills/<username>/<name>/skill.md."""

    name: str
    description: str
    instructions: str
    root_dir: Path
    skill_file: Path
    username: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)

    def get_instructions(self) -> str:
        skill_dir = str(self.root_dir.resolve())
        return self.instructions.replace("${MYAGENT_SKILL_DIR}", skill_dir)

    def to_summary(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "username": self.username,
        }


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content.strip()

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            raw = "\n".join(lines[1:idx]).strip()
            body = "\n".join(lines[idx + 1:]).strip()
            data = yaml.safe_load(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
            return data, body

    return {}, content.strip()


def _description_from_body(body: str) -> str:
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return ""


def _parse_allowed_tools(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def load_skill_from_dir(path: str | Path, username: str) -> SkillDefinition | None:
    """Load one Skill from a directory containing lowercase skill.md."""
    root_dir = Path(path)
    skill_file = root_dir / "skill.md"
    if not skill_file.exists() or not skill_file.is_file():
        return None

    content = skill_file.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(content)
    directory_name = root_dir.name
    skill_name = str(frontmatter.get("name") or directory_name).strip()
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = _description_from_body(body)
        if not description:
            logger.warning("Skill '%s' has no description", skill_name)

    if skill_name != directory_name:
        logger.warning(
            "Skill name '%s' differs from directory '%s'; using frontmatter name",
            skill_name,
            directory_name,
        )

    return SkillDefinition(
        name=skill_name,
        description=description,
        instructions=body,
        root_dir=root_dir,
        skill_file=skill_file,
        username=username,
        frontmatter=frontmatter,
        allowed_tools=_parse_allowed_tools(frontmatter.get("allowed-tools")),
    )


class SkillRegistry:
    """Registry for file-backed Skills for one username."""

    def __init__(self, username: str = "default"):
        self.username = username
        self._skills: dict[str, SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> None:
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def list_available(self) -> list[dict]:
        return [self._skills[name].to_summary() for name in self.registered_names]

    def load_from_user_dir(
        self,
        user_dir: str | Path,
        active_names: set[str] | list[str] | None = None,
    ) -> None:
        root = Path(user_dir)
        if not root.exists():
            logger.info("Skill directory not found: %s", root)
            return
        if not root.is_dir():
            logger.warning("Skill root is not a directory: %s", root)
            return

        active = set(active_names or [])
        for skill_dir in sorted(item for item in root.iterdir() if item.is_dir()):
            if active and skill_dir.name not in active:
                continue
            skill = load_skill_from_dir(skill_dir, self.username)
            if skill is None:
                continue
            if active and skill.name not in active:
                continue
            self.register(skill)
            logger.info("Skill registered: %s (%s)", skill.name, skill.skill_file)

        missing = active - set(self._skills.keys())
        if missing:
            logger.warning("Configured skills not found for %s: %s", self.username, sorted(missing))

    async def activate(self, variables: dict) -> list[SkillContext]:
        """No declarative activation by default for file-backed Skills."""
        return []

    @property
    def registered_names(self) -> list[str]:
        return sorted(self._skills.keys())


__all__ = [
    "SkillContext",
    "SkillDefinition",
    "SkillRegistry",
    "load_skill_from_dir",
]
