"""Strict, content-addressed Acceptance Pack registry.

Only administrator-authored YAML files under the configured registry may name a
test suite.  API callers select an opaque ``pack_id``; they never submit a local
path or command.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.workspace.errors import InvalidRelativePathError, UnsafeFilesystemEntryError
from app.workspace.paths import canonical_root, is_reparse_point, normalize_relative_path, resolve_within


class AcceptancePackError(RuntimeError):
    pass


class AcceptancePackValidationError(AcceptancePackError, ValueError):
    pass


class AcceptancePackSecurityError(AcceptancePackError, ValueError):
    pass


class AcceptancePackNotRunnableError(AcceptancePackError):
    pass


class AcceptancePackChangedError(AcceptancePackError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class _Runner(_StrictModel):
    image: str = Field(min_length=1)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    command: tuple[str, ...] = Field(min_length=1)

    @field_validator("command")
    @classmethod
    def safe_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not token or "\x00" in token for token in value):
            raise ValueError("runner command must be non-empty argv tokens")
        return value


class _PackConfig(_StrictModel):
    pack_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    project_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    version: int = Field(gt=0)
    status: Literal["draft", "ready"]
    task_spec: str = Field(min_length=1)
    test_dir: str = Field(min_length=1)
    runner: _Runner
    dependencies: tuple[str, ...] = ()
    harness_files: tuple[str, ...] = ()

    @field_validator("test_dir")
    @classmethod
    def test_dir_is_one_component(cls, value: str) -> str:
        normalized = normalize_relative_path(value)
        if "/" in normalized:
            raise ValueError("test_dir must be a registry-owned directory name")
        return normalized

    @field_validator("harness_files")
    @classmethod
    def harness_paths_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(normalize_relative_path(item) for item in value)
        except InvalidRelativePathError as exc:
            raise ValueError(str(exc)) from exc
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("harness file paths must be unique")
        return normalized

    @field_validator("dependencies")
    @classmethod
    def dependencies_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item or "\n" in item for item in value):
            raise ValueError("dependencies must be single-line requirement strings")
        if len({item.casefold() for item in value}) != len(value):
            raise ValueError("dependencies must be unique")
        return value


@dataclass(frozen=True, slots=True)
class AcceptancePack:
    pack_id: str
    project_id: str
    version: int
    status: str
    task_spec: str
    task_spec_hash: str
    test_dir: Path | None
    test_files: tuple[str, ...]
    runner_image: str
    runner_digest: str
    command: tuple[str, ...]
    dependencies: tuple[str, ...]
    harness_files: tuple[str, ...]
    suite_hash: str


def _normalized_requirement(value: str) -> str:
    return " ".join(value.split())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_GENERATED_CACHE_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _is_generated_cache_path(relative: str) -> bool:
    parts = tuple(part.casefold() for part in relative.split("/"))
    if any(part in _GENERATED_CACHE_DIRECTORIES for part in parts):
        return True
    name = parts[-1]
    return name.endswith((".pyc", ".pyo")) or name == ".coverage" or name.startswith(
        ".coverage."
    )


def _tree_bytes(
    root: Path, *, generated_before: float | None = None
) -> tuple[tuple[str, bytes], ...]:
    root = canonical_root(root)
    result: list[tuple[str, bytes]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise AcceptancePackSecurityError(f"cannot scan acceptance tree: {directory}") from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise AcceptancePackSecurityError(f"cannot inspect acceptance entry: {relative}") from exc
            attributes = getattr(metadata, "st_file_attributes", 0)
            if child.is_symlink() or is_reparse_point(path) or attributes & 0x400:
                raise AcceptancePackSecurityError(
                    f"symbolic link or reparse point is forbidden: {relative}"
                )
            generated = _is_generated_cache_path(relative)
            if child.is_dir(follow_symlinks=False):
                # Test discovery/import may create cache directories beside an
                # immutable suite.  Canonical hashes ignore them.  The bounded
                # legacy mode traverses them only to verify hashes created by
                # older versions that accidentally included generated files.
                if generated and generated_before is None:
                    continue
                pending.append(path)
                continue
            if not child.is_file(follow_symlinks=False):
                raise AcceptancePackSecurityError(f"non-regular acceptance entry: {relative}")
            if generated and (
                generated_before is None or metadata.st_mtime > generated_before
            ):
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise AcceptancePackSecurityError(f"cannot read acceptance file: {relative}") from exc
            if len(data) != metadata.st_size:
                raise AcceptancePackChangedError(f"acceptance file changed while hashing: {relative}")
            result.append((relative, data))
    return tuple(sorted(result, key=lambda item: item[0].casefold()))


class AcceptancePackRegistry:
    def __init__(self, config_root: Path, *, tests_root: Path, harness_root: Path) -> None:
        self.config_root = canonical_root(config_root)
        self.tests_root = canonical_root(tests_root)
        self.harness_root = canonical_root(harness_root)
        self._configs: dict[str, tuple[_PackConfig, Path]] = {}
        self._load_configs()

    def _load_configs(self) -> None:
        for path in sorted(self.config_root.glob("*.yaml"), key=lambda item: item.name.casefold()):
            try:
                if path.is_symlink() or is_reparse_point(path):
                    raise AcceptancePackSecurityError(f"linked registry file is forbidden: {path.name}")
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                config = _PackConfig.model_validate(raw)
            except AcceptancePackSecurityError:
                raise
            except ValidationError as exc:
                if any(error.get("loc") == ("test_dir",) for error in exc.errors()):
                    raise AcceptancePackSecurityError(
                        f"unsafe acceptance test directory in {path.name}: {exc}"
                    ) from exc
                raise AcceptancePackValidationError(
                    f"invalid acceptance pack config {path.name}: {exc}"
                ) from exc
            except (OSError, UnicodeError, yaml.YAMLError, TypeError) as exc:
                raise AcceptancePackValidationError(
                    f"invalid acceptance pack config {path.name}: {exc}"
                ) from exc
            folded = config.pack_id.casefold()
            if folded in self._configs:
                raise AcceptancePackValidationError(f"duplicate acceptance pack id: {config.pack_id}")
            self._configs[folded] = (config, path)
            if config.status == "ready":
                self._materialize(config)

    def list(self) -> tuple[AcceptancePack, ...]:
        return tuple(self._materialize(config) for config, _ in self._configs.values())

    def get(self, pack_id: str) -> AcceptancePack:
        item = self._configs.get(pack_id.casefold())
        if item is None:
            raise AcceptancePackValidationError(f"unknown acceptance pack id: {pack_id}")
        return self._materialize(item[0])

    def resolve_for_run(
        self, pack_id: str, *, project_id: str, requirement: str
    ) -> AcceptancePack:
        pack = self.get(pack_id)
        if pack.status != "ready":
            raise AcceptancePackNotRunnableError(f"acceptance pack is not ready: {pack_id}")
        if pack.project_id != project_id:
            raise AcceptancePackValidationError("acceptance pack project does not match the run")
        if _normalized_requirement(pack.task_spec) != _normalized_requirement(requirement):
            raise AcceptancePackValidationError("run requirement does not match acceptance task spec")
        return pack

    def assert_unchanged(self, frozen: AcceptancePack) -> None:
        try:
            # Config, command, dependency, and runner changes must be detected too;
            # the in-memory parsed config is intentionally not trusted for a
            # post-approval immutability check.
            current = AcceptancePackRegistry(
                self.config_root,
                tests_root=self.tests_root,
                harness_root=self.harness_root,
            ).get(frozen.pack_id)
        except AcceptancePackError as exc:
            raise AcceptancePackChangedError(
                f"acceptance pack became invalid after run binding: {frozen.pack_id}"
            ) from exc
        if current.suite_hash != frozen.suite_hash:
            raise AcceptancePackChangedError(
                f"acceptance pack changed after run binding: {frozen.pack_id}"
            )

    def matches_bound_hash(
        self, pack_id: str, bound_hash: str | None, *, bound_at: float
    ) -> bool:
        """Match canonical hashes and cache-polluted hashes from older runs.

        Before cache filtering was introduced, local ``.pyc`` and pytest cache
        files were accidentally included.  A legacy hash is accepted only when
        the current authored suite plus generated files whose mtime predates
        the run reconstruct that exact SHA-256.  Newer cache files cannot
        affect the result, and any authored test/config change still fails.
        """

        if bound_hash is None:
            return False
        current = self.get(pack_id)
        if current.suite_hash == bound_hash:
            return True
        item = self._configs.get(pack_id.casefold())
        if item is None:
            return False
        return self._legacy_suite_hash(pack_id, bound_at=bound_at) == bound_hash

    def _legacy_suite_hash(self, pack_id: str, *, bound_at: float) -> str:
        item = self._configs.get(pack_id.casefold())
        if item is None:
            raise AcceptancePackValidationError(f"unknown acceptance pack id: {pack_id}")
        return self._materialize(item[0], generated_before=bound_at).suite_hash

    def _materialize(
        self, config: _PackConfig, *, generated_before: float | None = None
    ) -> AcceptancePack:
        normalized_spec = _normalized_requirement(config.task_spec)
        task_spec_hash = _sha256(normalized_spec.encode("utf-8"))
        if config.status == "draft":
            test_dir: Path | None = None
            test_bytes: tuple[tuple[str, bytes], ...] = ()
            harness_bytes: tuple[tuple[str, bytes], ...] = ()
        else:
            try:
                test_dir = resolve_within(self.tests_root, config.test_dir, must_exist=True)
                test_bytes = _tree_bytes(
                    test_dir, generated_before=generated_before
                )
            except (InvalidRelativePathError, UnsafeFilesystemEntryError, OSError) as exc:
                raise AcceptancePackSecurityError(
                    f"unsafe acceptance test directory for {config.pack_id}"
                ) from exc
            if not test_bytes:
                raise AcceptancePackValidationError(
                    f"ready acceptance pack contains no test files: {config.pack_id}"
                )
            harness_items: list[tuple[str, bytes]] = []
            for relative in config.harness_files:
                try:
                    path = resolve_within(self.harness_root, relative, must_exist=True)
                except (InvalidRelativePathError, UnsafeFilesystemEntryError, OSError) as exc:
                    raise AcceptancePackSecurityError(
                        f"unsafe harness path for {config.pack_id}: {relative}"
                    ) from exc
                if not path.is_file():
                    raise AcceptancePackSecurityError(f"harness path is not a file: {relative}")
                harness_items.append((relative, path.read_bytes()))
            harness_bytes = tuple(sorted(harness_items, key=lambda item: item[0].casefold()))

        suite_payload = {
            "pack_id": config.pack_id,
            "project_id": config.project_id,
            "version": config.version,
            "task_spec": normalized_spec,
            "task_spec_hash": task_spec_hash,
            "test_dir": config.test_dir,
            "runner": {
                "image": config.runner.image,
                "digest": config.runner.digest,
                "command": list(config.runner.command),
            },
            "dependencies": list(config.dependencies),
            "tests": [
                {"path": path, "sha256": _sha256(data), "size": len(data)}
                for path, data in test_bytes
            ],
            "harness": [
                {"path": path, "sha256": _sha256(data), "size": len(data)}
                for path, data in harness_bytes
            ],
        }
        suite_hash = _sha256(
            json.dumps(
                suite_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        return AcceptancePack(
            pack_id=config.pack_id,
            project_id=config.project_id,
            version=config.version,
            status=config.status,
            task_spec=config.task_spec,
            task_spec_hash=task_spec_hash,
            test_dir=test_dir,
            test_files=tuple(path for path, _ in test_bytes),
            runner_image=config.runner.image,
            runner_digest=config.runner.digest,
            command=config.runner.command,
            dependencies=config.dependencies,
            harness_files=tuple(path for path, _ in harness_bytes),
            suite_hash=suite_hash,
        )
