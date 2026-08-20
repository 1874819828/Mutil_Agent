"""Fail-closed Git delivery primitives for reviewed publications.

The workflow runtime owns file and test validation.  This module adds a second,
independent invariant: a publication starts from a clean, bound commit and the
resulting commit contains exactly the reviewed path set.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
from typing import Iterable


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_COMMIT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitDeliveryError(RuntimeError):
    """The target repository cannot satisfy the safe delivery contract."""


@dataclass(frozen=True, slots=True)
class GitRepositoryState:
    repository_root: Path
    ready: bool
    clean: bool
    reason: str | None
    current_branch: str | None
    head_commit: str | None
    target_branch: str


@dataclass(frozen=True, slots=True)
class GitDeliverySession:
    repository_root: Path
    original_branch: str
    base_commit: str
    target_branch: str


class GitDeliveryService:
    """Create one isolated branch and one exact-path commit per completed run."""

    def __init__(self, *, git_binary: str = "git", timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_binary = git_binary
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def branch_for_run(run_id: str) -> str:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise GitDeliveryError("run id cannot be represented as a safe Git branch")
        return f"agent/run-{run_id}"

    def inspect(self, root: str | Path, *, run_id: str) -> GitRepositoryState:
        repository = Path(root).resolve(strict=True)
        target_branch = self.branch_for_run(run_id)
        try:
            actual_root = Path(
                self._run(repository, "rev-parse", "--show-toplevel").strip()
            ).resolve(strict=True)
        except GitDeliveryError:
            return GitRepositoryState(
                repository_root=repository,
                ready=False,
                clean=False,
                reason="target project is not a Git repository",
                current_branch=None,
                head_commit=None,
                target_branch=target_branch,
            )
        if os.path.normcase(str(actual_root)) != os.path.normcase(str(repository)):
            return GitRepositoryState(
                repository_root=repository,
                ready=False,
                clean=False,
                reason="project source_path must be the Git repository root",
                current_branch=None,
                head_commit=None,
                target_branch=target_branch,
            )
        branch = self._run(repository, "branch", "--show-current").strip() or None
        head = self._run(repository, "rev-parse", "--verify", "HEAD").strip()
        if not _COMMIT_ID.fullmatch(head):
            raise GitDeliveryError("Git HEAD is not a supported commit id")
        status = self._status_entries(repository)
        clean = not status
        branch_exists = self._ref_exists(repository, f"refs/heads/{target_branch}")
        reason = None
        if branch is None:
            reason = "detached Git HEAD cannot be used for publication"
        elif not clean:
            reason = "target Git repository contains uncommitted changes"
        elif branch_exists:
            reason = "target run branch already exists"
        return GitRepositoryState(
            repository_root=repository,
            ready=reason is None,
            clean=clean,
            reason=reason,
            current_branch=branch,
            head_commit=head,
            target_branch=target_branch,
        )

    def start(
        self,
        root: str | Path,
        *,
        run_id: str,
        expected_original_branch: str,
        expected_base_commit: str,
        expected_target_branch: str,
    ) -> GitDeliverySession:
        state = self.inspect(root, run_id=run_id)
        if not state.ready:
            raise GitDeliveryError(state.reason or "Git repository is not ready")
        if (
            state.current_branch != expected_original_branch
            or state.head_commit != expected_base_commit
            or state.target_branch != expected_target_branch
        ):
            raise GitDeliveryError("Git publication binding changed after preview")
        self._check_branch(state.repository_root, expected_original_branch)
        self._check_branch(state.repository_root, expected_target_branch)
        self._run(
            state.repository_root,
            "switch",
            "-c",
            expected_target_branch,
            expected_base_commit,
        )
        current = self._run(state.repository_root, "branch", "--show-current").strip()
        if current != expected_target_branch:
            raise GitDeliveryError("Git did not enter the run delivery branch")
        return GitDeliverySession(
            repository_root=state.repository_root,
            original_branch=expected_original_branch,
            base_commit=expected_base_commit,
            target_branch=expected_target_branch,
        )

    def commit(
        self,
        session: GitDeliverySession,
        *,
        changed_files: Iterable[str],
        message: str,
    ) -> str:
        repository = self._session_repository(session)
        self._assert_session_branch(session)
        paths = self._normalized_paths(changed_files)
        if not paths:
            raise GitDeliveryError("Git delivery requires at least one reviewed file")
        actual = self._status_entries(repository)
        actual_paths = {path for _, path in actual}
        expected_paths = set(paths)
        unexpected = actual_paths - expected_paths
        missing = expected_paths - actual_paths
        if unexpected:
            raise GitDeliveryError(
                "Git repository contains unexpected changes outside the reviewed set"
            )
        if missing:
            raise GitDeliveryError("reviewed files are missing from the Git change set")
        for status, _ in actual:
            if "D" in status or "R" in status or "C" in status:
                raise GitDeliveryError("Git deletion or rename delivery is not supported")

        self._run(repository, "add", "--", *paths)
        staged = self._nul_paths(
            self._run_bytes(
                repository,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--diff-filter=ACM",
            )
        )
        if set(staged) != expected_paths:
            raise GitDeliveryError("staged Git paths do not match the reviewed change set")
        message = " ".join(message.split())[:240]
        if not message:
            raise GitDeliveryError("Git commit message must not be blank")
        self._run(
            repository,
            "-c",
            "user.name=Multi-Agent Assistant",
            "-c",
            "user.email=multi-agent@local.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            message,
        )
        commit = self._run(repository, "rev-parse", "HEAD").strip()
        if not _COMMIT_ID.fullmatch(commit) or commit == session.base_commit:
            raise GitDeliveryError("Git commit was not created")
        parent = self._run(repository, "rev-parse", "HEAD^").strip()
        if parent != session.base_commit:
            raise GitDeliveryError("Git delivery commit is not based on the bound commit")
        if self._status_entries(repository):
            raise GitDeliveryError("Git repository is not clean after commit")
        return commit

    def abort(self, session: GitDeliverySession) -> None:
        """Discard only the generated run branch and return to its bound base."""

        repository = self._session_repository(session)
        current = self._run(repository, "branch", "--show-current").strip()
        target_ref = f"refs/heads/{session.target_branch}"
        if not self._ref_exists(repository, target_ref):
            if current != session.original_branch:
                raise GitDeliveryError("run branch is missing and original branch is not active")
            return
        if current == session.target_branch:
            # The publication journal has already restored the exact original
            # bytes.  Move only the generated branch/index back to the bound
            # base; ``--hard`` would rewrite line endings on Windows.
            self._run(repository, "reset", "--mixed", session.base_commit)
            if self._status_entries(repository):
                raise GitDeliveryError(
                    "unexpected untracked files prevent automatic Git recovery"
                )
            self._run(repository, "switch", session.original_branch)
        elif current != session.original_branch:
            raise GitDeliveryError("another Git branch is active; recovery stopped")
        branch_head = self._run(repository, "rev-parse", target_ref).strip()
        if branch_head != session.base_commit:
            raise GitDeliveryError("run branch no longer points at the bound base commit")
        self._run(repository, "branch", "-d", session.target_branch)

    def _session_repository(self, session: GitDeliverySession) -> Path:
        repository = session.repository_root.resolve(strict=True)
        actual = Path(
            self._run(repository, "rev-parse", "--show-toplevel").strip()
        ).resolve(strict=True)
        if os.path.normcase(str(actual)) != os.path.normcase(str(repository)):
            raise GitDeliveryError("Git session repository root changed")
        return repository

    def _assert_session_branch(self, session: GitDeliverySession) -> None:
        current = self._run(
            session.repository_root, "branch", "--show-current"
        ).strip()
        if current != session.target_branch:
            raise GitDeliveryError("Git run branch is no longer active")
        merge_base = self._run(
            session.repository_root, "merge-base", "HEAD", session.base_commit
        ).strip()
        if merge_base != session.base_commit:
            raise GitDeliveryError("Git run branch no longer contains the bound base")

    def _status_entries(self, repository: Path) -> tuple[tuple[str, str], ...]:
        raw = self._run_bytes(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        chunks = raw.split(b"\x00")
        entries: list[tuple[str, str]] = []
        index = 0
        while index < len(chunks):
            chunk = chunks[index]
            index += 1
            if not chunk:
                continue
            if len(chunk) < 4 or chunk[2:3] != b" ":
                raise GitDeliveryError("Git returned an invalid worktree status")
            status = chunk[:2].decode("ascii", errors="strict")
            path = chunk[3:].decode("utf-8", errors="surrogateescape")
            entries.append((status, self._normalized_path(path)))
            if "R" in status or "C" in status:
                if index >= len(chunks) or not chunks[index]:
                    raise GitDeliveryError("Git rename status is incomplete")
                old_path = chunks[index].decode("utf-8", errors="surrogateescape")
                index += 1
                entries.append((status, self._normalized_path(old_path)))
        return tuple(entries)

    @staticmethod
    def _nul_paths(raw: bytes) -> tuple[str, ...]:
        return tuple(
            GitDeliveryService._normalized_path(item.decode("utf-8", errors="surrogateescape"))
            for item in raw.split(b"\x00")
            if item
        )

    @staticmethod
    def _normalized_paths(paths: Iterable[str]) -> tuple[str, ...]:
        result = tuple(GitDeliveryService._normalized_path(path) for path in paths)
        if len(set(result)) != len(result):
            raise GitDeliveryError("reviewed Git paths must be unique")
        return result

    @staticmethod
    def _normalized_path(value: str) -> str:
        if "\x00" in value or "\\" in value:
            raise GitDeliveryError("Git path must be a portable relative path")
        path = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            not value
            or path.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", "..", ".git"} for part in path.parts)
        ):
            raise GitDeliveryError("Git path must be a portable relative path")
        return path.as_posix()

    def _check_branch(self, repository: Path, branch: str) -> None:
        self._run(repository, "check-ref-format", "--branch", branch)

    def _ref_exists(self, repository: Path, ref: str) -> bool:
        completed = self._execute(
            repository, "show-ref", "--verify", "--quiet", ref, check=False
        )
        if completed.returncode not in {0, 1}:
            raise GitDeliveryError("Git could not inspect the target branch")
        return completed.returncode == 0

    def _run(self, repository: Path, *args: str) -> str:
        return self._run_bytes(repository, *args).decode(
            "utf-8", errors="surrogateescape"
        )

    def _run_bytes(self, repository: Path, *args: str) -> bytes:
        return self._execute(repository, *args).stdout

    def _execute(
        self, repository: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                [
                    self._git_binary,
                    "-c",
                    f"safe.directory={repository.as_posix()}",
                    "-C",
                    str(repository),
                    *args,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitDeliveryError("Git executable is unavailable or timed out") from exc
        if check and completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")
            detail = " ".join(detail.split())[:300]
            raise GitDeliveryError(detail or "Git command failed")
        return completed
