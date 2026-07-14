"""Tokenizer — 中英文查询分词

职责：
- tokenize_query: 结巴分词 + 英文碎片提取，返回 {verbs_zh, nouns_zh, nouns_en}
- extract_proper_en: 从 query 中提取领域专有名词（整 token 匹配，非子串）
- is_english: 判断 query 是否为纯英文

依赖：
- jieba（可选）：未安装时退化到按字符切
"""

import re
from typing import Optional

# 领域专有名词集（硬编码兜底，Preprocessor 可自动膨胀）
# 用户可在 .skill-local.yaml 的 router_proper_en_append 追加
PROPER_EN: set[str] = {
    "lc", "leetcode", "dfs", "bfs", "bst",
    "binary", "tree", "array", "string", "graph",
    "dp", "dynamic", "programming", "recursion",
    "backtracking", "greedy", "sort", "search",
    "linked", "list", "stack", "queue", "heap",
    "hash", "map", "set", "trie", "segment",
    "fenwick", "bitmask", "sliding", "window",
    "two", "pointers", "prefix", "suffix",
    "topological", "dijkstra", "floyd", "kruskal",
    "prim", "kmp", "rabin", "karp", "mst",
    "lca", "lcs", "lis", "lps",
}


def tokenize_query(query: str, proper_en: Optional[set[str]] = None) -> dict:
    """对用户 query 进行分词，返回结构化的 token 字典

    Args:
        query: 用户输入（中/英/中英混）
        proper_en: 领域专有名词集，None 则用 PROPER_EN

    Returns:
        {"verbs_zh": [...], "nouns_zh": [...], "nouns_en": [...]}
    """
    verbs_zh: list[str] = []
    nouns_zh: list[str] = []
    nouns_en: list[str] = []

    # 1. 提取英文专有名词（整 token 匹配，非子串）
    nouns_en = extract_proper_en(query, proper_en or PROPER_EN)

    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(query):
            if flag.startswith("v"):
                verbs_zh.append(word)
            elif flag.startswith("n"):
                # 排除纯英文 token（已被 nouns_en 覆盖）
                if not re.match(r"^[a-zA-Z0-9#+]+$", word):
                    nouns_zh.append(word)
    except ImportError:
        # 无 jieba：退化到按字符切中文
        for char in query:
            if "\u4e00" <= char <= "\u9fff":
                verbs_zh.append(char)  # 无词性标注，全当动词
            elif char.isalpha() and char.lower() not in nouns_en:
                pass  # 英文已在 nouns_en 中

    return {
        "verbs_zh": verbs_zh,
        "nouns_zh": nouns_zh,
        "nouns_en": nouns_en,
    }


def extract_proper_en(query: str, proper_set: set[str]) -> list[str]:
    """从 query 中提取领域专有名词（整 token 匹配）

    按空格+标点拆分英文 token，每个 token 去 proper_set 查。
    避免子串误伤（如 "lc" 不匹配 "leetcode"）。

    Args:
        query: 用户输入
        proper_set: 领域专有名词集合

    Returns:
        命中的专有名词列表（小写去重，保留顺序）
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9#+]*", query)
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in proper_set and tl not in seen:
            result.append(tl)
            seen.add(tl)
    return result


def is_english(query: str) -> bool:
    """判断 query 是否为纯英文（或绝大部分英文）

    Heuristic：英文字母占比 > 80% 视为纯英文。

    Args:
        query: 用户输入

    Returns:
        True 如果纯英文
    """
    if not query or not query.strip():
        return False
    cleaned = query.strip()
    alpha_count = len(re.findall(r"[a-zA-Z]", cleaned))
    return alpha_count / len(cleaned) > 0.7