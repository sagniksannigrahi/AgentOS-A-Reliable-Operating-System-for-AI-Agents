"""Restricted execution of allowlisted test commands.

Backends
--------
``docker`` (default, recommended): a throw-away container with no network, a read-only root
filesystem, the workspace mounted read-only, dropped capabilities, ``no-new-privileges``, a
non-root user and memory/CPU/PID limits.

``local``: a subprocess with a scrubbed environment (no secrets), the workspace as working
directory, CPU/file-size/open-file rlimits, its own process group (killed on timeout) and capped
output. **This is not an isolation boundary**: code under test can still read the host
filesystem and use the network. It exists for development and CI on machines without Docker and
must be enabled explicitly (``AGENTOS_SANDBOX_BACKEND=local``).

``disabled``: test execution is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import SandboxBackend, Settings
from app.domain.errors import ToolDeniedError, ToolExecutionError
from app.security.commands import ValidatedCommand
from app.tools.workspace import PYTEST_INI


@dataclass
class SandboxResult:
    exit_code: int | None
    output: str
    timed_out: bool
    duration_ms: int
    truncated: bool
    backend: str


async def _read_capped(stream: asyncio.StreamReader, limit: int, buf: bytearray) -> bool:
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return truncated
        room = limit - len(buf)
        if room > 0:
            buf.extend(chunk[:room])
        if len(chunk) > room:
            truncated = True


def _limits(cpu_seconds: int) -> None:  # runs in the child before exec
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, hard), hard))


class Sandbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _pytest_args(self, cmd: ValidatedCommand, workdir: str) -> list[str]:
        return ["-m", "pytest", "-c", f"{workdir}/{PYTEST_INI}", "--rootdir", workdir, *cmd.args]

    async def run(self, cmd: ValidatedCommand, workspace: Path, timeout_s: float | None = None) -> SandboxResult:
        backend = self.settings.sandbox_backend
        timeout = timeout_s or self.settings.sandbox_timeout_s
        if backend == SandboxBackend.DISABLED:
            raise ToolDeniedError("code execution is disabled (AGENTOS_SANDBOX_BACKEND=disabled)")
        if cmd.name != "pytest":
            raise ToolDeniedError(f"sandbox has no runner for '{cmd.name}'")
        if backend == SandboxBackend.LOCAL:
            home = tempfile.mkdtemp(prefix="agentos-sbx-")
            # -I isolates from the host's PYTHON* variables and user site-packages; because it also
            # ignores PYTHONDONTWRITEBYTECODE, -B is required: stale .pyc files with an identical
            # mtime/size would otherwise make the sandbox test old code.
            argv = [sys.executable, "-I", "-B", *self._pytest_args(cmd, str(workspace))]
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "LANG": "C.UTF-8",
                "PYTHONHASHSEED": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TMPDIR": home,
            }
            return await self._exec(argv, env, str(workspace), timeout, backend.value, None)
        name = f"agentos-sbx-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108 - tmpfs inside the container
            "--memory",
            f"{self.settings.sandbox_memory_mb}m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{workspace}:/workspace:ro",
            "-w",
            "/workspace",
            self.settings.sandbox_image,
            "python",
            "-I",
            "-B",
            *self._pytest_args(cmd, "/workspace"),
        ]
        return await self._exec(
            argv, {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, None, timeout, backend.value, name
        )

    async def _exec(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        timeout: float,
        backend: str,
        container: str | None,
    ) -> SandboxResult:
        limit = self.settings.tool_output_limit_bytes
        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=(lambda: _limits(int(timeout) + 5)) if backend == "local" else None,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(f"sandbox backend '{backend}' unavailable: {exc.filename} not found") from exc
        buf = bytearray()
        assert proc.stdout is not None
        reader = asyncio.create_task(_read_capped(proc.stdout, limit, buf))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._kill(proc, container)
        truncated = False
        try:
            truncated = await asyncio.wait_for(reader, timeout=5)
        except TimeoutError:
            reader.cancel()
        return SandboxResult(
            exit_code=None if timed_out else proc.returncode,
            output=buf.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
            backend=backend,
        )

    async def _kill(self, proc: asyncio.subprocess.Process, container: str | None) -> None:
        if container:
            with contextlib.suppress(Exception):
                k = await asyncio.create_subprocess_exec(
                    "docker", "kill", container, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                await asyncio.wait_for(k.wait(), 10)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)


def classify_pytest(result: SandboxResult) -> str:
    """Map a pytest run to passed / failed / infra_error / timeout.

    Exit codes: 0 all passed, 1 tests failed, 2 interrupted, 3 internal error, 4 usage error,
    5 no tests collected. 2-4 indicate a problem with the harness, not the code under test.
    """
    if result.timed_out:
        return "timeout"
    if result.exit_code == 0:
        return "passed"
    if result.exit_code in (1, 5):
        return "failed"
    # Exit 2 with collection errors means the code under test does not import (syntax error,
    # missing name...): that is a code failure, not an infrastructure failure.
    if result.exit_code == 2 and ("errors during collection" in result.output or "ERROR collecting" in result.output):
        return "failed"
    return "infra_error"
