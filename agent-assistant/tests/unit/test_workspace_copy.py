from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config.project_profiles import ProjectProfile, freeze_project_profile
from app.workspace.errors import (
    QuotaExceededError,
    UnsafeFilesystemEntryError,
    WorkspaceExistsError,
)
from app.workspace.service import WorkspaceService


def _profile(source: Path, **limit_overrides: object):
    limits = {
        "timeout_seconds": 30,
        "memory_mb": 256,
        "cpus": 1,
        "max_files": 20,
        "max_file_mb": 1,
        "max_workspace_mb": 2,
    }
    limits.update(limit_overrides)
    return freeze_project_profile(
        ProjectProfile.model_validate(
            {
                "project_id": "demo",
                "source_path": source,
                "include_globs": ["backend/**/*.py", "backend/requirements.txt"],
                "exclude_globs": ["**/__pycache__/**"],
                "runner": "python-fastapi",
                "commands": {
                    "syntax": ["python", "-m", "compileall", "backend"],
                    "baseline_test": ["python", "-m", "pytest", "/baseline"],
                    "acceptance_test": ["python", "-m", "pytest", "/acceptance"],
                },
                "runner_image": "runner:test",
                "limits": limits,
            }
        )
    )


def _write_fixture(source: Path) -> None:
    (source / "backend" / "pkg").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    (source / "backend" / "pkg" / "module.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    (source / "backend" / "requirements.txt").write_text(
        "fastapi==1\n", encoding="utf-8"
    )
    (source / "backend" / ".env.secret").write_text("TOKEN=secret\n", encoding="utf-8")
    (source / "backend" / "student.db").write_bytes(b"SQLite format 3\x00")
    (source / "frontend").mkdir()
    (source / "frontend" / "ignored.js").write_text("ignored\n", encoding="utf-8")


def test_create_workspace_only_copies_allowlisted_safe_text_and_preserves_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_fixture(source)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    destination = tmp_path / "run" / "workspace"

    snapshot = WorkspaceService().create_workspace(_profile(source), destination)

    assert (destination / "backend" / "app.py").read_text(encoding="utf-8") == "APP = 1\n"
    assert (destination / "backend" / "pkg" / "module.py").exists()
    assert (destination / "backend" / "requirements.txt").exists()
    assert not (destination / "backend" / ".env.secret").exists()
    assert not (destination / "backend" / "student.db").exists()
    assert not (destination / "frontend").exists()
    assert snapshot.source_manifest.digest == snapshot.workspace_manifest.digest
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    WorkspaceService().assert_source_unchanged(snapshot)


def test_create_workspace_is_idempotent_only_for_the_same_frozen_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_fixture(source)
    destination = tmp_path / "workspace"
    service = WorkspaceService()
    profile = _profile(source)

    first = service.create_workspace(profile, destination)
    second = service.create_workspace(profile, destination)

    assert first.workspace_manifest.digest == second.workspace_manifest.digest
    other_source = tmp_path / "other"
    _write_fixture(other_source)
    with pytest.raises(WorkspaceExistsError):
        service.create_workspace(_profile(other_source), destination)


def test_quota_failure_leaves_no_partial_workspace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_fixture(source)
    destination = tmp_path / "workspace"

    with pytest.raises(QuotaExceededError):
        WorkspaceService().create_workspace(
            _profile(source, max_files=1), destination
        )

    assert not destination.exists()


def test_single_file_quota_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_fixture(source)
    (source / "backend" / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(QuotaExceededError):
        WorkspaceService().create_workspace(_profile(source), tmp_path / "workspace")


def test_total_workspace_quota_is_enforced(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "one.py").write_text("x" * 700, encoding="utf-8")
    (source / "backend" / "two.py").write_text("y" * 700, encoding="utf-8")

    with pytest.raises(QuotaExceededError, match="max_workspace_mb"):
        WorkspaceService().create_workspace(
            _profile(source, max_file_mb=0.001, max_workspace_mb=0.0012),
            tmp_path / "workspace",
        )


def test_selected_symlink_or_reparse_point_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_fixture(source)
    external = tmp_path / "external.py"
    external.write_text("SECRET = True\n", encoding="utf-8")
    link = source / "backend" / "linked.py"
    try:
        os.symlink(external, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    with pytest.raises(UnsafeFilesystemEntryError):
        WorkspaceService().create_workspace(_profile(source), tmp_path / "workspace")


def test_destination_must_not_be_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_fixture(source)

    with pytest.raises(UnsafeFilesystemEntryError):
        WorkspaceService().create_workspace(_profile(source), source / "runtime" / "workspace")
