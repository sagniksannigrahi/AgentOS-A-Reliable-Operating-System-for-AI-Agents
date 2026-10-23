"""Per-task isolated workspaces.

Layout::

    <workspace_root>/<task_id>/repo   # the copy agents read and modify (through tools only)
    <workspace_root>/<task_id>/base   # pristine baseline, used for diffs and change review
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

SEED_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
PYTEST_INI = ".agentos-pytest.ini"
PYTEST_INI_CONTENT = "[pytest]\npythonpath = .\naddopts = -p no:cacheprovider\n"


def list_seeds(seed_dir: Path) -> list[dict[str, str]]:
    seeds: list[dict[str, str]] = []
    if not seed_dir.is_dir():
        return seeds
    for p in sorted(seed_dir.iterdir()):
        if p.is_dir() and SEED_NAME.match(p.name):
            desc = ""
            readme = p / "README.md"
            if readme.is_file():
                for line in readme.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        desc = line.lstrip("# ").strip()
                        break
            seeds.append({"name": p.name, "description": desc})
    return seeds


def seed_exists(seed_dir: Path, name: str) -> bool:
    return bool(SEED_NAME.match(name)) and (seed_dir / name).is_dir()


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for f in sorted(filenames):
            if f.endswith(".pyc"):
                continue
            files.append(Path(dirpath) / f)
    return files


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


@dataclass
class Workspace:
    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def base(self) -> Path:
        return self.root / "base"

    @classmethod
    def for_task(cls, workspace_root: Path, task_id: str) -> Workspace:
        return cls(workspace_root / task_id)

    def exists(self) -> bool:
        return self.repo.is_dir() and self.base.is_dir()

    def create(self, seed_dir: Path, seed: str) -> None:
        """Idempotent: an existing workspace (e.g. after crash recovery) is left untouched."""
        if self.exists():
            return
        if not seed_exists(seed_dir, seed):
            raise FileNotFoundError(f"unknown workspace seed '{seed}'")
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = shutil.ignore_patterns(*IGNORED_DIRS, "*.pyc")
        staging = Path(tempfile.mkdtemp(dir=self.root, prefix=".staging-"))
        try:
            shutil.copytree(seed_dir / seed, staging / "repo", ignore=ignore, symlinks=True)
            ini = staging / "repo" / PYTEST_INI
            if not ini.exists():
                ini.write_text(PYTEST_INI_CONTENT, encoding="utf-8")
            shutil.copytree(staging / "repo", staging / "base", symlinks=True)
            os.replace(staging / "base", self.base)
            os.replace(staging / "repo", self.repo)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def reset_to_base(self) -> None:
        """Discard all changes: the working copy becomes identical to the pristine baseline."""
        staging = Path(tempfile.mkdtemp(dir=self.root, prefix=".reset-"))
        try:
            shutil.copytree(self.base, staging / "repo", symlinks=True)
            old = self.root / f".old-{os.getpid()}-{id(staging)}"
            os.replace(self.repo, old)
            os.replace(staging / "repo", self.repo)
            shutil.rmtree(old, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def manifest(self, which: str = "repo") -> dict[str, str]:
        root = self.repo if which == "repo" else self.base
        return {str(p.relative_to(root)).replace(os.sep, "/"): _sha(p) for p in _iter_files(root) if not p.is_symlink()}

    def fingerprint(self) -> str:
        """Content hash of the working copy: identical code => identical fingerprint."""
        items = sorted(self.manifest().items())
        return hashlib.sha256(repr(items).encode()).hexdigest()[:16]

    def changed_files(self) -> list[tuple[str, str]]:
        cur, base = self.manifest("repo"), self.manifest("base")
        changes: list[tuple[str, str]] = []
        for path in sorted(set(cur) | set(base)):
            if path not in base:
                changes.append((path, "added"))
            elif path not in cur:
                changes.append((path, "deleted"))
            elif cur[path] != base[path]:
                changes.append((path, "modified"))
        return changes

    def diff(self, max_bytes: int = 400_000) -> str:
        chunks: list[str] = []
        for path, status in self.changed_files():
            a, b = self.base / path, self.repo / path
            if (a.exists() and not _is_text(a)) or (b.exists() and not _is_text(b)):
                chunks.append(f"Binary file {path} {status}\n")
                continue
            old = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if a.exists() else []
            new = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if b.exists() else []
            chunks.extend(
                difflib.unified_diff(
                    old,
                    new,
                    fromfile=f"a/{path}" if a.exists() else "/dev/null",
                    tofile=f"b/{path}" if b.exists() else "/dev/null",
                )
            )
            if chunks and not chunks[-1].endswith("\n"):
                chunks.append("\n")
        text = "".join(chunks)
        if len(text) > max_bytes:
            text = text[:max_bytes] + "\n…[diff truncated]\n"
        return text

    def added_lines(self) -> dict[str, list[str]]:
        """Lines added per changed file (used by the security auditor)."""
        result: dict[str, list[str]] = {}
        current: str | None = None
        for line in self.diff().splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("+") and not line.startswith("+++") and current:
                result.setdefault(current, []).append(line[1:])
        return result


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
