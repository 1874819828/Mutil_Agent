"""Portable relative path validation and canonical filesystem checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath

from .errors import InvalidRelativePathError, UnsafeFilesystemEntryError


_WINDOWS_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_relative_path(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise InvalidRelativePathError("path must be text")
    if not raw or "\x00" in raw or ":" in raw:
        raise InvalidRelativePathError("path is empty or contains NUL/NTFS ADS syntax")
    windows = PureWindowsPath(raw)
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or windows.is_absolute() or bool(windows.drive):
        raise InvalidRelativePathError("absolute, UNC, and drive-qualified paths are forbidden")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidRelativePathError("path traversal and non-canonical components are forbidden")
    for part in parts:
        if part.rstrip(" .") != part:
            raise InvalidRelativePathError("Windows-trimmed path components are forbidden")
        base_name = part.split(".", 1)[0].upper()
        if base_name in _WINDOWS_DEVICES:
            raise InvalidRelativePathError("Windows device names are forbidden")
    return "/".join(parts)


def is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"cannot inspect filesystem entry: {path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def reject_link_or_reparse(path: Path) -> None:
    if path.is_symlink() or is_reparse_point(path):
        raise UnsafeFilesystemEntryError(f"symbolic link or reparse point is forbidden: {path}")


def reject_reparse_ancestors(path: Path) -> None:
    """Reject an existing lexical ancestor that redirects through a reparse point."""

    existing: list[Path] = []
    cursor = path.absolute()
    while True:
        if cursor.exists() or cursor.is_symlink():
            existing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(existing):
        reject_link_or_reparse(item)


def canonical_root(path: Path, *, must_exist: bool = True) -> Path:
    if must_exist and not path.exists():
        raise UnsafeFilesystemEntryError(f"filesystem root does not exist: {path}")
    reject_reparse_ancestors(path)
    try:
        root = path.resolve(strict=must_exist)
    except OSError as exc:
        raise UnsafeFilesystemEntryError(f"cannot resolve filesystem root: {path}") from exc
    if must_exist and not root.is_dir():
        raise UnsafeFilesystemEntryError(f"filesystem root is not a directory: {path}")
    return root


def resolve_within(
    root: Path, relative_path: str | os.PathLike[str], *, must_exist: bool = False
) -> Path:
    normalized = normalize_relative_path(relative_path)
    root_canonical = canonical_root(root, must_exist=True)
    candidate = root.joinpath(*normalized.split("/"))

    cursor = root
    for component in normalized.split("/"):
        cursor = cursor / component
        if cursor.exists() or cursor.is_symlink():
            reject_link_or_reparse(cursor)
        else:
            break
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root_canonical)
    except (OSError, ValueError) as exc:
        raise UnsafeFilesystemEntryError(
            f"path escapes the canonical workspace root: {normalized}"
        ) from exc
    return resolved


def roots_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve(strict=False)
    second_resolved = second.resolve(strict=False)
    try:
        first_resolved.relative_to(second_resolved)
        return True
    except ValueError:
        pass
    try:
        second_resolved.relative_to(first_resolved)
        return True
    except ValueError:
        return False

