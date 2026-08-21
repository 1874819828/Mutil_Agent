"""Fail-closed test runner abstractions.

Production execution is intentionally Docker-only.  ``ScriptedRunner`` is a
pure in-memory test double; it never starts a process and therefore cannot
become an accidental host-execution fallback.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Protocol


class RunnerError(RuntimeError):
    """Base class for runner failures."""


class DockerUnavailableError(RunnerError):
    """Docker is not installed or its daemon cannot be reached."""


class DockerImageError(RunnerError):
    """The configured immutable runner image is unavailable or changed."""


class UnknownCommandError(RunnerError):
    """A command id is not present in the frozen runner profile."""


class InvalidRunnerRequest(RunnerError):
    """A runner request contains an unsafe path or label."""


class OutputLimitExceeded(RunnerError):
    """A child process exceeded its independent stdout/stderr byte budget."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        stream: str,
        limit_bytes: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.argv = tuple(argv)
        self.stream = stream
        self.limit_bytes = limit_bytes
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"{stream} exceeded the fixed {limit_bytes}-byte output limit")


@dataclass(frozen=True, slots=True)
class RunnerLimits:
    timeout_seconds: int = 120
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 128
    tmpfs_mb: int = 256
    max_file_mb: int = 16

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            self.memory_mb <= 0
            or self.cpus <= 0
            or self.pids <= 0
            or self.tmpfs_mb <= 0
            or self.max_file_mb <= 0
        ):
            raise ValueError("all resource limits must be positive")


@dataclass(frozen=True, slots=True)
class RunnerProfile:
    image: str
    expected_digest: str
    commands: Mapping[str, tuple[str, ...]]
    limits: RunnerLimits = field(default_factory=RunnerLimits)
    sut_command: tuple[str, ...] = (
        "python",
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    )

    def __post_init__(self) -> None:
        if (
            not self.image.strip()
            or self.image.startswith("-")
            or "\x00" in self.image
            or any(char.isspace() for char in self.image)
        ):
            raise ValueError("runner image must be a safe, non-empty Docker reference")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.expected_digest):
            raise ValueError("an immutable lowercase sha256 runner digest is required")
        if not self.commands:
            raise ValueError("at least one frozen command is required")
        for command_id, command in self.commands.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", command_id):
                raise ValueError(f"invalid command id: {command_id!r}")
            if not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
                raise ValueError(f"command {command_id!r} must be a non-empty argv tuple")
        if not self.sut_command or any(
            not isinstance(arg, str) or "\x00" in arg for arg in self.sut_command
        ):
            raise ValueError("sut_command must be a non-empty argv tuple")


@dataclass(frozen=True, slots=True)
class RunnerRequest:
    run_id: str
    lease_generation: int
    command_id: str
    workspace: Path
    baseline_tests: Path
    acceptance_tests: Path
    output_dir: Path


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    passed: int = 0
    failed: int = 0
    failures: tuple[tuple[str, str], ...] = ()


class TestRunner(Protocol):
    """Runner port consumed by the Tester node."""

    def preflight(self) -> str | None:
        """Validate availability and return an implementation identity."""

    def run(self, request: RunnerRequest) -> ExecutionResult:
        """Execute one command selected by its frozen profile id."""

    def cancel(self, run_id: str, lease_generation: int) -> None:
        """Stop the exact fenced execution, if it exists."""


class CommandExecutor(Protocol):
    def run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


class SubprocessCommandExecutor:
    """Invoke Docker CLI while bounding stdout and stderr independently."""

    DEFAULT_MAX_OUTPUT_BYTES = 5 * 1024 * 1024

    def __init__(
        self,
        *,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        read_chunk_bytes: int = 64 * 1024,
    ) -> None:
        if max_output_bytes <= 0 or read_chunk_bytes <= 0:
            raise ValueError("output and read chunk limits must be positive")
        self.max_output_bytes = max_output_bytes
        self.read_chunk_bytes = read_chunk_bytes

    def run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
        )
        assert process.stdout is not None
        assert process.stderr is not None

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        overflow: list[str] = []
        reader_errors: list[BaseException] = []
        wake = threading.Event()
        guard = threading.Lock()

        def drain(stream: str, pipe: Any) -> None:
            try:
                while True:
                    remaining = self.max_output_bytes - len(buffers[stream])
                    # Read at most one byte beyond the remaining budget so the
                    # retained buffer itself can never cross the hard limit.
                    chunk = pipe.read(min(self.read_chunk_bytes, remaining + 1))
                    if not chunk:
                        return
                    if len(chunk) > remaining:
                        if remaining:
                            buffers[stream].extend(chunk[:remaining])
                        with guard:
                            if not overflow:
                                overflow.append(stream)
                        wake.set()
                        return
                    buffers[stream].extend(chunk)
            except BaseException as exc:  # pragma: no cover - OS pipe edge case
                with guard:
                    reader_errors.append(exc)
                wake.set()
            finally:
                pipe.close()

        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        while process.poll() is None:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                _kill_process(process)
                break
            wake.wait(min(0.05, remaining_time))
            if overflow or reader_errors:
                _kill_process(process)
                break
            wake.clear()

        for reader in readers:
            reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):  # pragma: no cover - OS pipe failure
            _kill_process(process)
            raise OSError("Docker CLI output reader did not terminate")
        if process.poll() is None:
            _kill_process(process)

        stdout = _decode_output(buffers["stdout"])
        stderr = _decode_output(buffers["stderr"])
        if timed_out:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        if reader_errors:
            raise OSError("failed to capture Docker CLI output") from reader_errors[0]
        if overflow:
            raise OutputLimitExceeded(
                argv,
                stream=overflow[0],
                limit_bytes=self.max_output_bytes,
                stdout=stdout,
                stderr=stderr,
            )
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class ScriptedRunner:
    """Deterministic, process-free runner for unit and integration tests."""

    def __init__(self, scripts: Mapping[str, Sequence[ExecutionResult]]) -> None:
        self._scripts = {key: deque(values) for key, values in scripts.items()}
        self.calls: list[RunnerRequest] = []

    def preflight(self) -> str:
        return "scripted-runner"

    def run(self, request: RunnerRequest) -> ExecutionResult:
        try:
            queue = self._scripts[request.command_id]
        except KeyError as exc:
            raise UnknownCommandError(
                f"command id {request.command_id!r} is not scripted"
            ) from exc
        if not queue:
            raise RunnerError(f"script for {request.command_id!r} is exhausted")
        self.calls.append(request)
        return queue.popleft()

    def cancel(self, run_id: str, lease_generation: int) -> None:
        # Pure test double: it has no child process or host side effect.
        return None


class DockerRunner:
    """Execute frozen commands in a hardened Docker container.

    There is deliberately no host runner and no fallback branch in this
    class.  A failed preflight aborts the call.
    """

    _LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    _ACCEPTANCE_COMMAND_ID = "acceptance_test"
    _SUT_PORT = 8000

    def __init__(
        self,
        profile: RunnerProfile,
        *,
        executor: CommandExecutor | None = None,
    ) -> None:
        self.profile = profile
        self._executor = executor or SubprocessCommandExecutor()
        self._verified_digest: str | None = None
        self._verified_reference: str | None = None

    def preflight(self) -> str:
        try:
            daemon = self._executor.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError(
                "Docker executable was not found; sandbox execution is required and host fallback is disabled"
            ) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerUnavailableError(
                "Docker daemon is unavailable; sandbox execution is required and host fallback is disabled"
            ) from exc
        if daemon.returncode != 0 or not daemon.stdout.strip():
            detail = _single_line(daemon.stderr or daemon.stdout)
            raise DockerUnavailableError(
                "Docker daemon is unavailable; sandbox execution is required and host fallback is disabled"
                + (f": {detail}" if detail else "")
            )

        try:
            inspected = self._executor.run(
                ["docker", "image", "inspect", self.profile.image],
                timeout=15,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise DockerImageError(f"runner image {self.profile.image!r} cannot be inspected") from exc
        if inspected.returncode != 0:
            raise DockerImageError(
                f"runner image {self.profile.image!r} is unavailable: "
                f"{_single_line(inspected.stderr)}"
            )
        try:
            data = json.loads(inspected.stdout)
            image = data[0]
            candidates = {image.get("Id", ""), *image.get("RepoDigests", [])}
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise DockerImageError("Docker returned invalid runner image metadata") from exc

        expected = self.profile.expected_digest
        matching_references = [
            candidate
            for candidate in candidates
            if isinstance(candidate, str) and candidate.endswith(f"@{expected}")
        ]
        if expected not in candidates and not matching_references:
            raise DockerImageError(
                f"runner image digest mismatch: expected {expected}; refusing mutable or unknown image"
            )
        self._verified_digest = expected
        # Execute the verified immutable reference, never the mutable tag that
        # was supplied for human-readable configuration.
        self._verified_reference = expected if expected in candidates else matching_references[0]
        return expected

    def run(self, request: RunnerRequest) -> ExecutionResult:
        # Reject model-invented command ids before touching Docker.
        try:
            command = self.profile.commands[request.command_id]
        except KeyError as exc:
            raise UnknownCommandError(
                f"command id {request.command_id!r} is not in the frozen runner profile"
            ) from exc

        paths = _validated_paths(request)
        if self._verified_digest is None:
            self.preflight()

        if request.command_id == self._ACCEPTANCE_COMMAND_ID:
            return self._run_blackbox_acceptance(request, paths, command)

        container_name = _container_name(request)
        argv = self._docker_argv(request, paths, container_name, command)
        started = time.monotonic()
        try:
            completed = self._executor.run(
                argv,
                timeout=self.profile.limits.timeout_seconds + 5,
            )
        except subprocess.TimeoutExpired:
            self._force_remove(request.run_id, request.lease_generation)
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecutionResult(
                exit_code=124,
                duration_ms=duration_ms,
                stdout="",
                stderr="Docker test command exceeded its fixed timeout",
                failed=1,
                failures=(("runner_timeout", "fixed Docker timeout exceeded"),),
            )
        except OutputLimitExceeded as exc:
            self._force_remove(request.run_id, request.lease_generation)
            duration_ms = int((time.monotonic() - started) * 1000)
            reason = f"Docker {exc.stream} exceeded {exc.limit_bytes} bytes"
            return ExecutionResult(
                exit_code=125,
                duration_ms=duration_ms,
                stdout=exc.stdout,
                stderr=f"Docker {exc.stream} exceeded its fixed {exc.limit_bytes}-byte output limit",
                failed=1,
                failures=(("runner_output_limit", reason),),
            )
        except (FileNotFoundError, OSError) as exc:
            # Docker disappearing after preflight is still a hard failure.  Do
            # not execute the command through subprocess on the host.
            raise DockerUnavailableError(
                "Docker became unavailable during sandbox execution; host fallback is disabled"
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        passed, failed, failures = _parse_pytest_summary(completed.stdout, completed.stderr)
        if completed.returncode != 0 and failed == 0 and not failures:
            failed = 1
            failures = (("runner_command", _single_line(completed.stderr or completed.stdout) or "command failed"),)
        return ExecutionResult(
            exit_code=completed.returncode,
            duration_ms=duration_ms,
            stdout=completed.stdout,
            stderr=completed.stderr,
            passed=passed,
            failed=failed,
            failures=failures,
        )

    def cancel(self, run_id: str, lease_generation: int) -> None:
        """Remove only resources derived from validated run/generation data.

        Cancellation is idempotent.  It cleans both the legacy single-container
        execution and the black-box acceptance topology so cancellation remains
        safe even when it races resource creation.
        """

        cleanup_commands: tuple[list[str], ...] = (
            ["docker", "rm", "-f", _container_name_parts(run_id, lease_generation)],
        )
        if self._ACCEPTANCE_COMMAND_ID in self.profile.commands:
            resources = _blackbox_resource_names(run_id, lease_generation)
            cleanup_commands += (
                ["docker", "rm", "-f", resources["driver"]],
                ["docker", "rm", "-f", resources["sut"]],
                ["docker", "network", "rm", resources["network"]],
                ["docker", "volume", "rm", resources["volume"]],
            )
        errors: list[str] = []
        for argv in cleanup_commands:
            try:
                completed = self._executor.run(argv, timeout=10)
            except FileNotFoundError as exc:
                raise DockerUnavailableError(
                    "Docker executable was not found during cancellation"
                ) from exc
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DockerUnavailableError(
                    "Docker daemon is unavailable during cancellation"
                ) from exc
            detail = (completed.stderr or completed.stdout).casefold()
            if completed.returncode != 0 and not _is_missing_docker_resource(detail):
                errors.append(_single_line(completed.stderr or completed.stdout))
        if errors:
            raise RunnerError(f"failed to cancel fenced Docker resources: {errors[0]}")

    def _run_blackbox_acceptance(
        self,
        request: RunnerRequest,
        paths: Mapping[str, Path],
        command: tuple[str, ...],
    ) -> ExecutionResult:
        """Run immutable acceptance tests strictly over the SUT's HTTP surface.

        The generated workspace is mounted only into the SUT.  The acceptance
        suite is mounted only into a separate driver container.  Their sole
        connection is a generation-scoped internal Docker network.
        """

        assert self._verified_reference is not None
        resources = _blackbox_resource_names(request.run_id, request.lease_generation)
        started = time.monotonic()
        labels = _docker_labels(request)
        try:
            network = self._executor.run(
                [
                    "docker",
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--internal",
                    *labels,
                    resources["network"],
                ],
                timeout=15,
            )
            failure = self._orchestration_failure(network, started, "network_create")
            if failure is not None:
                return failure

            limits = self.profile.limits
            volume = self._executor.run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--driver",
                    "local",
                    "--opt",
                    "type=tmpfs",
                    "--opt",
                    "device=tmpfs",
                    "--opt",
                    (
                        f"o=size={limits.tmpfs_mb}m,uid=10001,gid=10001,"
                        "mode=0700,nosuid,nodev"
                    ),
                    *labels,
                    resources["volume"],
                ],
                timeout=15,
            )
            failure = self._orchestration_failure(volume, started, "volume_create")
            if failure is not None:
                return failure

            sut = self._executor.run(
                self._sut_argv(request, paths, resources),
                timeout=15,
            )
            failure = self._orchestration_failure(sut, started, "sut_start")
            if failure is not None:
                return failure

            ready = self._wait_for_sut(resources["sut"])
            if not ready:
                return ExecutionResult(
                    exit_code=125,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    stdout="",
                    stderr="SUT did not become ready inside the fixed startup budget",
                    failed=1,
                    failures=(("sut_startup", "SUT readiness probe failed"),),
                )

            completed = self._executor.run(
                self._driver_argv(request, paths, resources, command),
                timeout=limits.timeout_seconds + 5,
            )
            return _execution_result(completed, started)
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                exit_code=124,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout="",
                stderr="Docker test command exceeded its fixed timeout",
                failed=1,
                failures=(("runner_timeout", "fixed Docker timeout exceeded"),),
            )
        except OutputLimitExceeded as exc:
            reason = f"Docker {exc.stream} exceeded {exc.limit_bytes} bytes"
            return ExecutionResult(
                exit_code=125,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout=exc.stdout,
                stderr=(
                    f"Docker {exc.stream} exceeded its fixed "
                    f"{exc.limit_bytes}-byte output limit"
                ),
                failed=1,
                failures=(("runner_output_limit", reason),),
            )
        except (FileNotFoundError, OSError) as exc:
            raise DockerUnavailableError(
                "Docker became unavailable during sandbox execution; host fallback is disabled"
            ) from exc
        finally:
            self._cleanup_blackbox(resources)

    def _sut_argv(
        self,
        request: RunnerRequest,
        paths: Mapping[str, Path],
        resources: Mapping[str, str],
    ) -> list[str]:
        labels = _docker_labels(request)
        labels.extend(("--label", "ai-agent.component=sut"))
        argv = self._hardened_run_prefix(
            request,
            name=resources["sut"],
            network=resources["network"],
            detach=True,
            labels=labels,
        )
        argv.extend(("--network-alias", "sut"))
        argv.extend(("--mount", _mount(paths["workspace"], "/workspace", readonly=True)))
        argv.extend(
            (
                "--mount",
                f"type=volume,source={resources['volume']},target=/data",
                "--workdir",
                "/workspace/backend",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPATH=/workspace/backend",
                "--env",
                "DATABASE_URL=sqlite+aiosqlite:////data/agent-test.db",
                "--env",
                "SECRET_KEY=isolated-test-only-secret",
                "--entrypoint",
                "",
                self._verified_reference or "",
                *self.profile.sut_command,
            )
        )
        return argv

    def _driver_argv(
        self,
        request: RunnerRequest,
        paths: Mapping[str, Path],
        resources: Mapping[str, str],
        command: tuple[str, ...],
    ) -> list[str]:
        labels = _docker_labels(request)
        labels.extend(("--label", "ai-agent.component=driver"))
        argv = self._hardened_run_prefix(
            request,
            name=resources["driver"],
            network=resources["network"],
            detach=False,
            labels=labels,
        )
        argv.extend(
            (
                "--mount",
                _mount(paths["acceptance_tests"], "/acceptance_tests", readonly=True),
                "--workdir",
                "/acceptance_tests",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPATH=/acceptance_tests",
                "--env",
                "RUN_GOLDEN_TESTS=1",
                "--env",
                f"SUT_BASE_URL=http://sut:{self._SUT_PORT}",
                "--entrypoint",
                "",
                self._verified_reference or "",
                *command,
            )
        )
        return argv

    def _hardened_run_prefix(
        self,
        request: RunnerRequest,
        *,
        name: str,
        network: str,
        detach: bool,
        labels: Sequence[str],
    ) -> list[str]:
        limits = self.profile.limits
        argv = ["docker", "run"]
        if detach:
            argv.extend(("--detach", "--log-driver", "none"))
        argv.extend(
            (
                "--name",
                name,
                *labels,
                "--network",
                network,
                "--user",
                "10001:10001",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                f"{limits.memory_mb}m",
                "--cpus",
                str(limits.cpus),
                "--pids-limit",
                str(limits.pids),
                "--ulimit",
                f"fsize={limits.max_file_mb * 1024 * 1024}:{limits.max_file_mb * 1024 * 1024}",
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
            )
        )
        return argv

    def _wait_for_sut(self, container_name: str) -> bool:
        probe = (
            "from pathlib import Path; import urllib.request; "
            "assert not Path('/acceptance_tests').exists(); "
            f"urllib.request.urlopen('http://127.0.0.1:{self._SUT_PORT}/openapi.json', "
            "timeout=2).read(1)"
        )
        deadline = time.monotonic() + min(15, self.profile.limits.timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            completed = self._executor.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "10001:10001",
                    container_name,
                    "python",
                    "-c",
                    probe,
                ],
                timeout=min(3, max(0.1, remaining)),
            )
            if completed.returncode == 0:
                return True
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def _orchestration_failure(
        self,
        completed: subprocess.CompletedProcess[str],
        started: float,
        phase: str,
    ) -> ExecutionResult | None:
        if completed.returncode == 0:
            return None
        detail = _single_line(completed.stderr or completed.stdout) or "Docker command failed"
        return ExecutionResult(
            exit_code=125,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=completed.stdout,
            stderr=completed.stderr,
            failed=1,
            failures=((phase, detail),),
        )

    def _cleanup_blackbox(self, resources: Mapping[str, str]) -> None:
        cleanup_commands = (
            ["docker", "rm", "-f", resources["driver"]],
            ["docker", "rm", "-f", resources["sut"]],
            ["docker", "network", "rm", resources["network"]],
            ["docker", "volume", "rm", resources["volume"]],
        )
        errors: list[str] = []
        for argv in cleanup_commands:
            try:
                completed = self._executor.run(argv, timeout=10)
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
                continue
            detail = (completed.stderr or completed.stdout).casefold()
            if completed.returncode != 0 and not _is_missing_docker_resource(detail):
                errors.append(_single_line(completed.stderr or completed.stdout))
        if errors:
            if sys.exc_info()[0] is not None:
                # Preserve the primary Docker failure. Cancellation/reaping can
                # retry these exact fenced names after the exception surfaces.
                return
            raise RunnerError(f"black-box Docker cleanup failed: {errors[0]}")

    def _docker_argv(
        self,
        request: RunnerRequest,
        paths: Mapping[str, Path],
        container_name: str,
        command: tuple[str, ...],
    ) -> list[str]:
        limits = self.profile.limits
        mounts = [_mount(paths["workspace"], "/workspace", readonly=True)]
        if "baseline" in request.command_id:
            mounts.append(
                _mount(paths["baseline_tests"], "/baseline_tests", readonly=True)
            )
        elif "acceptance" in request.command_id:
            mounts.append(
                _mount(paths["acceptance_tests"], "/acceptance_tests", readonly=True)
            )
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            f"ai-agent.run_id={request.run_id}",
            "--label",
            f"ai-agent.lease_generation={request.lease_generation}",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--ulimit",
            f"fsize={limits.max_file_mb * 1024 * 1024}:{limits.max_file_mb * 1024 * 1024}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONPATH=/workspace/backend",
            "--env",
            "RUN_GOLDEN_TESTS=1",
            "--env",
            "GOLDEN_BACKEND=/workspace/backend",
            "--env",
            "DATABASE_URL=sqlite+aiosqlite:////tmp/agent-test.db",
            "--env",
            "SECRET_KEY=isolated-test-only-secret",
        ]
        for mount in mounts:
            argv.extend(("--mount", mount))
        # The image has a Python ENTRYPOINT, while frozen profiles intentionally
        # contain complete argv arrays beginning with ``python``.
        assert self._verified_reference is not None
        argv.extend(("--entrypoint", "", self._verified_reference, *command))
        return argv

    def _force_remove(self, run_id: str, lease_generation: int) -> None:
        try:
            self.cancel(run_id, lease_generation)
        except Exception:
            # Preserve the timeout result. Cleanup is best-effort and targets a
            # name derived solely from the validated run id and generation.
            pass


def _validated_paths(request: RunnerRequest) -> dict[str, Path]:
    if not DockerRunner._LABEL_RE.fullmatch(request.run_id):
        raise InvalidRunnerRequest("run_id is not safe for a Docker label")
    if request.lease_generation < 1:
        raise InvalidRunnerRequest("lease_generation must be positive")
    result: dict[str, Path] = {}
    # ``output_dir`` remains on RunnerRequest for API compatibility but is
    # deliberately unused: the container receives no writable host bind.
    for name in ("workspace", "baseline_tests", "acceptance_tests"):
        raw = getattr(request, name)
        unresolved = Path(raw).absolute()
        if unresolved.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(unresolved)
        ):
            raise InvalidRunnerRequest(f"{name} cannot be a symlink or junction")
        path = Path(raw).resolve(strict=True)
        if not path.is_dir():
            raise InvalidRunnerRequest(f"{name} must be an existing directory")
        if "," in str(path) or "\n" in str(path) or "\r" in str(path):
            raise InvalidRunnerRequest(f"{name} cannot be represented as a safe Docker mount")
        result[name] = path
    if len({str(path).casefold() for path in result.values()}) != len(result):
        raise InvalidRunnerRequest("runner mount directories must be distinct")
    return result


def _container_name(request: RunnerRequest) -> str:
    return _container_name_parts(request.run_id, request.lease_generation)


def _container_name_parts(run_id: str, lease_generation: int) -> str:
    if not DockerRunner._LABEL_RE.fullmatch(run_id):
        raise InvalidRunnerRequest("run_id is not safe for a Docker container name")
    if lease_generation < 1:
        raise InvalidRunnerRequest("lease_generation must be positive")
    raw = f"ai-agent-{run_id}-{lease_generation}".lower()
    if len(raw) <= 128:
        return raw
    suffix = hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]
    prefix_budget = 128 - len(f"ai-agent--{lease_generation}-{suffix}")
    return f"ai-agent-{run_id[:prefix_budget].lower()}-{lease_generation}-{suffix}"


def _blackbox_resource_names(run_id: str, lease_generation: int) -> dict[str, str]:
    base = _container_name_parts(run_id, lease_generation)
    return {
        "driver": _bounded_resource_name(base, "driver"),
        "sut": _bounded_resource_name(base, "sut"),
        "network": _bounded_resource_name(base, "net"),
        "volume": _bounded_resource_name(base, "db"),
    }


def _bounded_resource_name(base: str, suffix: str) -> str:
    raw = f"{base}-{suffix}"
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]
    # Keep the generation-bearing tail of the already validated base visible
    # while bounding Docker resource names.
    tail = base[-20:]
    prefix_budget = 128 - len(tail) - len(suffix) - len(digest) - 3
    return f"{base[:prefix_budget]}-{tail}-{suffix}-{digest}"


def _docker_labels(request: RunnerRequest) -> list[str]:
    return [
        "--label",
        f"ai-agent.run_id={request.run_id}",
        "--label",
        f"ai-agent.lease_generation={request.lease_generation}",
    ]


def _is_missing_docker_resource(detail: str) -> bool:
    return any(
        marker in detail
        for marker in (
            "no such container",
            "no such network",
            "no such volume",
            "not found",
        )
    )


def _execution_result(
    completed: subprocess.CompletedProcess[str], started: float
) -> ExecutionResult:
    passed, failed, failures = _parse_pytest_summary(completed.stdout, completed.stderr)
    if completed.returncode != 0 and failed == 0 and not failures:
        failed = 1
        failures = (
            (
                "runner_command",
                _single_line(completed.stderr or completed.stdout) or "command failed",
            ),
        )
    return ExecutionResult(
        exit_code=completed.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout=completed.stdout,
        stderr=completed.stderr,
        passed=passed,
        failed=failed,
        failures=failures,
    )


def _mount(source: Path, target: str, *, readonly: bool) -> str:
    value = f"type=bind,source={source},target={target}"
    return f"{value},readonly" if readonly else value


def _single_line(value: str) -> str:
    return " ".join(value.strip().split())[:500]


def _decode_output(value: bytearray) -> str:
    return bytes(value).decode("utf-8", errors="replace")


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - OS failure
        raise OSError("child process could not be killed") from exc


def _parse_pytest_summary(stdout: str, stderr: str) -> tuple[int, int, tuple[tuple[str, str], ...]]:
    combined = f"{stdout}\n{stderr}"
    passed_matches = re.findall(r"(?<!\d)(\d+)\s+passed\b", combined)
    failed_matches = re.findall(r"(?<!\d)(\d+)\s+failed\b", combined)
    passed = int(passed_matches[-1]) if passed_matches else 0
    failed = int(failed_matches[-1]) if failed_matches else 0
    failures: list[tuple[str, str]] = []
    for line in combined.splitlines():
        match = re.match(r"FAILED\s+([^\s]+)(?:\s+-\s+(.+))?", line.strip())
        if match:
            failures.append((match.group(1)[:300], (match.group(2) or "test failed")[:500]))
    return passed, failed, tuple(failures)
