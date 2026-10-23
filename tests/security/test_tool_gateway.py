"""Security tests for the tool gateway, sandbox and policies (against real PostgreSQL)."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.domain.errors import ReconciliationRequiredError, TaskCancelledError
from app.orchestration.faults import FaultInjector
from app.persistence.models import ApprovalRequest, ExecutionEvent, Task, ToolInvocation
from app.persistence.repository import claim_run
from app.tools.builtin import ToolEnv, build_registry
from app.tools.gateway import ApprovalRequiredError, ToolCallContext, ToolGateway
from app.tools.sandbox import Sandbox
from app.tools.workspace import Workspace
from tests.conftest import calc_task

READ = frozenset({"workspace:read"})
WRITE = frozenset({"workspace:read", "workspace:write"})
TESTS = frozenset({"workspace:read", "tests:run"})


@dataclass
class GW:
    gateway: ToolGateway
    task_id: uuid.UUID
    run_id: uuid.UUID
    token: int
    ws: Workspace

    def cc(self, scopes=WRITE, role="coder", prefix="coder:impl:a1:p1", agent_run_id=None):
        return ToolCallContext(self.task_id, self.run_id, self.token, agent_run_id, role, scopes, prefix)


@pytest.fixture
async def gw(api, settings):
    r = await api.client.post("/api/v1/tasks", json=calc_task(), headers=api.h())
    tid = uuid.UUID(r.json()["id"])
    async with api.state.db.session() as s:
        claimed = await claim_run(s, "test", 60)
    ws = Workspace.for_task(settings.workspace_root, str(tid))
    ws.create(settings.seed_repos_dir, "calculator")
    env = ToolEnv(settings=settings, workspace=ws, sandbox=Sandbox(settings), db=api.state.db, task_id=tid)
    gateway = ToolGateway(
        build_registry(settings),
        env,
        api.state.db,
        FaultInjector.disabled(),
        max_identical_calls=3,
        output_limit=2000,
        approval_ttl_s=3600,
    )
    return GW(gateway, tid, claimed.run_id, claimed.fencing_token, ws)


async def _rows(api, tid):
    async with api.state.db.session() as s:
        return (
            await s.scalars(
                select(ToolInvocation).where(ToolInvocation.task_id == tid).order_by(ToolInvocation.created_at)
            )
        ).all()


async def test_read_and_list(gw):
    r = await gw.gateway.invoke(gw.cc(READ), "list_files", {"path": "."}, 0)
    assert r.ok and "calc.py" in r.output["files"] and not any(f.startswith(".agentos") for f in r.output["files"])
    r = await gw.gateway.invoke(gw.cc(READ), "read_file", {"path": "calc.py"}, 1)
    assert r.ok and "def add" in r.output["content"]


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", "tests/../../x", ".git/config", ".env", "~/.ssh/id_rsa"]
)
async def test_path_attacks_denied_and_audited(api, gw, path):
    r = await gw.gateway.invoke(gw.cc(READ), "read_file", {"path": path}, 0)
    assert not r.ok and r.status.value == "DENIED"
    rows = await _rows(api, gw.task_id)
    assert rows[-1].status == "DENIED" and rows[-1].decision_reason


async def test_symlink_escape_denied(gw, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(secret, gw.ws.repo / "link.txt")
    r = await gw.gateway.invoke(gw.cc(READ), "read_file", {"path": "link.txt"}, 0)
    assert not r.ok and "symlink" in r.error


async def test_missing_scope_denied(gw):
    r = await gw.gateway.invoke(gw.cc(READ), "write_file", {"path": "calc.py", "content": "x"}, 0)
    assert not r.ok and "lacks permission 'workspace:write'" in r.error
    r = await gw.gateway.invoke(gw.cc(WRITE), "run_tests", {"command": ["pytest"]}, 1)
    assert not r.ok and "tests:run" in r.error


async def test_unknown_tool_and_invalid_args(gw):
    r = await gw.gateway.invoke(gw.cc(), "shell", {"cmd": "ls"}, 0)
    assert not r.ok and "unknown tool" in r.error
    r = await gw.gateway.invoke(gw.cc(), "read_file", {"path": "calc.py", "extra": 1}, 1)
    assert not r.ok and "invalid arguments" in r.error
    r = await gw.gateway.invoke(gw.cc(), "read_file", {}, 2)
    assert not r.ok


@pytest.mark.parametrize(
    "cmd", [["bash", "-c", "id"], ["curl", "http://x"], ["pytest", "-p", "x"], ["pytest", "; rm -rf /"]]
)
async def test_commands_outside_allowlist_denied(gw, cmd):
    r = await gw.gateway.invoke(gw.cc(TESTS, role="tester", prefix="tester:-:a1:p1"), "run_tests", {"command": cmd}, 0)
    assert not r.ok and r.status.value == "DENIED"


async def test_forbidden_write_denied(gw):
    r = await gw.gateway.invoke(gw.cc(), "write_file", {"path": ".env", "content": "A=1"}, 0)
    assert not r.ok and r.status.value == "DENIED"
    assert not (gw.ws.repo / ".env").exists()


async def test_sensitive_write_requires_human_approval(api, gw):
    args = {"path": "tests/test_calc.py", "content": "def test_x():\n    assert True\n"}
    original = (gw.ws.repo / "tests/test_calc.py").read_text()
    with pytest.raises(ApprovalRequiredError) as ei:
        await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert (gw.ws.repo / "tests/test_calc.py").read_text() == original  # nothing written yet
    approval_id = ei.value.approval_id
    # still pending -> asking again raises again (bound to the same request)
    with pytest.raises(ApprovalRequiredError) as ei2:
        await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert ei2.value.approval_id == approval_id
    # creator cannot self-approve; a separate approver can
    r = await api.client.post(
        f"/api/v1/approvals/{approval_id}/decision", json={"decision": "approve"}, headers=api.h("operator")
    )
    assert r.status_code == 403
    r = await api.client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approve", "comment": "ok"},
        headers=api.h("approver"),
    )
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.json()["decision"] == "approve"
    r = await api.client.post(
        f"/api/v1/approvals/{approval_id}/decision", json={"decision": "reject"}, headers=api.h("approver")
    )
    assert r.status_code == 409
    res = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert res.ok and (gw.ws.repo / "tests/test_calc.py").read_text() == args["content"]
    # approval is bound to the exact arguments: different content needs a new approval
    with pytest.raises(ApprovalRequiredError):
        await gw.gateway.invoke(gw.cc(), "write_file", {**args, "content": "x = 1\n"}, 1)


async def test_rejected_approval_denies(api, gw):
    args = {"path": "conftest.py", "content": "import os\n"}
    with pytest.raises(ApprovalRequiredError) as ei:
        await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    await api.client.post(
        f"/api/v1/approvals/{ei.value.approval_id}/decision", json={"decision": "reject"}, headers=api.h("approver")
    )
    res = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert not res.ok and "rejected" in res.error


async def test_duplicate_actions_are_bounded(api, gw):
    aid = await _agent_run(api, gw)
    results = [
        await gw.gateway.invoke(gw.cc(READ, agent_run_id=aid), "read_file", {"path": "README.md"}, i) for i in range(5)
    ]
    assert [r.ok for r in results] == [True, True, True, False, False]
    assert "duplicate action" in results[3].error
    async with api.state.db.session() as s:
        loops = (
            await s.scalars(
                select(ExecutionEvent).where(
                    ExecutionEvent.task_id == gw.task_id, ExecutionEvent.type == "loop.detected"
                )
            )
        ).all()
    assert len(loops) == 2


async def _agent_run(api, gw):
    from app.persistence.repository import start_agent_run

    async with api.state.db.session() as s:
        aid, _ = await start_agent_run(
            s,
            task_id=gw.task_id,
            run_id=gw.run_id,
            role="coder",
            subtask_key="impl",
            attempt=1,
            idempotency_key=f"{gw.task_id}:test",
            input_summary={},
        )
    return aid


async def test_replay_is_deduplicated(api, gw):
    args = {"path": "notes.txt", "content": "hello\n"}
    r1 = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    (gw.ws.repo / "notes.txt").write_text("changed by someone else\n")
    r2 = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)  # same key -> recorded output, no re-execution
    assert r1.ok and r2.ok and r2.deduplicated
    assert (gw.ws.repo / "notes.txt").read_text() == "changed by someone else\n"
    assert len([r for r in await _rows(api, gw.task_id) if r.tool_name == "write_file"]) == 1


async def _orphan_intent(api, gw, tool, args, applied: bool):
    """Simulate: a dead run recorded the write-ahead intent; did the side effect happen?"""
    from app.persistence.repository import stable_hash

    if applied and tool == "write_file":
        (gw.ws.repo / args["path"]).write_text(args["content"])
    key = f"{gw.task_id}:coder:impl:a1:p1:0:{tool}:{stable_hash(args)[:16]}"
    other_run = uuid.uuid4()
    async with api.state.db.session() as s:
        from app.persistence.models import TaskRun

        s.add(TaskRun(id=other_run, task_id=gw.task_id, attempt=99, status="ABANDONED", reason="initial"))
        await s.flush()
        s.add(
            ToolInvocation(
                task_id=gw.task_id,
                run_id=other_run,
                agent_role="coder",
                tool_name=tool,
                status="ABANDONED",
                mutating=True,
                args_redacted=args,
                args_hash=stable_hash(args),
                idempotency_key=key,
            )
        )


async def test_crash_reconciliation_applied(api, gw):
    args = {"path": "new.py", "content": "x = 1\n"}
    await _orphan_intent(api, gw, "write_file", args, applied=True)
    r = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert r.ok and r.status.value == "RECONCILED" and r.deduplicated


async def test_crash_reconciliation_not_applied_executes(api, gw):
    args = {"path": "new.py", "content": "x = 2\n"}
    await _orphan_intent(api, gw, "write_file", args, applied=False)
    r = await gw.gateway.invoke(gw.cc(), "write_file", args, 0)
    assert r.ok and r.status.value == "COMPLETED"
    assert (gw.ws.repo / "new.py").read_text() == "x = 2\n"


async def test_unverifiable_side_effect_requires_human(api, gw):
    """A mutating tool without a verifier must never be silently re-executed after a crash."""
    from app.tools.builtin import WriteFileIn, WriteFileOut, write_file
    from app.tools.registry import ToolSpec

    gw.gateway.registry.register(
        ToolSpec(
            name="send_email",
            description="non-idempotent external side effect",
            input_model=WriteFileIn,
            output_model=WriteFileOut,
            scope="workspace:write",
            mutating=True,
            idempotent=False,
            timeout_s=5,
            handler=write_file,
        )
    )
    args = {"path": "mail.txt", "content": "hi"}
    await _orphan_intent(api, gw, "send_email", args, applied=False)
    with pytest.raises(ReconciliationRequiredError):
        await gw.gateway.invoke(gw.cc(), "send_email", args, 0)


async def test_secrets_redacted_in_audit_log(api, gw):
    secret = "sk-" + "ant-api03-SUPERSECRETVALUE1234567890abcdef"
    await gw.gateway.invoke(gw.cc(), "write_file", {"path": "cfg.py", "content": f'KEY = "{secret}"\n'}, 0)
    await gw.gateway.invoke(gw.cc(READ), "read_file", {"path": "cfg.py"}, 1)
    for row in await _rows(api, gw.task_id):
        assert (
            secret not in str(row.args_redacted)
            and secret not in str(row.output_preview)
            and secret not in str(row.output)
        )


async def test_output_is_capped(gw):
    (gw.ws.repo / "big.txt").write_text("x" * 50_000)
    r = await gw.gateway.invoke(gw.cc(READ), "read_file", {"path": "big.txt"}, 0)
    assert r.ok and len(r.output["content"]) < 2100 and r.output["truncated"]


async def test_cancellation_blocks_side_effects(api, gw):
    async with api.state.db.session() as s:
        task = await s.get(Task, gw.task_id)
        from app.persistence.repository import utcnow

        task.cancel_requested_at = utcnow()
    with pytest.raises(TaskCancelledError):
        await gw.gateway.invoke(gw.cc(), "write_file", {"path": "a.py", "content": "x"}, 0)
    assert not (gw.ws.repo / "a.py").exists()


async def test_sandbox_runs_real_tests_with_scrubbed_env(gw, monkeypatch):
    monkeypatch.setenv("AGENTOS_ANTHROPIC_API_KEY", "sk-" + "ant-should-not-leak-123456789")
    (gw.ws.repo / "tests" / "test_env.py").write_text(
        "import os\n\ndef test_no_secrets():\n    assert not any('ANTHROPIC' in k or 'AGENTOS' in k for k in os.environ)\n"
    )
    r = await gw.gateway.invoke(
        gw.cc(TESTS, role="tester", prefix="tester:-:a1:p1"),
        "run_tests",
        {"command": ["pytest", "-q", "tests/test_env.py"]},
        0,
    )
    assert r.ok, r.error
    assert r.output["outcome"] == "passed", r.output["output"]


async def test_sandbox_timeout_kills_process(gw, settings):
    settings.sandbox_timeout_s = 2
    (gw.ws.repo / "tests" / "test_hang.py").write_text("import time\n\ndef test_hang():\n    time.sleep(60)\n")
    from app.security.commands import validate_command

    res = await Sandbox(settings).run(validate_command(["pytest", "tests/test_hang.py"], gw.ws.repo), gw.ws.repo)
    assert res.timed_out and res.duration_ms < 10_000


async def test_sandbox_disabled_refuses(gw, settings):
    settings.sandbox_backend = "disabled"
    r = await gw.gateway.invoke(
        gw.cc(TESTS, role="tester", prefix="tester:-:a1:p1"), "run_tests", {"command": ["pytest"]}, 0
    )
    assert not r.ok and "disabled" in r.error


async def test_approval_records_have_no_secrets(api, gw):
    secret = "ghp" + "_abcdefghijklmnopqrstuvwxyz0123456789AB"
    with pytest.raises(ApprovalRequiredError):
        await gw.gateway.invoke(gw.cc(), "write_file", {"path": "tests/t.py", "content": f"TOKEN = '{secret}'"}, 0)
    async with api.state.db.session() as s:
        req = (await s.scalars(select(ApprovalRequest).where(ApprovalRequest.task_id == gw.task_id))).one()
    assert secret not in str(req.payload)


async def test_sandbox_never_tests_stale_bytecode(gw, settings):
    """Regression: same-size rewrites within one second must not be masked by cached .pyc files."""
    from app.security.commands import validate_command

    sb = Sandbox(settings)
    mod, test = gw.ws.repo / "same.py", gw.ws.repo / "tests" / "test_same.py"
    test.write_text("from same import SEP\n\ndef test_sep():\n    assert SEP == '-'\n")
    cmd = validate_command(["pytest", "-q", "tests/test_same.py"], gw.ws.repo)
    for sep, want in (("_", "failed"), ("-", "passed"), ("_", "failed"), ("-", "passed")):
        mod.write_text(f"SEP = '{sep}'\n")
        from app.tools.sandbox import classify_pytest

        assert classify_pytest(await sb.run(cmd, gw.ws.repo)) == want
    assert not list(gw.ws.repo.rglob("*.pyc"))
