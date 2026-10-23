"""LangGraph workflow definitions.

Supervised / unsupervised multi-agent graph::

    START -> validate_request -> planner -> plan_policy_check -> supervisor
    supervisor --(conditional)--> human_approval | execute_subtasks | security_review |
                                  run_tests | planner (replan) | finalize | fail | cancel
    human_approval, execute_subtasks, security_review, run_tests -> supervisor
    finalize, fail, cancel -> END

Single-agent baseline::

    START -> validate_request -> single_agent -> run_tests -> finalize | fail -> END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.orchestration import nodes as n
from app.orchestration.state import WorkflowState


def build_graph(mode: str, checkpointer: Any) -> Any:
    g = StateGraph(WorkflowState)
    g.add_node("validate_request", n.validate_request)
    g.add_node("run_tests", n.run_tests)
    g.add_node("finalize", n.finalize)
    g.add_node("fail", n.fail)
    g.add_node("cancel", n.cancel)
    g.add_edge(START, "validate_request")
    if mode == "single_agent":
        g.add_node("single_agent", n.single_agent)
        g.add_conditional_edges(
            "validate_request",
            lambda s: "fail" if s.get("error") else "single_agent",
            {"single_agent": "single_agent", "fail": "fail"},
        )
        g.add_conditional_edges("single_agent", n.route_after_single_agent, {"run_tests": "run_tests", "fail": "fail"})
        g.add_conditional_edges("run_tests", n.route_after_single_tests, {"finalize": "finalize", "fail": "fail"})
    else:
        g.add_node("planner", n.planner)
        g.add_node("plan_policy_check", n.plan_policy_check)
        g.add_node("supervisor", n.supervisor)
        g.add_node("human_approval", n.human_approval)
        g.add_node("execute_subtasks", n.execute_subtasks)
        g.add_node("security_review", n.security_review)
        g.add_edge("validate_request", "planner")
        g.add_edge("planner", "plan_policy_check")
        g.add_edge("plan_policy_check", "supervisor")
        g.add_conditional_edges(
            "supervisor",
            n.route_from_supervisor,
            {
                k: k
                for k in (
                    "human_approval",
                    "execute_subtasks",
                    "security_review",
                    "run_tests",
                    "planner",
                    "finalize",
                    "fail",
                    "cancel",
                )
            },
        )
        for name in ("human_approval", "execute_subtasks", "security_review", "run_tests"):
            g.add_edge(name, "supervisor")
    g.add_conditional_edges(
        "finalize", lambda s: "fail" if s.get("error") and not s.get("final") else "end", {"fail": "fail", "end": END}
    )
    g.add_edge("fail", END)
    g.add_edge("cancel", END)
    return g.compile(checkpointer=checkpointer)
