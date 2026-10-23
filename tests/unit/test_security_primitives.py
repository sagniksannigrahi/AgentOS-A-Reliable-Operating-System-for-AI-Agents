import os
from pathlib import Path

import pytest

from app.domain.errors import ToolDeniedError
from app.security.commands import validate_command
from app.security.paths import PathPolicyError, WriteClass, classify_write, resolve_in_workspace
from app.security.redaction import REDACTED, find_secrets, redact, redact_text

# ----------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "secret",
    [
        # Fake credentials are assembled at runtime so secret scanners do not flag the repository.
        "sk-" + "ant-api03-" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "aos_" + "abcdefghijklmnopqrstuvwxyz012345",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789AB",
        "Bearer abcdefghijklmnop.qrstuvwx",
        "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
    ],
)
def test_known_secret_formats_are_redacted(secret):
    out = redact_text(f"value={secret} trailing")
    assert secret not in out
    assert REDACTED in out


def test_assignment_and_url_passwords_redacted():
    out = redact_text('PASSWORD="hunter2hunter2" and postgresql://user:pa55word@db/x')
    assert "hunter2hunter2" not in out
    assert "pa55word" not in out


def test_sensitive_keys_redacted_recursively():
    data = {
        "headers": {"Authorization": "Bearer xyz123456789", "x": 1},
        "api_key": "abc",
        "nested": [{"password": "p"}],
    }
    out = redact(data)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["nested"][0]["password"] == REDACTED
    assert out["headers"]["x"] == 1


def test_private_key_block_redacted():
    kind = "RSA PRIVATE " + "KEY"
    pem = f"-----BEGIN {kind}-----\nMIIEow\n-----END {kind}-----"
    assert "MIIEow" not in redact_text(pem)
    assert "private_key" in find_secrets(pem)


def test_plain_text_untouched():
    assert redact_text("def add(a, b): return a + b") == "def add(a, b): return a + b"


# --------------------------------------------------------------------------------- paths


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "outside.txt").write_text("secret\n")
    return root


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.txt",
        "pkg/../../outside.txt",
        "/etc/passwd",
        "~/x",
        "C:/x",
        "a\\b",
        "x\x00y",
        "",
        " ",
        ".git/config",
        ".env",
    ],
)
def test_bad_paths_rejected(ws, bad):
    with pytest.raises(PathPolicyError):
        resolve_in_workspace(ws, bad)


def test_symlink_escape_rejected(ws, tmp_path):
    os.symlink(tmp_path / "outside.txt", ws / "link.txt")
    with pytest.raises(PathPolicyError):
        resolve_in_workspace(ws, "link.txt")
    os.symlink(tmp_path, ws / "dirlink")
    with pytest.raises(PathPolicyError):
        resolve_in_workspace(ws, "dirlink/outside.txt")


def test_valid_paths_resolve_inside(ws):
    p, norm = resolve_in_workspace(ws, "./pkg/a.py", must_exist=True)
    assert p == (ws / "pkg" / "a.py").resolve()
    assert norm == "pkg/a.py"
    p, norm = resolve_in_workspace(ws, "pkg/new.py")
    assert norm == "pkg/new.py"


def test_write_classification():
    assert classify_write("calc.py") == WriteClass.NORMAL
    assert classify_write("tests/test_calc.py") == WriteClass.SENSITIVE
    assert classify_write("conftest.py") == WriteClass.SENSITIVE
    assert classify_write(".github/workflows/ci.yml") == WriteClass.SENSITIVE
    assert classify_write(".env") == WriteClass.FORBIDDEN


# ------------------------------------------------------------------------------ commands


def test_allowed_pytest_commands(ws):
    assert validate_command(["pytest", "-q"]).display() == ["pytest", "-q"]
    assert validate_command(["python", "-m", "pytest", "-x", "--tb=short"]).name == "pytest"
    assert validate_command(["pytest", "-k", "divide and not zero"]).args == ("-k", "divide and not zero")
    assert validate_command(["pytest", "pkg/a.py::test_x"], ws).args == ("pkg/a.py::test_x",)


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "ls"],
        ["sh"],
        ["curl", "http://attacker.example"],
        ["rm", "-rf", "/"],
        ["pytest", "--rootdir=/"],
        ["pytest", "-p", "evil_plugin"],
        ["pytest", "-k", "x; rm -rf /"],
        ["pytest", "../outside.txt"],
        ["pytest", "$(whoami)"],
        ["pytest", "a.py && curl x"],
        "pytest -q",
        [],
        ["pytest"] + ["-q"] * 20,
    ],
)
def test_disallowed_commands(argv, ws):
    with pytest.raises(ToolDeniedError):
        validate_command(argv, ws)


def test_nonexistent_path_argument_rejected(ws):
    with pytest.raises(PathPolicyError):
        validate_command(["pytest", "missing.py"], ws)
