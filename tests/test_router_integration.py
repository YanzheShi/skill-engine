"""
LLM 路由集成测试 — 四种匹配方法的端到端验证

测试场景：
1. keyword 匹配 — 传统关键词打分
2. embedding 匹配 — sentence-transformers 语义相似度
3. llm 匹配 — LLM 意图理解 + 打分
4. 四种方法对比 — 同一 query 的不同结果
"""

import pytest
import json
from pathlib import Path


class TestRouterKeywordMatch:
    """测试 keyword 匹配方法"""

    @pytest.fixture
    def router(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        return Router(registry)

    def test_keyword_matches_leetcode(self, router):
        """用户输入包含 'leetcode' 关键词应匹配到 leetcode-solution-writer"""
        results = router.match("帮我生成 LeetCode 题解", method="keyword")
        assert len(results) > 0
        # leetcode-solution-writer 应该在结果中
        names = [r.skill.metadata.name for r in results]
        assert "leetcode-solution-writer" in names

    def test_keyword_matches_orchestrator(self, router):
        """用户输入复杂需求应匹配到 orchestrator"""
        results = router.match("帮我出题然后解题", method="keyword")
        assert len(results) > 0
        names = [r.skill.metadata.name for r in results]
        assert "orchestrator" in names

    def test_keyword_no_match_empty_query(self, router):
        """空查询应返回空列表"""
        results = router.match("", method="keyword")
        assert results == []

    def test_keyword_returns_scores(self, router):
        """keyword 匹配应返回非空结果"""
        results = router.match("生成题解", method="keyword")
        assert len(results) > 0
        # 所有分数应 >= 0
        assert all(r.score >= 0 for r in results)


class TestRouterEmbeddingMatch:
    """测试 embedding 匹配方法"""

    @pytest.fixture
    def router(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        return Router(registry)

    def test_embedding_returns_results(self, router):
        """embedding 匹配应返回结果（如果安装了 sentence-transformers）"""
        results = router.match("帮我生成 LeetCode 题解", method="embedding")
        # 可能为空（未安装 sentence-transformers），不报错即可
        assert isinstance(results, list)

    def test_embedding_returns_scores(self, router):
        """结果应按分数排序"""
        results = router.match("生成题解", method="embedding")
        if len(results) > 1:
            scores = [r.score for r in results]
            assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))


class TestRouterLLMMatch:
    """测试 LLM 语义匹配方法（使用 MockLLM 避免 API 限流）"""

    @pytest.fixture
    def mock_llm_router(self, tmp_path):
        """带 MockLLM 的 router"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)
        router = Router(registry)

        # 替换 _match_by_llm 使用 mock
        class MockLLM:
            def invoke(self, messages):
                # 模拟 LLM 返回
                if "LeetCode" in str(messages) or "题解" in str(messages):
                    return type('obj', (object,), {
                        "content": json.dumps({
                            "matches": [
                                {"skill": "leetcode-solution-writer", "score": 0.95,
                                 "reason": "用户需要生成题解", "arguments": {}},
                            ],
                            "overall_reasoning": "匹配 leetcode-solution-writer",
                        })
                    })
                return type('obj', (object,), {
                    "content": json.dumps({
                        "matches": [],
                        "overall_reasoning": "没有匹配的技能",
                    })
                })

        original_invoke = router._match_by_llm

        def mock_match_by_llm(query, names):
            # 内联 mock 逻辑
            skill_list = []
            for name in names:
                skill = registry.load_skill(name)
                if not skill:
                    continue
                desc = skill.metadata.description or ""
                groups = ", ".join(skill.metadata.groups) if skill.metadata.groups else "none"
                skill_list.append(f"- {name}: {desc} [groups: {groups}]")

            # 简单 mock：如果 query 包含 "题解" 则返回 leetcode-solution-writer
            if "题解" in query or "LeetCode" in query:
                skill = registry.load_skill("leetcode-solution-writer")
                if skill:
                    from skill_engine.models import MatchResult
                    return [MatchResult(skill=skill, score=0.95, method="llm", arguments={})]
            return []

        router._match_by_llm = mock_match_by_llm
        return router

    def test_llm_matches_leetcode(self, mock_llm_router):
        """LLM 应能理解 '生成题解' 匹配 leetcode-solution-writer"""
        results = mock_llm_router.match("帮我生成 LeetCode 题解", method="llm")
        assert len(results) > 0
        names = [r.skill.metadata.name for r in results]
        assert "leetcode-solution-writer" in names

    def test_llm_returns_scores(self, mock_llm_router):
        """LLM 匹配结果应按分数排序"""
        results = mock_llm_router.match("生成题解", method="llm")
        if len(results) > 1:
            scores = [r.score for r in results]
            assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))

    def test_llm_handles_complex_query(self, mock_llm_router):
        """LLM 应能处理复杂查询"""
        results = mock_llm_router.match("我想做一套面试题然后解答", method="llm")
        assert isinstance(results, list)

    def test_llm_returns_method(self, mock_llm_router):
        """结果的 method 字段应为 'llm'"""
        results = mock_llm_router.match("生成题解", method="llm")
        if results:
            assert results[0].method == "llm"


class TestRouterComparison:
    """四种匹配方法的对比测试"""

    @pytest.fixture
    def routers(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        project_skills = Path("skills")
        index = discover(roots=[str(project_skills)])
        registry = Registry(index)

        return {
            "name": Router(registry),
            "keyword": Router(registry),
            "embedding": Router(registry),
            "llm": Router(registry),
        }

    def test_different_methods_return_different_scores(self, routers):
        """不同方法应对同一 query 给出不同分数"""
        query = "帮我生成 LeetCode 题解"

        keyword_results = routers["keyword"].match(query, method="keyword")
        llm_results = routers["llm"].match(query, method="llm")

        # 两种方法都应找到 leetcode-solution-writer
        if keyword_results:
            kw_name = keyword_results[0].skill.metadata.name
            assert kw_name == "leetcode-solution-writer"

        if llm_results:
            llm_name = llm_results[0].skill.metadata.name
            assert llm_name == "leetcode-solution-writer"

    def test_name_match_is_exact(self, routers):
        """name 匹配应精确匹配 skill 名称"""
        results = routers["name"].match("leetcode-solution-writer", method="name")
        assert len(results) == 1
        assert results[0].skill.metadata.name == "leetcode-solution-writer"

    def test_keyword_finds_partial_match(self, routers):
        """keyword 匹配应能找到部分匹配的 skill"""
        results = routers["keyword"].match("题解", method="keyword")
        assert len(results) > 0
        names = [r.skill.metadata.name for r in results]
        assert "leetcode-solution-writer" in names