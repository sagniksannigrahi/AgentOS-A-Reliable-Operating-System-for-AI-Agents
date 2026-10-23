"""Workspace path validation: confinement, traversal and symlink protection, path policy."""

from __future__ import annotations

import fnmatch
import re
from enum import StrEnum
from pathlib import Path, PurePosixPath

from app.domain.errors import ToolDeniedError

MAX_PATH_LEN = 300

# Never readable or writable by agents.
FORBIDDEN_PATTERNS = (
    ".git",
    ".git/*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".agentos*",
    "*/.git/*",
    "**/.env",
)
# Writable only with explicit human approval: tests, CI and build configuration. An agent that
# edits the tests it is graded by can make a task "pass" without solving it.
SENSITIVE_WRITE_PATTERNS = (
    "tests/*",
    "test/*",
    "*/tests/*",
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
    "conftest.py",
    "*/conftest.py",
    ".github/*",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
    "tox.ini",
    "Dockerfile",
    "*.sh",
    "Makefile",
)

_DRIVE = re.compile(r"^[A-Za-z]:")


class WriteClass(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    FORBIDDEN = "forbidden"


class PathPolicyError(ToolDeniedError):
    pass


def _match(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def normalize_relative(rel: object) -> str:
    """Validate the *syntax* of an agent-supplied path and return it in normalized POSIX form."""
    if not isinstance(rel, str) or not rel.strip():
        raise PathPolicyError("path must be a non-empty string")
    if len(rel) > MAX_PATH_LEN:
        raise PathPolicyError("path too long")
    if "\x00" in rel or any(ord(c) < 32 for c in rel):
        raise PathPolicyError("path contains control characters")
    if "\\" in rel:
        raise PathPolicyError("backslashes are not allowed in paths")
    if rel.startswith("/") or rel.startswith("~") or _DRIVE.match(rel):
        raise PathPolicyError("absolute paths are not allowed", details={"path": rel})
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    if not parts:
        return "."
    if any(p == ".." for p in parts):
        raise PathPolicyError("path traversal ('..') is not allowed", details={"path": rel})
    return "/".join(parts)


def resolve_in_workspace(root: Path, rel: object, *, must_exist: bool = False) -> tuple[Path, str]:
    """Resolve ``rel`` inside ``root``. Returns (absolute path, normalized relative path).

    Rejects absolute paths, ``..`` components, symlinks anywhere along the path, and anything
    whose resolved location escapes the workspace root.
    """
    norm = normalize_relative(rel)
    root_real = root.resolve(strict=True)
    if norm == ".":
        return root_real, "."
    current = root_real
    for part in norm.split("/"):
        current = current / part
        if current.is_symlink():
            raise PathPolicyError("symlinks are not allowed in the workspace", details={"path": norm})
    resolved = current.resolve(strict=False)
    if not resolved.is_relative_to(root_real):
        raise PathPolicyError("path escapes the workspace", details={"path": norm})
    if _match(norm, FORBIDDEN_PATTERNS) or any(p == ".git" for p in norm.split("/")):
        raise PathPolicyError("access to this path is forbidden by policy", details={"path": norm})
    if must_exist and not resolved.exists():
        raise PathPolicyError("path does not exist", details={"path": norm})
    return resolved, norm


def classify_write(norm: str) -> WriteClass:
    if _match(norm, FORBIDDEN_PATTERNS):
        return WriteClass.FORBIDDEN
    if _match(norm, SENSITIVE_WRITE_PATTERNS):
        return WriteClass.SENSITIVE
    return WriteClass.NORMAL
