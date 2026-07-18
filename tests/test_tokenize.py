"""Phase AB: 中英文分词和专有名词提取测试"""
import pytest
from skill_engine.routing.tokenize import (
    tokenize_query,
    extract_proper_en,
    is_english,
    PROPER_EN,
)


class TestExtractProperEn:
    """专有名词提取（整 token 匹配）"""

    def test_single_token(self):
        assert extract_proper_en("leetcode", PROPER_EN) == ["leetcode"]

    def test_no_substring_mistake(self):
        """确保 'lc' 不因 'leetcode' 中包含 'lc' 而误匹配"""
        assert extract_proper_en("leetcode", {"lc", "leetcode"}) == ["leetcode"]

    def test_multiple_tokens(self):
        result = extract_proper_en("solve lc 104 with dfs", PROPER_EN)
        assert "lc" in result
        assert "dfs" in result

    def test_case_insensitive(self):
        result = extract_proper_en("LeetCode DFS", PROPER_EN)
        assert "leetcode" in result
        assert "dfs" in result

    def test_empty_query(self):
        assert extract_proper_en("", PROPER_EN) == []

    def test_no_match(self):
        assert extract_proper_en("hello world python", PROPER_EN) == []

    def test_duplicate_dedup(self):
        result = extract_proper_en("dfs dfs bfs", PROPER_EN)
        assert result == ["dfs", "bfs"]


class TestIsEnglish:
    """英文判断"""

    def test_pure_english(self):
        assert is_english("solve leetcode problem 104") is True

    def test_english_with_numbers(self):
        assert is_english("please solve lc 104 easy problem") is True

    def test_chinese(self):
        """纯中文返回 False"""
        assert is_english("出个 leetcode 题") is False

    def test_mixed_majority_chinese(self):
        """中文为主的混合"""
        assert is_english("帮我写个题解 谢谢 lc") is False

    def test_empty(self):
        assert is_english("") is False


class TestTokenizeQuery:
    """分词（含 fallback：无 jieba 时按字符切）"""

    def test_chinese_verbs_nouns(self):
        """结巴能分出动词和名词"""
        result = tokenize_query("出个 leetcode 题")
        # 至少能分出动词和名词（结巴结果）
        assert "verbs_zh" in result
        assert "nouns_zh" in result
        # leetcode 应被识别为专有名词
        assert "leetcode" in result.get("nouns_en", [])

    def test_extract_proper_en_integration(self):
        """专有名词通过 extract_proper_en 提取"""
        result = tokenize_query("用 dfs 解 lc 104")
        assert "dfs" in result.get("nouns_en", [])
        assert "lc" in result.get("nouns_en", [])

    def test_empty_query(self):
        result = tokenize_query("")
        assert result == {"verbs_zh": [], "nouns_zh": [], "nouns_en": [], "raw": ""}

    def test_pure_english_query(self):
        """纯英文 query 的专有名词提取"""
        result = tokenize_query("solve leetcode problem 104 with dfs")
        assert "leetcode" in result.get("nouns_en", [])
        assert "dfs" in result.get("nouns_en", [])