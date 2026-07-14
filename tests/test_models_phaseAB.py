"""Phase AB: 新增模型测试（MatchPlan / SelectedSkill / MergedMeta）"""
from skill_engine.models import MatchPlan, SelectedSkill, MergedMeta, SkillMetadata


class TestSelectedSkill:
    def test_basic(self):
        ss = SelectedSkill(name="leetcode-generate")
        assert ss.name == "leetcode-generate"
        assert ss.role is None
        assert ss.args_override is None

    def test_with_role(self):
        ss = SelectedSkill(name="leetcode-solve", role="解题")
        assert ss.role == "解题"


class TestMatchPlan:
    def test_single_mode(self):
        plan = MatchPlan(
            mode="single",
            primary=SelectedSkill(name="leetcode-generate"),
            method="exact",
            score=1.0,
        )
        assert plan.mode == "single"
        assert plan.primary is not None
        assert plan.primary.name == "leetcode-generate"
        assert plan.score == 1.0
        assert plan.selections == []

    def test_multi_mode(self):
        plan = MatchPlan(
            mode="multi",
            primary=SelectedSkill(name="leetcode-generate"),
            selections=[
                SelectedSkill(name="leetcode-generate", role="出题"),
                SelectedSkill(name="leetcode-solve", role="解题"),
            ],
            method="llm",
            reason="用户需要出题+解题",
        )
        assert plan.mode == "multi"
        assert len(plan.selections) == 2
        assert plan.reason is not None

    def test_defaults(self):
        plan = MatchPlan(method="keyword")
        assert plan.mode == "single"
        assert plan.primary is None
        assert plan.selections == []
        assert plan.uncertain is False

    def test_uncertain_flag(self):
        plan = MatchPlan(method="keyword", uncertain=True)
        assert plan.uncertain is True


class TestMergedMeta:
    def test_basic(self):
        meta = MergedMeta(name="test-skill", description="描述")
        assert meta.name == "test-skill"
        assert meta.description == "描述"
        assert meta.meta_cache == {}

    def test_with_meta_cache(self):
        meta = MergedMeta(
            name="test-skill",
            description="描述",
            meta_cache={"intention": ["出题"], "keywords": {"动词": ["出"], "名词": ["lc"]}},
        )
        assert meta.meta_cache["intention"] == ["出题"]
        assert meta.meta_cache["keywords"]["名词"] == ["lc"]

    def test_extra_fields_allowed(self):
        """extra = 'allow' 确保 .skill-local.yaml 追加字段不炸"""
        meta = MergedMeta(
            name="test-skill",
            description="描述",
            router_proper_en_append=["mistral", "vllm"],
        )
        assert meta.router_proper_en_append == ["mistral", "vllm"]

    def test_from_skill_metadata_fields(self):
        """MergedMeta 包含 SkillMetadata 所有字段"""
        meta = MergedMeta(
            name="test",
            description="test",
            when_to_use="when user asks",
            groups=["coding", "education"],
            alias=["tc", "test-code"],
            shortcuts=["tc"],
            intent_verbs=["测试", "写测试"],
        )
        assert meta.when_to_use == "when user asks"
        assert meta.groups == ["coding", "education"]
        assert meta.alias == ["tc", "test-code"]
        assert meta.shortcuts == ["tc"]
        assert meta.intent_verbs == ["测试", "写测试"]


class TestSkillMetadataExtension:
    def test_alias_default_none(self):
        """原有 SKILL.md 不含 alias 字段也不报错"""
        meta = SkillMetadata(name="test", description="test")
        assert meta.alias is None

    def test_alias_custom(self):
        meta = SkillMetadata(name="test", description="test", alias=["tc", "test-code"])
        assert meta.alias == ["tc", "test-code"]