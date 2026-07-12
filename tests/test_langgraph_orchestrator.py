"""
LangGraph 编排器集成测试

验证 LangGraph StateGraph 替代 while 循环后的行为一致性。
"""

import pytest
import json
from pathlib import Path
import asyncio


class TestLangGraphOrchestrator:
    """测试 LangGraph 编排器"""

    @pytest.fixture
    def orchestrator(self, tmp_path):
        """构建 orchestrator 实例"""
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.orchestrator import Orchestrator

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        return Orchestrator(assembler, executor)

    @pytest.fixture
    def mock_llm(self):
        """模拟 LLM：返回编排决策"""
        class MockLLM:
            def invoke(self, messages_or_prompt):
                # 支持 list[dict] 或 str
                if isinstance(messages_or_prompt, list):
                    # 取最后一条 message 的 content
                    content = messages_or_prompt[-1].get("content", "")
                else:
                    content = messages_or_prompt
                
                # 如果是 catalog prompt，返回编排决策
                if "可用技能清单" in content or "技能清单" in content:
                    return json.dumps({
                        "plan": [
                            {
                                "skill": "leetcode-solution-writer",
                                "args": {"$ARGUMENTS": "第1题"},
                                "description": "生成题解",
                            },
                        ],
                        "reasoning": "用户需要生成题解",
                    })
                
                # 如果是 skill prompt，返回模拟结果
                return "[模拟 LLM 输出]"
        
        return MockLLM()

    def test_orchestrator_builds_graph(self, orchestrator):
        """验证 graph 构建成功"""
        assert orchestrator.graph is not None
    
    def test_orchestrator_catalog_format(self, orchestrator):
        """验证 catalog 格式（LangGraph 版本）"""
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        catalog = orchestrator._build_catalog(registry)
        assert "可用技能清单" in catalog
        assert "分组:" in catalog  # 应该有分组展示
        assert "leetcode-solution-writer" in catalog

    def test_orchestrator_system_prompt(self, orchestrator):
        """验证 prompt 构建"""
        catalog = "## 测试 catalog\n- **test**: 测试 skill"
        prompt = orchestrator._build_system_prompt(catalog, "测试需求")
        assert "技能编排器" in prompt
        assert "测试 catalog" in prompt
        assert "测试需求" in prompt
        assert "JSON 格式" in prompt

    def test_langgraph_plan_node(self, orchestrator):
        """测试 plan 节点"""
        from skill_engine.orchestrator import plan_node

        state = {
            "planning_messages": [{"role": "user", "content": "测试"}],
            "_llm": None,
            "planning_iterations": 0,
        }
        
        # LLM 为 None 时应返回空 plan
        result = asyncio.get_event_loop().run_until_complete(
            plan_node(state)
        )
        assert result["plan"] == []
        assert "LLM client not available" in result["reasoning"]

    def test_langgraph_execute_chain_node(self, orchestrator):
        """测试 execute_chain 节点"""
        from skill_engine.orchestrator import execute_chain_node
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        state = {
            "plan": [{"skill": "leetcode-solution-writer", "args": {}, "description": "测试"}],
            "prev_outputs": {},
            "_llm": None,
        }
        
        # LLM 为 None，应该跳过执行
        result = execute_chain_node(state, orchestrator.assembler, orchestrator.executor, registry)
        assert "chain_results" in result

    def test_langgraph_should_continue(self):
        """测试条件路由"""
        from skill_engine.orchestrator import should_continue_node

        # 空 plan → direct_answer
        state = {"plan": [], "planning_iterations": 0}
        assert should_continue_node(state) == "direct_answer"

        # 有 plan → execute
        state = {"plan": [{"skill": "test"}], "planning_iterations": 0}
        assert should_continue_node(state) == "execute"

        # 超过最大迭代 → max_planning_steps
        state = {"plan": [{"skill": "test"}], "planning_iterations": 5, "_max_planning_steps": 5}
        assert should_continue_node(state) == "max_planning_steps"

    def test_langgraph_format_result(self):
        """测试结果格式化"""
        from skill_engine.orchestrator import format_result_node

        # 空 plan → direct_answer
        state = {"plan": [], "reasoning": "不需要 skill"}
        result = format_result_node(state)
        assert result["stopped_by"] == "direct_answer"

        # 有执行结果 → complete
        state = {
            "plan": [{"skill": "test"}],
            "reasoning": "测试推理",
            "chain_results": [
                {"skill": "test", "status": "success", "description": "测试"}
            ],
            "all_outputs": "输出内容",
        }
        result = format_result_node(state)
        assert result["stopped_by"] == "complete"
        assert "推理" in result["output"]
        assert "✅" in result["output"]

    def test_full_langgraph_pipeline(self, orchestrator, mock_llm):
        """完整 LangGraph 管道测试"""
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        # 运行编排
        result = orchestrator.run(
            user_input="帮我生成题解",
            registry=registry,
            llm=mock_llm,
            max_planning_steps=2,
        )

        # 验证结果结构
        assert result["skill_name"] == "orchestrator"
        assert result["score"] == 1.0
        assert "chain" in result
        assert "reasoning" in result
        assert "output" in result
        assert "stopped_by" in result
        
        # 应该有执行链
        assert len(result["chain"]) > 0
        assert result["chain"][0]["skill"] == "leetcode-solution-writer"


class TestLangGraphVsWhileLoop:
    """对比 LangGraph 和 while 循环的行为一致性"""

    def test_behavior_consistency(self, tmp_path):
        """LangGraph 编排结果应与 while 循环一致"""
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.runner import Runner
        from skill_engine.orchestrator import Orchestrator
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry
        import json

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)
        orch = Orchestrator(assembler, executor)

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        # Mock LLM 返回相同的决策
        class MockLLM:
            def invoke(self, messages_or_prompt):
                if isinstance(messages_or_prompt, list):
                    content = messages_or_prompt[-1].get("content", "")
                else:
                    content = messages_or_prompt
                
                if "可用技能清单" in content or "技能清单" in content:
                    return json.dumps({
                        "plan": [
                            {"skill": "leetcode-solution-writer", "args": {}, "description": "生成题解"},
                        ],
                        "reasoning": "测试编排",
                    })
                return "[LLM 输出]"

        llm = MockLLM()

        # while 循环版本
        result_while = runner.run_orchestration(
            user_input="帮我生成题解",
            registry=registry,
            llm=llm,
            max_planning_steps=2,
        )

        # LangGraph 版本
        result_langgraph = orch.run(
            user_input="帮我生成题解",
            registry=registry,
            llm=llm,
            max_planning_steps=2,
        )

        # 验证关键结果一致
        assert result_while["skill_name"] == result_langgraph["skill_name"]
        assert result_while["score"] == result_langgraph["score"]
        # iterations 可能不同（LangGraph 多跑一轮），只验证 stopped_by 一致
        assert result_while["stopped_by"] == result_langgraph["stopped_by"]
        assert len(result_while["chain"]) == len(result_langgraph["chain"])
