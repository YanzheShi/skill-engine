"""
集成测试 — 模拟真实用户场景，端到端验证整个系统

测试场景：
1. 用户搜索 skill → 匹配 → 编译 → 输出 prompt
2. 用户提交复杂需求 → orchestrator 自动编排多 skill
3. 用户要求创建新 skill → LLM 调用 create_skill → 验证 → 注册
4. CLI 命令行端到端测试
"""

import pytest
import subprocess
import json
import sys
from pathlib import Path


# ================================================================
# 场景 1: 用户搜索 + 匹配 + 编译
# ================================================================

class TestScenarioSearchAndCompile:
    """场景：用户搜索 skill，系统匹配并编译"""

    @pytest.fixture
    def runner(self, tmp_path):
        """构建 runner 实例"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        project_skills = Path("/home/andre/Code/PycharmProjects/skill-engine/skills")
        if not project_skills.exists():
            # 回退到项目相对路径
            project_skills = Path("skills")
        roots = [str(project_skills)] if project_skills.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        router = Router(registry)
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)
        return registry, router, runner

    def test_scenario_user_searches_for_leetcode(self, runner):
        """用户输入：生成 LeetCode 题解"""
        registry, router, r = runner

        # 用户搜索
        results = router.match("生成题解", method="keyword", top_k=3)

        # 应该有匹配结果
        assert len(results) > 0

        # 取最高分匹配
        best = results[0]
        assert best.score > 0

        # 编译 prompt
        prompt = r.assembler.assemble(best.skill, best.arguments)
        assert len(prompt) > 0
        # 编译后的 prompt 应包含 skill 正文内容
        assert "LeetCode" in prompt

    def test_scenario_user_wants_pdf_conversion(self, runner):
        """用户输入：把 markdown 转成 pdf"""
        registry, router, r = runner

        results = router.match("把 markdown 转成 pdf", method="keyword", top_k=3)

        # 可能没有精确匹配，但应该有部分匹配
        if results:
            prompt = r.assembler.assemble(results[0].skill, results[0].arguments)
            assert len(prompt) > 0


# ================================================================
# 场景 2: Orchestrator 编排多 skill
# ================================================================

class TestScenarioOrchestration:
    """场景：用户提交复杂需求，orchestrator 自动编排"""

    @pytest.fixture
    def mock_llm(self):
        """模拟 LLM：返回编排决策"""
        class MockLLM:
            def invoke(self, prompt):
                # 模拟编排决策
                return json.dumps({
                    "plan": [
                        {
                            "skill": "leetcode-solution-writer",
                            "args": {"$ARGUMENTS": "第1题"},
                            "description": "生成第1题题解",
                        },
                    ],
                    "reasoning": "用户需要生成题解",
                })
        return MockLLM()

    @pytest.fixture
    def runner(self):
        """构建 runner 实例"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        project_skills = Path("/home/andre/Code/PycharmProjects/skill-engine/skills")
        if not project_skills.exists():
            project_skills = Path("skills")
        roots = [str(project_skills)] if project_skills.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)
        return registry, runner

    def test_scenario_complex_request(self, runner, mock_llm):
        """用户：帮我出一道题并生成题解"""
        registry, r = runner

        result = r.run_orchestration(
            user_input="帮我出一道题并生成题解",
            registry=registry,
            llm=mock_llm,
            max_planning_steps=2,
        )

        assert result["skill_name"] == "orchestrator"
        assert result["stopped_by"] == "complete"
        assert "推理" in result["output"]
        assert len(result["chain"]) > 0


# ================================================================
# 场景 3: 创建新 skill 并验证
# ================================================================

class TestScenarioCreateSkill:
    """场景：用户要求创建新 skill"""

    def test_scenario_user_requests_new_skill(self, tmp_path):
        """用户：帮我创建一个 markdown 转 pdf 的 skill"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")
        result = runner.create_skill(
            name="markdown-to-pdf",
            description="将 Markdown 文件转换为 PDF 格式",
            groups=["documents", "conversion"],
            when_to_use="用户需要将 Markdown 转为 PDF 时",
            argument_hint="markdown 文件路径",
            body_template="# Markdown 转 PDF\n\n使用 pandoc 将 Markdown 文件转换为 PDF。",
            skills_dir=skills_dir,
        )

        assert result["status"] == "success"
        assert result["validated"] is True
        assert result["valid"] is True

        # 验证文件确实存在
        skill_dir = Path(skills_dir) / "markdown-to-pdf"
        assert (skill_dir / "SKILL.md").exists()
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "markdown" in content.lower()

    def test_scenario_create_skill_with_dependency(self, tmp_path):
        """用户：创建一个带依赖脚本的 skill"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")
        result = runner.create_skill(
            name="image-resize",
            description="调整图片尺寸",
            groups=["images", "processing"],
            scripts={
                "resize.py": "from PIL import Image\nimport sys\nimg = Image.open(sys.argv[1])\nimg.resize((800, 600)).save(sys.argv[2])",
            },
            skills_dir=skills_dir,
        )

        assert result["status"] == "success"
        scripts_dir = Path(skills_dir) / "image-resize" / "scripts"
        assert (scripts_dir / "resize.py").exists()


# ================================================================
# 场景 4: CLI 端到端测试（用 terminal 工具跑，不靠 subprocess）
# ================================================================

class TestScenarioCLI:
    """场景：通过 Python API 模拟 CLI 行为"""

    @pytest.fixture
    def cli_project_dir(self):
        """项目根目录"""
        return Path("/mnt/d/Code/PycharmProjects/skill-engine")

    def test_cli_list_command(self, tmp_path):
        """CLI: skill-engine list — 通过 API 验证"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        # 在项目目录运行
        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        active = registry.list_active()

        assert len(active) > 0
        assert "leetcode-solution-writer" in active

    def test_cli_match_command(self, tmp_path):
        """CLI: skill-engine match — 通过 API 验证"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        router = Router(registry)

        results = router.match("生成题解", method="keyword", top_k=5)
        # 至少有一些匹配
        assert len(results) > 0

    def test_cli_scan_command(self, tmp_path):
        """CLI: skill-engine scan — 通过 API 验证"""
        from skill_engine.routing.discovery import discover

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        assert len(index) > 0

    def test_cli_info_command(self, tmp_path):
        """CLI: skill-engine info — 通过 API 验证"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        skill = registry.load_skill("leetcode-solution-writer")
        assert skill is not None
        assert "LeetCode" in skill.metadata.description

    def test_cli_dry_run_command(self, tmp_path):
        """CLI: skill-engine run --dry-run — 通过 API 验证"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        router = Router(registry)
        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)

        results = router.match("生成题解", method="keyword", top_k=1)
        if results:
            prompt = assembler.assemble(results[0].skill, results[0].arguments)
            assert len(prompt) > 50


# ================================================================
# 场景 5: 用户创建 skill 后立即可用
# ================================================================

class TestScenarioNewSkillImmediatelyUsable:
    """场景：创建 skill 后，立即可被 discovery 和 router 发现"""

    def test_new_skill_is_discoverable(self, tmp_path):
        """创建 skill 后立即可被 discover 发现"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")

        # 1. 创建 skill
        result = runner.create_skill(
            name="test-usable",
            description="测试新 skill 立即可用",
            groups=["test"],
            skills_dir=skills_dir,
        )
        assert result["status"] == "success"

        # 2. 重新 discover
        index = discover(roots=[skills_dir])
        registry = Registry(index)
        router = Router(registry)

        # 3. 应该能匹配到
        results = router.match("测试新 skill 立即可用", method="keyword", top_k=5)
        names = [r.skill.metadata.name for r in results]
        assert "test-usable" in names

        # 4. 应该能编译
        skill = registry.load_skill("test-usable")
        assert skill is not None
        prompt = assembler.assemble(skill, {})
        assert len(prompt) > 0


# ================================================================
# 场景 6: 编排模式 + 创建 skill 的组合
# ================================================================

class TestScenarioCombinedWorkflow:
    """场景：编排模式 + 创建 skill 组合使用"""

    def test_orchestrator_can_direct_create(self, tmp_path):
        """编排器可以指示创建新 skill"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")

        # 模拟 orchestrator 决定创建新 skill
        result = runner.create_skill(
            name="weather-checker",
            description="查询天气信息",
            groups=["utilities", "weather"],
            when_to_use="用户想知道天气情况时",
            body_template="# 天气查询\n\n使用 curl 查询天气 API。",
            skills_dir=skills_dir,
        )

        assert result["status"] == "success"

        # 验证新 skill 可以被编排
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        index = discover(roots=[skills_dir])
        registry = Registry(index)

        # 验证 catalog 中包含新 skill
        catalog = runner._build_skill_catalog(registry)
        assert "weather-checker" in catalog

        # 验证分组正确
        groups = registry.get_groups()
        assert "utilities" in groups
        assert "weather" in groups
        assert "weather-checker" in groups["utilities"]
