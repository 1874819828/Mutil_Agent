from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.acceptance.registry import (
    AcceptancePackChangedError,
    AcceptancePackNotRunnableError,
    AcceptancePackRegistry,
    AcceptancePackSecurityError,
    AcceptancePackValidationError,
)


RUNNER_DIGEST = "sha256:" + "a" * 64


def _write_pack(
    config_root: Path,
    *,
    pack_id: str = "filter-grade-v1",
    project_id: str = "student-management-backend",
    task_spec: str = "为班级列表增加 grade 过滤。",
    test_dir: str = "filter-grade-v1",
    status: str = "ready",
    command: str = "[python, -m, pytest, -q, /acceptance]",
    harness_files: str = "[http_probe.py]",
) -> Path:
    config_root.mkdir(parents=True, exist_ok=True)
    path = config_root / f"{pack_id}.yaml"
    path.write_text(
        f"""
pack_id: {pack_id}
project_id: {project_id}
version: 1
status: {status}
task_spec: {task_spec}
test_dir: {test_dir}
runner:
  image: runner:test
  digest: {RUNNER_DIGEST}
  command: {command}
dependencies:
  - httpx==0.28.1
harness_files: {harness_files}
""".strip(),
        encoding="utf-8",
    )
    return path


def _ready_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    config = tmp_path / "config"
    tests = tmp_path / "tests"
    harness = tmp_path / "harness"
    case = tests / "filter-grade-v1"
    case.mkdir(parents=True)
    (case / "test_filter.py").write_text("def test_filter():\n    assert True\n", encoding="utf-8")
    harness.mkdir()
    (harness / "http_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_pack(config)
    return config, tests, harness


def test_registry_loads_pack_and_binds_requirement_to_immutable_suite(
    tmp_path: Path,
) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)

    pack = registry.resolve_for_run(
        "filter-grade-v1",
        project_id="student-management-backend",
        requirement="  为班级列表增加 grade 过滤。\n",
    )

    assert pack.version == 1
    assert pack.test_dir == (tests / "filter-grade-v1").resolve()
    assert len(pack.task_spec_hash) == 64
    assert len(pack.suite_hash) == 64
    assert pack.test_files == ("test_filter.py",)
    assert pack.harness_files == ("http_probe.py",)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("spec", "为班级列表增加 school_year 过滤。"),
        ("command", "[python, -m, pytest, -vv, /acceptance]"),
        ("dependency", "httpx==0.29.0"),
    ],
)
def test_suite_hash_covers_spec_command_and_dependencies(
    tmp_path: Path, field: str, replacement: str
) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    first = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )
    text = (config / "filter-grade-v1.yaml").read_text(encoding="utf-8")
    if field == "spec":
        text = text.replace("为班级列表增加 grade 过滤。", replacement)
    elif field == "command":
        text = text.replace("[python, -m, pytest, -q, /acceptance]", replacement)
    else:
        text = text.replace("httpx==0.28.1", replacement)
    (config / "filter-grade-v1.yaml").write_text(text, encoding="utf-8")
    second = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )

    assert first.suite_hash != second.suite_hash


def test_suite_hash_covers_registered_test_directory_path(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    clone = tests / "filter-grade-v1-copy"
    clone.mkdir()
    clone.joinpath("test_filter.py").write_bytes(
        (tests / "filter-grade-v1" / "test_filter.py").read_bytes()
    )
    first = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )
    path = config / "filter-grade-v1.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "test_dir: filter-grade-v1", "test_dir: filter-grade-v1-copy"
        ),
        encoding="utf-8",
    )
    second = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )

    assert first.suite_hash != second.suite_hash


def test_suite_hash_covers_every_test_and_harness_byte(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)
    first = registry.get("filter-grade-v1")

    (tests / "filter-grade-v1" / "test_filter.py").write_text(
        "def test_filter():\n    assert False\n", encoding="utf-8"
    )
    second = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )
    (harness / "http_probe.py").write_text("VALUE = 2\n", encoding="utf-8")
    third = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )

    assert first.suite_hash != second.suite_hash
    assert second.suite_hash != third.suite_hash
    with pytest.raises(AcceptancePackChangedError):
        registry.assert_unchanged(first)


def test_suite_hash_ignores_only_generated_python_and_pytest_caches(
    tmp_path: Path,
) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    first = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )
    case = tests / "filter-grade-v1"
    cache = case / "__pycache__"
    cache.mkdir()
    (cache / "test_filter.cpython-312.pyc").write_bytes(b"generated bytecode")
    pytest_cache = case / ".pytest_cache" / "v" / "cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "nodeids").write_text("[]", encoding="utf-8")
    for generated in (
        cache / "test_filter.cpython-312.pyc",
        pytest_cache / "nodeids",
    ):
        os.utime(generated, (100.0, 100.0))

    second = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness).get(
        "filter-grade-v1"
    )

    assert second.suite_hash == first.suite_hash
    assert second.test_files == ("test_filter.py",)

    legacy_hash = AcceptancePackRegistry(
        config, tests_root=tests, harness_root=harness
    )._legacy_suite_hash("filter-grade-v1", bound_at=200.0)
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)
    assert legacy_hash != first.suite_hash
    assert registry.matches_bound_hash(
        "filter-grade-v1", legacy_hash, bound_at=200.0
    )

    newer = cache / "test_filter.cpython-313.pyc"
    newer.write_bytes(b"new cache created after the run")
    os.utime(newer, (300.0, 300.0))
    assert AcceptancePackRegistry(
        config, tests_root=tests, harness_root=harness
    ).matches_bound_hash("filter-grade-v1", legacy_hash, bound_at=200.0)

    (case / "test_filter.py").write_text(
        "def test_filter():\n    assert False\n", encoding="utf-8"
    )
    assert not AcceptancePackRegistry(
        config, tests_root=tests, harness_root=harness
    ).matches_bound_hash("filter-grade-v1", legacy_hash, bound_at=200.0)


def test_assert_unchanged_reloads_registry_config_from_disk(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)
    frozen = registry.get("filter-grade-v1")
    path = config / "filter-grade-v1.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[python, -m, pytest, -q, /acceptance]",
            "[python, -m, pytest, -vv, /acceptance]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(AcceptancePackChangedError):
        registry.assert_unchanged(frozen)


def test_requirement_and_project_must_match_pack(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)

    with pytest.raises(AcceptancePackValidationError, match="project"):
        registry.resolve_for_run(
            "filter-grade-v1", project_id="other", requirement="为班级列表增加 grade 过滤。"
        )
    with pytest.raises(AcceptancePackValidationError, match="requirement"):
        registry.resolve_for_run(
            "filter-grade-v1",
            project_id="student-management-backend",
            requirement="做另一件事",
        )


@pytest.mark.parametrize("test_dir", ["../outside", "C:/outside", "/outside"])
def test_registry_forbids_arbitrary_test_paths(tmp_path: Path, test_dir: str) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    _write_pack(config, test_dir=test_dir)

    with pytest.raises(AcceptancePackSecurityError):
        AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)


def test_registry_rejects_symlinks_inside_test_tree(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    link = tests / "filter-grade-v1" / "linked.py"
    try:
        os.symlink(harness / "http_probe.py", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(AcceptancePackSecurityError, match="link|reparse"):
        AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)


def test_draft_skeleton_can_be_listed_but_not_selected_for_a_run(tmp_path: Path) -> None:
    config = tmp_path / "config"
    tests = tmp_path / "tests"
    harness = tmp_path / "harness"
    tests.mkdir()
    harness.mkdir()
    _write_pack(
        config,
        pack_id="draft-v1",
        test_dir="not-authored-yet",
        status="draft",
        harness_files="[]",
    )
    registry = AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)

    assert registry.get("draft-v1").status == "draft"
    with pytest.raises(AcceptancePackNotRunnableError):
        registry.resolve_for_run(
            "draft-v1",
            project_id="student-management-backend",
            requirement="为班级列表增加 grade 过滤。",
        )


def test_duplicate_pack_ids_are_rejected(tmp_path: Path) -> None:
    config, tests, harness = _ready_tree(tmp_path)
    duplicate = config / "duplicate.yaml"
    duplicate.write_text(
        (config / "filter-grade-v1.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    with pytest.raises(AcceptancePackValidationError, match="duplicate"):
        AcceptancePackRegistry(config, tests_root=tests, harness_root=harness)
