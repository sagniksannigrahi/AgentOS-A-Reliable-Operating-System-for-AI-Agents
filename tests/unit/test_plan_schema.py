import pytest
from pydantic import ValidationError

from app.schemas.agent import AgentStep, FinishAction, ToolCallAction
from app.schemas.plan import AcceptanceCriterion, Plan, topological_layers


def _plan(subtasks):
    return {"summary": "s", "subtasks": subtasks}


def st(id, deps=()):
    return {"id": id, "title": id, "description": id, "depends_on": list(deps)}


def test_valid_plan_and_layers():
    p = Plan.model_validate(_plan([st("a"), st("b"), st("c", ["a", "b"]), st("d", ["c"])]))
    assert topological_layers(p.subtasks) == [["a", "b"], ["c"], ["d"]]


def test_cycle_rejected():
    with pytest.raises(ValidationError, match="circular dependency"):
        Plan.model_validate(_plan([st("a", ["c"]), st("b", ["a"]), st("c", ["b"])]))


def test_unknown_dependency_rejected():
    with pytest.raises(ValidationError, match="unknown subtasks"):
        Plan.model_validate(_plan([st("a", ["zzz"])]))


def test_self_dependency_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate(_plan([st("a", ["a"])]))


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError, match="unique"):
        Plan.model_validate(_plan([st("a"), st("a")]))


def test_empty_plan_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate(_plan([]))


def test_bad_subtask_id_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate(_plan([st("Bad ID!")]))


def test_criterion_field_rules():
    AcceptanceCriterion(id="t", description="d", kind="tests_pass", command=["pytest"])
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="t", description="d", kind="tests_pass")
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="t", description="d", kind="file_contains", path="a.py")


def test_agent_step_discriminator():
    s = AgentStep.model_validate({"step": {"action": "call_tool", "tool": "read_file", "args": {"path": "a"}}})
    assert isinstance(s.step, ToolCallAction)
    s = AgentStep.model_validate({"step": {"action": "finish", "summary": "done"}})
    assert isinstance(s.step, FinishAction)
    with pytest.raises(ValidationError):
        AgentStep.model_validate({"step": {"action": "shell", "cmd": "ls"}})
