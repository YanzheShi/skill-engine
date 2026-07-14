"""Router V0.3 — 三步路由管线

匹配策略：
1️⃣ name / alias / shortcut 精确（不挑语言）
1.5️⃣ 纯英文 exact 没中 → 直接跳 LLM
2️⃣ 结巴分词 + intention 权重打分 → 多候选?
3️⃣ LLM 兜底（0 命中 / 多匹配）→ MatchPlan(single | multi)

使用方式：
>>> index = discover()
>>> registry = Registry(index)
>>> router = Router(registry, preprocessor=preprocessor)
>>> plan = router.match("出个 lc 题", llm=llm_client)
>>> print(plan.mode)  # "single" / "multi"
"""

import json
import re
from typing import Optional
from .models import MatchPlan, SelectedSkill, MergedMeta
from .registry import Registry
from .scoring import score_keyword
from .tokenize import tokenize_query, is_english, PROPER_EN

THRESH_SINGLE = 0.7


class Router:
    """Skill 匹配器 V0.3（三步路由）

    Args:
        registry: Registry 实例
        preprocessor: 可选，Preprocessor 实例（用于 lazy ensure_meta）
    """

    def __init__(
        self,
        registry: Registry,
        preprocessor: Optional[object] = None,
    ):
        self.registry = registry
        self.preprocessor = preprocessor
        self._alias_index: dict[str, str] = {}  # "lc-sol" -> "leetcode-solve"
        self._shortcut_index: dict[str, str] = {}
        self._build_indices()

    def _build_indices(self):
            """把 alias / shortcut 摊平成一阶 map"""
            for name in self.registry.list_active():
                meta = self.registry.load_meta(name)
                if not meta:
                    continue
                for a in (meta.alias or []):
                    self._alias_index[a.lower()] = name
                for s in (meta.shortcuts or []):
                    self._shortcut_index[s.lower()] = name

    def _load_meta(self, name: str) -> Optional[MergedMeta]:
        """加载并缓存 MergedMeta，支持 Preprocessor lazy 补 meta"""
        meta = self.registry.load_meta(name)
        if meta is None:
            return None
        # 如果 meta_cache 为空且 Preprocessor 可用，lazy 补
        if not meta.meta_cache:
            pp = self._get_preprocessor()
            if pp is not None:
                skill = self.registry.load_skill(name)
                if skill:
                    try:
                        pp.ensure_meta(skill)
                        meta = self.registry.load_meta(name)  # 重新加载
                    except Exception:
                        pass  # 预处理失败不影响流程
        return meta

    def _get_preprocessor(self):
            """懒加载 Preprocessor（仅首次需要时创建）"""
            if self.preprocessor is not None:
                return self.preprocessor
            try:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError
                from .config import get_llm
                from .preprocessor import Preprocessor

                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(get_llm)
                    llm = future.result(timeout=5)  # 5 秒超时
                self.preprocessor = Preprocessor(llm=llm)
                return self.preprocessor
            except TimeoutError:
                return None  # 超时
            except Exception:
                return None

    def match(
        self,
        query: str,
        *,
        llm=None,
        top_k: int = 3,
    ) -> MatchPlan:
        """三步路由

        Args:
            query: 用户输入
            llm: LLM 客户端（兜底用）
            top_k: keyword 阶段保留的候选数

        Returns:
            MatchPlan
        """
        # ──────────────────────────────────────────────
        # 1️⃣ 精确：name / alias / shortcut
        # ──────────────────────────────────────────────
        exact = self._match_exact(query)
        if exact:
            return MatchPlan(
                mode="single",
                primary=SelectedSkill(name=exact),
                method="exact",
                score=1.0,
            )

        # 1.5️⃣ 纯英文 → 直接跳 LLM（keyword 对英文不准）
        if is_english(query):
            if llm:
                plan = self._llm_fallback(query, self.registry.list_active()[:top_k], llm, kws=[])
                if plan:
                    return plan
            return MatchPlan(
                mode="single",
                selections=[],
                method="keyword",
                reason="纯英文 query 无 exact 命中",
                uncertain=True,
            )

        # ──────────────────────────────────────────────
        # 2️⃣ 关键词：结巴 + intention 权重
        # ──────────────────────────────────────────────
        qtokens = tokenize_query(query)
        kws: list[tuple[float, str]] = []

        for name in self.registry.list_active():
            meta = self._load_meta(name)
            if not meta:
                continue
            s = score_keyword(qtokens, meta)
            if s > 0:
                kws.append((s, name))

        kws.sort(key=lambda x: x[0], reverse=True)

        # ──────────────────────────────────────────────
        # 3️⃣ 决定要不要 LLM（三触发）
        # ──────────────────────────────────────────────
        should_llm, llm_candidates, reason = self._should_llm(query, kws, qtokens)

        if should_llm and llm:
            plan = self._llm_fallback(query, llm_candidates, llm, kws)
            if plan:
                return plan

        # LLM 不可用 / LLM 没返回 → 退化
        if not kws:
            return MatchPlan(
                mode="single",
                selections=[],
                method="keyword",
                reason="无匹配 skill",
                uncertain=True,
            )

        # 多候选退化：返 top1 但标 uncertain
        return MatchPlan(
            mode="single",
            primary=SelectedSkill(name=kws[0][1]),
            method="keyword",
            score=kws[0][0],
            uncertain=True,
        )

    # ================================================================
    # 子方法
    # ================================================================

    def _match_exact(self, query: str) -> Optional[str]:
        """精确匹配 name / alias / shortcut

        Returns:
            注册表中的原始 skill name，或 None
        """
        q = query.strip().lower()
        if not q:
            return None

        # name 精确匹配（返回注册表原始 name，不是用户输入）
        for n in self.registry.list_active():
            if n.lower() == q:
                return n

        # alias 匹配
        if q in self._alias_index:
            return self._alias_index[q]

        # shortcut 匹配
        if q in self._shortcut_index:
            return self._shortcut_index[q]

        return None

    def _should_llm(self, query: str, kws: list, qtokens: dict):
        """三触发：决定是否进 LLM 兜底

        Returns:
            (should_llm: bool, candidate_names: list[str], reason: str)
        """
        active = self.registry.list_active()

        if not kws:
            return True, active, "0 命中"

        if len(kws) == 1 and kws[0][0] >= THRESH_SINGLE:
            return False, [], ""  # 单候选高分，直接返

        # 多候选 / 单候选低分 → LLM 决定
        candidates = [k[1] for k in kws[:10]]
        if len(kws) == 1:
            return True, candidates, f"单候选分低 {kws[0][0]}"
        return True, candidates, "多候选，LLM 决定 single/multi"

    def _llm_fallback(
        self,
        query: str,
        candidate_names: list[str],
        llm,
        kws: list,
    ) -> Optional[MatchPlan]:
        """LLM 兜底，返回 MatchPlan（支持 single/multi）

        Args:
            query: 用户输入
            candidate_names: 候选 skill 名称列表
            llm: LLM 客户端
            kws: keyword 阶段的 (score, name) 列表（可为空）

        Returns:
            MatchPlan 或 None（LLM 返回格式错误时）
        """
        # 构建候选列表文本
        lines = []
        for name in candidate_names[:10]:
            meta = self._load_meta(name)
            if not meta:
                continue
            mc = meta.meta_cache or {}
            purpose = mc.get("purpose", meta.description or "")
            intention = mc.get("intention", [])
            lines.append(f"- {name}: purpose={purpose}, intention={intention}")

        skills_text = "\n".join(lines) if lines else "(无候选)"

        prompt = f"""你是一个 Skill 路由助手。判断用户 query 是单 skill 能搞定，还是多 skill 协同。

## 用户 query
{query}

## 候选 skill（≤10）
{skills_text}

返回 JSON（不要包含任何解释文字）：
- 单 skill：{{"mode":"single","name":"skill_name","reason":"..."}}
- 多 skill：{{"mode":"multi","selections":[{{"name":"x","role":"出题"}},{{"name":"y","role":"解题"}}],"reason":"..."}}
- 无匹配：{{"mode":"none","reason":"..."}}"""

        resp = llm.invoke(prompt)
        raw_text = ""
        if hasattr(resp, "content"):
            raw_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        else:
            raw_text = str(resp)

        # 提取 JSON（可能在 markdown code block 中）
        json_match = re.search(r"\{[\s\S]*\}", raw_text)
        if not json_match:
            return None

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return None

        mode = data.get("mode", "none")

        if mode == "single":
            name = data.get("name", "")
            if not name or not self.registry.load_skill(name):
                return None
            return MatchPlan(
                mode="single",
                primary=SelectedSkill(name=name),
                method="llm",
                reason=data.get("reason"),
            )

        elif mode == "multi":
            selections_data = data.get("selections", [])
            if not selections_data:
                return None
            selections = []
            for s in selections_data:
                name = s.get("name", "")
                if not name or not self.registry.load_skill(name):
                    continue
                selections.append(SelectedSkill(
                    name=name,
                    role=s.get("role"),
                ))
            if not selections:
                return None
            # 从 keyword 分数推断 primary
            primary_score = 0.0
            primary_name = selections[0].name
            for score, name in kws:
                if name == selections[0].name:
                    primary_score = score
                    break
            return MatchPlan(
                mode="multi",
                primary=selections[0],
                selections=selections,
                method="llm",
                score=primary_score if primary_score > 0 else None,
                reason=data.get("reason"),
            )

        # mode == "none"
        return None