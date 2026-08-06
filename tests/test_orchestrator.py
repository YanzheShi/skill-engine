"""
Orchestrator 关键链路测试（当前 API，不依赖 langgraph）

langgraph 是可选依赖（pyproject 的 langchain extra），本环境未安装，
因此只测编排器的纯逻辑与节点函数：
- _parse_orchestration_plan：JSON 解析（合法 / 非法）
- _build_catalog：从 registry 生成技能清单文本
- plan_node：调用 LLM 生成 plan / 无 LLM 返回空 plan
- execute_chain_node：按 plan 执行各 skill（成功 / 缺失 skill 报错）
- should_continue_node：direct_answer / execute / max_planning_steps
- format_result_node：格式化最终输出（complete / direct_answer）

run() 端到端（LangGraph 图）在 langgraph 可用时单独验证，否则跳过。
"""

import asyncio
import pytest
from unittest.mock import MagicMock
from skill_engine.execution.orchestrator import (
    Orchestrator,
    _parse_orchestration_plan,
    plan_node,
    execute_chain_node,
    should_continue_node,
    format_result_node,
)
from skill_engine.models import Skill, SkillMetadata


class _FakeReg:
    def get_groups(self):
        return {"__ungrouped__": ["skill-a", "skill-b"]}

    def info_full(self, name):
        return {
            "name": name,
            "description": f"desc of {name}",
            "when_to_use": "",
            "argument_hint": "",
        }


def test_parse_valid_json():
    out = _parse_orchestration_plan(
        '{"plan":[{"skill":"x","args":{},"description":"d"}],"reasoning":"r"}'
    )
    assert out["plan"] == [{"skill": "x", "args": {}, "description": "d"}]
    assert out["reasoning"] == "r"


def test_parse_invalid_returns_raw_reasoning():
    out = _parse_orchestration_plan("not json at all")
    assert out["plan"] == []
    assert out["reasoning"] == "not json at all"


def test_build_catalog_lists_skills():
    orch = Orchestrator(MagicMock(), MagicMock())
    cat = orch._build_catalog(_FakeReg())
    assert "skill-a" in cat
    assert "skill-b" in cat
    assert "desc of skill-a" in cat


def test_plan_node_with_llm():
    class _LLM:
        def invoke(self, msgs):
            return '{"plan":[{"skill":"a","args":{},"description":"do a"}],"reasoning":"because"}'

    st = {"planning_messages": [{"role": "user", "content": "x"}],
          "_llm": _LLM(), "planning_iterations": 0}
    res = asyncio.run(plan_node(st))
    assert res["plan"][0]["skill"] == "a"
    assert res["reasoning"] == "because"
    assert res["planning_iterations"] == 1


def test_plan_node_no_llm_returns_empty():
    st = {"planning_messages": [], "_llm": None, "planning_iterations": 0}
    res = asyncio.run(plan_node(st))
    assert res["plan"] == []
    assert res["planning_iterations"] == 1


def test_execute_chain_runs_skills():
    class _Reg:
        def load_skill(self, name):
            return Skill(metadata=SkillMetadata(name=name, description="d"),
                         body="", directory="/tmp")

    class _Asm:
        def assemble(self, skill, args):
            return f"PROMPT:{skill.metadata.name}"

    class _LLM:
        def invoke(self, prompt):
            return f"OUT:{prompt}"

    st = {"plan": [{"skill": "a", "args": {"k": "v"}, "description": "run a"}],
          "_llm": _LLM(), "prev_outputs": {}}
    res = execute_chain_node(st, _Asm(), MagicMock(), _Reg())
    assert len(res["chain_results"]) == 1
    assert res["chain_results"][0]["status"] == "success"
    assert "OUT:PROMPT:a" in res["all_outputs"]


def test_execute_chain_missing_skill_errors():
    class _Reg:
        def load_skill(self, name):
            return None

    st = {"plan": [{"skill": "missing", "args": {}, "description": "x"}],
          "_llm": MagicMock(), "prev_outputs": {}}
    res = execute_chain_node(st, MagicMock(), MagicMock(), _Reg())
    assert res["chain_results"][0]["status"] == "error"
    assert "missing" in res["chain_results"][0]["error"]


def test_should_continue_direct_answer_when_empty_plan():
    assert should_continue_node({"plan": [], "planning_iterations": 0}) == "direct_answer"


def test_should_continue_max_steps():
    assert should_continue_node({"plan": [1], "planning_iterations": 5,
                                 "_max_planning_steps": 5}) == "max_planning_steps"


def test_should_continue_execute():
    assert should_continue_node({"plan": [1], "planning_iterations": 0,
                                 "_max_planning_steps": 5}) == "execute"


def test_format_result_with_plan():
    st = {"plan": [{"skill": "a"}],
          "chain_results": [{"skill": "a", "status": "success", "description": "run a"}],
          "all_outputs": "## [a] run a\nout", "reasoning": "r"}
    res = format_result_node(st)
    assert res["stopped_by"] == "complete"
    assert "a" in res["output"]


def test_format_result_empty_plan_direct_answer():
    st = {"plan": [], "chain_results": [], "all_outputs": "", "reasoning": "just answer"}
    res = format_result_node(st)
    assert res["stopped_by"] == "direct_answer"
    assert res["output"] == "just answer"


def test_orchestrator_run_direct_answer_when_llm_returns_empty_plan():
    """langgraph 可用时，验证 run() 能编译并跑通图（空 plan → direct_answer）。"""
    pytest.importorskip("langgraph")
    orch = Orchestrator(MagicMock(), MagicMock())

    class _LLM:
        def invoke(self, prompt):
            return '{"plan": [], "reasoning": "直接回答"}'

    result = orch.run("hi", _FakeReg(), llm=_LLM())
    assert result["skill_name"] == "orchestrator"
    assert result["stopped_by"] == "direct_answer"
