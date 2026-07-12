"""
Phase 1 测试套件

测试 discovery、registry、router 三个模块的正确性。
"""

import pytest
from pathlib import Path
from skill_engine.models import (
    SkillMeta, SkillMetadata, Skill, MatchResult,
    SkillContext, SkillOverride,
)
from skill_engine.discovery import discover, _parse_frontmatter, _discover_skill_dir
from skill_engine.registry import Registry
from skill_engine.router import Router


# 测试 fixture 目录
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sample-skills"


class TestParseFrontmatter:
    """测试 frontmatter 解析"""

    def test_valid_frontmatter(self):
        content = "---\nname: deploy\ndescription: 部署应用\n---\n\n部署步骤..."
        fm_dict, body = _parse_frontmatter(content)
        assert fm_dict["name"] == "deploy"
        assert fm_dict["description"] == "部署应用"
        assert "部署步骤" in body

    def test_no_frontmatter(self):
        content = "纯文本内容，没有 frontmatter"
        fm_dict, body = _parse_frontmatter(content)
        assert fm_dict == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\n\n正文内容"
        fm_dict, body = _parse_frontmatter(content)
        assert fm_dict == {}
        assert "正文内容" in body

    def test_frontmatter_with_yaml_errors(self):
        content = "---\ninvalid: yaml: content:\n---\n\n正文"
        fm_dict, body = _parse_frontmatter(content)
        assert fm_dict == {}
        assert "正文" in body


class TestDiscoverSkillDir:
    """测试单目录 skill 发现"""

    def test_discover_single_skill(self):
        # deploy 目录本身包含 SKILL.md，但 _discover_skill_dir 扫描其子目录
        # 所以 deploy 目录作为子目录，其 SKILL.md 会被发现
        index = _discover_skill_dir(FIXTURES_DIR, priority=10)
        assert "deploy" in index
        assert index["deploy"].directory.endswith("deploy")

    def test_discover_multiple_skills(self):
        index = _discover_skill_dir(FIXTURES_DIR, priority=10)
        assert len(index) == 3
        assert "deploy" in index
        assert "code-review" in index
        assert "leetcode-solution-writer" in index

    def test_discover_nonexistent_dir(self):
        index = _discover_skill_dir(Path("/nonexistent/path"), priority=10)
        assert index == {}

    def test_discover_empty_dir(self):
        tmpdir = Path(__file__).parent / "fixtures" / "empty"
        tmpdir.mkdir(exist_ok=True)
        index = _discover_skill_dir(tmpdir, priority=10)
        assert index == {}
        tmpdir.rmdir()


class TestDiscover:
    """测试多根 discovery"""

    def test_discover_default_roots(self):
        """测试默认扫描（用户级 + 项目级）"""
        index = discover()
        # 默认应该能扫描到项目级 skills
        assert isinstance(index, dict)

    def test_discover_custom_root(self):
        """测试自定义根目录"""
        index = discover(roots=[FIXTURES_DIR])
        assert "deploy" in index
        assert "code-review" in index
        assert "leetcode-solution-writer" in index
        # 自定义根 priority=30，高于默认
        assert index["deploy"].priority == 30

    def test_priority_override(self):
        """测试高 priority 覆盖低 priority"""
        # 先扫描用户级（priority=10）
        index = discover()
        deploy_meta = index.get("deploy")
        if deploy_meta:
            # 再扫描自定义根（priority=30）
            index2 = discover(roots=[FIXTURES_DIR])
            assert index2["deploy"].priority == 30

    def test_override_state(self):
        """测试 state 覆盖"""
        index = discover(roots=[FIXTURES_DIR], overrides={"deploy": "off"})
        assert index["deploy"].state == "off"

    def test_override_add_new(self):
        """测试 overrides 添加不存在的 skill"""
        index = discover(overrides={"nonexistent": "on"})
        assert "nonexistent" not in index


class TestRegistry:
    """测试 Registry"""

    @pytest.fixture
    def registry(self):
        index = discover(roots=[FIXTURES_DIR])
        return Registry(index)

    def test_list_names(self, registry):
        names = registry.list_names()
        assert "deploy" in names
        assert "code-review" in names
        assert "leetcode-solution-writer" in names

    def test_list_active(self, registry):
        active = registry.list_active()
        assert "deploy" in active
        assert "code-review" in active

    def test_list_active_filters_off(self, registry):
        # 手动标记 deploy 为 off
        registry.index["deploy"].state = "off"
        active = registry.list_active()
        assert "deploy" not in active

    def test_info_returns_meta(self, registry):
        meta = registry.info("deploy")
        assert meta is not None
        assert meta.name == "deploy"
        assert isinstance(meta, SkillMeta)

    def test_info_nonexistent(self, registry):
        meta = registry.info("nonexistent")
        assert meta is None

    def test_load_skill_returns_full_object(self, registry):
        skill = registry.load_skill("deploy")
        assert skill is not None
        assert isinstance(skill, Skill)
        assert skill.metadata.name == "deploy"
        assert len(skill.body) > 0
        assert skill.directory.endswith("deploy")

    def test_load_skill_nonexistent(self, registry):
        skill = registry.load_skill("nonexistent")
        assert skill is None

    def test_load_skill_off(self, registry):
        registry.index["deploy"].state = "off"
        skill = registry.load_skill("deploy")
        assert skill is None

    def test_cache_stores_loaded_skill(self, registry):
        skill1 = registry.load_skill("deploy")
        skill2 = registry.load_skill("deploy")
        assert skill1 is skill2  # 同一个对象（缓存命中）

    def test_invalidate_clears_cache(self, registry):
        registry.load_skill("deploy")
        assert "deploy" in registry._cache
        registry.invalidate("deploy")
        assert "deploy" not in registry._cache

    def test_clear_cache_clears_all(self, registry):
        registry.load_skill("deploy")
        registry.load_skill("code-review")
        assert len(registry._cache) == 2
        registry.clear_cache()
        assert len(registry._cache) == 0

    def test_load_skill_has_supporting_files(self, registry):
        skill = registry.load_skill("leetcode-solution-writer")
        assert skill is not None
        assert len(skill.supporting_files) > 0


class TestRouter:
    """测试 Router"""

    @pytest.fixture
    def router(self):
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        return Router(registry)

    def test_match_by_name_exact(self, router):
        results = router.match("deploy", method="name")
        assert len(results) == 1
        assert results[0].skill.metadata.name == "deploy"
        assert results[0].score == 1.0
        assert results[0].method == "name"

    def test_match_by_name_case_insensitive(self, router):
        results = router.match("Deploy", method="name")
        assert len(results) == 1
        assert results[0].skill.metadata.name == "deploy"

    def test_match_by_name_not_found(self, router):
        results = router.match("nonexistent", method="name")
        assert len(results) == 0

    def test_match_by_keyword_finds_relevant(self, router):
        results = router.match("部署应用", method="keyword")
        assert len(results) > 0
        deploy_result = [r for r in results if r.skill.metadata.name == "deploy"]
        assert len(deploy_result) == 1
        assert deploy_result[0].score > 0

    def test_match_by_keyword_scores_ordered(self, router):
        results = router.match("部署", method="keyword")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_match_by_keyword_min_score_filter(self, router):
        results = router.match("部署", method="keyword", min_score=0.9)
        for r in results:
            assert r.score >= 0.9

    def test_match_by_keyword_top_k(self, router):
        results = router.match("代码", method="keyword", top_k=1)
        assert len(results) <= 1

    def test_match_by_keyword_returns_arguments(self, router):
        results = router.match("部署 staging v1", method="keyword")
        if results:
            assert "$ARGUMENTS" in results[0].arguments
            assert "$0" in results[0].arguments

    def test_match_by_keyword_empty_query(self, router):
        results = router.match("", method="keyword")
        assert len(results) == 0

    def test_match_by_keyword_no_match(self, router):
        results = router.match("xyzabc123notexist", method="keyword")
        assert len(results) == 0

    def test_match_by_name_higher_score(self, router):
        """精确匹配应该比关键词匹配得分更高"""
        name_results = router.match("deploy", method="name")
        keyword_results = router.match("deploy", method="keyword")

        name_score = name_results[0].score if name_results else 0
        keyword_score = max((r.score for r in keyword_results), default=0)

        assert name_score >= keyword_score

    def test_match_invalid_method(self, router):
        with pytest.raises(ValueError, match="Unknown method"):
            router.match("test", method="invalid")

    def test_match_by_embedding_returns_empty(self, router):
        """embedding 匹配 MVP 阶段返回空"""
        results = router.match("test", method="embedding")
        assert len(results) == 0


class TestModels:
    """测试数据模型"""

    def test_skill_metadata_defaults(self):
        meta = SkillMetadata(name="test", description="测试")
        assert meta.when_to_use == ""
        assert meta.arguments == []
        assert meta.disable_model_invocation is False
        assert meta.user_invocable is True
        assert meta.allowed_tools == []
        assert meta.context.value == "inline"
        assert meta.effort == "inherit"
        assert meta.model == "inherit"

    def test_skill_metadata_custom(self):
        meta = SkillMetadata(
            name="test",
            description="测试",
            when_to_use="用户提到部署时",
            arguments=["environment", "version"],
            disable_model_invocation=True,
            allowed_tools=["Bash(git *)"],
        )
        assert meta.when_to_use == "用户提到部署时"
        assert meta.arguments == ["environment", "version"]
        assert meta.disable_model_invocation is True
        assert meta.allowed_tools == ["Bash(git *)"]

    def test_skill_full(self):
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试内容",
            directory="/tmp/test",
        )
        assert skill.metadata.name == "test"
        assert skill.body == "测试内容"
        assert skill.directory == "/tmp/test"
        assert skill.supporting_files == []

    def test_match_result_full(self):
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="测试内容",
            directory="/tmp/test",
        )
        result = MatchResult(
            skill=skill,
            score=0.8,
            method="keyword",
            arguments={"$ARGUMENTS": "test query"},
        )
        assert result.score == 0.8
        assert result.method == "keyword"
        assert result.arguments["$ARGUMENTS"] == "test query"

    def test_skill_context_enum(self):
        assert SkillContext.INLINE.value == "inline"
        assert SkillContext.FORK.value == "fork"

    def test_skill_override(self):
        override = SkillOverride(skill_name="deploy", state="off")
        assert override.skill_name == "deploy"
        assert override.state == "off"


class TestIntegration:
    """端到端集成测试"""

    def test_full_pipeline(self):
        """完整流程：discover → registry → router → match"""
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)

        # 匹配"部署"
        results = router.match("部署", method="keyword")
        assert len(results) > 0

        # 检查 deploy skill 被匹配
        deploy_results = [r for r in results if r.skill.metadata.name == "deploy"]
        assert len(deploy_results) == 1
        assert deploy_results[0].score > 0

    def test_full_pipeline_by_name(self):
        """完整流程：按名称匹配"""
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)

        results = router.match("deploy", method="name")
        assert len(results) == 1
        assert results[0].skill.metadata.name == "deploy"

    def test_load_and_check_skill(self):
        """加载 skill 并检查 body 内容"""
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)

        skill = registry.load_skill("deploy")
        assert skill is not None
        assert "部署" in skill.body
        assert "git diff" in skill.body

    def test_multiple_skills_loaded(self):
        """加载多个 skill（仅从 fixture，跳过默认路径）"""
        index = discover(roots=[FIXTURES_DIR], skip_defaults=True)
        registry = Registry(index)

        skills = [registry.load_skill(name) for name in registry.list_names()]
        assert all(s is not None for s in skills)
        assert len([s for s in skills if s is not None]) == 3
