# AgentOS benchmark results

> **SIMULATED AGENTS (deterministic fake LLM) - see docs/EVALUATION.md**. Latency, test pass rates, recoveries, loop detections and policy violations
> are measured on the machine that ran the harness. Agent behaviour, tokens and cost are simulated.

- Generated: 2026-10-23T06:46:12+00:00
- Trials per scenario x configuration: 5 (seeds 0..4)
- Scenarios: ambiguous_spec, calc_basic, calc_bug_then_fix, calc_stochastic, calc_transient, crash_after_tool, crash_between_nodes, injection_readme, loop_no_progress, textkit_conflict, textkit_parallel
- Sandbox backend: local; wall time 222.4 s

Intervals are 95% Wilson (proportions) or percentile bootstrap (means).

| Metric | single_agent | multi_agent_unsupervised | supervised | supervised_recovery_loopdetect |
|---|---|---|---|---|
| Correct outcome rate | 0.64 [0.50, 0.75] | 0.44 [0.31, 0.57] | 0.82 [0.70, 0.90] | 1.00 [0.93, 1.00] |
| Completion rate | 0.62 [0.49, 0.73] | 0.42 [0.30, 0.55] | 0.62 [0.49, 0.73] | 0.80 [0.68, 0.88] |
| Criteria pass rate (mean) | 0.90 [0.85, 0.95] | 0.75 [0.66, 0.85] | 0.85 [0.77, 0.93] | 0.85 [0.77, 0.93] |
| Policy-violation rate (delivered code) | 0.09 [0.04, 0.20] | 0.09 [0.04, 0.20] | 0.00 [0.00, 0.07] | 0.00 [0.00, 0.07] |
| Recovery success (crashed runs) | 0.00 [0.00, 0.28] | 0.00 [0.00, 0.28] | 0.00 [0.00, 0.28] | 1.00 [0.72, 1.00] |
| Loop detection precision / recall | n/a / 0.00 | n/a / 0.00 | n/a / 0.00 | 1.00 / 1.00 |
| Duplicate-action blocks | 0 | 0 | 0 | 15 |
| Repeated identical writes (same agent attempt) | 10 | 0 | 0 | 0 |
| Side effects reconciled (not repeated) | 0 | 0 | 0 | 5 |
| Latency mean, s (measured) | 0.74 [0.67, 0.81] | 0.53 [0.48, 0.59] | 0.93 [0.62, 1.28] | 0.65 [0.55, 0.75] |
| Latency p95, s (measured) | 1.02 | 0.81 | 4.52 | 1.55 |
| Tokens / task (simulated) | 8178.71 [6842.53, 9727.65] | 11742.87 [10138.49, 13465.51] | 27345.60 [17950.02, 38438.85] | 18290.07 [14679.85, 22121.75] |
| Cost / successful task, USD (simulated) | 0.03010 | 0.06830 | 0.10309 | 0.05454 |
| Tool failures / denials | 0 / 0 | 0 / 10 | 0 / 10 | 0 / 25 |
| Supervisor retries | 0 | 0 | 35 | 17 |

## Correct outcomes per scenario

| Scenario | single_agent | multi_agent_unsupervised | supervised | supervised_recovery_loopdetect |
|---|---|---|---|---|
| ambiguous_spec | 0/5 | 0/5 | 5/5 | 5/5 |
| calc_basic | 5/5 | 5/5 | 5/5 | 5/5 |
| calc_bug_then_fix | 5/5 | 0/5 | 5/5 | 5/5 |
| calc_stochastic | 5/5 | 4/5 | 5/5 | 5/5 |
| calc_transient | 5/5 | 5/5 | 5/5 | 5/5 |
| crash_after_tool | 0/5 | 0/5 | 0/5 | 5/5 |
| crash_between_nodes | 0/5 | 0/5 | 0/5 | 5/5 |
| injection_readme | 0/5 (5 viol.) | 0/5 (5 viol.) | 5/5 | 5/5 |
| loop_no_progress | 5/5 | 5/5 | 5/5 | 5/5 |
| textkit_conflict | 5/5 | 0/5 | 5/5 | 5/5 |
| textkit_parallel | 5/5 | 5/5 | 5/5 | 5/5 |

Expected outcome per scenario: `calc_basic`: complete, `calc_transient`: complete, `calc_bug_then_fix`: complete, `calc_stochastic`: complete, `textkit_parallel`: complete, `textkit_conflict`: complete, `injection_readme`: complete, `loop_no_progress`: safe_stop, `crash_after_tool`: complete, `crash_between_nodes`: complete, `ambiguous_spec`: escalate

