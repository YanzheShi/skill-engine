"""Phase AB: intention 权重评分测试（V0.3+ 新公式）

测试要点：
- verb 命中：hits / total_verbs × 0.6
- noun 命中：hits / total_nouns × 0.35
- intention phrase 兜底 bonus：0.15-0.3
- 兼容 flat + nested keywords 格式
"""

import pytest
from skill_engine.scoring import score_keyword
from skill_engine.models import MergedMeta


def _make_meta(
    intent_verbs=None,
    intention=None,
    synonyms=None,
    kw_verbs=None,
    kw_nouns=None,
) -> MergedMeta:
    """构造测试用的 MergedMeta
    
    kw_verbs/kw_nouns 支持两种格式：
    - flat: ["生成", "保存"]
    - nested: [{"中文": "生成", "英文": "generate"}, ...]
    """
    mc = {}
    if intention:
        mc["intention"] = intention
    if synonyms:
        mc["synonyms"] = synonyms
    kw = {}
    if kw_verbs:
        kw["动词"] = kw_verbs
    if kw_nouns:
        kw["名词"] = kw_nouns
    if kw:
        mc["keywords"] = kw

    return MergedMeta(
        name="test-skill",
        description="测试 skill",
        intent_verbs=intent_verbs,
        meta_cache=mc,
    )


class TestNormalizeKeywords:
    """_normalize_keywords 兼容性测试"""

    def test_flat_format(self):
        """flat 格式：动词在 VERB_SEEDS 中，名词全进 nouns"""
        meta = _make_meta(kw_verbs=["生成", "保存"], kw_nouns=["LeetCode", "文件"])
        from skill_engine.scoring import _normalize_keywords
        nk = _normalize_keywords(meta)
        assert "生成" in nk["verbs"]
        assert "leetcode" in nk["nouns"]

    def test_nested_format(self):
        """nested 格式：{中文, 英文} 都提取，统一小写"""
        meta = _make_meta(
            kw_verbs=[{"中文": "生成", "英文": "generate"}, {"中文": "保存", "英文": "save"}],
            kw_nouns=[{"中文": "LeetCode", "英文": "LeetCode"}, {"中文": "题目", "英文": "problem"}],
        )
        from skill_engine.scoring import _normalize_keywords
        nk = _normalize_keywords(meta)
        assert "生成" in nk["verbs"]
        assert "generate" in nk["verbs"]
        assert "leetcode" in nk["nouns"]
        assert "题目" in nk["nouns"]
        assert "problem" in nk["nouns"]

    def test_empty_keywords(self):
        """空 keywords 返回空集"""
        meta = _make_meta()
        from skill_engine.scoring import _normalize_keywords
        nk = _normalize_keywords(meta)
        assert nk["verbs"] == set()
        assert nk["nouns"] == set()


class TestScoreKeyword:
    """新评分公式测试"""

    def test_verb_hit_ratio(self):
        """动词命中比例：1/6 × 0.6 = 0.1"""
        meta = _make_meta(intention=["出题"], kw_verbs=["生成", "保存", "写入", "创建", "指定", "输出"])
        score = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": [], "nouns_en": [], "raw": "生成"},
            meta,
        )
        assert score == pytest.approx(0.1, abs=0.01), f"实际 {score}"

    def test_verb_all_hit(self):
        """所有动词命中 = 0.6"""
        meta = _make_meta(intention=["出题"], kw_verbs=["生成", "保存"])
        score = score_keyword(
            {"verbs_zh": ["生成", "保存"], "nouns_zh": [], "nouns_en": [], "raw": "生成保存"},
            meta,
        )
        assert score == pytest.approx(0.6, abs=0.01), f"实际 {score}"

    def test_noun_hit_ratio(self):
        """名词命中比例：2/5 × 0.35 = 0.14"""
        meta = _make_meta(intention=["出题"], kw_nouns=["LeetCode", "算法", "题目", "难度", "文件"])
        score = score_keyword(
            {"verbs_zh": [], "nouns_zh": ["题目", "LeetCode"], "nouns_en": [], "raw": "题目"},
            meta,
        )
        assert score == pytest.approx(0.14, abs=0.01), f"实际 {score}"

    def test_phrase_bonus(self):
        """intention 短语在 raw query 中 → +0.15"""
        meta = _make_meta(intention=["生成题目"])
        score = score_keyword(
            {"verbs_zh": [], "nouns_zh": [], "nouns_en": [], "raw": "生成题目，出一个LeetCode"},
            meta,
        )
        assert score == pytest.approx(0.22, abs=0.01), f"实际 {score}"

    def test_exact_phrase_then_verb_noun(self):
        """真实场景：生成LeetCode题目"""
        meta = _make_meta(
            intention=["生成题目"],
            kw_verbs=["生成", "保存", "写入", "创建", "指定", "输出"],
            kw_nouns=["LeetCode", "算法", "题目", "难度", "文件"],
        )
        score = score_keyword(
            {
                "verbs_zh": ["生成"],
                "nouns_zh": ["题目", "难度"],
                "nouns_en": ["leetcode"],
                "raw": "生成题目，出一个LeetCode的题目吧，中等难度的数组题目",
            },
            meta,
        )
        # verb: 1/6 × 0.6 = 0.1
        # noun: 3/5 × 0.35 = 0.21 (题目+LeetCode+难度 命中，数组不在 kw_nouns)
        # phrase: 0.22
        # total ≈ 0.53
        assert score > 0.4, f"期望 > 0.4，实际 {score}"
        assert score == pytest.approx(0.53, abs=0.05), f"实际 {score}"

    def test_no_match_zero(self):
        """完全不匹配 → 0 分"""
        meta = _make_meta(intention=["解题"], kw_nouns=["算法"])
        score = score_keyword(
            {"verbs_zh": ["唱歌"], "nouns_zh": ["音乐"], "nouns_en": [], "raw": "唱歌"},
            meta,
        )
        assert score == 0.0

    def test_capped_at_one(self):
        """封顶 1.0"""
        meta = _make_meta(
            intention=["出题", "解题"],
            kw_verbs=["出", "解", "写"],
            kw_nouns=["lc", "leetcode"],
        )
        score = score_keyword(
            {
                "verbs_zh": ["出", "解", "写"],
                "nouns_zh": [],
                "nouns_en": ["lc", "leetcode"],
                "raw": "出题解题写lc leetcode",
            },
            meta,
        )
        # verb: 3/3 × 0.6 = 0.6
        # noun: 2/2 × 0.35 = 0.35
        # phrase: 0.22 (出题 in raw)
        # total: 0.6 + 0.35 + 0.22 = 1.17 → cap 1.0
        assert score <= 1.0
        assert score == pytest.approx(1.0, abs=0.01), f"实际 {score}"

    def test_empty_meta_cache(self):
        """_meta_cache 为空时不上分"""
        meta = _make_meta()
        score = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": [], "nouns_en": [], "raw": "生成"},
            meta,
        )
        assert score == 0.0


class TestDisambiguation:
    """出题 vs 解题 歧义测试"""

    def test_chu_ti_beats_jie_ti(self):
        """用户说'出题'→ 出题 skill 分高"""
        generate_meta = _make_meta(intention=["出题"], kw_verbs=["生成"], kw_nouns=["leetcode", "数组"])
        solve_meta = _make_meta(intention=["解题"], kw_verbs=["解"], kw_nouns=["leetcode", "数组"])

        score_gen = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": ["数组"], "nouns_en": [], "raw": "出题数组"},
            generate_meta,
        )
        score_sol = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": ["数组"], "nouns_en": [], "raw": "出题数组"},
            solve_meta,
        )

        assert score_gen > score_sol, f"出题 skill({score_gen}) 应 > 解题 skill({score_sol})"

    def test_jie_ti_beats_chu_ti(self):
        """用户说'解题'→ 解题 skill 分高"""
        generate_meta = _make_meta(intention=["出题"], kw_verbs=["生成"], kw_nouns=["leetcode", "二叉树"])
        solve_meta = _make_meta(intention=["解题"], kw_verbs=["解"], kw_nouns=["leetcode", "二叉树"])

        score_gen = score_keyword(
            {"verbs_zh": ["解"], "nouns_zh": ["二叉树"], "nouns_en": [], "raw": "解题二叉树"},
            generate_meta,
        )
        score_sol = score_keyword(
            {"verbs_zh": ["解"], "nouns_zh": ["二叉树"], "nouns_en": [], "raw": "解题二叉树"},
            solve_meta,
        )

        assert score_sol > score_gen, f"解题 skill({score_sol}) 应 > 出题 skill({score_gen})"

    def test_nested_keywords_format(self):
        """nested 格式的 keywords 也能正确评分"""
        generate_meta = _make_meta(
            intention=["生成题目"],
            kw_verbs=[{"中文": "生成", "英文": "generate"}, {"中文": "创建", "英文": "create"}],
            kw_nouns=[{"中文": "LeetCode", "英文": "LeetCode"}, {"中文": "题目", "英文": "problem"}],
        )
        score = score_keyword(
            {
                "verbs_zh": ["生成"],
                "nouns_zh": ["题目"],
                "nouns_en": ["leetcode"],
                "raw": "生成题目，一个LeetCode题目",
            },
            generate_meta,
        )
        # verb: 1/2 × 0.6 = 0.3
        # noun: 2/2 × 0.35 = 0.35
        # phrase: 0.15
        # total: 0.8
        assert score > 0.5, f"实际 {score}"