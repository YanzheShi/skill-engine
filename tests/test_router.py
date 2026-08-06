"""
Router 关键链路测试（当前 API：Router.match → MatchPlan）

覆盖三步路由管线：
- 精确匹配（name / alias / shortcut）→ method="exact", score=1.0
- 英文无命中 → 退化 uncertain（不触发 LLM，避免网络依赖）
- 中文无命中 → 无匹配 uncertain
- 关键词命中（intention phrase）→ method="keyword" 路由到目标 skill

注：通过 monkeypatch 把 get_llm 置为 None，强制路由在「精确/关键词」阶段确定性收敛，
不依赖外部 LLM（LLM 兜底路径由 test_orchestrator 的节点测试覆盖）。
"""

import pytest
from skill_engine.routing.router import Router
from skill_engine.models import MergedMeta, MatchPlan, SelectedSkill


class _FakeRegistry:
    """最小可用 Registry：仅实现 Router 在确定性路径上调用的方法。"""

    def __init__(self, metas: dict):
        self._metas = metas  # name -> MergedMeta

    def list_active(self):
        return list(self._metas.keys())

    def load_meta(self, name):
        return self._metas.get(name)

    def load_skill(self, name):
        # 确定性路径（无 LLM）不会走到这里
        return None


def _meta(name, *, alias=None, shortcuts=None, intention=None, keywords=None):
    meta_cache = {}
    if intention is not None:
        meta_cache["intention"] = intention
    if keywords is not None:
        meta_cache["keywords"] = keywords
    return MergedMeta(
        name=name,
        description=f"desc of {name}",
        alias=alias,
        shortcuts=shortcuts,
        meta_cache=meta_cache,
    )


@pytest.fixture
def no_llm(monkeypatch):
    """让路由在确定性阶段收敛，不触发真实 LLM。"""
    import skill_engine.routing.router as rm
    monkeypatch.setattr(rm, "get_llm", lambda *a, **k: None)
    yield


def test_exact_name_match(no_llm):
    reg = _FakeRegistry({"leetcode-solution-writer": _meta("leetcode-solution-writer")})
    plan = Router(reg).match("leetcode-solution-writer")
    assert isinstance(plan, MatchPlan)
    assert plan.mode == "single"
    assert plan.method == "exact"
    assert plan.score == 1.0
    assert isinstance(plan.primary, SelectedSkill)
    assert plan.primary.name == "leetcode-solution-writer"


def test_exact_alias_match(no_llm):
    reg = _FakeRegistry({"leetcode-solution-writer": _meta(
        "leetcode-solution-writer", alias=["lc", "刷题"])})
    plan = Router(reg).match("lc")
    assert plan.method == "exact"
    assert plan.primary.name == "leetcode-solution-writer"


def test_exact_shortcut_match(no_llm):
    reg = _FakeRegistry({"leetcode-solution-writer": _meta(
        "leetcode-solution-writer", shortcuts=["/lc"])})
    plan = Router(reg).match("/lc")
    assert plan.method == "exact"
    assert plan.primary.name == "leetcode-solution-writer"


def test_unknown_english_query_is_uncertain(no_llm):
    reg = _FakeRegistry({"leetcode-solution-writer": _meta("leetcode-solution-writer")})
    plan = Router(reg).match("please solve a leetcode problem")
    assert plan.method == "keyword"
    assert plan.uncertain is True
    assert plan.primary is None


def test_unknown_chinese_query_no_match(no_llm):
    reg = _FakeRegistry({"leetcode-solution-writer": _meta("leetcode-solution-writer")})
    plan = Router(reg).match("今天天气真好")
    assert plan.method == "keyword"
    assert plan.uncertain is True
    assert plan.primary is None


def test_keyword_intention_routes_to_skill(no_llm):
    # intention="出题" 与 query「帮我出一道算法题」逐字顺序命中 → phrase bonus > 0
    reg = _FakeRegistry({"leetcode-solution-writer": _meta(
        "leetcode-solution-writer", intention=["出题"])})
    plan = Router(reg).match("帮我出一道算法题")
    assert plan.method == "keyword"
    assert plan.primary is not None
    assert plan.primary.name == "leetcode-solution-writer"
