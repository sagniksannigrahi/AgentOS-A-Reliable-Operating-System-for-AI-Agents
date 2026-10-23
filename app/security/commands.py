"""Command allowlist. Agents never get a shell: they may only request commands listed here, as an
argv list, with every argument checked against an explicit grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.errors import ToolDeniedError
from app.security.paths import resolve_in_workspace

_PATH_ARG = re.compile(r"^[A-Za-z0-9_./\-]+(::[A-Za-z0-9_\[\]\-]+)*$")
_K_EXPR = re.compile(r"^[A-Za-z0-9_ ]{1,120}$")


class CommandNotAllowedError(ToolDeniedError):
    pass


@dataclass(frozen=True)
class AllowedCommand:
    name: str
    description: str
    flags: frozenset[str]
    value_flags: dict[str, re.Pattern[str]] = field(default_factory=dict)
    allow_paths: bool = True


ALLOWLIST: dict[str, AllowedCommand] = {
    "pytest": AllowedCommand(
        name="pytest",
        description="Run the workspace's pytest suite (optionally specific files/tests).",
        flags=frozenset({"-q", "-qq", "-x", "-v", "-vv", "--no-header", "-rA", "-rf", "--maxfail=1"}),
        value_flags={"-k": _K_EXPR, "--tb": re.compile(r"^(short|line|no|long|native)$")},
    ),
}


@dataclass(frozen=True)
class ValidatedCommand:
    name: str
    args: tuple[str, ...]

    def display(self) -> list[str]:
        return [self.name, *self.args]


def validate_command(argv: object, workspace: Path | None = None) -> ValidatedCommand:
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise CommandNotAllowedError("command must be a non-empty list of strings")
    if len(argv) > 16:
        raise CommandNotAllowedError("too many arguments")
    argv = list(argv)
    # Accept the common "python -m pytest" spelling.
    if argv[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        argv = ["pytest", *argv[3:]]
    spec = ALLOWLIST.get(argv[0])
    if spec is None:
        raise CommandNotAllowedError(
            f"command '{argv[0][:40]}' is not on the allowlist",
            details={"allowed": sorted(ALLOWLIST)},
        )
    out: list[str] = []
    it = iter(argv[1:])
    for arg in it:
        if arg in spec.flags:
            out.append(arg)
            continue
        if "=" in arg and arg.split("=", 1)[0] in spec.value_flags:
            flag, value = arg.split("=", 1)
            if not spec.value_flags[flag].match(value):
                raise CommandNotAllowedError(f"invalid value for {flag}")
            out.append(f"{flag}={value}")
            continue
        if arg in spec.value_flags:
            value = next(it, None)
            if value is None or not spec.value_flags[arg].match(value):
                raise CommandNotAllowedError(f"invalid value for {arg}")
            out.extend([arg, value])
            continue
        if arg.startswith("-"):
            raise CommandNotAllowedError(f"flag '{arg[:40]}' is not allowed")
        if not spec.allow_paths or not _PATH_ARG.match(arg):
            raise CommandNotAllowedError(f"argument '{arg[:60]}' is not allowed")
        file_part = arg.split("::", 1)[0]
        if workspace is not None:
            resolve_in_workspace(workspace, file_part, must_exist=True)
        else:
            from app.security.paths import normalize_relative

            normalize_relative(file_part)
        out.append(arg)
    return ValidatedCommand(spec.name, tuple(out))
