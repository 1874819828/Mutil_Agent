from __future__ import annotations

import hashlib
from pathlib import Path
import os
import subprocess

import pytest

from app.git_delivery import GitDeliveryError, GitDeliveryService


RUN_ID = "12345678-abcd-4000-8000-123456789abc"
REMOTE_URL = "https://example.invalid/owner/repository.git"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    project = repository / "student-management"
    project.mkdir(parents=True)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "remote", "add", "origin", REMOTE_URL)
    (repository / "README.md").write_text(
        "monorepo fixture\n", encoding="utf-8", newline=""
    )
    (project / "app.py").write_text("value = 1\n", encoding="utf-8", newline="")
    _git(
        repository,
        "add",
        "--",
        "README.md",
        "student-management/app.py",
    )
    _git(repository, "commit", "-m", "initial")
    return repository, project


def _inspect(
    service: GitDeliveryService,
    repository: Path,
    project: Path,
):
    return service.inspect(
        project,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="main",
        remote_name="origin",
        branch_prefix="agent/run-",
    )


def _start(
    service: GitDeliveryService,
    repository: Path,
    project: Path,
):
    state = _inspect(service, repository, project)
    return state, service.start(
        project,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="main",
        remote_name="origin",
        branch_prefix="agent/run-",
        expected_original_branch=state.current_branch or "",
        expected_base_commit=state.head_commit or "",
        expected_target_branch=state.target_branch,
        expected_remote_url=state.remote_url,
    )


def test_git_delivery_creates_run_branch_and_auditable_commit(tmp_path: Path) -> None:
    repository, project = _repository(tmp_path)
    service = GitDeliveryService()
    state = _inspect(service, repository, project)

    assert state.ready is True
    assert state.clean is True
    assert state.repository_root == repository.resolve()
    assert state.project_root == project.resolve()
    assert state.project_subpath == "student-management"
    assert state.remote_name == "origin"
    assert state.remote_url == REMOTE_URL
    assert state.base_branch == "main"
    assert state.current_branch == "main"
    assert state.target_branch == f"agent/run-{RUN_ID}"
    assert len(state.head_commit or "") == 40

    session = service.start(
        project,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="main",
        remote_name="origin",
        branch_prefix="agent/run-",
        expected_original_branch=state.current_branch or "",
        expected_base_commit=state.head_commit or "",
        expected_target_branch=state.target_branch,
        expected_remote_url=state.remote_url,
    )
    assert session.repository_root == repository.resolve()
    assert session.project_root == project.resolve()
    assert session.project_subpath == "student-management"
    assert session.remote_name == "origin"
    assert session.remote_url == REMOTE_URL
    assert session.base_branch == "main"

    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    commit = service.commit(
        session,
        changed_files=("app.py",),
        expected_content_sha256={"app.py": _content_hash(project / "app.py")},
        message=f"agent: publish run {RUN_ID}",
    )

    assert _git(repository, "branch", "--show-current") == state.target_branch
    assert _git(repository, "rev-parse", "HEAD") == commit
    assert _git(repository, "show", "--pretty=format:", "--name-only", "HEAD") == (
        "student-management/app.py"
    )
    assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_git_delivery_rejects_dirty_repository_before_branch_creation(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    (repository / "README.md").write_text(
        "operator edit outside project\n", encoding="utf-8", newline=""
    )
    service = GitDeliveryService()
    state = _inspect(service, repository, project)

    assert state.ready is False
    assert state.clean is False
    assert "uncommitted" in (state.reason or "").lower()


def test_git_delivery_refuses_files_outside_reviewed_change_set(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    service = GitDeliveryService()
    _, session = _start(service, repository, project)
    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    (repository / "surprise.txt").write_text("not reviewed\n", encoding="utf-8")

    with pytest.raises(GitDeliveryError, match="unexpected"):
        service.commit(
            session,
            changed_files=("app.py",),
            expected_content_sha256={"app.py": _content_hash(project / "app.py")},
            message="agent: publish test",
        )


def test_git_delivery_rejects_remote_drift_before_commit(tmp_path: Path) -> None:
    repository, project = _repository(tmp_path)
    service = GitDeliveryService()
    _, session = _start(service, repository, project)
    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    _git(
        repository,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/attacker/repository.git",
    )

    with pytest.raises(GitDeliveryError, match="remote binding changed"):
        service.commit(
            session,
            changed_files=("app.py",),
            expected_content_sha256={"app.py": _content_hash(project / "app.py")},
            message="agent: publish test",
        )


def test_git_delivery_disables_repository_hooks(tmp_path: Path) -> None:
    repository, project = _repository(tmp_path)
    pre_commit = repository / ".git" / "hooks" / "pre-commit"
    pre_commit.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8", newline="")
    os.chmod(pre_commit, 0o755)
    post_checkout = repository / ".git" / "hooks" / "post-checkout"
    post_checkout.write_text(
        "#!/bin/sh\necho hook-ran > hook-ran.txt\n",
        encoding="utf-8",
        newline="",
    )
    os.chmod(post_checkout, 0o755)
    service = GitDeliveryService()
    _, session = _start(service, repository, project)
    assert not (repository / "hook-ran.txt").exists()
    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")

    commit = service.commit(
        session,
        changed_files=("app.py",),
        expected_content_sha256={"app.py": _content_hash(project / "app.py")},
        message="agent: publish test",
    )

    assert _git(repository, "rev-parse", "HEAD") == commit


def test_git_delivery_rejects_clean_filter_content_transformation(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    attributes = project / ".gitattributes"
    attributes.write_text("*.py filter=rewrite\n", encoding="utf-8", newline="")
    filter_script = repository / "rewrite.sh"
    filter_script.write_text(
        "#!/bin/sh\nsed 's/value = 2/value = 999/'\n",
        encoding="utf-8",
        newline="",
    )
    os.chmod(filter_script, 0o755)
    _git(
        repository,
        "add",
        "--",
        "student-management/.gitattributes",
        "rewrite.sh",
    )
    _git(repository, "commit", "-m", "configure fixture filter")
    _git(repository, "config", "filter.rewrite.clean", "./rewrite.sh")
    service = GitDeliveryService()
    _, session = _start(service, repository, project)
    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")

    with pytest.raises(GitDeliveryError, match="staging transformed"):
        service.commit(
            session,
            changed_files=("app.py",),
            expected_content_sha256={"app.py": _content_hash(project / "app.py")},
            message="agent: publish test",
        )


def test_git_delivery_abort_restores_original_branch_and_removes_run_branch(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    service = GitDeliveryService()
    state, session = _start(service, repository, project)
    original = (project / "app.py").read_bytes()
    (project / "app.py").write_text("value = 2\n", encoding="utf-8", newline="")
    # Runtime publication recovery restores the journaled bytes first.
    (project / "app.py").write_bytes(original)

    service.abort(session)

    assert _git(repository, "branch", "--show-current") == "main"
    assert _git(repository, "branch", "--list", state.target_branch) == ""
    assert (project / "app.py").read_bytes() == original


def test_git_delivery_rejects_non_repository(tmp_path: Path) -> None:
    repository = tmp_path / "plain"
    project = repository / "project"
    project.mkdir(parents=True)

    state = GitDeliveryService().inspect(
        project,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="main",
        remote_name="origin",
        branch_prefix="agent/run-",
    )

    assert state.ready is False
    assert "repository" in (state.reason or "").lower()


def test_git_delivery_rejects_project_outside_configured_repository(
    tmp_path: Path,
) -> None:
    repository, _ = _repository(tmp_path)
    outside = tmp_path / "outside-project"
    outside.mkdir()

    state = GitDeliveryService().inspect(
        outside,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="main",
        remote_name="origin",
        branch_prefix="agent/run-",
    )

    assert state.ready is False
    assert "outside" in (state.reason or "").lower()


def test_git_delivery_honors_configured_base_remote_and_branch_prefix(
    tmp_path: Path,
) -> None:
    repository, project = _repository(tmp_path)
    _git(repository, "branch", "-m", "develop")
    _git(repository, "remote", "rename", "origin", "upstream")

    state = GitDeliveryService().inspect(
        project,
        repository_root=repository,
        run_id=RUN_ID,
        base_branch="develop",
        remote_name="upstream",
        branch_prefix="automation/run-",
    )

    assert state.ready is True
    assert state.base_branch == "develop"
    assert state.remote_name == "upstream"
    assert state.remote_url == REMOTE_URL
    assert state.target_branch == f"automation/run-{RUN_ID}"
