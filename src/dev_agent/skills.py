from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str


class SkillRegistry:
    def __init__(self, directory: Path):
        self.directory = directory

    def all(self) -> list[Skill]:
        return [self._load(path) for path in sorted(self.directory.glob("*/SKILL.md"))]

    def relevant(self, prompt: str) -> list[Skill]:
        words = set(prompt.lower().split())
        scored: list[tuple[int, Skill]] = []
        for skill in self.all():
            haystack = f"{skill.name} {skill.description}".lower()
            score = sum(1 for word in words if len(word) > 3 and word in haystack)
            if score:
                scored.append((score, skill))
        return [skill for _, skill in sorted(scored, key=lambda item: -item[0])[:2]]

    @staticmethod
    def _load(path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"Skill inválida: {path}")
        _, header, body = text.split("---", 2)
        metadata: dict[str, str] = {}
        for line in header.strip().splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip('"')
        return Skill(
            name=metadata.get("name", path.parent.name),
            description=metadata.get("description", ""),
            instructions=body.strip(),
        )

