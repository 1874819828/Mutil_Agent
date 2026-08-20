from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from app.git_delivery import GitDeliveryError, GitDeliveryService


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8", newline="")
    _git(root, "add", "--", "app.py")
    _git(root, "commit", "-m", "initial")
    return root


def test_git_delivery_creates_run_branch_and_auditable_commit(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = GitDeliveryService()
    state = service.inspect(root, run_id="12345678-abcd-4000-8000-123456789abc")

    assert state.ready is True
    assert state.clean is True
    assert state.current_branch == "main"
    assert state.target_branch == "agent/run-12345678-abcd-4000-8000-123456789abc"
    assert len(state.head_commit or "") == 40

    session = service.start(
        root,
        run_id="12345678-abcd-4000-8000-123456789abc",
        expected_original_branch=state.current_branch or "",
        expected_base_commit=state.head_commit or "",
        expected_target_branch=state.target_branch,
    )
    (root / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    commit = service.commit(
        session,
        changed_files=("app.py",),
        message="agent: publish run 12345678-abcd-4000-8000-123456789abc",
    )

    assert _git(root, "branch", "--show-current") == state.target_branch
    assert _git(root, "rev-parse", "HEAD") == commit
    assert _git(root, "show", "--pretty=format:", "--name-only", "HEAD") == "app.py"
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_git_delivery_rejects_dirty_repository_before_branch_creation(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "app.py").write_text("operator edit\n", encoding="utf-8", newline="")
    state = GitDeliveryService().inspect(
        root, run_id="12345678-abcd-4000-8000-123456789abc"
    )

    assert state.ready is False
    assert state.clean is False
    assert "uncommitted" in (state.reason or "").lower()


def test_git_delivery_refuses_files_outside_reviewed_change_set(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    service = GitDeliveryService()
    state = service.inspect(root, run_id="12345678-abcd-4000-8000-123456789abc")
    session = service.start(
        root,
        run_id="12345678-abcd-4000-8000-123456789abc",
        expected_original_branch=state.current_branch or "",
        expected_base_commit=state.head_commit or "",
        expected_target_branch=state.target_branch,
    )
    (root / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    (root / "surprise.txt").write_text("not reviewed\n", encoding="utf-8")

    with pytest.raises(GitDeliveryError, match="unexpected"):
        service.commit(
            session,
            changed_files=("app.py",),
            message="agent: publish test",
        )


def test_git_delivery_abort_restores_original_branch_and_removes_run_branch(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    service = GitDeliveryService()
    state = service.inspect(root, run_id="12345678-abcd-4000-8000-123456789abc")
    session = service.start(
        root,
        run_id="12345678-abcd-4000-8000-123456789abc",
        expected_original_branch=state.current_branch or "",
        expected_base_commit=state.head_commit or "",
        expected_target_branch=state.target_branch,
    )
    (root / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    # Runtime publication recovery restores the journaled bytes first.
    (root / "app.py").write_text("value = 1\n", encoding="utf-8", newline="")

    service.abort(session)

    assert _git(root, "branch", "--show-current") == "main"
    assert _git(root, "branch", "--list", state.target_branch) == ""
    assert (root / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_git_delivery_rejects_non_repository(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()

    state = GitDeliveryService().inspect(
        root, run_id="12345678-abcd-4000-8000-123456789abc"
    )

    assert state.ready is False
    assert "repository" in (state.reason or "").lower()
