"""Run the AgentOS benchmark and write JSON + Markdown reports.

    uv run python scripts/run_benchmark.py --trials 5
    uv run python scripts/run_benchmark.py --trials 3 --scenarios calc_basic,loop_no_progress

Uses AGENTOS_DATABASE_URL if set (it must be a disposable database: the harness creates tasks);
otherwise starts a throw-away PostgreSQL with pgserver. Results are labelled SIMULATED because
agent behaviour comes from scripted scenarios, not a real model.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import Settings  # noqa: E402
from app.evaluation.harness import CONFIGS, run_sync  # noqa: E402
from app.evaluation.scenarios import SCENARIOS  # noqa: E402


def _fmt_p(t: tuple | None) -> str:
    if not t or t[0] is None:
        return "n/a"
    p, lo, hi = t
    return f"{p:.2f} [{lo:.2f}, {hi:.2f}]"


def to_markdown(report: dict) -> str:
    m = report["meta"]
    lines = [
        "# AgentOS benchmark results",
        "",
        f"> **{m['label']}**. Latency, test pass rates, recoveries, loop detections and policy violations",
        "> are measured on the machine that ran the harness. Agent behaviour, tokens and cost are simulated.",
        "",
        f"- Generated: {m.get('generated_at', '')}",
        f"- Trials per scenario x configuration: {m['trials_per_cell']} (seeds 0..{m['trials_per_cell'] - 1})",
        f"- Scenarios: {', '.join(m['scenarios'])}",
        f"- Sandbox backend: {m['sandbox_backend']}; wall time {m['duration_s']} s",
        "",
        "Intervals are 95% Wilson (proportions) or percentile bootstrap (means).",
        "",
        "| Metric | " + " | ".join(report["summary"]) + " |",
        "|---|" + "---|" * len(report["summary"]),
    ]
    rows = [
        ("Correct outcome rate", lambda s: _fmt_p(s["correct_outcome_rate"])),
        ("Completion rate", lambda s: _fmt_p(s["completion_rate"])),
        ("Criteria pass rate (mean)", lambda s: _fmt_p(s["criteria_pass_rate_mean"])),
        ("Policy-violation rate (delivered code)", lambda s: _fmt_p(s["policy_violation_rate"])),
        ("Recovery success (crashed runs)", lambda s: _fmt_p(s["recovery_success_rate"])),
        (
            "Loop detection precision / recall",
            lambda s: f"{_f(s['loop_detection']['precision'])} / {_f(s['loop_detection']['recall'])}",
        ),
        ("Duplicate-action blocks", lambda s: str(s["duplicate_action_blocks"])),
        ("Repeated identical writes (same agent attempt)", lambda s: str(s["duplicate_side_effects"])),
        ("Side effects reconciled (not repeated)", lambda s: str(s["duplicates_prevented_by_reconciliation"])),
        ("Latency mean, s (measured)", lambda s: _fmt_p(s["latency_s"]["mean"])),
        ("Latency p95, s (measured)", lambda s: _f(s["latency_s"]["p95"])),
        ("Tokens / task (simulated)", lambda s: _fmt_p(s["tokens_mean"])),
        ("Cost / successful task, USD (simulated)", lambda s: _f(s["cost_per_successful_task_usd"], 5)),
        ("Tool failures / denials", lambda s: f"{s['tool_failures']} / {s['tool_denials']}"),
        ("Supervisor retries", lambda s: str(s["supervisor_retries"])),
    ]
    for name, fn in rows:
        lines.append(f"| {name} | " + " | ".join(fn(s) for s in report["summary"].values()) + " |")
    lines += [
        "",
        "## Correct outcomes per scenario",
        "",
        "| Scenario | " + " | ".join(report["summary"]) + " |",
        "|---|" + "---|" * len(report["summary"]),
    ]
    for sc, cells in report["per_scenario"].items():
        lines.append(
            f"| {sc} | "
            + " | ".join(
                f"{cells[c]['correct']}" + (f" ({cells[c]['violations']} viol.)" if cells[c]["violations"] else "")
                for c in report["summary"]
            )
            + " |"
        )
    lines += [
        "",
        "Expected outcome per scenario: "
        + ", ".join(f"`{k}`: {v.expected}" for k, v in SCENARIOS.items() if k in m["scenarios"]),
        "",
    ]
    return "\n".join(lines) + "\n"


def _f(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--scenarios", default="")
    ap.add_argument("--configs", default="")
    ap.add_argument("--out", default=str(ROOT / "docs" / "benchmarks"))
    args = ap.parse_args()
    scenarios = [s for s in args.scenarios.split(",") if s] or None
    configs = [c for c in args.configs.split(",") if c] or None
    for c in configs or []:
        if c not in CONFIGS:
            raise SystemExit(f"unknown config {c}; choose from {list(CONFIGS)}")

    tmp = Path(tempfile.mkdtemp(prefix="agentos-bench-"))
    server = None
    url = os.environ.get("AGENTOS_DATABASE_URL")
    if not url:
        import pgserver
        import psycopg

        server = pgserver.get_server(str(tmp / "pg"), cleanup_mode="delete")
        base = server.get_uri()
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute("CREATE DATABASE agentos_bench")
        url = base.replace("postgresql://", "postgresql+psycopg://", 1).replace("/postgres?", "/agentos_bench?", 1)
    from app.cli import migrate
    from app.observability.logging import configure_logging

    migrate(url)
    configure_logging("WARNING", json=True)
    settings = Settings(
        _env_file=None,
        database_url=url,
        redis_url=None,
        evaluation_mode=True,
        llm_provider="fake",  # type: ignore[call-arg]
        sandbox_backend=os.environ.get("AGENTOS_SANDBOX_BACKEND", "local"),
        workspace_root=tmp / "ws",
        artifact_root=tmp / "artifacts",
        persist_spans=False,
        retry_base_delay_s=0.01,
        retry_max_delay_s=0.05,
        log_level="WARNING",
        task_create_rate_per_minute=0,
    )

    def progress(r) -> None:
        mark = "ok " if r.correct else "BAD"
        print(f"[{mark}] {r.scenario:<20} {r.config:<32} seed={r.seed} {r.status:<17} {r.latency_s:.2f}s", flush=True)

    try:
        report = run_sync(settings, trials=args.trials, scenarios=scenarios, configs=configs, progress=progress)
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)
    report["meta"]["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "latest.md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    print(f"wrote {out / 'latest.json'} and {out / 'latest.md'}")


if __name__ == "__main__":
    main()
