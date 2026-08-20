from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config.project_profiles import ProjectProfile, freeze_project_profile
from app.context.repository import (
    ContextBudget,
    ContextBudgetExceededError,
    InspectionRequest,
    RepositoryChangedError,
    RepositoryContextService,
    UnsafeContextContentError,
    UnsafeContextPathError,
)


def _profile(
    source: Path,
    *,
    max_file_mb: float = 1,
    max_files: int = 20,
    max_workspace_mb: float = 2,
) -> object:
    return freeze_project_profile(
        ProjectProfile.model_validate(
            {
                "project_id": "demo",
                "source_path": source,
                "repository_path": source,
                "include_globs": ["src/**/*.py", "README.md"],
                "exclude_globs": ["src/generated/**"],
                "runner": "python-fastapi",
                "commands": {
                    "syntax": ["python", "-m", "compileall", "src"],
                    "baseline_test": ["python", "-m", "pytest", "/baseline"],
                    "acceptance_test": ["python", "-m", "pytest", "/acceptance"],
                },
                "runner_image": "runner:test",
                "runner_digest": "sha256:" + "1" * 64,
                "limits": {
                    "timeout_seconds": 30,
                    "memory_mb": 256,
                    "cpus": 1,
                    "max_files": max_files,
                    "max_file_mb": max_file_mb,
                    "max_workspace_mb": max_workspace_mb,
                },
            }
        )
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / ".env").write_text("OPENAI_API_KEY=must-not-leak\n", encoding="utf-8")
    (root / "notes.txt").write_text("outside profile\n", encoding="utf-8")
    return root


def test_index_and_bundle_only_expose_approved_utf8_text(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = RepositoryContextService(_profile(root))

    index = service.build_index()
    bundle = service.build_bundle(
        index,
        InspectionRequest(paths=("src/app.py", "README.md")),
        round_number=1,
    )

    assert [entry.path for entry in index.entries] == [
        "README.md",
        "src/app.py",
        "src/util.py",
    ]
    assert ".env" in index.rejected_paths
    assert "notes.txt" not in {entry.path for entry in index.entries}
    assert [(item.path, item.content) for item in bundle.files] == [
        ("README.md", (root / "README.md").read_bytes().decode("utf-8")),
        ("src/app.py", (root / "src" / "app.py").read_bytes().decode("utf-8")),
    ]
    assert bundle.source_manifest_hash == index.digest
    assert len(bundle.bundle_hash) == 64
    assert all(len(item.sha256) == 64 for item in bundle.files)


@pytest.mark.parametrize(
    "requested",
    [
        "../outside.py",
        "C:/outside.py",
        "/outside.py",
        "src/*.py",
        ".env",
        "notes.txt",
        "src/generated/result.py",
    ],
)
def test_bundle_rejects_traversal_globs_sensitive_and_profile_external_paths(
    tmp_path: Path, requested: str
) -> None:
    root = _repository(tmp_path)
    service = RepositoryContextService(_profile(root))
    index = service.build_index()

    with pytest.raises(UnsafeContextPathError):
        service.build_bundle(
            index,
            InspectionRequest(paths=(requested,)),
            round_number=1,
        )


def test_bundle_rejects_duplicates_repository_mutation_and_secret_content(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    service = RepositoryContextService(_profile(root))
    index = service.build_index()

    with pytest.raises(ValueError, match="unique"):
        InspectionRequest(paths=("src/app.py", "SRC/APP.PY"))

    (root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RepositoryChangedError):
        service.build_bundle(
            index,
            InspectionRequest(paths=("src/app.py",)),
            round_number=1,
        )

    (root / "src" / "app.py").write_text(
        'OPENAI_API_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"\n',
        encoding="utf-8",
    )
    secret_index = service.build_index()
    assert "src/app.py" in secret_index.rejected_paths
    with pytest.raises(UnsafeContextContentError, match="secret"):
        service.build_bundle(
            secret_index,
            InspectionRequest(paths=("src/app.py",)),
            round_number=1,
        )


def test_binary_and_oversized_selected_files_are_never_exposed(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "src" / "binary.py").write_bytes(b"abc\x00def")
    (root / "src" / "large.py").write_text("x" * 100, encoding="utf-8")
    service = RepositoryContextService(
        _profile(root),
        budget=ContextBudget(
            max_files_per_round=5,
            round_byte_limits=(64, 64),
            max_total_files=8,
            max_total_bytes=100,
        ),
    )
    index = service.build_index()

    assert {"src/binary.py", "src/large.py"}.issubset(index.rejected_paths)
    for path in ("src/binary.py", "src/large.py"):
        with pytest.raises(UnsafeContextContentError):
            service.build_bundle(
                index,
                InspectionRequest(paths=(path,)),
                round_number=1,
            )


def test_repository_index_enforces_profile_file_and_total_byte_quotas(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    with pytest.raises(ContextBudgetExceededError, match="max_files"):
        RepositoryContextService(_profile(root, max_files=2)).build_index()

    with pytest.raises(ContextBudgetExceededError, match="workspace"):
        RepositoryContextService(
            _profile(root, max_file_mb=0.000015, max_workspace_mb=0.000015)
        ).build_index()


def test_two_round_context_budget_is_enforced_across_prior_bundles(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = RepositoryContextService(
        _profile(root),
        budget=ContextBudget(
            max_files_per_round=1,
            round_byte_limits=(20, 40),
            max_total_files=2,
            max_total_bytes=50,
        ),
    )
    index = service.build_index()
    first = service.build_bundle(
        index,
        InspectionRequest(paths=("src/app.py",)),
        round_number=1,
    )
    second = service.build_bundle(
        index,
        InspectionRequest(paths=("src/util.py",)),
        round_number=2,
        prior_bundles=(first,),
    )

    assert first.total_bytes + second.total_bytes <= 50
    with pytest.raises(ContextBudgetExceededError, match="two rounds"):
        service.build_bundle(
            index,
            InspectionRequest(paths=("README.md",)),
            round_number=3,
            prior_bundles=(first, second),
        )
    with pytest.raises(ContextBudgetExceededError, match="per round"):
        service.build_bundle(
            index,
            InspectionRequest(paths=("src/app.py", "README.md")),
            round_number=2,
            prior_bundles=(first,),
        )
    with pytest.raises(ContextBudgetExceededError, match="already inspected"):
        service.build_bundle(
            index,
            InspectionRequest(paths=("src/app.py",)),
            round_number=2,
            prior_bundles=(first,),
        )


def test_prior_bundle_must_be_the_first_round_of_the_same_snapshot(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    service = RepositoryContextService(_profile(root))
    index = service.build_index()
    first = service.build_bundle(
        index,
        InspectionRequest(paths=("src/app.py",)),
        round_number=1,
    )

    with pytest.raises(ValueError, match="first-round"):
        service.build_bundle(
            index,
            InspectionRequest(paths=("README.md",)),
            round_number=1,
            prior_bundles=(first,),
        )
    forged = type(first)(
        round_number=1,
        source_manifest_hash="0" * 64,
        files=first.files,
        total_bytes=first.total_bytes,
        bundle_hash=first.bundle_hash,
    )
    with pytest.raises(UnsafeContextPathError, match="snapshot"):
        service.build_bundle(
            index,
            InspectionRequest(paths=("README.md",)),
            round_number=2,
            prior_bundles=(forged,),
        )


def test_symlink_or_reparse_selected_path_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    link = root / "src" / "link.py"
    try:
        os.symlink(root / "src" / "app.py", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    service = RepositoryContextService(_profile(root))
    with pytest.raises(UnsafeContextPathError, match="link|reparse"):
        service.build_index()
