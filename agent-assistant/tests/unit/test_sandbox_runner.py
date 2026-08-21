from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.sandbox import (
    DockerImageError,
    DockerRunner,
    DockerUnavailableError,
    ExecutionResult,
    InvalidRunnerRequest,
    OutputLimitExceeded,
    RunnerError,
    RunnerLimits,
    RunnerProfile,
    RunnerRequest,
    ScriptedRunner,
    SubprocessCommandExecutor,
    UnknownCommandError,
)


class FakeExecutor:
    def __init__(self, results: list[subprocess.CompletedProcess[str] | Exception]):
        self.results = list(results)
        self.calls: list[tuple[list[str], float]] = []

    def run(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _completed(argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _profile() -> RunnerProfile:
    digest = "sha256:" + "a" * 64
    return RunnerProfile(
        image="ai-agent/python-fastapi-runner:locked",
        expected_digest=digest,
        commands={
            "baseline_pytest": (
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/baseline_tests",
            ),
            "acceptance_pytest": (
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/acceptance_tests",
            ),
        },
        limits=RunnerLimits(timeout_seconds=30, memory_mb=256, cpus=0.5, pids=64),
    )


def _request(tmp_path: Path, command_id: str = "baseline_pytest") -> RunnerRequest:
    workspace = tmp_path / "workspace"
    baseline = tmp_path / "baseline"
    acceptance = tmp_path / "acceptance"
    output = tmp_path / "output"
    for directory in (workspace, baseline, acceptance, output):
        directory.mkdir()
    return RunnerRequest(
        run_id="run-123",
        lease_generation=7,
        command_id=command_id,
        workspace=workspace,
        baseline_tests=baseline,
        acceptance_tests=acceptance,
        output_dir=output,
    )


def test_scripted_runner_is_deterministic_and_never_executes_host_commands(tmp_path: Path) -> None:
    first = ExecutionResult(exit_code=1, duration_ms=10, stdout="1 failed", stderr="")
    second = ExecutionResult(exit_code=0, duration_ms=12, stdout="2 passed", stderr="")
    runner = ScriptedRunner({"acceptance_pytest": [first, second]})
    request = _request(tmp_path, "acceptance_pytest")

    assert runner.run(request) == first
    assert runner.run(request) == second
    assert runner.calls == [request, request]


def test_docker_preflight_fails_clearly_when_executable_is_missing() -> None:
    executor = FakeExecutor([FileNotFoundError("docker")])
    runner = DockerRunner(_profile(), executor=executor)

    with pytest.raises(DockerUnavailableError, match="Docker executable"):
        runner.preflight()


def test_docker_preflight_fails_clearly_when_daemon_is_unavailable() -> None:
    executor = FakeExecutor([_completed([], 1, stderr="daemon is stopped")])
    runner = DockerRunner(_profile(), executor=executor)

    with pytest.raises(DockerUnavailableError, match="daemon is unavailable"):
        runner.preflight()


def test_docker_preflight_rejects_an_unlocked_image_digest() -> None:
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"sha256:' + "b" * 64 + '","RepoDigests":[]}]'),
        ]
    )
    runner = DockerRunner(_profile(), executor=executor)

    with pytest.raises(DockerImageError, match="digest mismatch"):
        runner.preflight()


def test_docker_run_uses_only_profile_command_and_hardened_flags(tmp_path: Path) -> None:
    digest = _profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed(
                [],
                stdout='[{"Id":"' + digest + '","RepoDigests":["runner@' + digest + '"]}]',
            ),
            _completed([], stdout="3 passed in 0.20s\n"),
        ]
    )
    runner = DockerRunner(_profile(), executor=executor)
    request = _request(tmp_path)

    result = runner.run(request)

    argv, timeout = executor.calls[-1]
    assert result.exit_code == 0
    assert result.passed == 3
    assert timeout == 35
    assert argv[:3] == ["docker", "run", "--rm"]
    assert ["--network", "none"] == argv[argv.index("--network") : argv.index("--network") + 2]
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "--read-only" in argv
    assert "no-new-privileges" in argv
    assert "ai-agent.run_id=run-123" in argv
    assert "ai-agent.lease_generation=7" in argv
    assert "type=bind" in " ".join(argv)
    assert ",readonly" in " ".join(argv)
    mounts = [argv[index + 1] for index, value in enumerate(argv) if value == "--mount"]
    assert mounts
    assert all(mount.endswith(",readonly") for mount in mounts)
    assert all("/acceptance_tests" not in mount for mount in mounts)
    assert "/test-output" not in " ".join(argv)
    assert str(request.output_dir.resolve()) not in " ".join(argv)
    image_index = argv.index(_profile().expected_digest)
    assert argv[image_index + 1 :] == list(_profile().commands["baseline_pytest"])


def test_unknown_command_is_rejected_before_docker_is_called(tmp_path: Path) -> None:
    executor = FakeExecutor([])
    runner = DockerRunner(_profile(), executor=executor)

    with pytest.raises(UnknownCommandError):
        runner.run(_request(tmp_path, "model_supplied_shell"))

    assert executor.calls == []


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_subprocess_executor_enforces_an_independent_hard_limit_per_stream(stream: str) -> None:
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    script = f"import sys,time; {target}.write(b'x' * 4096); {target}.flush(); time.sleep(60)"
    executor = SubprocessCommandExecutor(max_output_bytes=1024, read_chunk_bytes=128)
    started = time.monotonic()

    with pytest.raises(OutputLimitExceeded) as raised:
        executor.run([sys.executable, "-c", script], timeout=10)

    assert raised.value.stream == stream
    assert raised.value.limit_bytes == 1024
    assert len(raised.value.stdout.encode("utf-8")) <= 1024
    assert len(raised.value.stderr.encode("utf-8")) <= 1024
    assert time.monotonic() - started < 5


def test_docker_output_overflow_removes_container_and_returns_explicit_failure(
    tmp_path: Path,
) -> None:
    digest = _profile().expected_digest
    overflow = OutputLimitExceeded(
        ["docker", "run"],
        stream="stderr",
        limit_bytes=1024,
        stdout="bounded stdout",
        stderr="x" * 1024,
    )
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            overflow,
            _completed([], stdout="container removed"),
        ]
    )
    runner = DockerRunner(_profile(), executor=executor)

    result = runner.run(_request(tmp_path))

    assert result.exit_code == 125
    assert result.failed == 1
    assert result.failures == (("runner_output_limit", "Docker stderr exceeded 1024 bytes"),)
    assert result.stderr == "Docker stderr exceeded its fixed 1024-byte output limit"
    assert executor.calls[-1][0] == ["docker", "rm", "-f", "ai-agent-run-123-7"]


def test_docker_cancel_uses_only_the_validated_container_name() -> None:
    executor = FakeExecutor([_completed([], stdout="removed")])
    runner = DockerRunner(_profile(), executor=executor)

    runner.cancel("run-123", 7)

    assert executor.calls == [(["docker", "rm", "-f", "ai-agent-run-123-7"], 10)]


def test_docker_cancel_rejects_an_unsafe_run_id_before_cli_execution() -> None:
    executor = FakeExecutor([])
    runner = DockerRunner(_profile(), executor=executor)

    with pytest.raises(InvalidRunnerRequest):
        runner.cancel("../other-container", 7)

    assert executor.calls == []


def test_scripted_runner_cancel_is_a_process_free_noop(tmp_path: Path) -> None:
    runner = ScriptedRunner({"baseline_pytest": []})

    runner.cancel("run-123", 7)

    assert runner.calls == []


def _blackbox_profile() -> RunnerProfile:
    digest = "sha256:" + "c" * 64
    return RunnerProfile(
        image="ai-agent/python-fastapi-runner:locked",
        expected_digest=digest,
        commands={
            "baseline_test": (
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/baseline_tests",
            ),
            "acceptance_test": (
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "/acceptance_tests",
            ),
        },
        limits=RunnerLimits(timeout_seconds=30, memory_mb=256, cpus=0.5, pids=64),
    )


def test_acceptance_uses_isolated_sut_and_driver_containers(tmp_path: Path) -> None:
    digest = _blackbox_profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            _completed([], stdout="network"),
            _completed([], stdout="volume"),
            _completed([], stdout="sut-container"),
            _completed([], stdout="ready"),
            _completed([], stdout="4 passed in 0.20s\n"),
            _completed([], stderr="No such container"),
            _completed([], stdout="sut removed"),
            _completed([], stdout="network removed"),
            _completed([], stdout="volume removed"),
        ]
    )
    runner = DockerRunner(_blackbox_profile(), executor=executor)
    request = _request(tmp_path, "acceptance_test")

    result = runner.run(request)

    assert result.exit_code == 0
    assert result.passed == 4
    calls = [argv for argv, _ in executor.calls]
    network_create = calls[2]
    volume_create = calls[3]
    sut_run = calls[4]
    driver_run = calls[6]

    assert network_create[:3] == ["docker", "network", "create"]
    assert "--internal" in network_create
    assert "ai-agent.run_id=run-123" in network_create
    assert "ai-agent.lease_generation=7" in network_create

    assert volume_create[:3] == ["docker", "volume", "create"]
    assert "type=tmpfs" in volume_create
    assert "uid=10001,gid=10001" in " ".join(volume_create)

    for argv in (sut_run, driver_run):
        assert argv[:2] == ["docker", "run"]
        assert ["--user", "10001:10001"] == argv[argv.index("--user") : argv.index("--user") + 2]
        assert "--read-only" in argv
        assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
        assert "no-new-privileges" in argv
        assert "ai-agent.run_id=run-123" in argv
        assert "ai-agent.lease_generation=7" in argv

    assert ["--network-alias", "sut"] == sut_run[
        sut_run.index("--network-alias") : sut_run.index("--network-alias") + 2
    ]
    assert "ai-agent.component=sut" in sut_run
    assert "ai-agent.component=driver" in driver_run
    sut_mounts = [sut_run[index + 1] for index, value in enumerate(sut_run) if value == "--mount"]
    assert any("target=/workspace,readonly" in mount for mount in sut_mounts)
    assert any("type=volume" in mount and "target=/data" in mount for mount in sut_mounts)
    assert all("acceptance_tests" not in mount for mount in sut_mounts)
    assert "DATABASE_URL=sqlite+aiosqlite:////data/agent-test.db" in sut_run
    readiness_probe = calls[5]
    assert readiness_probe[:2] == ["docker", "exec"]
    assert "acceptance_tests" in readiness_probe[-1]

    driver_mounts = [
        driver_run[index + 1] for index, value in enumerate(driver_run) if value == "--mount"
    ]
    assert len(driver_mounts) == 1
    assert "target=/acceptance_tests,readonly" in driver_mounts[0]
    assert all("target=/workspace" not in mount for mount in driver_mounts)
    assert "SUT_BASE_URL=http://sut:8000" in driver_run
    assert str(request.workspace.resolve()) not in " ".join(driver_run)

    assert calls[-4][0:3] == ["docker", "rm", "-f"]
    assert calls[-3][0:3] == ["docker", "rm", "-f"]
    assert calls[-2][0:3] == ["docker", "network", "rm"]
    assert calls[-1][0:3] == ["docker", "volume", "rm"]


def test_acceptance_cleanup_runs_when_driver_times_out(tmp_path: Path) -> None:
    digest = _blackbox_profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            _completed([], stdout="network"),
            _completed([], stdout="volume"),
            _completed([], stdout="sut-container"),
            _completed([], stdout="ready"),
            subprocess.TimeoutExpired(["docker", "run"], 30),
            _completed([], stderr="No such container"),
            _completed([], stdout="sut removed"),
            _completed([], stdout="network removed"),
            _completed([], stdout="volume removed"),
        ]
    )
    runner = DockerRunner(_blackbox_profile(), executor=executor)

    result = runner.run(_request(tmp_path, "acceptance_test"))

    assert result.exit_code == 124
    assert result.failures == (("runner_timeout", "fixed Docker timeout exceeded"),)
    calls = [argv for argv, _ in executor.calls]
    assert calls[-4][0:3] == ["docker", "rm", "-f"]
    assert calls[-3][0:3] == ["docker", "rm", "-f"]
    assert calls[-2][0:3] == ["docker", "network", "rm"]
    assert calls[-1][0:3] == ["docker", "volume", "rm"]


def test_acceptance_cancel_removes_only_generation_scoped_resources() -> None:
    executor = FakeExecutor([_completed([], stderr="No such container") for _ in range(5)])
    runner = DockerRunner(_blackbox_profile(), executor=executor)

    runner.cancel("run-123", 7)

    calls = [argv for argv, _ in executor.calls]
    assert calls == [
        ["docker", "rm", "-f", "ai-agent-run-123-7"],
        ["docker", "rm", "-f", "ai-agent-run-123-7-driver"],
        ["docker", "rm", "-f", "ai-agent-run-123-7-sut"],
        ["docker", "network", "rm", "ai-agent-run-123-7-net"],
        ["docker", "volume", "rm", "ai-agent-run-123-7-db"],
    ]


def test_acceptance_resource_names_are_bounded_and_generation_specific(tmp_path: Path) -> None:
    base_request = _request(tmp_path)
    request = RunnerRequest(
        run_id="r" * 128,
        lease_generation=17,
        command_id="acceptance_test",
        workspace=base_request.workspace,
        baseline_tests=base_request.baseline_tests,
        acceptance_tests=base_request.acceptance_tests,
        output_dir=base_request.output_dir,
    )
    digest = _blackbox_profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            _completed([], stdout="network"),
            _completed([], stdout="volume"),
            _completed([], stdout="sut"),
            _completed([], stdout="ready"),
            _completed([], stdout="1 passed"),
            *[_completed([], stderr="No such container") for _ in range(4)],
        ]
    )

    DockerRunner(_blackbox_profile(), executor=executor).run(request)

    created_names = [executor.calls[2][0][-1], executor.calls[3][0][-1]]
    sut_argv = executor.calls[4][0]
    driver_argv = executor.calls[6][0]
    created_names.extend((sut_argv[sut_argv.index("--name") + 1], driver_argv[driver_argv.index("--name") + 1]))
    assert all(len(name) <= 128 for name in created_names)
    assert all("17" in name for name in created_names)


def test_acceptance_fails_closed_when_resource_cleanup_fails(tmp_path: Path) -> None:
    digest = _blackbox_profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            _completed([], stdout="network"),
            _completed([], stdout="volume"),
            _completed([], stdout="sut"),
            _completed([], stdout="ready"),
            _completed([], stdout="1 passed"),
            _completed([], stderr="No such container"),
            _completed([], stdout="sut removed"),
            _completed([], returncode=1, stderr="network is still in use"),
            _completed([], stdout="volume removed"),
        ]
    )

    with pytest.raises(RunnerError, match="cleanup failed"):
        DockerRunner(_blackbox_profile(), executor=executor).run(
            _request(tmp_path, "acceptance_test")
        )


def test_primary_docker_error_is_not_masked_by_cleanup_failure(tmp_path: Path) -> None:
    digest = _blackbox_profile().expected_digest
    executor = FakeExecutor(
        [
            _completed([], stdout="27.1.1\n"),
            _completed([], stdout='[{"Id":"' + digest + '","RepoDigests":[]}]'),
            _completed([], stdout="network"),
            _completed([], stdout="volume"),
            OSError("docker pipe broke"),
            _completed([], stderr="No such container"),
            _completed([], stderr="No such container"),
            _completed([], returncode=1, stderr="network is still in use"),
            _completed([], stdout="volume removed"),
        ]
    )

    with pytest.raises(DockerUnavailableError, match="Docker became unavailable"):
        DockerRunner(_blackbox_profile(), executor=executor).run(
            _request(tmp_path, "acceptance_test")
        )
