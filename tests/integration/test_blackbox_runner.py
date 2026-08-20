"""Opt-in smoke test for the real Docker black-box acceptance topology.

This test intentionally does not run in the default test suite.  It requires a
locally built runner image whose digest matches the project's frozen profile.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import load_frozen_project_profile
from app.sandbox import (
    DockerRunner,
    DockerUnavailableError,
    RunnerLimits,
    RunnerProfile,
    RunnerRequest,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DOCKER_INTEGRATION") != "1",
    reason="set RUN_DOCKER_INTEGRATION=1 to exercise the local Docker daemon",
)


def test_blackbox_runner_keeps_acceptance_suite_out_of_sut(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    frozen = load_frozen_project_profile(
        project_root / "config" / "projects" / "student-management-backend.yaml"
    )
    acceptance = tmp_path / "acceptance"
    baseline = tmp_path / "baseline"
    output = tmp_path / "output"
    for directory in (acceptance, baseline, output):
        directory.mkdir()

    (acceptance / "test_blackbox_boundary.py").write_text(
        """
import os
from pathlib import Path
import httpx

def test_driver_has_suite_but_no_workspace_and_can_reach_sut():
    assert Path('/acceptance_tests/test_blackbox_boundary.py').exists()
    assert not any(Path('/workspace').iterdir())
    response = httpx.get(os.environ['SUT_BASE_URL'] + '/openapi.json', timeout=10)
    assert response.status_code == 200
""".lstrip(),
        encoding="utf-8",
    )

    runner = DockerRunner(
        RunnerProfile(
            image=frozen.runner_image,
            expected_digest=frozen.runner_digest,
            commands={"acceptance_test": frozen.commands.acceptance_test},
            limits=RunnerLimits(
                timeout_seconds=frozen.limits.timeout_seconds,
                memory_mb=frozen.limits.memory_mb,
                cpus=frozen.limits.cpus,
            ),
        )
    )
    try:
        result = runner.run(
            RunnerRequest(
                run_id="docker-blackbox-smoke",
                lease_generation=1,
                command_id="acceptance_test",
                workspace=frozen.source_path,
                baseline_tests=baseline,
                acceptance_tests=acceptance,
                output_dir=output,
            )
        )
    except DockerUnavailableError as exc:
        pytest.skip(str(exc))

    assert result.exit_code == 0, result.stderr or result.stdout
