"""Scoring — intention 权重评分（V0.3+ scoring v2）

纯函数，不依赖 Router 或 Registry。
输入 query 分词结果 + 三层合并后的 MergedMeta，输出匹配分数。

权重规则（scoring v2）：
- verb 命中：len(v_hit) / max(len(vq), 1) × 0.5       ← query 侧分母，压单路
- noun 命中：len(n_hit) / max(len(nq), 1) × 0.25      ← query 侧分母，压单路
- phrase bonus: 0.08 + 0.08 × len(ph)，单条封 0.48，总封 0.6，单字不给
- link bonus: 动名双中时 0.15 + 0.05 × pairs，封顶 0.25
- 整体封顶 1.0

注意：
- keywords 支持两种格式：flat（旧 SKILL.md frontmatter）和 nested（Preprocessor heal 后）
- intention 是 phrase_soft_in 匹配（逐字顺序，不要求连续），单字不触发
"""

from typing import Optional

from .models import MergedMeta

VERB_SEEDS = {"生成", "保存", "写", "创建", "指定", "输出", "部署", "打包", "上传",
              "出", "改", "修", "删", "增", "查", "看", "读", "搜索", "找",
              "发", "发布", "下", "下载", "装", "安装", "配", "配置", "启",
              "启动", "停", "停止", "执", "执行", "跑", "运", "运行",
              "generate", "create", "deploy", "build", "run", "write", "solve",
              "分析", "评估", "选择", "返回", "计算", "提取", "填充", "记录",
              "获取", "检查", "编排", "调用", "写诗", "作诗", "赋诗"}

# FILLERS: 防御性清 intention 短语侧（防手写脏 intention）
# 只用于 phrase_soft_in 的 ph 侧清理，不碰 raw 侧
FILLERS = {"一", "一", "道", "一首", "一个", "个", "的", "来", "去", "呀", "嘛"}


def phrase_soft_in(ph: str, raw: str) -> bool:
    """逐字顺序匹配 ph 是否在 raw 中出现（不要求连续）

    Args:
        ph: intention 短语，如 "写诗"、"出题"
        raw: 用户原始 query

    Returns:
        True 如果 ph 的每个字按顺序在 raw 中出现（可跳过中间字符）
    """
    seeds = [c for c in ph if c not in FILLERS]
    if not seeds:
        return False
    i = 0
    for c in raw:
        if i < len(seeds) and c == seeds[i]:
            i += 1
    return i == len(seeds)


def _phrase_bonus(intention: list[str], raw: str) -> float:
    """计算 intention phrase bonus

    单字不触发（已在 verb 侧消费），多字累加，总封 0.6。

    Args:
        intention: intention 字符串列表
        raw: 用户原始 query

    Returns:
        phrase bonus (0.0 - 0.6)
    """
    if not raw or not intention:
        return 0.0

    bonus = 0.0
    for ph in intention:
        if not ph or len(ph) < 2:  # 单字不给，已在 verb 侧消费
            continue
        if phrase_soft_in(ph, raw):
            b = min(0.08 + 0.08 * len(ph), 0.48)
            bonus += b
    return min(bonus, 0.6)


def _normalize_keywords(meta: MergedMeta) -> dict:
    """归一化 keywords，兼容 flat 和 nested 两种格式

    Returns:
        {"verbs": set[str], "nouns": set[str]}
    """
    mc = meta.meta_cache or {}
    raw_kw = mc.get("keywords", {})

    if not raw_kw:
        return {"verbs": set(), "nouns": set()}

    verbs: set[str] = set()
    nouns: set[str] = set()

    # flat（旧 SKILL.md frontmatter）: 不分动词/名词的无结构列表
    if isinstance(raw_kw, list):
        for w in raw_kw:
            if isinstance(w, str):
                wl = w.lower()
                if wl in VERB_SEEDS:
                    verbs.add(wl)
                else:
                    nouns.add(wl)
        return {"verbs": verbs, "nouns": nouns}

    # nested（Preprocessor heal 后）: {动词: [{中文, 英文}], 名词: [{中文, 英文}]}
    for v in (raw_kw.get("动词", []) or []):
        if isinstance(v, dict):
            if v.get("中文"):
                verbs.add(v["中文"].lower())
            if v.get("英文"):
                verbs.add(v["英文"].lower())
        elif isinstance(v, str):
            verbs.add(v.lower())

    for n in (raw_kw.get("名词", []) or []):
        if isinstance(n, dict):
            if n.get("中文"):
                nouns.add(n["中文"].lower())
            if n.get("英文"):
                nouns.add(n["英文"].lower())
        elif isinstance(n, str):
            nouns.add(n.lower())

    return {"verbs": verbs, "nouns": nouns}


def score_keyword(qtokens: dict, meta: MergedMeta) -> float:
    """计算关键词匹配分数（scoring v2）

    Args:
        qtokens: tokenize_query 的输出 {verbs_zh, nouns_zh, nouns_en, raw}
        meta: 三层合并后的 MergedMeta（含 _meta_cache）

    Returns:
        匹配分数 (0.0 - 1.0)
    """
    mc = meta.meta_cache or {}

    # ──────────────────────────────────────────────
    # 1. keywords 归一化（同时认 flat + nested）
    # ──────────────────────────────────────────────
    nk = _normalize_keywords(meta)

    # ──────────────────────────────────────────────
    # 2. query 分词集
    # ──────────────────────────────────────────────
    verbs_zh = set(v.lower() for v in qtokens.get("verbs_zh", []) if v)
    nouns_query = set(n.lower() for n in qtokens.get("nouns_zh", []) if n)
    nouns_en = set(n.lower() for n in qtokens.get("nouns_en", []) if n)
    nouns_all = nouns_query | nouns_en

    vq_len = max(len(verbs_zh), 1)
    nq_len = max(len(nouns_all), 1)

    # ──────────────────────────────────────────────
    # 3. verb 命中：query 侧分母 × 0.5
    # ──────────────────────────────────────────────
    v_hit: set[str] = set()
    if nk["verbs"] and verbs_zh:
        v_hit = verbs_zh & nk["verbs"]
    v_score = len(v_hit) / vq_len * 0.5

    # ──────────────────────────────────────────────
    # 4. noun 命中：query 侧分母 × 0.25
    # ──────────────────────────────────────────────
    n_hit: set[str] = set()
    if nk["nouns"] and nouns_all:
        n_hit = nouns_all & nk["nouns"]
    n_score = len(n_hit) / nq_len * 0.25

    # ──────────────────────────────────────────────
    # 5. intention — phrase_soft_in 匹配（单字不给）
    # ──────────────────────────────────────────────
    intention = (
        [v for v in (meta.intent_verbs or []) if v]
        or mc.get("intention", [])
    )
    raw_query = qtokens.get("raw", "")
    if isinstance(raw_query, list):
        raw_query = " ".join(raw_query)
    phrase = _phrase_bonus(intention, raw_query)

    # ──────────────────────────────────────────────
    # 6. link bonus：动名双中才加，单中不加
    # ──────────────────────────────────────────────
    link_bonus = 0.0
    if v_hit and n_hit:
        pairs = min(len(v_hit) + len(n_hit), 2)
        link_bonus = 0.15 + 0.05 * pairs  # 1对→0.20, 2对+→0.25

    total = v_score + n_score + phrase + link_bonus
    return round(min(total, 1.0), 4)