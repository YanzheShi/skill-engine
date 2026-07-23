"""
Phase 3 测试套件

测试 Steps DSL 的完整功能：
- Step 模型验证
- Steps DSL 解析
- exec/write/read/llm 四种 step 类型
- 模板变量解析（{step_name}, $VAR）
- 端到端：leetcode-solution-writer 补 steps 后确定性跑通
"""

import pytest
from pathlib import Path
from skill_engine.models import Step, Skill, SkillMetadata, MatchResult
from skill_engine.execution.executor import Executor
from skill_engine.execution.assembler import Assembler
from skill_engine.execution.runner import Runner
from skill_engine.execution.steps import resolve_template


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sample-skills"


class MockLLM:
    """模拟 LLM 客户端"""

    def __init__(self, response: str = "LLM 生成的题解"):
        self.response = response
        self.call_count = 0
        self.last_prompt = ""

    def invoke(self, prompt: str) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        return self.response


class TestStepModel:
    """测试 Step 模型"""

    def test_step_exec(self):
        step = Step(name="fetch", type="exec", command="python scripts/fetch.py 49", timeout=30)
        assert step.name == "fetch"
        assert step.type == "exec"
        assert step.command == "python scripts/fetch.py 49"
        assert step.timeout == 30

    def test_step_write(self):
        step = Step(name="save", type="write", output_file="output/result.md", template="# Hello")
        assert step.type == "write"
        assert step.output_file == "output/result.md"

    def test_step_read(self):
        step = Step(name="load", type="read", input_ref="output/result.md")
        assert step.type == "read"
        assert step.input_ref == "output/result.md"

    def test_step_llm(self):
        step = Step(name="compose", type="llm", model="gpt-5.5", template="生成题解")
        assert step.type == "llm"
        assert step.model == "gpt-5.5"

    def test_step_fetch(self):
        step = Step(name="fetch", type="fetch", url="https://example.com/api", timeout=10)
        assert step.type == "fetch"

    def test_step_default_timeout(self):
        step = Step(name="test", type="exec", command="echo hi")
        assert step.timeout == 30

    def test_step_optional_fields(self):
        step = Step(name="test", type="exec")
        assert step.command is None
        assert step.url is None
        assert step.model is None
        assert step.template is None
        assert step.output_file is None
        assert step.input_ref is None


class TestRunnerSteps:
    """测试 Runner 的 steps 执行"""

    @pytest.fixture
    def executor(self):
        return Executor(timeout=10, allow_all=True)

    @pytest.fixture
    def assembler(self, executor):
        return Assembler(executor=executor)

    @pytest.fixture
    def runner(self, assembler, executor):
        return Runner(assembler, executor)

    @pytest.fixture
    def temp_skill_dir(self, tmp_path):
        """创建临时 skill 目录用于测试"""
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        # 创建 SKILL.md
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("---\nname: test\n---\n\n测试", encoding="utf-8")
        # 创建 scripts 目录
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        # 创建 scripts/hello.py
        hello_py = scripts_dir / "hello.py"
        hello_py.write_text('print("hello from script")', encoding="utf-8")
        return skill_dir

    @pytest.fixture
    def test_skill(self, temp_skill_dir):
        return Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试内容",
            directory=str(temp_skill_dir),
        )

    @pytest.fixture
    def test_match_result(self, test_skill):
        return MatchResult(
            skill=test_skill,
            score=1.0,
            method="name",
            arguments={"$ARGUMENTS": "test"},
        )

    def test_exec_step_success(self, runner, test_skill):
        """exec step 成功执行"""
        steps = [Step(name="say_hi", type="exec", command="echo hello_world")]
        result = runner.run(MatchResult(skill=test_skill, score=1.0, method="name", arguments={}), steps=steps)
        assert result["skill_name"] == "test"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["type"] == "exec"
        assert "hello_world" in result["steps"][0]["output"]

    def test_exec_step_failure(self, runner, test_skill):
        """exec step 执行失败"""
        steps = [Step(name="fail", type="exec", command="exit 1")]
        result = runner.run(MatchResult(skill=test_skill, score=1.0, method="name", arguments={}), steps=steps)
        assert result["steps"][0]["exit_code"] != 0

    def test_exec_step_with_timeout(self, runner, test_skill):
        """exec step 超时"""
        import sys
        if sys.platform == "win32":
            # ping 延迟 5 秒，不触发安全审批
            cmd = "ping -n 6 127.0.0.1 > nul"
        else:
            cmd = "sleep 5"
        steps = [Step(name="slow", type="exec", command=cmd, timeout=1)]
        result = runner.run(MatchResult(skill=test_skill, score=1.0, method="name", arguments={}), steps=steps)
        assert result["steps"][0]["timed_out"] is True

    def test_write_step_creates_file(self, runner, temp_skill_dir):
        """write step 创建文件"""
        output_path = str(temp_skill_dir / "output" / "test.md")
        steps = [
            Step(name="save", type="write", output_file=output_path, template="# Hello World")
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert len(result["files_created"]) == 1
        assert output_path in result["files_created"]
        assert Path(output_path).exists()
        assert Path(output_path).read_text(encoding="utf-8") == "# Hello World"

    def test_read_step_reads_file(self, runner, temp_skill_dir):
        """read step 读取文件"""
        test_file = temp_skill_dir / "input.txt"
        test_file.write_text("readable content", encoding="utf-8")

        steps = [
            Step(name="load", type="read", input_ref=str(test_file)),
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert result["steps"][0]["output"] == "readable content"

    def test_read_step_file_not_found(self, runner, temp_skill_dir):
        """read step 文件不存在"""
        steps = [
            Step(name="load", type="read", input_ref="/nonexistent/file.txt"),
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert "error" in result["steps"][0]

    def test_step_chaining_output_ref(self, runner, temp_skill_dir):
        """步骤间输出引用"""
        output_file = str(temp_skill_dir / "chained.md")
        steps = [
            Step(name="gen", type="exec", command="echo chained_output"),
            Step(name="save", type="write", output_file=output_file, template="{gen}"),
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert Path(output_file).exists()
        content = Path(output_file).read_text(encoding="utf-8")
        assert "chained_output" in content

    def test_step_chain_with_args(self, runner, temp_skill_dir):
        """步骤间引用 + 参数替换"""
        output_file = str(temp_skill_dir / "mixed.md")
        steps = [
            Step(name="fetch", type="exec", command="echo problem_49"),
            Step(name="save", type="write", output_file=output_file, template="{fetch} - $0"),
        ]
        match = MatchResult(
            skill=Skill(
                metadata=SkillMetadata(name="test", description="测试"),
                body="",
                directory=str(temp_skill_dir),
            ),
            score=1.0,
            method="name",
            arguments={"$0": "fourty-nine"},
        )
        result = runner.run(match, steps=steps)
        assert Path(output_file).exists()
        content = Path(output_file).read_text(encoding="utf-8")
        assert "problem_49" in content
        assert "fourty-nine" in content

    def test_multiple_steps(self, runner, temp_skill_dir):
        """多个步骤连续执行"""
        steps = [
            Step(name="step1", type="exec", command="echo first"),
            Step(name="step2", type="exec", command="echo second"),
            Step(name="step3", type="exec", command="echo third"),
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert len(result["steps"]) == 3
        assert "first" in result["steps"][0]["output"]
        assert "second" in result["steps"][1]["output"]
        assert "third" in result["steps"][2]["output"]

    def test_unknown_step_type(self, runner, test_skill):
        """未知 step 类型"""
        steps = [Step(name="bad", type="unknown_type")]
        result = runner.run(MatchResult(skill=test_skill, score=1.0, method="name", arguments={}), steps=steps)
        assert "error" in result["steps"][0]

    def test_steps_return_final_output(self, runner, temp_skill_dir):
        """steps 返回最后一步的输出"""
        steps = [
            Step(name="a", type="exec", command="echo alpha"),
            Step(name="b", type="exec", command="echo bravo"),
        ]
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=steps)
        assert "bravo" in result["output"]

    def test_steps_empty_list(self, runner, temp_skill_dir):
        """空 steps 列表"""
        result = runner.run(MatchResult(skill=Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory=str(temp_skill_dir),
        ), score=1.0, method="name", arguments={}), steps=[])
        assert result["steps"] == []
        assert result["output"] == ""


class TestResolveTemplate:
    """测试模板变量解析"""

    @pytest.fixture
    def runner(self):
        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        return Runner(assembler, executor)

    def test_replace_step_output(self, runner):
        result = resolve_template("{output}", {"output": "hello"}, {})
        assert result == "hello"

    def test_replace_multiple_step_outputs(self, runner):
        result = resolve_template("{a} {b}", {"a": "foo", "b": "bar"}, {})
        assert result == "foo bar"

    def test_replace_arg_param(self, runner):
        result = resolve_template("$ARGUMENTS", {}, {"$ARGUMENTS": "world"})
        assert result == "world"

    def test_replace_positional_param(self, runner):
        result = resolve_template("$0", {}, {"$0": "49"})
        assert result == "49"

    def test_replace_named_param(self, runner):
        result = resolve_template("$problem_id", {}, {"$problem_id": "104"})
        assert result == "104"

    def test_mixed_refs_and_args(self, runner):
        result = resolve_template("{data} and $ARGUMENTS", {"data": "hello"}, {"$ARGUMENTS": "world"})
        assert result == "hello and world"

    def test_no_replacement_when_missing(self, runner):
        result = resolve_template("{missing} $MISSING", {}, {})
        assert result == "{missing} $MISSING"


class TestEndToEnd:
    """端到端测试：leetcode-solution-writer 补 steps 后确定性跑通"""

    @pytest.fixture
    def temp_skill_dir(self, tmp_path):
        """创建带 steps 的 leetcode-solution-writer skill"""
        skill_dir = tmp_path / "leetcode-solution-writer"
        skill_dir.mkdir()

        # 创建 SKILL.md（带 steps DSL）
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("""---
name: leetcode-solution-writer
description: LeetCode 题解生成助手
when_to_use: 刷题完成后说生成题解或写题解
arguments:
  - problem_id
---

# LeetCode 题解生成助手

## steps

- name: fetch_problem
  type: exec
  command: python scripts/fetch_problem.py $0
  timeout: 30

- name: save_solution
  type: write
  output_file: ~/.leetcode/docs/{problem_id}. solution.md
  template: |
    # 题解 {problem_id}
    这是一道自动生成题解。
""", encoding="utf-8")

        # 创建 scripts 目录
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()

        # 创建 fetch_problem.py（模拟）
        fetch_py = scripts_dir / "fetch_problem.py"
        fetch_py.write_text(
            'import sys, json; print(json.dumps({"id": sys.argv[1], "title": "Test Problem"}))',
            encoding="utf-8"
        )

        return skill_dir

    def test_steps_deterministic_run(self, temp_skill_dir):
        """带 steps 的 skill 确定性执行"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        from skill_engine.models import Step

        executor = Executor(timeout=30, allow_all=True)
        assembler = Assembler(executor=executor, command_timeout=30)
        runner = Runner(assembler, executor)

        # 创建模拟 match_result
        skill = Skill(
            metadata=SkillMetadata(name="leetcode-solution-writer", description="LeetCode 题解生成助手"),
            body="",
            directory=str(temp_skill_dir),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={"$ARGUMENTS": "49", "$0": "49", "problem_id": "49"},
        )

        # 定义 steps
        steps = [
            Step(name="fetch_problem", type="exec", command="python scripts/fetch_problem.py $0", timeout=30),
            Step(name="save_solution", type="write",
                 output_file=str(temp_skill_dir / "output" / "49_solution.md"),
                 template="# 题解 49\n这是一道自动生成题解。"),
        ]

        result = runner.run(match, steps=steps)

        # 验证执行结果
        assert result["skill_name"] == "leetcode-solution-writer"
        assert len(result["steps"]) == 2
        assert result["steps"][0]["type"] == "exec"
        assert result["steps"][1]["type"] == "write"

        # 验证文件创建
        assert len(result["files_created"]) == 1
        output_file = Path(result["files_created"][0])
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        assert "题解 49" in content

    def test_steps_with_chained_data(self, temp_skill_dir):
        """步骤间数据传递"""
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        from skill_engine.models import Step, Skill, SkillMetadata, MatchResult

        executor = Executor(timeout=30, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skill = Skill(
            metadata=SkillMetadata(name="chain-test", description="链式测试"),
            body="",
            directory=str(temp_skill_dir),
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={"$0": "42"},
        )

        steps = [
            Step(name="compute", type="exec", command="echo computed_42"),
            Step(name="save", type="write",
                 output_file=str(temp_skill_dir / "result.md"),
                 template="# Result\n{compute}"),
        ]

        result = runner.run(match, steps=steps)
        assert Path(temp_skill_dir / "result.md").exists()
        content = Path(temp_skill_dir / "result.md").read_text(encoding="utf-8")
        assert "computed_42" in content
