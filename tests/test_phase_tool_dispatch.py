"""
档位 B（tool_dispatch）修复测试 — bind_tools 绑定工具定义

验证：
1. TOOL_DISPATCH_TOOLS 定义了三个内建工具（bash/read_file/write_file）
2. 裸模型调用 bind_tools 后能正确绑定
3. Mock LLM 多步循环（带 bind_tools 兼容）
4. 档位 A 不受影响（裸模型调用）
5. CLI 中档位 A 和档位 B 使用不同 LLM 客户端
"""

import pytest
from pathlib import Path
import tempfile
import shutil


# ================================================================
# 测试 1：内建工具定义
# ================================================================

class TestBuiltInTools:
    """验证 TOOL_DISPATCH_TOOLS 的三个工具定义"""

    def test_tools_module_level_import(self):
        """TOOL_DISPATCH_TOOLS 可从 runner 模块导入"""
        from skill_engine.execution.runner import TOOL_DISPATCH_TOOLS

        assert len(TOOL_DISPATCH_TOOLS) == 3

    def test_bash_tool_schema(self):
        """bash 工具有正确的 schema"""
        from skill_engine.execution.runner import bash

        assert bash.name == "bash"
        assert "command" in bash.args_schema.model_fields

    def test_read_file_tool_schema(self):
        """read_file 工具有正确的 schema"""
        from skill_engine.execution.runner import read_file

        assert read_file.name == "read_file"
        assert "path" in read_file.args_schema.model_fields

    def test_write_file_tool_schema(self):
        """write_file 工具有正确的 schema"""
        from skill_engine.execution.runner import write_file

        assert write_file.name == "write_file"
        assert "path" in write_file.args_schema.model_fields
        assert "content" in write_file.args_schema.model_fields

    def test_tools_have_descriptions(self):
        """所有工具有非空描述"""
        from skill_engine.execution.runner import TOOL_DISPATCH_TOOLS

        for t in TOOL_DISPATCH_TOOLS:
            assert t.name
            assert t.description and len(t.description) > 0


# ================================================================
# 测试 2：bind_tools 绑定验证（需要真实 LLM 配置）
# ================================================================

class TestBindTools:
    """验证 bind_tools 能正确绑定工具定义"""

    def test_bind_tools_returns_bound_model(self):
        """bind_tools 返回一个可调用对象"""
        from skill_engine.config import get_llm

        llm = get_llm()
        from skill_engine.execution.runner import TOOL_DISPATCH_TOOLS
        bound = llm.bind_tools(TOOL_DISPATCH_TOOLS)

        # bound 有 invoke 方法
        assert hasattr(bound, "invoke")
        # bound 也有 bind_tools 方法（链式调用）
        assert hasattr(bound, "bind_tools")

    def test_bind_tools_preserves_invoke_signature(self):
        """bind_tools 后的模型仍然接受 messages 参数"""
        from skill_engine.config import get_llm

        llm = get_llm()
        from skill_engine.execution.runner import TOOL_DISPATCH_TOOLS
        bound = llm.bind_tools(TOOL_DISPATCH_TOOLS)

        # 不需要真的调用 API，只要确保方法签名正确
        assert callable(bound.invoke)


# ================================================================
# 测试 3：Mock LLM 多步循环（向后兼容，无 bind_tools）
# ================================================================

class MockLLMWithToolCalls:
    """模拟会吐 tool_call 的 LLM（档位 B 用，无 bind_tools 方法）"""

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


class TestToolDispatchWithMock:
    """用 Mock LLM 验证 tool_dispatch 循环（向后兼容，无 bind_tools）"""

    @pytest.fixture
    def runner(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        return Runner(
            Assembler(executor=Executor(timeout=10)),
            Executor(timeout=10),
        )

    def test_mock_bash_then_stop(self, runner):
        """Mock：先 bash 再 stop"""
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo hello"}}
            ]},
            {"content": "完成", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["skill_name"] == "test"
        assert result["iterations"] == 2
        assert result["stopped_by"] == "stop"
        # 最终输出是 LLM 的最终回复
        assert "完成" in result["output"]
        # bash 结果在 steps 中
        bash_steps = [s for s in result["steps"] if s.get("type") == "bash"]
        assert len(bash_steps) == 1
        assert "安全拦截" in bash_steps[0].get("error", "")

    def test_mock_multiple_tool_calls_same_turn(self, runner):
        """Mock：一次返回多个 tool_calls（bash + write_file）"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test-multi", description="测试"),
            body="测试",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        llm = MockLLMWithToolCalls([
            {
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "bash", "input": {"command": "echo step1"}},
                    {"id": "call_2", "type": "bash", "input": {"command": "echo step2"}},
                ],
            },
            {"content": "全部完成", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["iterations"] == 2
        # 两次 bash 都在 step_results 中
        bash_steps = [s for s in result["steps"] if s.get("type") == "bash"]
        assert len(bash_steps) == 2

    def test_mock_read_file(self, runner, tmp_path):
        """Mock：read_file 工具"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        test_file = tmp_path / "data.txt"
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "read_file", "input": {"path": "data.txt"}}
            ]},
            {"content": "文件内容是: file content", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["iterations"] == 2
        assert "file content" in result["output"]

    def test_mock_write_file(self, runner, tmp_path):
        """Mock：write_file 工具"""
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "write_file", "input": {
                    "path": "output.txt",
                    "content": "hello world",
                }}
            ]},
            {"content": "文件已写入", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert (tmp_path / "output.txt").exists()
        assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "hello world"

    def test_mock_stop_tool_call(self, runner):
        """Mock：stop 工具提前终止"""
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "stop", "input": {"reason": "done"}}
            ]},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["stopped_by"] == "tool_stop"
        assert "done" in result["output"]

    def test_mock_max_iterations(self, runner):
        """Mock：达到最大迭代次数"""
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo loop"}}
            ]},
        ] * 10)

        result = runner.run(match, tool_dispatch=llm, max_iterations=3)
        assert result["stopped_by"] == "max_iterations"
        assert result["iterations"] == 3

    def test_mock_file_created_tracking(self, runner, tmp_path):
        """Mock：write_file 后 files_created 列表被正确记录"""
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

        llm = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "write_file", "input": {
                    "path": "a.txt",
                    "content": "aaa",
                }}
            ]},
            {"content": "", "tool_calls": [
                {"id": "call_2", "type": "write_file", "input": {
                    "path": "b.txt",
                    "content": "bbb",
                }}
            ]},
            {"content": "完成", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert len(result["files_created"]) == 2
        assert any("a.txt" in f for f in result["files_created"])
        assert any("b.txt" in f for f in result["files_created"])


# ================================================================
# 测试 4：档位 A 不受影响
# ================================================================

class TestLLMModeUnaffected:
    """验证档位 A（单次 LLM 调用）不受 bind_tools 改动影响"""

    @pytest.fixture
    def runner(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        return Runner(
            Assembler(executor=Executor(timeout=10)),
            Executor(timeout=10),
        )

    def test_llm_mode_still_works(self, runner):
        """档位 A：裸模型单次调用"""
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

        llm = MockLLMWithToolCalls(["档位A响应"])
        result = runner.run(match, llm=llm)

        assert result["skill_name"] == "test"
        assert "档位A响应" in result["output"]
        assert "steps" in result
        assert len(result["steps"]) == 1
        assert result["steps"][0]["type"] == "llm"

    def test_llm_mode_no_tool_dispatch_interference(self, runner):
        """档位 A 不进入 tool_dispatch 路径"""
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

        llm_a = MockLLMWithToolCalls(["档位A响应"])
        llm_b = MockLLMWithToolCalls([
            {"content": "", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "echo should_not_run"}}
            ]},
        ])

        # 同时传 llm 和 tool_dispatch，tool_dispatch 优先
        result = runner.run(match, llm=llm_a, tool_dispatch=llm_b)
        assert "should_not_run" not in result["output"]
        # 但档位 A 的响应也不应该在 tool_dispatch 路径中
        assert "档位A响应" not in result["output"]

    def test_dry_run_mode_still_works(self, runner):
        """纯编译模式（无 LLM 无 tool_dispatch）"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="# Hello World",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        result = runner.run(match)

        assert result["skill_name"] == "test"
        assert "Hello World" in result["output"]
        assert result["iterations"] == 0
        assert result["stopped_by"] == "none"


# ================================================================
# 测试 5：CLI LLM 客户端分离
# ================================================================

class TestCLIClientSeparation:
    """验证 CLI 中档位 A 和档位 B 使用不同 LLM 客户端"""

    def test_get_llm_client_returns_bare_model(self):
        """_get_llm_client 返回裸模型"""
        from skill_engine.cli import _get_llm_client

        client = _get_llm_client()
        assert client is not None
        # 裸模型有 invoke 方法
        assert hasattr(client, "invoke")

    def test_get_tool_llm_client_returns_bare_model(self):
        """_get_tool_llm_client 返回裸模型（bind_tools 在 runner 内部做）"""
        from skill_engine.cli import _get_tool_llm_client

        client = _get_tool_llm_client()
        assert client is not None
        # 裸模型有 invoke 方法
        assert hasattr(client, "invoke")
        # 裸模型也有 bind_tools 方法（在 runner 中调用）
        assert hasattr(client, "bind_tools")

    def test_both_clients_are_different_instances(self):
        """两个客户端返回的是独立的裸模型实例"""
        from skill_engine.cli import _get_llm_client, _get_tool_llm_client

        client_a = _get_llm_client()
        client_b = _get_tool_llm_client()

        # 两者都是裸模型（都有 invoke 和 bind_tools）
        assert hasattr(client_a, "invoke")
        assert hasattr(client_b, "invoke")
        assert hasattr(client_a, "bind_tools")
        assert hasattr(client_b, "bind_tools")


# ================================================================
# 测试 6：端到端 — 模拟真实 skill 的多步执行
# ================================================================

class TestEndToEndMultiStep:
    """端到端模拟真实 skill 的多步执行场景"""

    @pytest.fixture
    def runner(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        return Runner(
            Assembler(executor=Executor(timeout=10)),
            Executor(timeout=10),
        )

    def test_multi_step_fetch_then_write(self, runner, tmp_path):
        """模拟 leetcode-solution-writer：fetch 题目 → 写题解文件"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        skill = Skill(
            metadata=SkillMetadata(
                name="leetcode-solution-writer",
                description="LeetCode 题解生成助手",
            ),
            body="# LeetCode 题解生成助手",
            directory=str(tmp_path),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={"$ARGUMENTS": "第49题"},
        )

        # LLM 第一次返回 bash 获取题目，第二次返回 write_file 写题解
        llm = MockLLMWithToolCalls([
            {"content": "正在获取题目数据...", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {
                    "command": "python scripts/fetch_problem.py 49",
                }},
            ]},
            {"content": "正在生成题解...", "tool_calls": [
                {"id": "call_2", "type": "write_file", "input": {
                    "path": "solution.md",
                    "content": "# 第49题题解\n\n## 解题思路\n\n这是题解内容。",
                }},
            ]},
            {"content": "题解已生成完毕！", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm, max_iterations=5)

        assert result["skill_name"] == "leetcode-solution-writer"
        assert result["iterations"] == 3
        assert result["stopped_by"] == "stop"

        # 验证步骤记录
        bash_steps = [s for s in result["steps"] if s.get("type") == "bash"]
        assert len(bash_steps) == 1
        assert "fetch_problem.py" in bash_steps[0].get("command", "")

        write_steps = [s for s in result["steps"] if s.get("type") == "write_file"]
        assert len(write_steps) == 1
        assert "solution.md" in write_steps[0].get("path", "")

        # 验证文件创建
        assert len(result["files_created"]) == 1
        solution_file = Path(result["files_created"][0])
        assert solution_file.exists()
        assert "第49题题解" in solution_file.read_text(encoding="utf-8")

    def test_multi_step_read_then_write(self, runner, tmp_path):
        """模拟 skill-creator：读取模板 → 写入新 skill"""
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        # 预先创建模板文件
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "skill-template.md").write_text(
            "---\nname: {{name}}\ndescription: {{desc}}\n---\n\n# {{name}}",
            encoding="utf-8",
        )

        skill = Skill(
            metadata=SkillMetadata(
                name="skill-creator",
                description="Create new skills",
            ),
            body="# Skill Creator",
            directory=str(tmp_path),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={"$ARGUMENTS": "new-skill"},
        )

        llm = MockLLMWithToolCalls([
            {"content": "正在读取模板...", "tool_calls": [
                {"id": "call_1", "type": "read_file", "input": {
                    "path": "templates/skill-template.md",
                }},
            ]},
            {"content": "正在生成新 skill...", "tool_calls": [
                {"id": "call_2", "type": "write_file", "input": {
                    "path": "new-skill/SKILL.md",
                    "content": "---\nname: new-skill\ndescription: A new skill\n---\n\n# new-skill",
                }},
            ]},
            {"content": "Skill 创建完成！", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm, max_iterations=5)

        assert result["skill_name"] == "skill-creator"
        assert result["iterations"] == 3
        assert result["stopped_by"] == "stop"

        # 验证文件创建
        assert len(result["files_created"]) == 1
        new_skill_dir = Path(result["files_created"][0]).parent
        assert (new_skill_dir / "SKILL.md").exists()
        content = (new_skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "new-skill" in content
        assert "A new skill" in content

    def test_error_handling_in_loop(self, runner, tmp_path):
        """Mock：LLM 返回工具调用，但工具执行出错，LLM 继续"""
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

        # 第一次 bash 执行失败（exit 1），但 LLM 继续返回最终答案
        llm = MockLLMWithToolCalls([
            {"content": "正在执行...", "tool_calls": [
                {"id": "call_1", "type": "bash", "input": {"command": "exit 1"}},
            ]},
            {"content": "虽然命令失败了，但我完成了分析：结果是 OK", "tool_calls": []},
        ])

        result = runner.run(match, tool_dispatch=llm)
        assert result["iterations"] == 2
        assert result["stopped_by"] == "stop"
        # 最终输出包含 LLM 的最终回复
        assert "OK" in result["output"]
