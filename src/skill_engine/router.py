"""
Router V0.2 — 加入 embedding 语义匹配

支持三种匹配策略：
1. by_name: 精确匹配 skill 名称
2. by_keyword: 关键词匹配 description + when_to_use
3. by_embedding: V0.2 引入，语义匹配（可选依赖 sentence-transformers）

使用方式：
>>> index = discover()
>>> registry = Registry(index)
>>> router = Router(registry)
>>> results = router.match("帮我部署", method="keyword")
>>> results_emb = router.match("帮我部署", method="embedding")
"""

from pathlib import Path
from typing import Optional
import re
from .models import Skill, MatchResult, SkillMetadata
from .registry import Registry


class Router:
    """Skill 匹配器 / 路由器

    使用方式：
    >>> router = Router(registry)
    >>> results = router.match("帮我部署", method="keyword")
    """

    def __init__(
        self,
        registry: Registry,
        embedding_model: Optional[str] = None,
        embedding_dim: int = 768,
    ):
        self.registry = registry
        self.embedding_model = embedding_model
        self.embedding_dim = embedding_dim
        self._embedding_cache: dict[str, list[float]] = {}
        self._has_embedding = False

    def match(
        self,
        query: str,
        method: str = "keyword",
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[MatchResult]:
        """匹配 skills 到用户输入

        Args:
            query: 用户输入
            method: 匹配方法 (name/keyword/embedding/llm)
            top_k: 返回前 K 个结果
            min_score: 最低分数阈值

        Returns:
            匹配结果列表，按分数降序排列
        """
        names = self.registry.list_active()

        if method == "name":
            return self._match_by_name(query, names)
        elif method == "keyword":
            return self._match_by_keyword(query, names)
        elif method == "embedding":
            return self._match_by_embedding(query, names)
        elif method == "llm":
            return self._match_by_llm(query, names)
        else:
            raise ValueError(f"Unknown method: {method}")

    def _match_by_name(self, query: str, names: list[str]) -> list[MatchResult]:
        """精确匹配 skill 名称（大小写不敏感）"""
        query_lower = query.lower().strip()
        results = []

        for name in names:
            if name.lower() == query_lower:
                skill = self.registry.load_skill(name)
                if skill:
                    results.append(MatchResult(
                        skill=skill,
                        score=1.0,
                        method="name",
                        arguments={"$ARGUMENTS": query},
                    ))
                    break  # 精确匹配只有一个结果

        return results

    def _match_by_keyword(self, query: str, names: list[str]) -> list[MatchResult]:
        """关键词匹配

        评分规则：
        - description 中包含关键词：+0.5/个
        - when_to_use 中包含关键词：+0.3/个
        - 完全匹配（整个 query 在 description 中）：+0.8

        Args:
            query: 用户输入
            names: 活跃 skill 名称列表

        Returns:
            匹配结果列表
        """
        # 空查询直接返回
        if not query or not query.strip():
            return []

        results = []

        # 提取关键词（中英文）
        keywords = self._extract_keywords(query)

        # 没有有效关键词直接返回
        if not keywords:
            return []

        for name in names:
            skill = self.registry.load_skill(name)
            if not skill:
                continue

            score = self._calculate_score(keywords, query, skill)
            if score > 0:
                args = self._parse_arguments(query, skill)
                results.append(MatchResult(
                    skill=skill,
                    score=score,
                    method="keyword",
                    arguments=args,
                ))

        return results

    def _match_by_embedding(self, query: str, names: list[str]) -> list[MatchResult]:
        """语义匹配（V0.2 引入，可选依赖）

        使用 sentence-transformers 计算 query 与 skill 描述的余弦相似度。
        如果未安装 sentence-transformers，返回空列表。
        """
        # 空查询直接返回
        if not query or not query.strip():
            return []

        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
        except ImportError:
            return []

        # 初始化 embedding 模型（懒加载）
        if not self._has_embedding:
            try:
                self._model = SentenceTransformer(self.embedding_model or "all-MiniLM-L6-v2")
                self._has_embedding = True
            except Exception:
                return []

        # 获取 query embedding（缓存）
        query_lower = query.lower()
        if query_lower in self._embedding_cache:
            query_vec = self._embedding_cache[query_lower]
        else:
            query_vec = self._model.encode(query).tolist()
            self._embedding_cache[query_lower] = query_vec

        results = []
        query_np = np.array(query_vec)

        for name in names:
            skill = self.registry.load_skill(name)
            if not skill:
                continue

            # 获取 skill embedding（缓存）
            cache_key = f"{skill.metadata.name}:{skill.metadata.description}"
            if cache_key in self._embedding_cache:
                skill_vec = self._embedding_cache[cache_key]
            else:
                text = f"{skill.metadata.description} {skill.metadata.when_to_use}"
                skill_vec = self._model.encode(text).tolist()
                self._embedding_cache[cache_key] = skill_vec

            # 计算余弦相似度
            skill_np = np.array(skill_vec)
            score = float(np.dot(query_np, skill_np) / (
                np.linalg.norm(query_np) * np.linalg.norm(skill_np) + 1e-8
            ))

            if score > 0:
                args = self._parse_arguments(query, skill)
                results.append(MatchResult(
                    skill=skill,
                    score=round(score, 4),
                    method="embedding",
                    arguments=args,
                ))

        # 按分数降序
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:5]

    def _calculate_score(self, keywords: list[str], query: str, skill: Skill) -> float:
        """计算关键词匹配分数

        Args:
            keywords: 提取的关键词列表
            query: 原始查询
            skill: Skill 对象

        Returns:
            匹配分数 (0.0 - 1.0)
        """
        score = 0.0

        # 获取描述文本
        desc = skill.metadata.description or ""
        desc_lower = desc.lower()
        when_to_use = skill.metadata.when_to_use or ""
        when_lower = when_to_use.lower()

        # 完全匹配（整个 query 在 description 中）
        cleaned = re.sub(r"[^\w\s]", " ", query).strip()
        if cleaned and cleaned.lower() in desc_lower:
            score += 0.8

        # 关键词匹配
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in desc_lower:
                score += 0.5
            if kw_lower in when_lower:
                score += 0.3

        # 分数上限 1.0（单个 skill 的 keyword 分数不超过 name 精确匹配）
        score = min(score, 1.0)

        return round(score, 4)

    def _extract_keywords(self, query: str) -> list[str]:
        """提取中英文关键词

        策略：
        - 英文：按空格分割，过滤停用词
        - 中文：按字符分割（简单实现，后续可用 jieba）

        Args:
            query: 用户输入

        Returns:
            关键词列表
        """
        if not query or not query.strip():
            return []

        # 简单停用词
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "be",
                      "been", "being", "have", "has", "had", "do", "does",
                      "did", "will", "would", "could", "should", "may",
                      "might", "must", "shall", "can", "need", "dare",
                      "ought", "used", "to", "of", "in", "for", "on",
                      "with", "at", "by", "from", "as", "into", "through",
                      "during", "before", "after", "and", "but", "or",
                      "nor", "not", "so", "yet", "both", "either",
                      "neither", "each", "every", "all", "any", "few",
                      "more", "most", "other", "some", "such", "no",
                      "only", "same", "than", "too", "very", "just",
                      "也", "和", "与", "及", "等", "这个", "那个", "什么",
                      "怎么", "如何", "哪里", "何时", "为什么", "因为",
                      "所以", "但是", "然而", "如果", "虽然", "尽管"}

        keywords = []

        # 英文关键词
        en_parts = query.split()
        for part in en_parts:
            if part.lower() not in stop_words and len(part) > 1:
                keywords.append(part)

        # 中文关键词（按字符分割，过滤单字停用词）
        for char in query:
            if '\u4e00' <= char <= '\u9fff' and char not in stop_words:
                keywords.append(char)

        return keywords

    def _parse_arguments(self, query: str, skill: Skill) -> dict:
        """解析用户输入中的参数

        策略：
        - 将 query 按空格分割
        - 第一个词作为 $0
        - 剩余词作为 $1, $2, ...
        - 所有词作为 $ARGUMENTS

        Args:
            query: 用户输入
            skill: Skill 对象

        Returns:
            参数字典
        """
        parts = query.strip().split()
        args = {"$ARGUMENTS": query}

        for i, part in enumerate(parts):
            args[f"${i}"] = part

        return args

    def _match_by_llm(self, query: str, names: list[str]) -> list[MatchResult]:
        """LLM 语义匹配 — 让 LLM 判断用户意图与 skill 的匹配度

        流程：
        1. 收集所有 skill 的 name + description + when_to_use
        2. 构造 prompt 让 LLM 打分
        3. 解析 JSON 返回结果

        Args:
            query: 用户输入
            names: 活跃 skill 名称列表

        Returns:
            匹配结果列表，按分数降序
        """
        print(f"[DEBUG router] _match_by_llm called, query='{query}', names={names}")
        try:
            from skill_engine.config import get_llm
        except ImportError:
            print(f"[DEBUG router] ImportError: config not available")
            return []

        # 构建 skill 列表文本
        skill_list = []
        for name in names:
            skill = self.registry.load_skill(name)
            if not skill:
                continue
            desc = skill.metadata.description or ""
            when = skill.metadata.when_to_use or ""
            groups = ", ".join(skill.metadata.groups) if skill.metadata.groups else "none"
            skill_list.append(f"- {name}: {desc} [groups: {groups}]")

        skills_text = "\n".join(skill_list)

        prompt = f"""你是一个技能匹配助手。你需要根据用户的输入，从以下可用技能中选择最相关的 N 个，并给出匹配分数。

## 可用技能列表

{skills_text}

## 用户输入

{query}

## 任务

1. 理解用户的真实意图
2. 从可用技能中选择最相关的技能（最多 5 个）
3. 为每个选中的技能打分（0.0-1.0，越相关分数越高）
4. 解释选择理由

请以 JSON 格式返回：
{{
  "matches": [
    {{
      "skill": "skill名称",
      "score": 0.9,
      "reason": "为什么匹配",
      "arguments": {{}}
    }}
  ],
  "overall_reasoning": "整体匹配理由"
}}

如果没有匹配的技能，返回：
{{
  "matches": [],
  "overall_reasoning": "没有匹配的技能"
}}"""

        llm = get_llm()
        print(f"[DEBUG router] LLM instance created, invoking...")
        resp = llm.invoke([{"role": "user", "content": prompt}])
        content = resp.content if hasattr(resp, "content") else str(resp)
        print(f"[DEBUG router] LLM response (first 300 chars): {content[:300]}")

        # 提取 JSON（可能在 markdown code block 中）
        import json
        import re
        json_match = re.search(r'\{[\s\S]*"matches"\s*:[\s\S]*\}', content)
        if not json_match:
            return []

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return []

        results = []
        for match in data.get("matches", []):
            skill_name = match.get("skill", "")
            score = match.get("score", 0.0)
            if score <= 0:
                continue

            skill = self.registry.load_skill(skill_name)
            if not skill:
                continue

            results.append(MatchResult(
                skill=skill,
                score=round(float(score), 4),
                method="llm",
                arguments=match.get("arguments", {}),
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:5]
