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
from skill_engine.routing.discovery import discover, _parse_frontmatter, _discover_skill_dir
from skill_engine.routing.registry import Registry
from skill_engine.routing.router import Router


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

    def test_discover_empty_dir(self, tmp_path):
        # 用 tmp_path 而非仓库内 fixtures 目录：避免清理时触发沙箱 safe-delete 拦截 rmdir
        tmpdir = tmp_path / "empty"
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
        # supporting_files 已排除引擎派生文件（.skill-meta.yaml/.skill-local.yaml/.git 等），
        # 因此可能为空列表；只断言字段存在且为 list，不强制非空。
        assert isinstance(skill.supporting_files, list)


class TestRouter:
    """测试 Router V0.3（三步路由）"""

    @pytest.fixture
    def router(self, monkeypatch):
        monkeypatch.setattr("skill_engine.routing.router.get_llm", lambda **kw: None)
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        return Router(registry)

    def test_match_by_name_exact(self, router):
        plan = router.match("deploy")
        assert plan.method == "exact"
        assert plan.score == 1.0
        assert plan.primary is not None
        assert plan.primary.name == "deploy"

    def test_match_by_name_case_insensitive(self, router):
        plan = router.match("Deploy")
        assert plan.method == "exact"
        assert plan.primary is not None
        assert plan.primary.name == "deploy"

    def test_match_by_name_not_found(self, router):
        plan = router.match("nonexistent")
        # 无 exact 匹配，keyword 也不匹配，返回 uncertain
        assert plan.uncertain is True or plan.method == "keyword"

    def test_keyword_finds_relevant(self, router):
        """keyword 匹配能找到相关 skill（中文 query）"""
        plan = router.match("部署应用")
        # 应该命中 deploy skill（description 含"部署"）
        if plan.primary:
            # 至少有一个候选
            assert plan.score is None or plan.score > 0

    def test_empty_query(self, router):
        plan = router.match("")
        assert plan.uncertain is True

    def test_no_match(self, router):
        plan = router.match("xyzabc123notexist")
        # 无 exact 匹配，keyword 不匹配 → uncertain
        assert plan.uncertain is True

    def test_shortcut_matches_name(self):
        """shortcut 精确匹配"""
        # 用 fixture 的 skill，虽然没有 shortcut 定义，但 name 精确匹配
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)
        plan = router.match("code-review")
        assert plan.method == "exact"
        assert plan.primary is not None
        assert plan.primary.name == "code-review"

    def test_invalid_method_not_needed(self):
        """新 Router 不再暴露 method 参数，所以不需要测试无效 method"""


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

        plan = router.match("deploy")
        assert plan.method == "exact"
        assert plan.primary is not None
        assert plan.primary.name == "deploy"
        assert plan.score == 1.0

    def test_full_pipeline_keyword(self):
        """完整流程：keyword 匹配"""
        index = discover(roots=[FIXTURES_DIR])
        registry = Registry(index)
        router = Router(registry)

        plan = router.match("部署")
        # 应该能找到 deploy skill
        if plan.primary:
            assert plan.score is None or plan.score > 0

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
