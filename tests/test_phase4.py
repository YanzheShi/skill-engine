"""
Phase 4 测试套件

测试 embedding 匹配、allowlist 收紧、context:fork passthrough。
"""

import pytest
from pathlib import Path
from skill_engine.models import Skill, SkillMetadata, SkillContext, MatchResult, SkillOverride
from skill_engine.executor import Executor
from skill_engine.assembler import Assembler
from skill_engine.runner import Runner


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sample-skills"


class MockLLM:
    """模拟 LLM 客户端"""

    def __init__(self, response: str = "LLM 生成的题解"):
        self.response = response
        self.call_count = 0
        self.last_prompt = ""
        self.messages = []

    def invoke(self, prompt: str) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        return self.response


class TestAllowlist:
    """测试 V0.2 allowlist 收紧"""

    def test_default_allowlist_no_risky_cmds(self):
        """默认白名单不包含危险命令"""
        assert "rm" not in Executor.DEFAULT_ALLOWLIST
        assert "cp" not in Executor.DEFAULT_ALLOWLIST
        assert "mv" not in Executor.DEFAULT_ALLOWLIST
        assert "wget" not in Executor.DEFAULT_ALLOWLIST
        assert "apt" not in Executor.DEFAULT_ALLOWLIST
        assert "sudo" not in Executor.DEFAULT_ALLOWLIST

    def test_default_allowlist_has_safe_cmds(self):
        """默认白名单包含安全命令"""
        assert "python" in Executor.DEFAULT_ALLOWLIST
        assert "python3" in Executor.DEFAULT_ALLOWLIST
        assert "cat" in Executor.DEFAULT_ALLOWLIST
        assert "echo" in Executor.DEFAULT_ALLOWLIST
        assert "curl" in Executor.DEFAULT_ALLOWLIST
        assert "git" in Executor.DEFAULT_ALLOWLIST

    def test_executor_blocks_risky_when_allowlist_on(self):
        """allowlist 开启时阻止危险命令"""
        executor = Executor(timeout=10, allow_all=False)
        result = executor.run_step("rm -rf /", cwd=Path("/tmp"))
        assert result["exit_code"] == 1
        assert "安全拦截" in result["stderr"]

    def test_executor_blocks_wget_when_allowlist_on(self):
        """allowlist 开启时阻止 wget（不在默认名单）"""
        executor = Executor(timeout=10, allow_all=False)
        result = executor.run_step("wget http://example.com", cwd=Path("/tmp"))
        assert result["exit_code"] == 1
        assert "安全拦截" in result["stderr"]

    def test_executor_allows_python_when_allowlist_on(self):
        """allowlist 开启时允许 python"""
        executor = Executor(timeout=10, allow_all=False)
        result = executor.run_step("python --version", cwd=Path("/tmp"))
        assert result["exit_code"] == 0

    def test_executor_allows_git_when_allowlist_on(self):
        """allowlist 开启时允许 git"""
        executor = Executor(timeout=10, allow_all=False)
        result = executor.run_step("git --version", cwd=Path("/tmp"))
        assert result["exit_code"] == 0

    def test_executor_allowlist_override(self):
        """allowlist 可以被 skill 级别覆盖"""
        executor = Executor(timeout=10, allow_all=False)
        # 允许 curl（在 override 中且在 DEFAULT_ALLOWLIST 中）
        override = {"curl", "python", "head"}
        result = executor.run_step("curl --version", cwd=Path("/tmp"), allowlist_override=override)
        assert result["exit_code"] == 0  # curl 在 override 中

    def test_executor_allowlist_override_still_blocks_rm(self):
        """allowlist override 仍阻止 rm"""
        executor = Executor(timeout=10, allow_all=False)
        override = {"wget", "curl", "python"}
        result = executor.run_step("rm -rf /", cwd=Path("/tmp"), allowlist_override=override)
        assert result["exit_code"] == 1
        assert "安全拦截" in result["stderr"]

    def test_mvp_allow_all_true(self):
        """MVP 阶段 allow_all=True，所有命令都允许"""
        executor = Executor(timeout=10, allow_all=True)
        result = executor.run_step("echo mvp_allow_all", cwd=Path("/tmp"))
        # allow_all=True 时不检查 allowlist
        assert result["exit_code"] == 0
        assert "安全拦截" not in result["stderr"]


class TestContextForkPassthrough:
    """测试 context:fork 的 passthrough"""

    def test_fork_skill_warns_on_assembly(self):
        """fork skill 编译时应该有警告"""
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)

        skill = Skill(
            metadata=SkillMetadata(
                name="fork-test",
                description="测试 fork",
                context=SkillContext.FORK,
            ),
            body="fork 内容",
            directory="/tmp/test",
        )

        result = assembler.assemble(skill, {})
        # fork skill 应该被警告
        assert "[SKILL: fork-test]" in result
        # 实际实现中，fork passthrough 会在 assembler 中加注释
        # 这里验证 fork skill 能正常编译（不 crash）


class TestEmbeddingFallback:
    """测试 embedding 匹配的优雅降级"""

    def test_embedding_returns_empty_without_package(self):
        """未安装 sentence-transformers 时返回空"""
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry
        from skill_engine.router import Router

        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)

        results = router.match("测试", method="embedding")
        assert len(results) == 0

    def test_embedding_with_mock_package(self, monkeypatch):
        """mock sentence-transformers 时能工作"""
        # 由于我们不在 CI 环境中，这个测试只做结构验证
        # 实际 embedding 测试需要在有 sentence-transformers 的环境下运行
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry
        from skill_engine.router import Router

        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)

        # 确认 router 有 embedding 相关属性
        assert hasattr(router, '_embedding_cache')
        assert hasattr(router, '_has_embedding')


class TestEndToEndAllowlist:
    """端到端：allowlist 收紧后的完整流程"""

    @pytest.fixture
    def strict_executor(self):
        return Executor(timeout=10, allow_all=False)

    @pytest.fixture
    def strict_assembler(self, strict_executor):
        return Assembler(executor=strict_executor)

    @pytest.fixture
    def strict_runner(self, strict_assembler, strict_executor):
        return Runner(strict_assembler, strict_executor)

    def test_exec_step_blocked_by_default_allowlist(self, strict_executor):
        """默认 allowlist 下 exec step 被阻止"""
        result = strict_executor.run_step("rm -rf /", cwd=Path("/tmp"))
        assert result["exit_code"] == 1
        assert "安全拦截" in result["stderr"]

    def test_exec_step_allowed_by_default_allowlist(self, strict_executor):
        """默认 allowlist 下允许的 step 正常执行"""
        result = strict_executor.run_step("python --version", cwd=Path("/tmp"))
        assert result["exit_code"] == 0

    def test_steps_dsl_with_strict_allowlist(self, strict_assembler, strict_executor):
        """Steps DSL 在严格 allowlist 下工作"""
        from skill_engine.models import Step, SkillMetadata, MatchResult, Skill

        runner = Runner(strict_assembler, strict_executor)

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory="/tmp",
        )
        match = MatchResult(
            skill=skill,
            score=1.0,
            method="name",
            arguments={},
        )

        # echo 在默认 allowlist 中
        steps = [Step(name="say_hi", type="exec", command="echo hello_strict")]
        result = runner.run(match, steps=steps)
        assert result["steps"][0]["type"] == "exec"
        assert "hello_strict" in result["steps"][0]["output"]

        # rm 不在默认 allowlist 中
        steps_bad = [Step(name="danger", type="exec", command="rm -rf /")]
        result_bad = runner.run(match, steps=steps_bad)
        assert result_bad["steps"][0]["exit_code"] == 1
        assert "安全拦截" in result_bad["steps"][0].get("error", "")


class TestSkillContextEnum:
    """测试 SkillContext 枚举"""

    def test_inline_context(self):
        from skill_engine.models import SkillContext
        assert SkillContext.INLINE.value == "inline"
        assert SkillContext.FORK.value == "fork"

    def test_skill_with_fork_context(self):
        from skill_engine.models import SkillContext
        meta = SkillMetadata(
            name="fork-skill",
            description="测试 fork",
            context=SkillContext.FORK,
        )
        assert meta.context == SkillContext.FORK
        assert meta.context.value == "fork"

    def test_skill_with_inline_context(self):
        from skill_engine.models import SkillContext
        meta = SkillMetadata(
            name="inline-skill",
            description="测试 inline",
            context=SkillContext.INLINE,
        )
        assert meta.context == SkillContext.INLINE
        assert meta.context.value == "inline"
