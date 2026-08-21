"""Fail-closed Git delivery primitives for reviewed publications.

The workflow runtime owns file and test validation.  This module adds a second,
independent invariant: a publication starts from a clean, bound commit and the
resulting commit contains exactly the reviewed path set.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
from typing import Iterable, Mapping
from urllib.parse import urlsplit


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_COMMIT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitDeliveryError(RuntimeError):
    """The target repository cannot satisfy the safe delivery contract."""


@dataclass(frozen=True, slots=True)
class GitRepositoryState:
    repository_root: Path
    project_root: Path
    project_subpath: str
    ready: bool
    clean: bool
    reason: str | None
    current_branch: str | None
    head_commit: str | None
    target_branch: str
    remote_name: str | None
    remote_url: str | None
    base_branch: str | None


@dataclass(frozen=True, slots=True)
class GitDeliverySession:
    repository_root: Path
    project_root: Path
    project_subpath: str
    original_branch: str
    base_commit: str
    target_branch: str
    remote_name: str | None = None
    remote_url: str | None = None
    base_branch: str | None = None


class GitDeliveryService:
    """Create one isolated branch and one exact-path commit per completed run."""

    def __init__(self, *, git_binary: str = "git", timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_binary = git_binary
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def branch_for_run(run_id: str, *, branch_prefix: str = "agent/run-") -> str:
        if not _SAFE_RUN_ID.fullmatch(run_id):
            raise GitDeliveryError("run id cannot be represented as a safe Git branch")
        target = f"{branch_prefix}{run_id}"
        GitDeliveryService._validate_branch_name(target)
        return target

    def inspect(
        self,
        project_root: str | Path,
        *,
        repository_root: str | Path | None = None,
        run_id: str,
        base_branch: str | None = None,
        remote_name: str | None = None,
        branch_prefix: str = "agent/run-",
    ) -> GitRepositoryState:
        project = Path(project_root).resolve(strict=True)
        repository = Path(repository_root or project).resolve(strict=True)
        target_branch = self.branch_for_run(run_id, branch_prefix=branch_prefix)
        if base_branch is not None:
            self._validate_branch_name(base_branch)
        if remote_name is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", remote_name
        ):
            raise GitDeliveryError("configured Git remote name is not safe")
        try:
            relative_project = project.relative_to(repository)
        except ValueError:
            return self._state(
                repository=repository,
                project=project,
                project_subpath="",
                target_branch=target_branch,
                base_branch=base_branch,
                remote_name=remote_name,
                reason="project source_path is outside the configured Git repository",
            )
        project_subpath = (
            "" if relative_project == Path(".") else relative_project.as_posix()
        )
        try:
            actual_root = Path(
                self._run(repository, "rev-parse", "--show-toplevel").strip()
            ).resolve(strict=True)
        except GitDeliveryError:
            return self._state(
                repository=repository,
                project=project,
                project_subpath=project_subpath,
                target_branch=target_branch,
                base_branch=base_branch,
                remote_name=remote_name,
                reason="configured repository_path is not a Git repository",
            )
        if os.path.normcase(str(actual_root)) != os.path.normcase(str(repository)):
            return self._state(
                repository=repository,
                project=project,
                project_subpath=project_subpath,
                target_branch=target_branch,
                base_branch=base_branch,
                remote_name=remote_name,
                reason="configured repository_path is not the Git repository root",
            )
        branch = self._run(repository, "branch", "--show-current").strip() or None
        try:
            head = self._run(repository, "rev-parse", "--verify", "HEAD").strip()
        except GitDeliveryError:
            return self._state(
                repository=repository,
                project=project,
                project_subpath=project_subpath,
                target_branch=target_branch,
                base_branch=base_branch,
                remote_name=remote_name,
                reason="Git repository does not have a base commit",
                current_branch=branch,
            )
        if not _COMMIT_ID.fullmatch(head):
            raise GitDeliveryError("Git HEAD is not a supported commit id")
        status = self._status_entries(repository)
        clean = not status
        branch_exists = self._ref_exists(repository, f"refs/heads/{target_branch}")
        remote_url: str | None = None
        reason = None
        if branch is None:
            reason = "detached Git HEAD cannot be used for publication"
        elif base_branch is not None and branch != base_branch:
            reason = "configured Git base branch is not currently checked out"
        elif not clean:
            reason = "target Git repository contains uncommitted changes"
        elif branch_exists:
            reason = "target run branch already exists"
        if remote_name is not None:
            try:
                remote_url = self._safe_remote_url(
                    self._run(repository, "remote", "get-url", remote_name).strip()
                )
            except GitDeliveryError:
                if reason is None:
                    reason = "configured Git remote is unavailable or unsafe"
        return GitRepositoryState(
            repository_root=repository,
            project_root=project,
            project_subpath=project_subpath,
            ready=reason is None,
            clean=clean,
            reason=reason,
            current_branch=branch,
            head_commit=head,
            target_branch=target_branch,
            remote_name=remote_name,
            remote_url=remote_url,
            base_branch=base_branch,
        )

    def start(
        self,
        project_root: str | Path,
        *,
        repository_root: str | Path | None = None,
        run_id: str,
        base_branch: str | None = None,
        remote_name: str | None = None,
        branch_prefix: str = "agent/run-",
        expected_original_branch: str,
        expected_base_commit: str,
        expected_target_branch: str,
        expected_remote_url: str | None = None,
    ) -> GitDeliverySession:
        state = self.inspect(
            project_root,
            repository_root=repository_root,
            run_id=run_id,
            base_branch=base_branch,
            remote_name=remote_name,
            branch_prefix=branch_prefix,
        )
        if not state.ready:
            raise GitDeliveryError(state.reason or "Git repository is not ready")
        if (
            state.current_branch != expected_original_branch
            or state.head_commit != expected_base_commit
            or state.target_branch != expected_target_branch
            or state.remote_url != expected_remote_url
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
            project_root=state.project_root,
            project_subpath=state.project_subpath,
            original_branch=expected_original_branch,
            base_commit=expected_base_commit,
            target_branch=expected_target_branch,
            remote_name=state.remote_name,
            remote_url=state.remote_url,
            base_branch=state.base_branch,
        )

    def commit(
        self,
        session: GitDeliverySession,
        *,
        changed_files: Iterable[str],
        expected_content_sha256: Mapping[str, str],
        message: str,
    ) -> str:
        repository = self._session_repository(session)
        self._assert_session_branch(session)
        project_paths = self._normalized_paths(changed_files)
        if not project_paths:
            raise GitDeliveryError("Git delivery requires at least one reviewed file")
        paths = tuple(
            self._repository_relative_path(session.project_subpath, path)
            for path in project_paths
        )
        expected_hashes = {
            self._normalized_path(path): digest
            for path, digest in expected_content_sha256.items()
        }
        if set(expected_hashes) != set(project_paths) or any(
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in expected_hashes.values()
        ):
            raise GitDeliveryError(
                "reviewed content hashes do not match the reviewed path set"
            )
        for project_path in project_paths:
            content = self._project_file_bytes(session, project_path)
            if hashlib.sha256(content).hexdigest() != expected_hashes[project_path]:
                raise GitDeliveryError(
                    "project content changed before Git staging"
                )
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
        for project_path, repository_path in zip(project_paths, paths, strict=True):
            staged_blob = self._run_bytes(repository, "show", f":{repository_path}")
            if hashlib.sha256(staged_blob).hexdigest() != expected_hashes[project_path]:
                raise GitDeliveryError(
                    "Git staging transformed reviewed file content"
                )
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
        committed = self._nul_paths(
            self._run_bytes(
                repository,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                "HEAD",
            )
        )
        if set(committed) != expected_paths:
            raise GitDeliveryError("Git commit paths do not match the reviewed change set")
        for project_path, repository_path in zip(project_paths, paths, strict=True):
            committed_blob = self._run_bytes(
                repository, "show", f"HEAD:{repository_path}"
            )
            if hashlib.sha256(committed_blob).hexdigest() != expected_hashes[project_path]:
                raise GitDeliveryError(
                    "Git commit content does not match the reviewed content"
                )
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
        project = session.project_root.resolve(strict=True)
        try:
            relative = project.relative_to(repository)
        except ValueError as exc:
            raise GitDeliveryError("Git session project moved outside its repository") from exc
        expected_subpath = "" if relative == Path(".") else relative.as_posix()
        if expected_subpath != session.project_subpath:
            raise GitDeliveryError("Git session project subpath changed")
        actual = Path(
            self._run(repository, "rev-parse", "--show-toplevel").strip()
        ).resolve(strict=True)
        if os.path.normcase(str(actual)) != os.path.normcase(str(repository)):
            raise GitDeliveryError("Git session repository root changed")
        return repository

    def _assert_session_branch(self, session: GitDeliverySession) -> None:
        if session.base_branch is not None and session.original_branch != session.base_branch:
            raise GitDeliveryError("Git session base branch binding changed")
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
        if session.remote_name is not None:
            remote_url = self._safe_remote_url(
                self._run(
                    session.repository_root,
                    "remote",
                    "get-url",
                    session.remote_name,
                ).strip()
            )
            if remote_url != session.remote_url:
                raise GitDeliveryError("Git remote binding changed during publication")

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
    def _repository_relative_path(project_subpath: str, project_path: str) -> str:
        prefix = GitDeliveryService._normalized_path(project_subpath) if project_subpath else ""
        normalized = GitDeliveryService._normalized_path(project_path)
        return f"{prefix}/{normalized}" if prefix else normalized

    @staticmethod
    def _project_file_bytes(session: GitDeliverySession, project_path: str) -> bytes:
        relative = Path(*PurePosixPath(project_path).parts)
        unresolved = session.project_root / relative
        if unresolved.is_symlink():
            raise GitDeliveryError("reviewed Git paths cannot be symbolic links")
        target = unresolved.resolve(strict=True)
        project = session.project_root.resolve(strict=True)
        try:
            target.relative_to(project)
        except ValueError as exc:
            raise GitDeliveryError("reviewed Git path escaped the project root") from exc
        if not target.is_file():
            raise GitDeliveryError("reviewed Git path is not a regular file")
        return target.read_bytes()

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

    @staticmethod
    def _validate_branch_name(value: str) -> None:
        if (
            not value
            or len(value) > 255
            or value == "HEAD"
            or value.startswith(("-", "/", "."))
            or value.endswith(("/", ".", ".lock"))
            or "//" in value
            or ".." in value
            or "@{" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(character in " ~^:?*[" for character in value)
        ):
            raise GitDeliveryError("configured Git branch name is not safe")

    @staticmethod
    def _safe_remote_url(value: str) -> str:
        if (
            not value
            or len(value) > 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise GitDeliveryError("configured Git remote URL is not safe")
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and (
            parsed.username is not None or parsed.password is not None
        ):
            raise GitDeliveryError("credentialed Git remote URLs are not supported")
        return value

    @staticmethod
    def _state(
        *,
        repository: Path,
        project: Path,
        project_subpath: str,
        target_branch: str,
        base_branch: str | None,
        remote_name: str | None,
        reason: str,
        current_branch: str | None = None,
    ) -> GitRepositoryState:
        return GitRepositoryState(
            repository_root=repository,
            project_root=project,
            project_subpath=project_subpath,
            ready=False,
            clean=False,
            reason=reason,
            current_branch=current_branch,
            head_commit=None,
            target_branch=target_branch,
            remote_name=remote_name,
            remote_url=None,
            base_branch=base_branch,
        )

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
                    "-c",
                    f"core.hooksPath={os.devnull}",
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
