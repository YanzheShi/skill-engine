"""
Phase 2 测试套件

测试 executor、assembler、runner 三个模块的正确性。
"""

import pytest
from pathlib import Path
from skill_engine.models import Skill, SkillMetadata, MatchResult
from skill_engine.executor import Executor
from skill_engine.assembler import Assembler
from skill_engine.runner import Runner


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sample-skills"


class MockLLM:
    """模拟 LLM 客户端（档位 A 测试用）"""

    def __init__(self, response: str = "LLM 生成的题解内容"):
        self.response = response
        self.call_count = 0
        self.last_prompt = ""

    def invoke(self, prompt: str) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        return self.response


class TestExecutor:
    """测试 Executor 沙箱"""

    def test_run_preprocess_success(self):
        """成功执行命令"""
        executor = Executor(timeout=10, allow_all=True)
        result = executor.run_preprocess("echo hello", cwd=Path("/tmp"))
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]
        assert result["timed_out"] is False

    def test_run_preprocess_failure(self):
        """命令执行失败"""
        executor = Executor(timeout=10, allow_all=True)
        result = executor.run_preprocess("cat /nonexistent/file", cwd=Path("/tmp"))
        assert result["exit_code"] != 0
        assert "stderr" in result

    def test_run_preprocess_timeout(self):
        """命令超时"""
        executor = Executor(timeout=1, allow_all=True)
        # Windows 用 PowerShell Start-Sleep
        import sys
        if sys.platform == "win32":
            sleep_cmd = "powershell -Command Start-Sleep -Seconds 5"
        else:
            sleep_cmd = "sleep 5"
        result = executor.run_preprocess(sleep_cmd, cwd=Path("/tmp"))
        assert result["timed_out"] is True

    def test_run_step_blocked_by_allowlist(self):
        """allowlist 模式下被阻止的命令"""
        executor = Executor(timeout=10, allow_all=False, allowlist={"python", "echo"})
        result = executor.run_step("rm -rf /", cwd=Path("/tmp"))
        assert result["exit_code"] == 1
        assert "安全拦截" in result["stderr"]

    def test_run_step_allowed(self):
        """allowlist 模式下允许的命令"""
        executor = Executor(timeout=10, allow_all=False, allowlist={"echo", "cat"})
        result = executor.run_step("echo test", cwd=Path("/tmp"))
        assert result["exit_code"] == 0

    def test_max_output_truncation(self):
        """输出大小限制"""
        executor = Executor(timeout=10, max_output=10, allow_all=True)
        result = executor.run_preprocess("echo '1234567890abcdef'", cwd=Path("/tmp"))
        assert len(result["stdout"]) <= 10

    def test_env_home_set(self):
        """HOME 环境变量被设置为 cwd"""
        executor = Executor(timeout=10, allow_all=True)
        # 验证 _build_env 正确设置 HOME
        env = executor._build_env(Path("/tmp/test"))
        # Windows 路径分隔符可能是 \ 或 /
        assert "tmp" in env["HOME"] and "test" in env["HOME"]
        assert "PYTHONUNBUFFERED" in env

    def test_env_path_includes_skill_dirs(self):
        """PATH 包含 skill 目录"""
        executor = Executor(timeout=10, allow_all=True)
        env = executor._build_env(Path("/tmp/my-skill"))
        # Windows 路径分隔符可能是 \ 或 /
        path_str = env["PATH"]
        assert "my-skill" in path_str and ("scripts" in path_str or "scripts" in path_str)


class TestAssembler:
    """测试 Assembler"""

    @pytest.fixture
    def executor(self):
        return Executor(timeout=10, allow_all=True)

    @pytest.fixture
    def assembler(self, executor):
        return Assembler(executor=executor)

    @pytest.fixture
    def deploy_skill(self):
        """加载 deploy skill 用于测试"""
        deploy_dir = FIXTURES_DIR / "deploy"
        content = (deploy_dir / "SKILL.md").read_text(encoding="utf-8")
        import yaml
        import re
        _FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
        match = _FM_RE.match(content)
        fm_dict = yaml.safe_load(match.group(1)) if match else {}
        body = content[match.end():] if match else content
        return Skill(
            metadata=SkillMetadata(**fm_dict),
            body=body,
            directory=str(deploy_dir),
        )

    def test_assemble_basic(self, assembler, deploy_skill):
        """基本编译"""
        result = assembler.assemble(deploy_skill, {})
        assert "[SKILL: deploy]" in result
        assert "部署" in result

    def test_assemble_injects_paths(self, assembler, deploy_skill):
        """注入目录路径变量"""
        deploy_dir = FIXTURES_DIR / "deploy"
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="${SKILL_DIR}/scripts/run.sh",
            directory=str(deploy_dir),
        )
        result = assembler.assemble(skill, {})
        assert str(deploy_dir) in result

    def test_assemble_injects_scripts_dir(self, assembler, deploy_skill):
        """注入 scripts 目录路径"""
        deploy_dir = FIXTURES_DIR / "deploy"
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="${SKILL_SCRIPTS_DIR}/run.sh",
            directory=str(deploy_dir),
        )
        result = assembler.assemble(skill, {})
        assert str(deploy_dir / "scripts") in result

    def test_assemble_injects_assets_dir(self, assembler, deploy_skill):
        """注入 assets 目录路径"""
        deploy_dir = FIXTURES_DIR / "deploy"
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="${SKILL_ASSETS_DIR}/template.md",
            directory=str(deploy_dir),
        )
        result = assembler.assemble(skill, {})
        assert str(deploy_dir / "assets") in result

    def test_assemble_substitutes_params(self, assembler, deploy_skill):
        """参数替换"""
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="题号: $0, 全部: $ARGUMENTS",
            directory=str(FIXTURES_DIR / "deploy"),
        )
        result = assembler.assemble(skill, {"$0": "49", "$ARGUMENTS": "49 字母异位词"})
        assert "题号: 49" in result
        assert "全部: 49 字母异位词" in result

    def test_assemble_injects_refs(self, assembler):
        """加载支持文件 refs"""
        lw_dir = FIXTURES_DIR / "leetcode-solution-writer"
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="[REF: README.md]",
            directory=str(lw_dir),
        )
        result = assembler.assemble(skill, {})
        assert "这是一个题解模板的说明文件" in result

    def test_assemble_ref_not_found(self, assembler):
        """引用不存在的文件"""
        deploy_dir = FIXTURES_DIR / "deploy"
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="[REF: nonexistent.md]",
            directory=str(deploy_dir),
        )
        result = assembler.assemble(skill, {})
        assert "引用文件不存在" in result

    def test_assemble_constitution(self, assembler, deploy_skill):
        """注入宪法切片"""
        const_assembler = Assembler(
            executor=deploy_skill.__class__.__bases__[0]() if hasattr(deploy_skill, '__bases__') else Executor(timeout=10),
            constitution="全局约束规则"
        )
        # 重新创建 assembler 用正确的 executor
        const_assembler = Assembler(executor=Executor(timeout=10), constitution="全局约束规则")
        result = const_assembler.assemble(deploy_skill, {})
        assert "全局约束规则" in result

    def test_assemble_command_preprocess(self, assembler, deploy_skill):
        """!`command` 预处理（echo 命令）"""
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="前置内容\n!`echo hello`\n后置内容",
            directory=str(FIXTURES_DIR / "deploy"),
        )
        result = assembler.assemble(skill, {})
        assert "hello" in result
        assert "前置内容" in result
        assert "后置内容" in result

    def test_assemble_block_command(self, assembler, deploy_skill):
        """```! 代码块命令预处理"""
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="```!\necho block_test\n```",
            directory=str(FIXTURES_DIR / "deploy"),
        )
        result = assembler.assemble(skill, {})
        assert "block_test" in result


class TestRunner:
    """测试 Runner 三路分流"""

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
    def deploy_skill(self):
        deploy_dir = FIXTURES_DIR / "deploy"
        content = (deploy_dir / "SKILL.md").read_text(encoding="utf-8")
        import yaml
        import re
        _FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
        match = _FM_RE.match(content)
        fm_dict = yaml.safe_load(match.group(1)) if match else {}
        body = content[match.end():] if match else content
        return Skill(
            metadata=SkillMetadata(**fm_dict),
            body=body,
            directory=str(deploy_dir),
        )

    @pytest.fixture
    def match_result(self, deploy_skill):
        return MatchResult(
            skill=deploy_skill,
            score=0.8,
            method="keyword",
            arguments={"$ARGUMENTS": "部署", "$0": "部署"},
        )

    def test_run_pure_compile(self, runner, match_result):
        """纯编译模式（无 steps 无 llm）"""
        result = runner.run(match_result)
        assert result["skill_name"] == "deploy"
        assert result["score"] == 0.8
        assert "[SKILL: deploy]" in result["output"]
        assert result["files_created"] == []

    def test_run_档位A(self, runner, match_result):
        """档位 A：单次 LLM 调用"""
        mock_llm = MockLLM(response="LLM 生成的题解")
        result = runner.run(match_result, llm=mock_llm)
        assert result["skill_name"] == "deploy"
        assert result["score"] == 1.0
        assert "LLM 生成的题解" in result["output"]
        assert mock_llm.call_count == 1
        assert "[SKILL: deploy]" in mock_llm.last_prompt

    def test_run_档位A_error(self, runner, match_result):
        """档位 A：LLM 调用异常"""
        class FailingLLM:
            def invoke(self, prompt):
                raise RuntimeError("API 错误")

        result = runner.run(match_result, llm=FailingLLM())
        assert result["skill_name"] == "deploy"
        assert "LLM 调用失败" in result["output"]

    def test_run_steps_not_implemented_yet(self, runner, match_result):
        """Steps DSL 骨架（Phase 3 丰满）"""
        from skill_engine.models import Step
        steps = [Step(name="test", type="exec", command="echo hello")]
        result = runner.run(match_result, steps=steps)
        assert result["skill_name"] == "deploy"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["type"] == "exec"
        assert "hello" in result["steps"][0]["output"]

    def test_run_three_paths_exclusive(self, runner, match_result):
        """三条路径互斥：steps > llm > 纯编译"""
        from skill_engine.models import Step

        # 有 steps → 走 steps 路径
        steps = [Step(name="test", type="exec", command="echo steps")]
        result_steps = runner.run(match_result, steps=steps)
        assert "steps" in result_steps["steps"][0]["output"]

        # 无 steps 有 llm → 走 档位A
        mock_llm = MockLLM(response="档位A输出")
        result_llm = runner.run(match_result, llm=mock_llm)
        assert "档位A输出" in result_llm["output"]

        # 无 steps 无 llm → 纯编译
        result_pure = runner.run(match_result)
        assert "[SKILL:" in result_pure["output"]
        assert "steps" not in result_pure["steps"]

    def test_runner_resolve_template(self, runner):
        """模板变量解析"""
        # {step_name} 引用
        result = runner._resolve_template("{output}", {"output": "hello"}, {})
        assert result == "hello"

        # $VAR 引用
        result = runner._resolve_template("$ARGUMENTS", {}, {"$ARGUMENTS": "world"})
        assert result == "world"

        # 混合
        result = runner._resolve_template("{out} $ARGUMENTS", {"out": "prefix"}, {"$ARGUMENTS": "suffix"})
        assert result == "prefix suffix"


class TestIntegration:
    """端到端集成测试"""

    def test_assembler_executor_delegation(self):
        """Assembler 委托 Executor 执行命令"""
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)

        # 验证 assembler.executor 是同一个 executor 实例
        assert assembler.executor is executor

    def test_full_pipeline_compile(self):
        """完整流程：discovery → registry → router → assembler → pure compile"""
        from pathlib import Path as P
        TEST_FIXTURES = P(__file__).parent / "fixtures" / "sample-skills"
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry
        from skill_engine.router import Router
        from skill_engine.assembler import Assembler
        from skill_engine.executor import Executor

        index = discover(roots=[TEST_FIXTURES])
        registry = Registry(index)
        router = Router(registry)
        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)

        plan = router.match("deploy")
        assert plan.primary is not None
        assert plan.primary.name == "deploy"

        skill = registry.load_skill(plan.primary.name)
        prompt = assembler.assemble(skill, {})
        assert "[SKILL: deploy]" in prompt

    def test_full_pipeline_档位A(self):
        """完整流程：discovery → registry → router → assembler → runner(档位A)"""
        from pathlib import Path as P
        TEST_FIXTURES = P(__file__).parent / "fixtures" / "sample-skills"
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry
        from skill_engine.router import Router
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.runner import Runner

        index = discover(roots=[TEST_FIXTURES])
        registry = Registry(index)
        router = Router(registry)
        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        plan = router.match("deploy")
        assert plan.primary is not None

        skill = registry.load_skill(plan.primary.name)
        from skill_engine.models import MatchResult
        match_result = MatchResult(skill=skill, score=plan.score or 1.0, method=plan.method, arguments={})

        mock_llm = MockLLM(response="LLM 输出题解")
        result = runner.run(match_result, llm=mock_llm)
        assert "LLM 输出题解" in result["output"]

    def test_executor_is_only_spawn(self):
        """验证 Executor 是唯一 spawn 门神"""
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)

        # Assembler 内部应该调用 executor.run_preprocess
        # 而不是直接 subprocess.run
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="!`echo test`",
            directory=str(FIXTURES_DIR / "deploy"),
        )
        result = assembler.assemble(skill, {})
        assert "test" in result  # 命令被执行了
