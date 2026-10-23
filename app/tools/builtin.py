"""The built-in tools. Handlers assume input was already validated and authorised by the gateway,
but still re-check paths themselves (defense in depth)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.config.settings import Settings
from app.persistence.db import Database
from app.persistence.models import ExecutionEvent, Task
from app.security.commands import validate_command
from app.security.injection import scan
from app.security.paths import PathPolicyError, WriteClass, classify_write, resolve_in_workspace
from app.tools.registry import RetryPolicy, ToolRegistry, ToolSpec
from app.tools.sandbox import Sandbox, classify_pytest
from app.tools.workspace import IGNORED_DIRS, Workspace, atomic_write

MAX_LIST = 500
MAX_MATCHES = 100


@dataclass
class ToolEnv:
    settings: Settings
    workspace: Workspace
    sandbox: Sandbox
    db: Database
    task_id: uuid.UUID


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------ list_files


class ListFilesIn(_In):
    path: str = Field(default=".", max_length=300)


class ListFilesOut(BaseModel):
    files: list[str]
    truncated: bool


async def list_files(env: ToolEnv, args: ListFilesIn) -> dict[str, Any]:
    root, _ = resolve_in_workspace(env.workspace.repo, args.path, must_exist=True)
    repo = env.workspace.repo.resolve()
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(repo))
        parts = rel.split("/")
        if any(x in IGNORED_DIRS for x in parts) or any(x.startswith(".agentos") for x in parts) or p.is_dir():
            continue
        try:
            resolve_in_workspace(env.workspace.repo, rel)
        except PathPolicyError:
            continue
        out.append(rel)
        if len(out) >= MAX_LIST:
            break
    return {"files": out, "truncated": len(out) >= MAX_LIST}


# ------------------------------------------------------------------------------- read_file


class ReadFileIn(_In):
    path: str = Field(max_length=300)


class ReadFileOut(BaseModel):
    path: str
    content: str
    size_bytes: int
    sha256: str
    truncated: bool
    injection_flags: list[str]


async def read_file(env: ToolEnv, args: ReadFileIn) -> dict[str, Any]:
    p, norm = resolve_in_workspace(env.workspace.repo, args.path, must_exist=True)
    if not p.is_file():
        raise PathPolicyError("not a regular file", details={"path": norm})
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        raise PathPolicyError("binary files cannot be read", details={"path": norm})
    limit = env.settings.max_file_size_bytes
    text = data[:limit].decode("utf-8", errors="replace")
    return {
        "path": norm,
        "content": text,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "truncated": len(data) > limit,
        "injection_flags": scan(text),
    }


# ---------------------------------------------------------------------------- search_files


class SearchIn(_In):
    query: str = Field(min_length=1, max_length=200)
    path: str = Field(default=".", max_length=300)


class SearchOut(BaseModel):
    matches: list[dict[str, Any]]
    truncated: bool


async def search_files(env: ToolEnv, args: SearchIn) -> dict[str, Any]:
    listing = await list_files(env, ListFilesIn(path=args.path))

    def _search() -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for rel in listing["files"]:
            p = env.workspace.repo / rel
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096] or len(data) > env.settings.max_file_size_bytes:
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if args.query in line:
                    matches.append({"path": rel, "line": i, "text": line[:300]})
                    if len(matches) >= MAX_MATCHES:
                        return matches
        return matches

    matches = await asyncio.to_thread(_search)
    return {"matches": matches, "truncated": len(matches) >= MAX_MATCHES}


# ------------------------------------------------------------------------------ write_file


class WriteFileIn(_In):
    path: str = Field(max_length=300)
    content: str = Field(max_length=256_000)


class WriteFileOut(BaseModel):
    path: str
    action: str
    sha256: str
    size_bytes: int


def _write_policy(env: ToolEnv, args: WriteFileIn) -> str | None:
    _, norm = resolve_in_workspace(env.workspace.repo, args.path)
    cls = classify_write(norm)
    if cls == WriteClass.FORBIDDEN:
        raise PathPolicyError("writing this path is forbidden", details={"path": norm})
    if len(args.content.encode("utf-8")) > env.settings.max_file_size_bytes:
        raise PathPolicyError("file too large", details={"path": norm})
    if cls == WriteClass.SENSITIVE:
        return f"write to protected path '{norm}' (tests/CI/build config) requires human approval"
    return None


async def write_file(env: ToolEnv, args: WriteFileIn) -> dict[str, Any]:
    _write_policy(env, args)  # re-check (defense in depth); approval was handled by the gateway
    p, norm = resolve_in_workspace(env.workspace.repo, args.path)
    existed = p.exists()
    atomic_write(p, args.content)
    data = args.content.encode("utf-8")
    return {
        "path": norm,
        "action": "modified" if existed else "created",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _write_verifier(env: ToolEnv, args: WriteFileIn) -> dict[str, Any] | None:
    """Did a write from a crashed run already take effect? Compare content hashes."""
    p, norm = resolve_in_workspace(env.workspace.repo, args.path)
    if not p.is_file():
        return None
    data = args.content.encode("utf-8")
    want = hashlib.sha256(data).hexdigest()
    if hashlib.sha256(p.read_bytes()).hexdigest() != want:
        return None
    return {"path": norm, "action": "modified", "sha256": want, "size_bytes": len(data)}


# ------------------------------------------------------------------------------- run_tests


class RunTestsIn(_In):
    command: list[str] = Field(min_length=1, max_length=16)


class RunTestsOut(BaseModel):
    command: list[str]
    outcome: str
    exit_code: int | None
    duration_ms: int
    output: str
    truncated: bool
    backend: str


def _tests_policy(env: ToolEnv, args: RunTestsIn) -> str | None:
    validate_command(args.command, env.workspace.repo)  # raises ToolDeniedError if not allowed
    return None


async def run_tests(env: ToolEnv, args: RunTestsIn) -> dict[str, Any]:
    cmd = validate_command(args.command, env.workspace.repo)
    res = await env.sandbox.run(cmd, env.workspace.repo.resolve())
    outcome = classify_pytest(res)
    if outcome == "infra_error":
        # Surface as a tool failure so the retry policy and failure classification apply.
        from app.domain.errors import ToolExecutionError

        raise ToolExecutionError(
            "test harness error (not a test failure)",
            details={"exit_code": res.exit_code, "output_tail": res.output[-2000:], "backend": res.backend},
        )
    return {
        "command": cmd.display(),
        "outcome": outcome,
        "exit_code": res.exit_code,
        "duration_ms": res.duration_ms,
        "output": res.output,
        "truncated": res.truncated,
        "backend": res.backend,
    }


# ---------------------------------------------------------------------------- task_history


class HistoryIn(_In):
    limit: int = Field(default=20, ge=1, le=100)


class HistoryOut(BaseModel):
    status: str
    events: list[dict[str, Any]]


async def task_history(env: ToolEnv, args: HistoryIn) -> dict[str, Any]:
    async with env.db.session() as s:
        task = await s.get(Task, env.task_id)
        rows = (
            await s.scalars(
                select(ExecutionEvent)
                .where(ExecutionEvent.task_id == env.task_id)
                .order_by(ExecutionEvent.seq.desc())
                .limit(args.limit)
            )
        ).all()
    return {
        "status": task.status if task else "UNKNOWN",
        "events": [
            {"seq": e.seq, "type": e.type, "status": e.status, "message": e.message[:200]} for e in reversed(rows)
        ],
    }


def build_registry(settings: Settings) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="list_files",
            description="List files in the workspace (relative paths).",
            input_model=ListFilesIn,
            output_model=ListFilesOut,
            scope="workspace:read",
            mutating=False,
            idempotent=True,
            timeout_s=10,
            handler=list_files,
        )
    )
    reg.register(
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            input_model=ReadFileIn,
            output_model=ReadFileOut,
            scope="workspace:read",
            mutating=False,
            idempotent=True,
            timeout_s=10,
            handler=read_file,
        )
    )
    reg.register(
        ToolSpec(
            name="search_files",
            description="Find lines containing a literal string in workspace files.",
            input_model=SearchIn,
            output_model=SearchOut,
            scope="workspace:read",
            mutating=False,
            idempotent=True,
            timeout_s=20,
            handler=search_files,
        )
    )
    reg.register(
        ToolSpec(
            name="write_file",
            description="Create or overwrite a workspace file with the full new content.",
            input_model=WriteFileIn,
            output_model=WriteFileOut,
            scope="workspace:write",
            mutating=True,
            idempotent=True,
            timeout_s=10,
            handler=write_file,
            approval_rule=_write_policy,
            verifier=_write_verifier,
        )
    )
    reg.register(
        ToolSpec(
            name="run_tests",
            description='Run an allowlisted test command (argv list, e.g. ["pytest", "-q"]) in the sandbox.',
            input_model=RunTestsIn,
            output_model=RunTestsOut,
            scope="tests:run",
            mutating=False,
            idempotent=True,
            timeout_s=settings.sandbox_timeout_s + 15,
            handler=run_tests,
            approval_rule=_tests_policy,
            retry=RetryPolicy(max_attempts=2, base_delay_s=0.5, max_delay_s=2.0),
        )
    )
    reg.register(
        ToolSpec(
            name="task_history",
            description="Inspect this task's status and recent execution events.",
            input_model=HistoryIn,
            output_model=HistoryOut,
            scope="history:read",
            mutating=False,
            idempotent=True,
            timeout_s=10,
            handler=task_history,
            audit=True,
        )
    )
    return reg
