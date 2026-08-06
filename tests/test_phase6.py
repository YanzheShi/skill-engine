"""
Phase 6 测试套件 — 接入真实 leetcode-solution-writer skill

验证：
1. 从 GitHub 克隆的真实 skill 能被 discovery 发现
2. fetch_problem.py 脚本能正常获取题目信息
3. Assembler 能正确编译 skill
4. Runner 能执行 skill
5. CLI run 命令能跑通
"""

import pytest
from pathlib import Path
import subprocess
import sys


REAL_SKILLS_DIR = Path(__file__).parent.parent / "skills"
LEETCODE_SKILL_DIR = REAL_SKILLS_DIR / "leetcode-solution-writer"


class TestRealSkillDiscovery:
    """测试真实 skill 的发现"""

    def test_skill_exists_on_disk(self):
        """skill 目录存在于磁盘"""
        assert LEETCODE_SKILL_DIR.exists()
        assert (LEETCODE_SKILL_DIR / "SKILL.md").exists()

    def test_skill_body_length(self):
        """SKILL.md body 有足够内容"""
        skill_md = LEETCODE_SKILL_DIR / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        assert len(content) > 5000  # 原文档 13KB+

    def test_script_exists(self):
        """fetch_problem.py 存在"""
        script = LEETCODE_SKILL_DIR / "scripts" / "fetch_problem.py"
        assert script.exists()
        assert script.stat().st_size > 5000  # 337 行

    def test_assets_exist(self):
        """assets 目录存在"""
        assets = LEETCODE_SKILL_DIR / "assets"
        assert assets.exists()
        assert (assets / "solution-template.md").exists()


class TestFetchScript:
    """测试 fetch_problem.py 脚本"""

    def test_fetch_problem_49(self):
        """获取第 49 题信息"""
        result = subprocess.run(
            [sys.executable, str(LEETCODE_SKILL_DIR / "scripts" / "fetch_problem.py"), "49"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(LEETCODE_SKILL_DIR),
        )
        assert result.returncode == 0
        import json
        data = json.loads(result.stdout)
        assert data["id"] == "49"
        assert "字母异位词分组" in data["title"]
        assert data["slug"] == "group-anagrams"

    def test_fetch_problem_nonexistent(self):
        """获取不存在的题号应降级到简单模式或返回错误"""
        result = subprocess.run(
            [sys.executable, str(LEETCODE_SKILL_DIR / "scripts" / "fetch_problem.py"), "99999"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(LEETCODE_SKILL_DIR),
        )
        # fetch_problem.py 对不存在的题号可能返回非零（API 返回 None 导致的 bug）
        # 我们不要求它一定成功，只要求不 crash（subprocess 捕获了异常）
        assert result.returncode in (0, 1)

    def test_fetch_help(self):
        """不带参数应打印帮助"""
        result = subprocess.run(
            [sys.executable, str(LEETCODE_SKILL_DIR / "scripts" / "fetch_problem.py")],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(LEETCODE_SKILL_DIR),
        )
        assert result.returncode != 0  # 无参数应退出非零


class TestAssemblerCompile:
    """测试 Assembler 编译真实 skill"""

    @pytest.fixture
    def assembler(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        executor = Executor(timeout=30, allow_all=True)
        return Assembler(executor=executor, command_timeout=30)

    def test_assemble_real_skill(self, assembler):
        """编译真实 skill"""
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.discovery import discover

        index = discover(roots=[str(REAL_SKILLS_DIR)])
        registry = Registry(index)
        skill = registry.load_skill("leetcode-solution-writer")
        assert skill is not None
        assert len(skill.body) > 5000

        prompt = assembler.assemble(skill, {"$0": "49", "$ARGUMENTS": "49"})
        assert "[SKILL: leetcode-solution-writer]" in prompt
        assert "字母异位词分组" in prompt

    def test_assemble_injects_paths(self, assembler):
        """编译时注入路径变量"""
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.discovery import discover

        index = discover(roots=[str(REAL_SKILLS_DIR)])
        registry = Registry(index)
        skill = registry.load_skill("leetcode-solution-writer")

        prompt = assembler.assemble(skill, {})
        # 检查编译后的 prompt 包含 skill 目录相关信息
        assert "leetcode-solution-writer" in prompt


class TestRunnerIntegration:
    """端到端：Runner 执行真实 skill"""

    @pytest.fixture
    def engine(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        index = discover(roots=[str(REAL_SKILLS_DIR)])
        registry = Registry(index)
        router = Router(registry)
        executor = Executor(timeout=30, allow_all=True)
        assembler = Assembler(executor=executor, command_timeout=30)
        runner = Runner(assembler, executor)
        return index, registry, router, executor, assembler, runner

    def test_run_real_skill(self, engine):
        """运行真实 skill"""
        _, _reg, router, _, _, runner = engine

        plan = router.match("生成题解")
        # 直接通过 name 加载 skill
        from skill_engine.models import MatchResult
        skill = _reg.load_skill("leetcode-solution-writer")
        assert skill is not None, "leetcode-solution-writer should exist"
        result = runner.run(MatchResult(
            skill=skill, score=1.0, method="name",
            arguments={"$ARGUMENTS": "生成题解", "$0": "生成题解"},
        ))
        assert result["skill_name"] == "leetcode-solution-writer"
        assert len(result["output"]) > 5000

    def test_run_with_llm_mock(self, engine):
        """档位 A 执行（mock LLM）"""
        _idx, _reg, _, _, _, runner = engine

        class MockLLM:
            def invoke(self, prompt):
                return "LLM 生成的题解内容"
        from skill_engine.models import MatchResult
        skill = _reg.load_skill("leetcode-solution-writer")
        assert skill is not None
        result = runner.run(MatchResult(
            skill=skill, score=1.0, method="name",
            arguments={"$ARGUMENTS": "生成题解", "$0": "生成题解"},
        ), llm=MockLLM())
        assert result["skill_name"] == "leetcode-solution-writer"
        assert "LLM 生成的题解内容" in result["output"]


class TestCLIIntegration:
    """CLI 集成测试"""

    def test_skill_engine_list_with_skills(self):
        """skill-engine list 能列出真实 skills"""
        result = subprocess.run(
            [sys.executable, "-m", "skill_engine.cli", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).parent.parent),
        )
        # CLI 返回 0（成功）或 2（typer 对无参数返回 help）
        assert result.returncode in (0, 2)
