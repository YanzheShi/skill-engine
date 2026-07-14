"""Scoring — intention 权重评分

纯函数，不依赖 Router 或 Registry。
输入 query 分词结果 + 三层合并后的 MergedMeta，输出匹配分数。

权重规则：
- intention 命中：每个 query 动词取最高路径，max 不累加 → 0.6
- synonym 命中：最高路径 max → 0.55
- keywords.动词命中：最高路径 max → 0.25
- 名词命中：累加，封顶 0.3
- 整体封顶 1.0
"""

from typing import Optional

from .models import MergedMeta


def score_keyword(qtokens: dict, meta: MergedMeta) -> float:
    """计算关键词匹配分数（intention 权重）

    Args:
        qtokens: tokenize_query 的输出 {verbs_zh, nouns_zh, nouns_en}
        meta: 三层合并后的 MergedMeta（含 _meta_cache）

    Returns:
        匹配分数 (0.0 - 1.0)
    """
    s = 0.0

    # 读取预处理缓存
    mc = meta.meta_cache or {}

    # intention：优先取 intent_verbs（SKILL.md 手写），其次取 .skill-meta.yaml 抽取
    intention = (
        [v for v in (meta.intent_verbs or []) if v]
        or mc.get("intention", [])
    )

    # synonyms：优先取 .skill-local.yaml 覆写，其次取 .skill-meta.yaml
    synonyms = mc.get("synonyms", {})

    # keywords.动词 / 名词
    kw_verbs = mc.get("keywords", {}).get("动词", [])
    kw_nouns = mc.get("keywords", {}).get("名词", [])

    # --- 动词段：每个 query 动词取最高路径，max 不累加 ---
    for v in qtokens.get("verbs_zh", []):
        if not v:
            continue  # 跳过空字符串
        # 跳过单 ASCII 字符（如分词器输出的单字母碎片），保留中文单字动词
        if len(v) == 1 and v.isascii():
            continue

        if v in intention:
            s = max(s, 0.6)
        else:
            # 查 synonyms
            found_synonym = False
            for iv in intention:
                syn_list = synonyms.get(iv, [])
                if v in syn_list:
                    s = max(s, 0.55)
                    found_synonym = True
                    break
            if not found_synonym and v in kw_verbs:
                s = max(s, 0.25)

    # --- 名词段：累加封顶 0.3 ---
    noun_hits = 0
    for n in qtokens.get("nouns_zh", []):
        if n and n in kw_nouns:
            noun_hits += 1
    for n in qtokens.get("nouns_en", []):
        if n and n in kw_nouns:
            noun_hits += 1
    s += min(noun_hits * 0.1, 0.3)

    return round(min(s, 1.0), 4)