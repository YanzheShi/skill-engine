"""
Phase 8 测试套件 — 档位 B：tool_dispatch loop

实现 CC 兼容的 tool_dispatch 循环：
LLM 吐 tool_call → Executor spawn → obs 回 LLM → 继续 loop

支持的工具类型：
- bash: 执行 shell 命令
- read_file: 读取文件
- write_file: 写入文件
- stop: 停止循环
"""

import pytest
from pathlib import Path
import json


class MockLLMWithTools:
    """模拟会吐 tool_call 的 LLM（档位 B 用）"""

    def __init__(self, responses: list):
        """responses: 每次调用 LLM 返回的响应列表

        每个响应可以是：
        - str: 纯文本（触发 stop）
        - dict: {"content": str, "tool_calls": [...]}
        """
        self.responses = responses
        self.call_count = 0
        self.last_messages = []

    def invoke(self, messages: list) -> dict:
        self.call_count += 1
        self.last_messages = messages
        resp = self.responses[self.call_count - 1]
        if isinstance(resp, str):
            return {"content": resp, "tool_calls": []}
        return resp


class TestToolCallParsing:
    """测试 tool_call 解析"""

    def test_parse_bash_tool_call(self):
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        # 模拟 LLM 返回 bash tool_call
        tool_call = {
            "id": "call_1",
            "type": "bash",
            "input": {"command": "echo hello"}
        }
        parsed = runner._parse_tool_calls({"content": "", "tool_calls": [tool_call]})
        assert len(parsed) == 1
        assert parsed[0]["type"] == "bash"
        assert parsed[0]["id"] == "call_1"
        assert parsed[0]["input"] == {"command": "echo hello"}

    def test_parse_stop_tool_call(self):
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        tool_call = {
            "id": "call_1",
            "type": "stop",
            "input": {"reason": "done"}
        }
        parsed = runner._parse_tool_calls({"content": "", "tool_calls": [tool_call]})
        assert len(parsed) == 1
        assert parsed[0]["type"] == "stop"

    def test_parse_mixed_tool_calls(self):
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        tool_calls = [
            {"id": "call_1", "type": "bash", "input": {"command": "echo 1"}},
            {"id": "call_2", "type": "bash", "input": {"command": "echo 2"}},
        ]
        parsed = runner._parse_tool_calls({"content": "", "tool_calls": tool_calls})
        assert len(parsed) == 2

    def test_parse_no_tool_calls(self):
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        parsed = runner._parse_tool_calls({"content": "纯文本响应"})
        assert len(parsed) == 0

    def test_parse_string_response(self):
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        parsed = runner._parse_tool_calls("纯文本")
        assert len(parsed) == 0


class TestToolDispatchLoop:
    """测试 tool_dispatch 循环"""

    @pytest.fixture
    def executor(self):
        from skill_engine.execution.executor import Executor
        return Executor(timeout=10, allow_all=True)

    @pytest.fixture
    def assembler(self, executor):
        from skill_engine.execution.assembler import Assembler
        return Assembler(executor=executor)

    @pytest.fixture
    def runner(self, assembler, executor):
        from skill_engine.execution.runner import Runner
        return Runner(assembler, executor)

    def test_loop_bash_then_stop(self, runner):
        """循环：先执行 bash 命令，然后 LLM 返回 stop"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        # LLM 第一次返回 bash tool_call，第二次返回纯文本
        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo hello"}}
            ]},
            {"content": "执行完成，结果是: hello", "tool_calls": []},
        ])

        result = runner._run_tool_dispatch(match, llm, max_iterations=5)

        assert result["skill_name"] == "test"
        assert result["iterations"] == 2
        assert "hello" in result["output"]

    def test_loop_max_iterations(self, runner):
        """循环达到最大迭代次数后停止"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        # LLM 每次都返回 bash tool_call（模拟无限循环）
        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo 1"}}
            ]},
        ] * 10)  # 10 次

        result = runner._run_tool_dispatch(match, llm, max_iterations=3)

        assert result["iterations"] == 3
        assert result["stopped_by"] == "max_iterations"

    def test_loop_error_recovery(self, runner):
        """循环中命令执行失败，LLM 继续"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        # LLM 第一次返回 bash（会失败），第二次返回 stop
        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "exit 1"}}
            ]},
            {"content": "命令失败了，但我完成了", "tool_calls": []},
        ])

        result = runner._run_tool_dispatch(match, llm, max_iterations=5)
        assert result["iterations"] == 2

    def test_loop_read_file(self, runner, tmp_path):
        """循环中读取文件"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        test_file = tmp_path / "test.txt"
        test_file.write_text("file content", encoding="utf-8")

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory=str(tmp_path),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "read_file", "input": {"path": "test.txt"}}
            ]},
            {"content": "文件内容是: file content", "tool_calls": []},
        ])

        result = runner._run_tool_dispatch(match, llm, max_iterations=5)
        assert result["iterations"] == 2
        assert "file content" in result["output"]

    def test_loop_write_file(self, runner, tmp_path):
        """循环中写入文件"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory=str(tmp_path),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "write_file", "input": {
                    "path": "output.txt",
                    "content": "written content"
                }}
            ]},
            {"content": "文件已写入", "tool_calls": []},
        ])

        result = runner._run_tool_dispatch(match, llm, max_iterations=5)
        assert (tmp_path / "output.txt").exists()
        assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "written content"

    def test_loop_tool_stop(self, runner):
        """LLM 返回 stop tool_call 提前终止"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "stop", "input": {"reason": "任务完成"}}
            ]},
        ])

        result = runner._run_tool_dispatch(match, llm, max_iterations=5)
        assert result["stopped_by"] == "tool_stop"
        assert "任务完成" in result["output"]


class TestToolDispatchIntegration:
    """端到端：Runner 调用 tool_dispatch"""

    @pytest.fixture
    def runner(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        return Runner(
            Assembler(executor=Executor(timeout=10)),
            Executor(timeout=10)
        )

    def test_run_with_tool_dispatch(self, runner):
        """run() 方法支持 tool_dispatch"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        # 先设 strict 模式，验证拦截机制
        import os
        old_mode = os.environ.get("SKILLS_ENGINE_SECURITY_MODE", "")
        os.environ["SKILLS_ENGINE_SECURITY_MODE"] = "strict"

        llm = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo dispatch_test"}}
            ]},
            {"content": "完成", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["skill_name"] == "test"
        assert result["iterations"] == 2
        assert result["stopped_by"] == "stop"
        assert any("安全拦截" in s.get("error", "") for s in result["steps"])

        if old_mode:
            os.environ["SKILLS_ENGINE_SECURITY_MODE"] = old_mode
        else:
            del os.environ["SKILLS_ENGINE_SECURITY_MODE"]

    def test_run_tool_dispatch_overrides_llm(self, runner):
        """tool_dispatch 优先级高于 llm（档位 A）"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        llm_a = MockLLMWithTools([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo td"}}
            ]},
            {"content": "dispatch done", "tool_calls": []},
        ])
        llm_b = MockLLMWithTools(["档位A响应"])

        result = runner.run(match, llm=llm_b, tool_dispatch=llm_a)
        # 应该走 tool_dispatch 而不是档位 A
        assert "dispatch done" in result["output"]
        assert "档位A" not in result["output"]


class TestHumanInLoop:
    """测试 human_in_loop 多轮对话模式"""

    def test_human_in_loop_off(self):
        """human_in_loop=False: LLM 无 tool_calls 时直接返回，不进入对话"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试", human_in_loop=False),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
        llm = MockLLMWithTools(["最终答案"])

        result = runner.run(match, tool_dispatch=llm, max_iterations=5)
        assert result["output"] == "最终答案"
        assert result["stopped_by"] == "stop"

    def test_turn_policy_should_stop(self):
        """TurnPolicy.should_stop 匹配 stop_when"""
        from skill_engine.models import TurnPolicy

        policy = TurnPolicy(stop_when=["已完成", "结束"])
        assert policy.should_stop("访谈已完成")
        assert policy.should_stop("结束面试")
        assert not policy.should_stop("继续提问")

    def test_turn_policy_should_stop_string(self):
        """TurnPolicy stop_when 接受 str，自动转 list"""
        from skill_engine.models import TurnPolicy

        policy = TurnPolicy(stop_when="已完成")
        assert policy.should_stop("访谈已完成")
        assert not policy.should_stop("继续提问")

    def test_turn_policy_should_stop_none(self):
        """TurnPolicy stop_when=None 时 should_stop 返回 False"""
        from skill_engine.models import TurnPolicy

        policy = TurnPolicy(stop_when=None)
        assert not policy.should_stop("任何文本")

    def test_turn_policy_max_turns(self):
        """TurnPolicy max_turns 默认值"""
        from skill_engine.models import TurnPolicy

        policy = TurnPolicy()
        assert policy.max_turns == 20
        assert "/done" in policy.user_exit
        assert "/exit" in policy.user_exit

    def test_run_result_dict_access(self):
        """RunResult 支持 dict 式访问 result["output"]"""
        from skill_engine.models import RunResult
        from skill_engine.models import TurnPolicy

        result = RunResult(
            output="测试输出",
            ctx={"steps": [], "files_created": [], "iterations": 1, "stopped_by": "stop", "skill_name": "test"},
            history=[{"role": "system", "content": "test"}],
        )
        assert result["output"] == "测试输出"
        assert result["iterations"] == 1
        assert result["stopped_by"] == "stop"
        assert result.get("nonexistent") is None

    def test_run_result_attr_access(self):
        """RunResult 支持属性访问 result.output"""
        from skill_engine.models import RunResult

        result = RunResult(
            output="测试输出",
            ctx={"steps": []},
            history=[],
        )
        assert result.output == "测试输出"
        assert result.history == []
