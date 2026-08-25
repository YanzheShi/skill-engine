"""Phase AB: Preprocessor 单元测试

需要 mock LLM，因为 Preprocessor 会调 LLM 抽取 intention/synonyms。
"""
import json
import pytest
from pathlib import Path
from skill_engine.creator.preprocessor import Preprocessor, extract_json, PROMPT_EXTRACT
from skill_engine.models import Skill, SkillMetadata


class MockLLM:
    """模拟 LLM 返回结构化 JSON"""

    def __init__(self, response: str = None):
        self.response = response or json.dumps({
            "intention": ["解题", "写题解"],
            "synonyms": {"解题": ["做题", "solve"]},
            "purpose": "生成 LeetCode 题解",
            "keywords": {"动词": ["解题", "分析"], "名词": ["leetcode", "算法"]},
        })

    def invoke(self, prompt):
        class Response:
            def __init__(self, content):
                self.content = content
        return Response(self.response)


class TestExtractJson:
    """JSON 提取（3 层容错）"""

    def test_direct_json(self):
        result = extract_json('{"intention": ["解题"]}')
        assert result == {"intention": ["解题"]}

    def test_codeblock_json(self):
        result = extract_json('```json\n{"intention": ["解题"]}\n```')
        assert result == {"intention": ["解题"]}

    def test_greedy_json(self):
        result = extract_json('一些文字\n{"intention": ["解题"]}\n更多文字')
        assert result == {"intention": ["解题"]}

    def test_invalid_json(self):
        assert extract_json("不是 JSON") is None

    def test_empty_string(self):
        assert extract_json("") is None


class TestPreprocessorEnsureMeta:
    """Preprocessor.ensure_meta 增量逻辑"""

    @pytest.fixture
    def tmp_skill_dir(self, tmp_path):
        """创建临时 skill 目录"""
        skill_dir = tmp_path / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        sk_path = skill_dir / "SKILL.md"
        sk_path.write_text(
            "---\nname: test-skill\ndescription: 测试 skill\n---\n\n# Test\n\n解题用的 skill",
            encoding="utf-8",
        )
        return skill_dir

    @pytest.fixture
    def skill(self, tmp_skill_dir):
        return Skill(
            metadata=SkillMetadata(name="test-skill", description="测试 skill"),
            body="# Test\n\n解题用的 skill",
            directory=str(tmp_skill_dir),
        )

    def test_first_run_creates_meta(self, skill, tmp_path):
        """首次调用 ensure_meta 应创建 meta 缓存（隔离到 tmp cache，不在 skill 源树）"""
        llm = MockLLM()
        pp = Preprocessor(llm=llm, cache_dir=tmp_path)

        meta = pp.ensure_meta(skill)

        assert meta["intention"] == ["解题", "写题解"]
        assert meta["purpose"] == "生成 LeetCode 题解"
        assert meta["meta_version"] == 2
        assert meta["provider"] == "llm"
        assert "source_hash" in meta
        assert "computed_at" in meta

        # 文件应写入隔离 cache 目录（内容寻址），不在 skill 源树
        from skill_engine.creator.preprocessor import meta_cache_path
        meta_path = meta_cache_path(skill, tmp_path)
        assert not (Path(skill.directory) / ".skill-meta.yaml").exists()
        assert meta_path.exists()

    def test_cache_hit_skips_llm(self, skill, tmp_path):
        """第二次调用（hash 一致）应跳过 LLM"""
        call_count = [0]

        class CountingMockLLM:
            def invoke(self, prompt):
                call_count[0] += 1
                return MockLLM().invoke(prompt)

        llm = CountingMockLLM()
        pp = Preprocessor(llm=llm, cache_dir=tmp_path)

        # 第一次：调 LLM
        meta1 = pp.ensure_meta(skill)
        assert call_count[0] == 1

        # 第二次：命中缓存，不调 LLM
        meta2 = pp.ensure_meta(skill)
        assert call_count[0] == 1  # 没增加
        assert meta2 == meta1

    def test_hash_change_triggers_re_extract(self, skill, tmp_path):
        """SKILL.md 变更后应重新抽取"""
        pp = Preprocessor(llm=MockLLM(), cache_dir=tmp_path)

        # 第一次
        pp.ensure_meta(skill)

        # 修改 SKILL.md 并更新 skill.body
        sk_path = Path(skill.directory) / "SKILL.md"
        new_body = "# 修改后\n\n出题用的 skill"
        sk_path.write_text(
            "---\nname: test-skill\ndescription: 测试 skill\n---\n\n" + new_body,
            encoding="utf-8",
        )
        skill.body = new_body  # 同步更新 body

        # 第二次：hash 变了，应重新抽取
        llm2 = MockLLM(response=json.dumps({
            "intention": ["出题"],
            "synonyms": {"出题": ["生成"]},
            "purpose": "生成题目",
            "keywords": {"动词": ["出题"], "名词": ["leetcode"]},
        }))
        pp2 = Preprocessor(llm=llm2, cache_dir=tmp_path)
        meta2 = pp2.ensure_meta(skill)
        assert meta2["intention"] == ["出题"]

    def test_batch_ensure(self, tmp_skill_dir, skill, tmp_path):
        """batch_ensure 批量处理"""
        pp = Preprocessor(llm=MockLLM(), cache_dir=tmp_path)
        results = pp.batch_ensure([skill])
        assert "test-skill" in results
        assert results["test-skill"]["intention"] == ["解题", "写题解"]


class TestPreprocessorEdgeCases:
    """边界情况"""

    def test_llm_returns_invalid_json(self, tmp_path):
        """LLM 返回非 JSON 时应抛出 ValueError"""
        llm = MockLLM(response="这不是 JSON")
        pp = Preprocessor(llm=llm, cache_dir=tmp_path)
        skill = Skill(
            metadata=SkillMetadata(name="test", description="test"),
            body="test",
            directory=str(tmp_path / "nonexistent"),
        )
        with pytest.raises(ValueError):
            pp.ensure_meta(skill)

    def test_llm_returns_partial_json(self, tmp_path):
        """LLM 返回不完整 JSON 时补默认字段"""
        llm = MockLLM(response=json.dumps({"intention": ["解题"]}))
        pp = Preprocessor(llm=llm, cache_dir=tmp_path)
        skill = Skill(
            metadata=SkillMetadata(name="test", description="test"),
            body="test",
            directory=str(tmp_path / "nonexistent"),
        )
        meta = pp.ensure_meta(skill)
        assert meta["intention"] == ["解题"]
        assert meta["synonyms"] == {}
        assert meta["keywords"] == {"动词": [], "名词": []}

    def test_hash_skill(self):
        """_hash_skill 稳定性"""
        skill = Skill(
            metadata=SkillMetadata(name="test", description="test"),
            body="hello world",
            directory="/tmp/nonexistent",
        )
        h1 = Preprocessor._hash_skill(skill)
        h2 = Preprocessor._hash_skill(skill)
        assert h1 == h2
        assert len(h1) == 16  # SHA256 前 16 字符

    def test_prompt_format(self):
        """PROMPT_EXTRACT 可格式化"""
        prompt = PROMPT_EXTRACT.format(skill_markdown="# Test\n\n解题用的 skill")
        assert "intention" in prompt
        assert "解题用的 skill" in prompt