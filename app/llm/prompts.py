"""Versioned, role-bound system prompts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files

from .provider import AgentRole


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    version: str
    role: AgentRole
    content: str
    sha256: str


class PromptRegistry:
    _PROMPTS = {
        "manager_select_context/v1": (AgentRole.MANAGER, "manager_select_context.md"),
        "manager_plan/v1": (AgentRole.MANAGER, "manager_plan.md"),
        "developer/v1": (AgentRole.DEVELOPER, "developer.md"),
        "reviewer/v1": (AgentRole.REVIEWER, "reviewer.md"),
    }

    def resolve(self, role: AgentRole, version: str) -> PromptTemplate:
        entry = self._PROMPTS.get(version)
        if entry is None:
            raise ValueError(f"unknown prompt version {version!r}")
        expected_role, filename = entry
        if expected_role is not role:
            raise ValueError(f"prompt {version!r} does not belong to role {role.value!r}")
        content = files("app.prompts").joinpath(filename).read_text(encoding="utf-8")
        return PromptTemplate(
            version=version,
            role=role,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
