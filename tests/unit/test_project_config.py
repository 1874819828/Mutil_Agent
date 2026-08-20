from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.project_profiles import (
    FrozenProjectProfile,
    ProjectProfile,
    freeze_project_profile,
    load_project_profile,
)


def _profile(source: Path, **overrides: object) -> ProjectProfile:
    data: dict[str, object] = {
        "project_id": "demo",
        "source_path": source,
        "include_globs": ["backend/app/**/*.py", "backend/requirements.txt"],
        "exclude_globs": ["**/__pycache__/**"],
        "runner": "python-fastapi",
        "commands": {
            "syntax": ["python", "-m", "compileall", "backend/app"],
            "baseline_test": ["python", "-m", "pytest", "/baseline_tests"],
            "acceptance_test": ["python", "-m", "pytest", "/acceptance_tests"],
        },
        "runner_image": "runner:test",
        "limits": {
            "timeout_seconds": 30,
            "memory_mb": 256,
            "cpus": 1,
            "max_files": 20,
            "max_file_mb": 1,
            "max_workspace_mb": 2,
        },
    }
    data.update(overrides)
    return ProjectProfile.model_validate(data)


def test_load_profile_resolves_relative_source_and_freezes_sequences(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    profile_path = config_dir / "demo.yaml"
    profile_path.write_text(
        """
project_id: demo
source_path: ../project
include_globs:
  - backend/app/**/*.py
exclude_globs:
  - "**/__pycache__/**"
runner: python-fastapi
commands:
  syntax: [python, -m, compileall, backend/app]
  baseline_test: [python, -m, pytest, /baseline_tests]
  acceptance_test: [python, -m, pytest, /acceptance_tests]
runner_image: runner:test
limits:
  timeout_seconds: 30
  memory_mb: 256
  cpus: 1
  max_files: 20
  max_file_mb: 1
  max_workspace_mb: 2
""".strip(),
        encoding="utf-8",
    )

    profile = load_project_profile(profile_path)

    assert profile.source_path == source.resolve()
    assert profile.include_globs == ("backend/app/**/*.py",)
    assert profile.commands.syntax == ("python", "-m", "compileall", "backend/app")


def test_frozen_profile_hash_is_stable_and_covers_effective_configuration(
    tmp_path: Path,
) -> None:
    profile = _profile(tmp_path)

    first = freeze_project_profile(profile)
    second = freeze_project_profile(ProjectProfile.model_validate(profile.model_dump()))
    changed = freeze_project_profile(
        _profile(tmp_path, include_globs=["backend/app/main.py"])
    )

    assert isinstance(first, FrozenProjectProfile)
    assert first.profile_hash == second.profile_hash
    assert first.profile_hash != changed.profile_hash
    assert len(first.profile_hash) == 64
    with pytest.raises(ValidationError):
        first.project_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("include_globs", ["../outside/**"]),
        ("include_globs", ["C:/outside/**"]),
        ("include_globs", ["backend/app.py:stream"]),
        ("commands", {"syntax": "python -m compileall ."}),
        ("include_globs", []),
    ],
)
def test_profile_rejects_unsafe_globs_and_shell_command_strings(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _profile(tmp_path, **{field: value})


def test_profile_rejects_unknown_fields(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _profile(tmp_path, unexpected="value")

