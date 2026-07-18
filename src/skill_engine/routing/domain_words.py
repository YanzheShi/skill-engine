"""Domain words — 自动注册领域词到 jieba

职责：
  扫 registry 中 active skill 的 MergedMeta.meta_cache.keywords，
  把名词/动词/英文名锁进 jieba，防止词性标注错误。

原理：
  jieba 默认词典不认识"题解""二叉树""DFS"等领域词，
  导致分词时词性标错（如"题解"被标为动词 v 而非名词 n）。
  注册后 jieba 正确识别，自然落入 nouns_zh/verbs_zh，与 keywords 正确匹配。

使用方式：
  >>> from skill_engine.routing.domain_words import register_domain_words
  >>> register_domain_words(registry)
"""

import logging

from skill_engine.routing.registry import Registry

logger = logging.getLogger("skill_engine.domain_words")


def register_domain_words(registry: Registry) -> int:
    """从 registry 中所有 active skill 的 keywords 注册领域词到 jieba

    Args:
        registry: Registry 实例（已 heal 并加载 meta）

    Returns:
        注册的词数
    """
    try:
        import jieba
    except ImportError:
        logger.warning("jieba 未安装，跳过领域词注册")
        return 0

    added = 0
    for name in registry.list_active():
        meta = registry.load_meta(name)  # → MergedMeta
        if not meta:
            continue

        mc = meta.meta_cache or {}
        kw = mc.get("keywords", {})

        # nested 格式: {动词: [{中文,英文}], 名词: [{中文,英文}]}
        for kind in ("名词", "动词"):
            for item in (kw.get(kind, []) or []):
                if isinstance(item, dict):
                    # 中文词
                    w = item.get("中文", "")
                    if w and len(w) >= 2:
                        tag = "n" if kind == "名词" else "v"
                        jieba.add_word(w, freq=2000, tag=tag)
                        added += 1
                    # 英文词（如 LeetCode）
                    eng = item.get("英文", "")
                    if eng and len(eng) >= 2:
                        jieba.add_word(eng, freq=2000, tag="eng")
                        added += 1
                        # 小写副本（用户可能输入 leetcode 而非 LeetCode）
                        if eng[0].isupper() and eng.lower() != eng:
                            jieba.add_word(eng.lower(), freq=1800, tag="eng")
                            added += 1
                elif isinstance(item, str):
                    # flat 老结构兜底
                    if len(item) >= 2:
                        tag = "n" if kind == "名词" else "v"
                        jieba.add_word(item, freq=2000, tag=tag)
                        added += 1

        # skill name 中的中文实义词（kebab-case 摘）
        cn_part = name.replace("-", " ").replace("_", " ")
        for word in cn_part.split():
            if len(word) >= 2 and any("\u4e00" <= c <= "\u9fff" for c in word):
                jieba.add_word(word, freq=1000, tag="n")
                added += 1

    if added:
        logger.info(f"register_domain_words: 注册 {added} 个领域词")
    return added