"""Scoring — intention 权重评分

纯函数，不依赖 Router 或 Registry。
输入 query 分词结果 + 三层合并后的 MergedMeta，输出匹配分数。

权重规则（V0.3+）：
- verb 命中：命中的动词数 / skill 动词总数 × 0.6，封顶 0.6
- noun 命中：命中的名词数 / skill 名词总数 × 0.35，封顶 0.35
- intention phrase 兜底：intention 整短语在 raw query 中出现 +0.25，封顶 0.3
- 整体封顶 1.0

注意：
- keywords 支持两种格式：flat（旧 SKILL.md frontmatter）和 nested（Preprocessor heal 后）
- intention 是 phrase substr 匹配，不是 token exact 匹配
"""

from typing import Optional

from .models import MergedMeta

VERB_SEEDS = {"生成", "保存", "写", "创建", "指定", "输出", "部署", "打包", "上传",
              "出", "改", "修", "删", "增", "查", "看", "读", "搜索", "找",
              "发", "发布", "下", "下载", "装", "安装", "配", "配置", "启",
              "启动", "停", "停止", "执", "执行", "跑", "运", "运行",
              "generate", "create", "deploy", "build", "run", "write", "solve"}


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
    """计算关键词匹配分数（intention 权重）

    Args:
        qtokens: tokenize_query 的输出 {verbs_zh, nouns_zh, nouns_en}
        meta: 三层合并后的 MergedMeta（含 _meta_cache）

    Returns:
        匹配分数 (0.0 - 1.0)
    """
    s = 0.0
    mc = meta.meta_cache or {}

    # ──────────────────────────────────────────────
    # 1. keywords 归一化（同时认 flat + nested）
    # ──────────────────────────────────────────────
    nk = _normalize_keywords(meta)

    # ──────────────────────────────────────────────
    # 2. intention — phrase substr 匹配
    # ──────────────────────────────────────────────
    intention = (
        [v for v in (meta.intent_verbs or []) if v]
        or mc.get("intention", [])
    )
    raw_query = " ".join(qtokens.get("raw", "")) if isinstance(qtokens.get("raw"), list) else qtokens.get("raw", "")
    phrase_bonus = 0.0
    if raw_query:
        for ph in intention:
            if ph and ph in raw_query:
                if len(ph) >= 2:
                    phrase_bonus = max(phrase_bonus, 0.22)  # 多字短语，强信号
                else:
                    phrase_bonus = max(phrase_bonus, 0.08)  # 单字，弱信号

    # ──────────────────────────────────────────────
    # 3. verb 命中：动词交集 / skill 动词总数 × 0.6
    # ──────────────────────────────────────────────
    verbs_zh = set(v.lower() for v in qtokens.get("verbs_zh", []) if v)
    if nk["verbs"]:
        v_hit = verbs_zh & nk["verbs"]
        v_score = len(v_hit) / len(nk["verbs"]) * 0.6
        s += min(v_score, 0.6)

    # ──────────────────────────────────────────────
    # 4. noun 命中：名词交集 / skill 名词总数 × 0.35
    # ──────────────────────────────────────────────
    nouns_query = set(n.lower() for n in qtokens.get("nouns_zh", []) if n)
    nouns_en = set(n.lower() for n in qtokens.get("nouns_en", []) if n)
    nouns_all = nouns_query | nouns_en
    if nk["nouns"]:
        n_hit = nouns_all & nk["nouns"]
        n_score = len(n_hit) / len(nk["nouns"]) * 0.35
        s += min(n_score, 0.35)

    # ──────────────────────────────────────────────
    # 5. intention phrase 兜底 bonus
    # ──────────────────────────────────────────────
    s += min(phrase_bonus, 0.3)

    return round(min(s, 1.0), 4)