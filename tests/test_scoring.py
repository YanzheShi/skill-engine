"""Phase AB: intention 权重评分测试（V0.3+ scoring v2）

测试要点：
- verb 命中：len(v_hit) / len(vq) × 0.5（query 侧分母，压单路）
- noun 命中：len(n_hit) / len(nq) × 0.25（query 侧分母，压单路）
- link bonus: 动名双中时 0.15 + 0.05 × pairs，封顶 0.25
- phrase bonus: 0.08 + 0.08 × len(ph)，单字不触发，多字累加
- phrase_soft_in: 逐字顺序匹配，不要求连续
- 兼容 flat + nested keywords 格式
"""

import pytest
from skill_engine.routing.scoring import score_keyword, phrase_soft_in, _phrase_bonus, _normalize_keywords
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


class TestPhraseSoftIn:
    """phrase_soft_in 逐字顺序匹配测试"""

    def test_continuous_match(self):
        assert phrase_soft_in("写诗", "写诗")

    def test_filler_inserted(self):
        assert phrase_soft_in("写诗", "写一首诗")

    def test_multiple_fillers(self):
        assert phrase_soft_in("出题", "出一个leetcode的题目")

    def test_no_match(self):
        assert not phrase_soft_in("写诗", "唱首歌")

    def test_wrong_order(self):
        assert not phrase_soft_in("写诗", "诗写完了")

    def test_partial_match(self):
        assert not phrase_soft_in("写古诗", "写一首诗")

    def test_intention_with_filler(self):
        """intention 本身带 filler（手写脏 intention），防御性清理"""
        assert phrase_soft_in("写一首诗", "写一首诗")
        assert phrase_soft_in("写一首诗", "写一首美丽的诗")


class TestPhraseBonus:
    """_phrase_bonus 计算测试"""

    def test_single_char_skipped(self):
        """单字 intention 不触发"""
        bonus = _phrase_bonus(["写"], "我想写一首诗")
        assert bonus == 0.0

    def test_double_char_phrase(self):
        """双字 intention"""
        bonus = _phrase_bonus(["写诗"], "我想写一首诗")
        assert bonus == pytest.approx(0.24, abs=0.01), f"实际 {bonus}"

    def test_four_char_phrase(self):
        """四字 intention"""
        bonus = _phrase_bonus(["生成题目"], "生成题目，出一个LeetCode")
        assert bonus == pytest.approx(0.40, abs=0.01), f"实际 {bonus}"

    def test_multiple_phrases_accumulate(self):
        """多短语命中累加，封顶 0.6"""
        bonus = _phrase_bonus(["写诗", "保存文件"], "写一首诗并保存到文件")
        # 0.24 + 0.40 = 0.64 → cap 0.6
        assert bonus == pytest.approx(0.6, abs=0.01), f"实际 {bonus}"

    def test_no_match(self):
        """不匹配"""
        bonus = _phrase_bonus(["写诗"], "唱首歌")
        assert bonus == 0.0

    def test_capped_at_06(self):
        """总封顶 0.6"""
        bonus = _phrase_bonus(["写诗", "生成题目", "保存文件", "创建模板"], "写诗并生成题目，保存文件到模板")
        assert bonus <= 0.6, f"实际 {bonus}"


class TestNormalizeKeywords:
    """_normalize_keywords 兼容性测试"""

    def test_flat_format(self):
        meta = _make_meta(kw_verbs=["生成", "保存"], kw_nouns=["LeetCode", "文件"])
        nk = _normalize_keywords(meta)
        assert "生成" in nk["verbs"]
        assert "leetcode" in nk["nouns"]

    def test_nested_format(self):
        meta = _make_meta(
            kw_verbs=[{"中文": "生成", "英文": "generate"}, {"中文": "保存", "英文": "save"}],
            kw_nouns=[{"中文": "LeetCode", "英文": "LeetCode"}, {"中文": "题目", "英文": "problem"}],
        )
        nk = _normalize_keywords(meta)
        assert "生成" in nk["verbs"]
        assert "generate" in nk["verbs"]
        assert "leetcode" in nk["nouns"]
        assert "题目" in nk["nouns"]
        assert "problem" in nk["nouns"]

    def test_empty_keywords(self):
        meta = _make_meta()
        nk = _normalize_keywords(meta)
        assert nk["verbs"] == set()
        assert nk["nouns"] == set()


class TestScoreKeyword:
    """新评分公式测试（scoring v2）"""

    def test_verb_only(self):
        """只有 verb 命中，无 noun 无 phrase"""
        meta = _make_meta(intention=["出题"], kw_verbs=["生成", "保存", "写入"])
        score = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": [], "nouns_en": [], "raw": "生成"},
            meta,
        )
        # vq={"生成"}, v_hit={"生成"} → 1/1 × 0.5 = 0.5
        # phrase: "出题" len=2 in raw? "出题" not in "生成" → 0
        # link: no v_hit... wait, v_hit IS {"生成"}. let me check.
        # Actually intention=["出题"], "出题" not in raw="生成", so phrase=0
        # n_hit empty → link=0
        # total: 0.5
        assert score == pytest.approx(0.5, abs=0.01), f"实际 {score}"

    def test_verb_two_of_three(self):
        """3 个 query verb 中 2 个"""
        meta = _make_meta(intention=["出题"], kw_verbs=["生成", "保存", "写入"])
        score = score_keyword(
            {"verbs_zh": ["生成", "保存", "唱歌"], "nouns_zh": [], "nouns_en": [], "raw": "生成保存"},
            meta,
        )
        # vq={"生成","保存","唱歌"}, v_hit={"生成","保存"} → 2/3 × 0.5 = 0.333
        assert score == pytest.approx(0.333, abs=0.01), f"实际 {score}"

    def test_verb_plus_noun_with_link(self):
        """verb + noun 命中 + link bonus"""
        meta = _make_meta(
            intention=["出题"],
            kw_verbs=["生成", "保存"],
            kw_nouns=["题目", "LeetCode"],
        )
        score = score_keyword(
            {"verbs_zh": ["生成"], "nouns_zh": ["题目"], "nouns_en": [], "raw": "生成题目"},
            meta,
        )
        # vq={"生成"}, v_hit={"生成"} → 1/1 × 0.5 = 0.5
        # nq={"题目"}, n_hit={"题目"} → 1/1 × 0.25 = 0.25
        # phrase: "出题" len=2 in raw? "生成题目" → does "出题" match via phrase_soft_in?
        #   ph="出题", seeds=["出","题"], raw="生成题目"
        #   '生'→no, '成'→no, '题'→no, '目'→no. i=0. False → 0
        # link: v_hit && n_hit → 0.15 + 0.05×min(1+1,2) = 0.25
        # total: 0.5 + 0.25 + 0.25 = 1.0
        assert score == pytest.approx(1.0, abs=0.01), f"实际 {score}"

    def test_noun_only_no_link(self):
        """只有 noun 命中，无 link bonus"""
        meta = _make_meta(intention=["出题"], kw_nouns=["题目", "LeetCode"])
        score = score_keyword(
            {"verbs_zh": [], "nouns_zh": ["题目"], "nouns_en": [], "raw": "题目"},
            meta,
        )
        # nq={"题目"}, n_hit={"题目"} → 1/1 × 0.25 = 0.25
        # v_hit empty → link=0
        # phrase: "出题" in "题目"? → False → 0
        assert score == pytest.approx(0.25, abs=0.01), f"实际 {score}"

    def test_phrase_only_no_link(self):
        """只有 intention phrase 匹配，无 verb/noun，无 link"""
        meta = _make_meta(intention=["生成题目"])
        score = score_keyword(
            {"verbs_zh": [], "nouns_zh": [], "nouns_en": [], "raw": "生成题目，出一个LeetCode"},
            meta,
        )
        # phrase: "生成题目" len=4 → 0.40
        # v_hit empty → link=0
        assert score == pytest.approx(0.40, abs=0.01), f"实际 {score}"

    def test_no_match_zero(self):
        """完全不匹配"""
        meta = _make_meta(intention=["解题"], kw_verbs=["解"], kw_nouns=["算法"])
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
        # phrase: "出题" len=2→0.24, "解题" len=2→0.24, sum=0.48
        # verb: 3/3 × 0.5 = 0.5
        # noun: 2/2 × 0.25 = 0.25
        # link: v_hit && n_hit → 0.15 + 0.05×min(3+2,2) = 0.25
        # total: 0.48 + 0.5 + 0.25 + 0.25 = 1.48 → cap 1.0
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
        # phrase: "生成题目" len=4 → 0.40
        # verb: 1/1 × 0.5 = 0.5
        # noun: 2/2 × 0.25 = 0.25
        # link: 0.15 + 0.05×min(1+2,2) = 0.25
        # total: 0.40 + 0.5 + 0.25 + 0.25 = 1.40 → cap 1.0
        assert score > 0.5, f"实际 {score}"