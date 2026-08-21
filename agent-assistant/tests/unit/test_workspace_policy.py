from __future__ import annotations

from pathlib import Path

import pytest

from app.config.project_profiles import ProjectProfile, freeze_project_profile
from app.workspace.errors import InvalidRelativePathError, PathNotAllowedError
from app.workspace.paths import normalize_relative_path
from app.workspace.policy import build_effective_policy
from app.workspace.service import validate_write_path


def _frozen_profile(tmp_path: Path):
    return freeze_project_profile(
        ProjectProfile.model_validate(
            {
                "project_id": "demo",
                "source_path": tmp_path,
                "repository_path": tmp_path,
                "include_globs": [
                    "backend/app/**/*.py",
                    "backend/requirements.txt",
                    "tests/**/*.py",
                ],
                "exclude_globs": ["**/__pycache__/**"],
                "runner": "python-fastapi",
                "commands": {
                    "syntax": ["python", "-m", "compileall", "backend/app"],
                    "baseline_test": ["python", "-m", "pytest", "/baseline"],
                    "acceptance_test": ["python", "-m", "pytest", "/acceptance"],
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
        )
    )


def test_effective_policy_is_an_enforced_intersection(tmp_path: Path) -> None:
    frozen = _frozen_profile(tmp_path)
    policy = build_effective_policy(frozen, ["**/*"])

    assert policy.allows("backend/app/main.py")
    assert policy.allows("backend/app/routers/health.py")
    assert not policy.allows("frontend/src/main.js")
    assert not policy.allows("backend/student.db")
    assert policy.profile_hash == frozen.profile_hash


def test_manager_can_narrow_but_cannot_expand_profile(tmp_path: Path) -> None:
    policy = build_effective_policy(
        _frozen_profile(tmp_path), ["backend/app/main.py", "frontend/**/*"]
    )

    assert policy.allows("backend/app/main.py")
    assert not policy.allows("backend/app/routers/health.py")
    assert not policy.allows("frontend/src/main.js")
    with pytest.raises(PathNotAllowedError):
        policy.require_allowed("frontend/src/main.js")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/absolute.py",
        "C:/absolute.py",
        r"\\server\share\file.py",
        "backend/app.py:stream",
        "backend/CON.py",
        "backend/trailing. /file.py",
        "backend/../outside.py",
        "backend/\x00bad.py",
    ],
)
def test_relative_path_normalization_rejects_traversal_ads_and_windows_devices(
    path: str,
) -> None:
    with pytest.raises(InvalidRelativePathError):
        normalize_relative_path(path)


def test_path_normalization_is_separator_stable() -> None:
    assert normalize_relative_path(r"backend\app\main.py") == "backend/app/main.py"


def test_validate_write_path_combines_policy_and_canonical_workspace_checks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "backend" / "app").mkdir(parents=True)
    policy = build_effective_policy(_frozen_profile(tmp_path), ["backend/app/**/*.py"])

    target = validate_write_path(workspace, "backend/app/health.py", policy)

    assert target == (workspace / "backend" / "app" / "health.py").resolve()
    with pytest.raises(PathNotAllowedError):
        validate_write_path(workspace, "tests/test_health.py", policy)
