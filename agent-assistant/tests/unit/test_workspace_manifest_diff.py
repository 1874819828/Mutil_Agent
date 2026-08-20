from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from app.config.project_profiles import ProjectProfile, freeze_project_profile
from app.workspace.diff import generate_unified_diff
from app.workspace.errors import OriginalProjectModifiedError, PathNotAllowedError
from app.workspace.manifest import build_manifest
from app.workspace.policy import build_effective_policy
from app.workspace.service import WorkspaceService


def _profile(source: Path):
    return freeze_project_profile(
        ProjectProfile.model_validate(
            {
                "project_id": "demo",
                "source_path": source,
                "repository_path": source,
                "include_globs": ["src/**/*.py"],
                "exclude_globs": [],
                "runner": "python-fastapi",
                "commands": {
                    "syntax": ["python", "-m", "compileall", "src"],
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


def test_manifest_is_deterministic_and_detects_casefold_collisions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "src" / "b.py").write_text("B = 1\n", encoding="utf-8")
    (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    profile = _profile(root)
    policy = build_effective_policy(profile, ["**/*"])

    first = build_manifest(root, policy, profile.limits)
    second = build_manifest(root, policy, profile.limits)

    assert [entry.path for entry in first.entries] == ["src/a.py", "src/b.py"]
    assert first.digest == second.digest

    collision_root = tmp_path / "collision"
    (collision_root / "src").mkdir(parents=True)
    (collision_root / "src" / "Name.py").write_text("x\n", encoding="utf-8")
    (collision_root / "src" / "name.py").write_text("y\n", encoding="utf-8")
    if len(list((collision_root / "src").iterdir())) < 2:
        pytest.skip("the filesystem is case-insensitive")
    with pytest.raises(Exception, match="case-insensitive"):
        build_manifest(collision_root, policy, profile.limits)


def test_real_diff_is_generated_from_filesystem_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = _profile(source)
    workspace = tmp_path / "workspace"
    WorkspaceService().create_workspace(profile, workspace)
    (workspace / "src" / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
    (workspace / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
    policy = build_effective_policy(profile, ["**/*"])

    result = generate_unified_diff(source, workspace, policy, profile.limits)

    assert result.changed_files == ("src/existing.py", "src/new.py")
    assert result.modified_files == ("src/existing.py",)
    assert result.added_files == ("src/new.py",)
    assert "--- a/src/existing.py" in result.text
    assert "+++ b/src/existing.py" in result.text
    assert "-VALUE = 1" in result.text
    assert "+VALUE = 2" in result.text
    assert "+NEW = True" in result.text


def test_multifile_diff_without_eof_newlines_is_applyable(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for patch validation")
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "first.py").write_bytes(b"FIRST = 1")
    (source / "src" / "second.py").write_bytes(b"SECOND = 1")
    profile = _profile(source)
    workspace = tmp_path / "workspace"
    WorkspaceService().create_workspace(profile, workspace)
    (workspace / "src" / "first.py").write_bytes(b"FIRST = 2")
    (workspace / "src" / "second.py").write_bytes(b"SECOND = 2")
    policy = build_effective_policy(profile, ["**/*"])

    result = generate_unified_diff(source, workspace, policy, profile.limits)

    assert "\\ No newline at end of file\n--- a/src/second.py" in result.text
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(result.text, encoding="utf-8", newline="")
    check = subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    subprocess.run(
        ["git", "apply", str(patch_path)], cwd=source, check=True
    )
    assert (source / "src" / "first.py").read_bytes() == b"FIRST = 2"
    assert (source / "src" / "second.py").read_bytes() == b"SECOND = 2"


def test_source_snapshot_detects_later_original_modification(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    original = source / "src" / "main.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    profile = _profile(source)
    snapshot = WorkspaceService().create_workspace(profile, tmp_path / "workspace")
    original.write_text("VALUE = 99\n", encoding="utf-8")

    with pytest.raises(OriginalProjectModifiedError):
        WorkspaceService().assert_source_unchanged(snapshot)


def test_real_diff_rejects_files_created_outside_effective_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = _profile(source)
    workspace = tmp_path / "workspace"
    WorkspaceService().create_workspace(profile, workspace)
    (workspace / "outside").mkdir()
    (workspace / "outside" / "injected.txt").write_text("bad\n", encoding="utf-8")
    policy = build_effective_policy(profile, ["**/*"])

    with pytest.raises(PathNotAllowedError):
        generate_unified_diff(source, workspace, policy, profile.limits)


def test_narrow_plan_ignores_unchanged_profile_files_but_rejects_their_modification(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "src" / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    profile = _profile(source)
    workspace = tmp_path / "workspace"
    WorkspaceService().create_workspace(profile, workspace)
    policy = build_effective_policy(profile, ["src/main.py"])
    (workspace / "src" / "main.py").write_text("VALUE = 2\n", encoding="utf-8")

    allowed = generate_unified_diff(source, workspace, policy, profile.limits)

    assert allowed.changed_files == ("src/main.py",)
    (workspace / "src" / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    with pytest.raises(PathNotAllowedError):
        generate_unified_diff(source, workspace, policy, profile.limits)
