"""Phase AB: intention 权重评分测试"""
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
    """构造测试用的 MergedMeta"""
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


class TestScoreKeyword:
    """intention 权重评分"""

    def test_intention_hit_max(self):
        """intention 命中多个动词时取 max 不累加"""
        meta = _make_meta(intention=["出题", "解题"], kw_verbs=["出", "解"])
        # 两个动词都命中 intention，只取 max(0.6, 0.6) = 0.6
        score = score_keyword({"verbs_zh": ["出题", "解题"], "nouns_zh": [], "nouns_en": []}, meta)
        assert score == 0.6, f"期望 0.6，实际 {score}"

    def test_intention_beats_synonym(self):
        """intention 命中 > synonym 命中"""
        meta = _make_meta(intention=["出题"], synonyms={"出题": ["生成", "写"]})
        score_intention = score_keyword({"verbs_zh": ["出题"], "nouns_zh": [], "nouns_en": []}, meta)
        score_synonym = score_keyword({"verbs_zh": ["生成"], "nouns_zh": [], "nouns_en": []}, meta)
        assert score_intention > score_synonym
        assert score_intention == 0.6
        assert score_synonym == 0.55

    def test_verb_keyword_lowest(self):
        """keywords.动词 命中 < synonym 命中 < intention 命中"""
        meta = _make_meta(
            intention=["出题"],
            synonyms={"出题": ["生成"]},
            kw_verbs=["写"],
        )
        score_kw = score_keyword({"verbs_zh": ["写"], "nouns_zh": [], "nouns_en": []}, meta)
        assert score_kw == 0.25

    def test_noun_accumulate_capped(self):
        """名词累加但封顶 0.3"""
        meta = _make_meta(intention=["解题"], kw_nouns=["leetcode", "dfs", "bfs", "tree"])
        # 4 个名词命中 → 0.4 → min(0.4, 0.3) = 0.3
        score = score_keyword(
            {"verbs_zh": [], "nouns_zh": ["leetcode", "dfs", "bfs", "tree"], "nouns_en": []},
            meta,
        )
        assert score == 0.3, f"期望 0.3，实际 {score}"

    def test_verb_plus_noun_combined(self):
        """动词 + 名词组合"""
        meta = _make_meta(intention=["出题"], kw_nouns=["leetcode", "数组"])
        score = score_keyword(
            {"verbs_zh": ["出题"], "nouns_zh": ["leetcode", "数组"], "nouns_en": []},
            meta,
        )
        # intention 0.6 + noun 0.2 = 0.8
        assert score == 0.8, f"期望 0.8，实际 {score}"

    def test_no_match_zero(self):
        """完全不匹配 → 0 分"""
        meta = _make_meta(intention=["解题"], kw_nouns=["算法"])
        score = score_keyword(
            {"verbs_zh": ["唱歌"], "nouns_zh": ["音乐"], "nouns_en": []},
            meta,
        )
        assert score == 0.0

    def test_capped_at_one(self):
        """封顶 1.0"""
        meta = _make_meta(intention=["出题", "解题", "写"], kw_nouns=["lc", "leetcode", "dfs", "bfs"])
        # 动词 max 0.6 + noun 0.3 = 0.9
        score = score_keyword(
            {
                "verbs_zh": ["出题", "解题", "写"],
                "nouns_zh": [],
                "nouns_en": ["lc", "leetcode", "dfs", "bfs"],
            },
            meta,
        )
        assert score <= 1.0

    def test_single_char_verb_skipped(self):
        """单字动词被跳过（结巴可能输出单字）"""
        meta = _make_meta(intention=["出题"])
        score = score_keyword(
            {"verbs_zh": ["出", "题"], "nouns_zh": [], "nouns_en": []},
            meta,
        )
        assert score == 0.0  # "出" 和 "题" 都是单字，被跳过

    def test_empty_meta_cache(self):
        """_meta_cache 为空时不上分"""
        meta = _make_meta()
        score = score_keyword(
            {"verbs_zh": ["出题"], "nouns_zh": [], "nouns_en": []},
            meta,
        )
        assert score == 0.0


class TestDisambiguation:
    """出题 vs 解题 歧义测试（核心用例）"""

    def test_chu_ti_beats_jie_ti(self):
        """用户说'出题'→ 出题 skill 分高"""
        generate_meta = _make_meta(intention=["出题"], kw_nouns=["leetcode", "数组"])
        solve_meta = _make_meta(intention=["解题"], kw_nouns=["leetcode", "数组"])

        score_gen = score_keyword(
            {"verbs_zh": ["出题"], "nouns_zh": ["数组"], "nouns_en": []},
            generate_meta,
        )
        score_sol = score_keyword(
            {"verbs_zh": ["出题"], "nouns_zh": ["数组"], "nouns_en": []},
            solve_meta,
        )

        assert score_gen > score_sol, f"出题 skill({score_gen}) 应 > 解题 skill({score_sol})"

    def test_jie_ti_beats_chu_ti(self):
        """用户说'解题'→ 解题 skill 分高"""
        generate_meta = _make_meta(intention=["出题"], kw_nouns=["leetcode", "二叉树"])
        solve_meta = _make_meta(intention=["解题"], kw_nouns=["leetcode", "二叉树"])

        score_gen = score_keyword(
            {"verbs_zh": ["解题"], "nouns_zh": ["二叉树"], "nouns_en": []},
            generate_meta,
        )
        score_sol = score_keyword(
            {"verbs_zh": ["解题"], "nouns_zh": ["二叉树"], "nouns_en": []},
            solve_meta,
        )

        assert score_sol > score_gen, f"解题 skill({score_sol}) 应 > 出题 skill({score_gen})"