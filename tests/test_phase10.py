"""
Phase 10 测试套件 — 多 skill 编排（Orchestrator）

LLM 自动选择并编排多个 skill 协同工作。
"""

import pytest
from pathlib import Path
import json
from unittest.mock import MagicMock


class MockOrchestratorLLM:
    """模拟 LLM：先返回编排决策，再返回各 skill 的结果"""

    def __init__(self, orchestration_plan=None, skill_responses=None):
        self.orchestration_plan = orchestration_plan or {"plan": [], "reasoning": ""}
        self.skill_responses = skill_responses or {}
        self.call_count = 0

    def invoke(self, messages):
        """支持两种调用方式：接收 messages 列表或 prompt 字符串"""
        self.call_count += 1

        # 提取用户输入内容
        if isinstance(messages, str):
            user_input = messages
        else:
            user_input = ""
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    user_input = msg.get("content", "")
                    break

        # 第一条消息：返回编排决策
        if self.call_count == 1:
            return {
                "content": json.dumps(self.orchestration_plan, ensure_ascii=False),
                "tool_calls": [],
            }

        # 后续消息：根据 user_input 中的 skill 名称返回对应结果
        for skill_name, response in self.skill_responses.items():
            if skill_name in user_input:
                return {"content": response, "tool_calls": []}

        return {"content": "[LLM 返回默认响应]", "tool_calls": []}


class TestOrchestratorLogic:
    """测试编排逻辑"""

    def test_build_skill_catalog(self):
        """构建 skill 目录 prompt"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.executor import Executor
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.discovery import discover

        index = discover(roots=["tests/fixtures/sample-skills"])
        registry = Registry(index)
        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        catalog = runner._build_skill_catalog(registry)
        assert "deploy" in catalog
        assert "code-review" in catalog
        # 应该包含 when_to_use 等信息
        assert "适用场景" in catalog or "描述" in catalog

    def test_catalog_with_groups(self):
        """catalog 按分组缩略展示"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.executor import Executor
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.discovery import discover

        index = discover(roots=["skills"])
        registry = Registry(index)
        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        catalog = runner._build_skill_catalog(registry)
        # 应该有分组标题
        assert "分组:" in catalog
        # leetcode-solution-writer 在分组下应该是缩略展示
        assert "leetcode" in catalog

    def test_info_full_skips_body(self):
        """info_full 只解析 frontmatter，不读 body"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)

        fm = registry.info_full("orchestrator")
        assert fm is not None
        assert fm["name"] == "orchestrator"
        assert "编排" in fm["description"]
        # 不包含 body
        assert "body" not in fm

    def test_get_groups(self):
        """get_groups 按 groups 字段分组"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)

        groups = registry.get_groups()
        # leetcode-solution-writer 有 groups: [leetcode, programming]
        assert "leetcode" in groups
        assert "programming" in groups
        assert "leetcode-solution-writer" in groups["leetcode"]
        assert "orchestrator" in groups.get("__ungrouped__", [])

    def test_parse_orchestration_plan(self):
        """解析编排计划"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.executor import Executor

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        plan_json = json.dumps({
            "plan": [
                {"skill": "deploy", "args": {"env": "prod"}, "description": "部署到生产环境"},
                {"skill": "code-review", "args": {}, "description": "审查部署配置"},
            ],
            "reasoning": "先部署再审查代码质量",
        }, ensure_ascii=False)

        parsed = runner._parse_orchestration_plan(plan_json)
        assert len(parsed["plan"]) == 2
        assert parsed["plan"][0]["skill"] == "deploy"
        assert parsed["plan"][0]["args"] == {"env": "prod"}
        assert parsed["reasoning"] == "先部署再审查代码质量"

    def test_parse_invalid_plan(self):
        """解析无效编排计划"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.executor import Executor

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        # 纯文本，不是 JSON
        parsed = runner._parse_orchestration_plan("我不需要任何 skill")
        assert parsed["plan"] == []
        assert "我不需要任何 skill" in parsed.get("reasoning", "")

    def test_parse_empty_plan(self):
        """空编排计划"""
        from skill_engine.execution.runner import Runner
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.executor import Executor

        executor = Executor(timeout=10)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        parsed = runner._parse_orchestration_plan(json.dumps({"plan": []}))
        assert parsed["plan"] == []


class TestOrchestrationFlow:
    """测试完整编排流程"""

    @pytest.fixture
    def engine(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner

        index = discover(roots=["tests/fixtures/sample-skills"])
        registry = Registry(index)
        executor = Executor(timeout=30, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)
        return registry, executor, assembler, runner

    def test_orchestration_two_skills(self, engine):
        """编排两个 skill 依次执行"""
        registry, executor, assembler, runner = engine

        llm = MockOrchestratorLLM(
            orchestration_plan={
                "plan": [
                    {"skill": "deploy", "args": {}, "description": "部署服务"},
                    {"skill": "code-review", "args": {}, "description": "代码审查"},
                ],
                "reasoning": "先部署再审查代码质量",
            },
            skill_responses={
                "deploy": "部署完成，服务运行在端口 8080",
                "code-review": "代码审查通过，无严重问题",
            },
        )

        result = runner.run_orchestration("帮我部署并审查代码", registry, llm, max_planning_steps=3)

        assert result["skill_name"] == "orchestrator"
        assert "先部署再审查" in result["reasoning"]
        assert len(result["chain"]) == 2
        assert result["chain"][0]["skill"] == "deploy"
        assert result["chain"][1]["skill"] == "code-review"
        assert "部署完成" in result["output"]

    def test_orchestration_no_skills_needed(self, engine):
        """LLM 判断不需要任何 skill"""
        registry, executor, assembler, runner = engine

        llm = MockOrchestratorLLM(
            orchestration_plan={
                "plan": [],
                "reasoning": "这个问题不需要调用任何 skill，直接回答即可",
            },
        )

        result = runner.run_orchestration("你好，最近怎么样", registry, llm)

        assert result["chain"] == []
        assert "不需要" in result["reasoning"]

    def test_orchestration_single_skill(self, engine):
        """只调用一个 skill"""
        registry, executor, assembler, runner = engine

        llm = MockOrchestratorLLM(
            orchestration_plan={
                "plan": [
                    {"skill": "deploy", "args": {}, "description": "部署"},
                ],
                "reasoning": "只需要部署",
            },
            skill_responses={
                "deploy": "部署成功",
            },
        )

        result = runner.run_orchestration("部署服务", registry, llm)

        assert len(result["chain"]) == 1
        assert result["chain"][0]["skill"] == "deploy"
        assert "部署成功" in result["output"]

    def test_orchestration_with_args(self, engine):
        """编排带参数的 skill"""
        registry, executor, assembler, runner = engine

        llm = MockOrchestratorLLM(
            orchestration_plan={
                "plan": [
                    {"skill": "deploy", "args": {"env": "staging", "port": "3000"}, "description": "部署到 staging"},
                ],
                "reasoning": "部署到 staging 环境",
            },
            skill_responses={
                "deploy": "已部署到 staging:3000",
            },
        )

        result = runner.run_orchestration("部署到 staging 环境", registry, llm)

        assert result["chain"][0]["skill"] == "deploy"
        assert result["chain"][0]["args"] == {"env": "staging", "port": "3000"}
        assert "staging" in result["output"]

    def test_orchestration_max_steps(self, engine):
        """达到最大编排步骤"""
        registry, executor, assembler, runner = engine

        call_count = [0]
        class CountingLLM:
            def invoke(self, messages):
                call_count[0] += 1
                if call_count[0] <= 3:
                    return {
                        "content": json.dumps({
                            "plan": [{"skill": "deploy", "args": {}, "description": f"第{call_count[0]}步"}],
                            "reasoning": f"继续第{call_count[0]}步",
                        }, ensure_ascii=False),
                        "tool_calls": [],
                    }
                # 之后返回空 plan → 触发 direct_answer
                return {
                    "content": json.dumps({"plan": [], "reasoning": "完成"}),
                    "tool_calls": [],
                }

        llm = CountingLLM()
        result = runner.run_orchestration("测试", registry, llm, max_planning_steps=5)

        # LLM 返回空 plan 后，runner 返回 direct_answer 或 complete
        assert result["stopped_by"] in ("direct_answer", "max_planning_steps", "complete")
        # 至少执行了几轮编排
        assert call_count[0] >= 2

    def test_orchestration_skill_not_found(self, engine):
        """编排中引用的 skill 不存在"""
        registry, executor, assembler, runner = engine

        llm = MockOrchestratorLLM(
            orchestration_plan={
                "plan": [
                    {"skill": "nonexistent-skill", "args": {}, "description": "不存在的 skill"},
                ],
                "reasoning": "调用一个不存在的 skill",
            },
        )

        result = runner.run_orchestration("测试", registry, llm)

        # 应该标记为 error
        assert len(result["chain"]) == 1
        assert result["chain"][0]["status"] == "error"
        assert "未找到" in result["output"] or "nonexistent" in result["output"]


class TestOrchestratorSKILL:
    """测试 orchestrator SKILL.md"""

    def test_orchestrator_skill_exists(self):
        """orchestrator SKILL.md 存在"""
        from pathlib import Path
        orch_path = Path(__file__).parent.parent / "skills" / "orchestrator"
        assert (orch_path / "SKILL.md").exists()

    def test_orchestrator_can_be_discovered(self):
        """orchestrator 能被 discovery 发现"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)
        assert "orchestrator" in index

    def test_orchestrator_has_correct_metadata(self):
        """orchestrator 有正确的元数据"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)
        skill = registry.load_skill("orchestrator")

        assert skill is not None
        assert "编排" in skill.metadata.description or "orchestrate" in skill.metadata.description.lower()
