"""MOA 多模型协作编排器测试（无需真实 LLM / API）

用 ScriptedLLM 脚本化指挥官决策与 worker 产出，验证：
- 多模型 profile 解析与取用
- 指挥官 STOP 提前终止
- worker 经 ToolDispatchRunner 执行并写入黑板
- 防死循环四道闸（max_rounds / max_llm_calls / 连续同 agent 无进展强制停）
- 决策解析 fail-safe（解析失败 / 非法代号 → STOP）
"""

import os
import sys
import json

import pytest

from skill_engine.execution.moa import MoaSession


@pytest.fixture(autouse=True)
def _no_real_router(monkeypatch):
    """无 skill worker 的自动匹配在测试里不得触发真实 Router / 真实 LLM。

    MOA 行为变更后（worker 未配置 skill → 按描述自动匹配），旧测试里的
    纯英文 instruction（如 "dev"）会命中 Router 的「纯英文 → LLM 兜底」分支，
    发起真实 API 调用（网络挂起）。此处统一替换为不调 LLM 的空匹配。
    """
    from skill_engine.models import MatchPlan

    class NoLLMRouter:
        def __init__(self, *a, **k):
            pass

        def match(self, query):
            return MatchPlan(mode="single", primary=None, method="keyword",
                             reason="test stub")

    monkeypatch.setattr("skill_engine.execution.moa.Router", NoLLMRouter)


# ── 测试替身 ──────────────────────────────────────────────────────────────
class ScriptedLLM:
    """脚本化 LLM：按队列返回响应，耗尽后回退到最后一条（保证可复现）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0
        self.last_messages = None

    def invoke(self, messages, **kwargs):
        self.last_messages = messages
        self.call_count += 1
        if self.responses:
            return self.responses.pop(0)
        # 耗尽后回退到最后一条，避免回退成不同内容破坏"黑板无变化"测试
        return self.responses[-1] if self.responses else "ok"

    def bind_tools(self, tools, **kwargs):
        # ToolDispatchRunner 会 bind_tools 后再 invoke；返回自身即可
        return self


class RaisingLLM:
    """每次 invoke 都抛异常的 LLM（模拟 worker 运行期崩溃）。"""

    def __init__(self, exc=RuntimeError):
        self.exc = exc
        self.call_count = 0

    def invoke(self, messages, **kwargs):
        self.call_count += 1
        raise self.exc("worker boom")

    def bind_tools(self, tools, **kwargs):
        return self


class RaisingAfterLLM:
    """前 N 次 invoke 正常返回，第 raise_on_call 次起抛异常。

    用于测试「决策正常、但最终综合阶段指挥官崩溃」的兜底路径。
    """

    def __init__(self, responses, raise_on_call=3, exc=RuntimeError):
        self.responses = list(responses)
        self.raise_on_call = raise_on_call
        self.exc = exc
        self.call_count = 0

    def invoke(self, messages, **kwargs):
        self.call_count += 1
        if self.call_count >= self.raise_on_call:
            raise self.exc("commander boom at synthesis")
        return self.responses.pop(0) if self.responses else "ok"

    def bind_tools(self, tools, **kwargs):
        return self


class FakeRegistry:
    """最小注册表替身：list_active / load_skill。"""

    def __init__(self, skills=None):
        self._skills = skills or {}

    def list_active(self):
        return list(self._skills.keys())

    def load_skill(self, name):
        return self._skills.get(name)


def _make_orchestrator(tmp_path):
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.moa import MoaOrchestrator

    executor = Executor(timeout=10, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=10)
    return MoaOrchestrator(
        executor=executor, assembler=assembler, approval_fn=lambda *a, **k: True,
        human_io=None, working_root=str(tmp_path), plain_text=True, verbose=False,
    )


def _agents_with_injected_llm(workers_spec, commander_spec, agent_llm, commander_llm):
    from skill_engine.models import MoaAgent

    workers = [
        MoaAgent(alias=s["alias"], model_profile=s["model_profile"],
                 skill_name=s.get("skill_name", ""), instruction=s.get("instruction", ""),
                 role="worker", llm=agent_llm)
        for s in workers_spec
    ]
    commander = MoaAgent(alias=commander_spec["alias"],
                         model_profile=commander_spec["model_profile"],
                         skill_name=commander_spec.get("skill_name", ""),
                         instruction=commander_spec.get("instruction", ""),
                         role="commander", llm=commander_llm)
    return workers, commander


# ── 1. 多模型配置 ──────────────────────────────────────────────────────────
def test_list_model_profiles_includes_custom(monkeypatch):
    import skill_engine.config as cfg
    monkeypatch.setattr(cfg, "MODEL_PROFILES", {
        "default": {"model": "gpt-4o", "model_provider": "openai",
                    "base_url": "", "api_key": "x"},
        "gpt4o": {"model": "gpt-4o", "model_provider": "openai",
                  "base_url": "", "api_key": "x"},
        "claude": {"model": "claude-3-5", "model_provider": "anthropic",
                   "base_url": "", "api_key": "y"},
    })
    profiles = cfg.list_model_profiles()
    assert "gpt4o" in profiles and "claude" in profiles
    # api_key 已被脱敏
    assert profiles["claude"]["api_key"] == "***"


def test_get_llm_by_profile_unknown_raises(monkeypatch):
    import skill_engine.config as cfg
    monkeypatch.setattr(cfg, "MODEL_PROFILES", {
        "default": {"model": "gpt-4o", "model_provider": "openai",
                    "base_url": "", "api_key": "x"},
    })
    with pytest.raises(ValueError):
        cfg.get_llm_by_profile("does-not-exist")


# ── 2. 决策解析 fail-safe ───────────────────────────────────────────────────
def test_parse_decision_stop():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A1"})(), type("A", (), {"alias": "A2"})()]
    o = MoaOrchestrator(None, None)
    d = o._parse_decision('<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>', agents)
    assert d["next"] == "STOP"


def test_parse_decision_alias_case_insensitive():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A1"})(), type("A", (), {"alias": "A2"})()]
    o = MoaOrchestrator(None, None)
    d = o._parse_decision('<moa_decision>{"next":"a2","task":"fix","rationale":"x"}</moa_decision>', agents)
    assert d["next"] == "A2"


def test_parse_decision_invalid_alias_safe_stop():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A1"})()]
    o = MoaOrchestrator(None, None)
    d = o._parse_decision('<moa_decision>{"next":"ZZ","task":"x"}</moa_decision>', agents)
    assert d["next"] == "STOP"


def test_parse_decision_unparseable_safe_stop():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A1"})()]
    o = MoaOrchestrator(None, None)
    d = o._parse_decision("我觉得到这吧，结束吧", agents)
    assert d["next"] == "STOP"


# ── 3. 指挥官 STOP 提前终止 ─────────────────────────────────────────────────
def test_commander_stop_terminates(tmp_path):
    """已行动过的 worker + 指挥官 STOP → 立即终止（防早停闸只拦未行动者）。"""
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["worker output"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"do","rationale":"go"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "do X"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 2
    # A1 已行动过 → STOP 未被拦截；决策 2 次 + 最终综合 1 次
    assert commander_llm.call_count == 3
    assert agent_llm.call_count == 1


def test_commander_stop_terminates_when_worker_no_instruction(tmp_path):
    """未声明职责的 worker 不触发防早停：首个 STOP 直接生效。"""
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["worker output"])
    commander_llm = ScriptedLLM(['<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>'])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": ""}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 1
    # 指挥官被调用 1 次，worker 未被执行
    assert commander_llm.call_count == 1
    assert agent_llm.call_count == 0


# ── 4. worker 执行并写入黑板 ───────────────────────────────────────────────
def test_worker_executes_and_blackboard(tmp_path):
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成：实现了登录函数"])
    # 第一轮派 A1，第二轮 STOP；之后指挥官再做一次「最终综合」（第 3 次调用）
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"实现登录","rationale":"先开发"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"完成"}</moa_decision>',
        "A1 已实现登录函数，任务完成。",
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 2
    assert agent_llm.call_count == 1           # A1 真正执行了一次
    assert "登录" in result["output"]           # 最终综合包含 worker 产出
    assert "A1" in result["output"]


# ── 4.5 worker 未配置 skill → 按描述自动匹配（问题 4） ───────────────────────
def _run_agent_with_router(orch, agent, tmp_path, monkeypatch, plan_factory):
    """预置 FakeRouter + FakeToolRunner，执行一次 _run_agent，返回 (seen_skill, out)。"""
    from skill_engine.execution.moa import MoaSession

    class FakeRouter:
        def __init__(self, *a, **k):
            pass

        def match(self, query):
            return plan_factory(query)

    monkeypatch.setattr("skill_engine.execution.moa.Router", FakeRouter)

    seen = {}

    class FakeRunner:
        def __init__(self, **kw):
            pass

        def run(self, mr, *a, **k):
            from skill_engine.models import RunResult
            seen["skill"] = mr.skill.metadata.name
            return RunResult(output="done", ctx={}, history=[])

    monkeypatch.setattr("skill_engine.execution.moa.ToolDispatchRunner", FakeRunner)

    out, _, _ = orch._run_agent(agent, MoaSession(str(tmp_path)), "task", "query", 1, 10)
    return seen, out


def test_worker_auto_match_skill_when_no_skill(tmp_path, monkeypatch):
    """未配置 skill 的 worker 按指示自动匹配现有 skill，而不是直接内置。"""
    from skill_engine.models import MoaAgent, MatchPlan, SelectedSkill, Skill, SkillMetadata
    orch = _make_orchestrator(tmp_path)
    skill = Skill(metadata=SkillMetadata(name="code-builder", description="写代码"),
                  body="开发 skill", directory=str(tmp_path))
    orch._registry = FakeRegistry({"code-builder": skill})
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     instruction="帮我实现登录页", role="worker")

    def plan_factory(query):
        assert "登录" in query
        return MatchPlan(mode="single", primary=SelectedSkill(name="code-builder"),
                         method="keyword", score=0.9)

    seen, out = _run_agent_with_router(orch, agent, tmp_path, monkeypatch, plan_factory)
    assert seen["skill"] == "code-builder"
    assert agent.skill_name == "code-builder"   # 回填，后续轮次直接加载


def test_worker_auto_match_falls_back_to_builtin(tmp_path, monkeypatch):
    """匹配不到 → 回退内置纯模型（原行为）。"""
    from skill_engine.models import MoaAgent, MatchPlan
    orch = _make_orchestrator(tmp_path)
    orch._registry = FakeRegistry({})
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     instruction="做点杂活", role="worker")

    seen, _ = _run_agent_with_router(orch, agent, tmp_path, monkeypatch,
                                     lambda q: MatchPlan(mode="single", primary=None,
                                                         method="keyword", reason="no match"))
    assert seen["skill"].startswith("moa-builtin-")


def test_worker_auto_match_skips_commander_skill(tmp_path, monkeypatch):
    """命中指挥官专用 skill（moa-commander）不给 worker 用 → 内置。"""
    from skill_engine.models import MoaAgent, MatchPlan, SelectedSkill, Skill, SkillMetadata
    orch = _make_orchestrator(tmp_path)
    skill = Skill(metadata=SkillMetadata(name="moa-commander", description="指挥"),
                  body="cmd", directory=str(tmp_path))
    orch._registry = FakeRegistry({"moa-commander": skill})
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     instruction="你来指挥", role="worker")

    seen, _ = _run_agent_with_router(
        orch, agent, tmp_path, monkeypatch,
        lambda q: MatchPlan(mode="single", primary=SelectedSkill(name="moa-commander"),
                            method="keyword", score=0.9))
    assert seen["skill"].startswith("moa-builtin-")


def test_worker_auto_match_cached_by_alias(tmp_path, monkeypatch):
    """匹配结果按 alias 缓存：第二轮不再构造 Router / 重复匹配。"""
    from skill_engine.models import MoaAgent, Skill, SkillMetadata
    orch = _make_orchestrator(tmp_path)
    skill = Skill(metadata=SkillMetadata(name="code-builder", description="写代码"),
                  body="开发 skill", directory=str(tmp_path))
    orch._registry = FakeRegistry({"code-builder": skill})
    orch._auto_skill_cache["A1"] = "code-builder"   # 模拟第一轮已匹配
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     instruction="x", role="worker")

    seen = {}

    class FakeRunner:
        def __init__(self, **kw):
            pass

        def run(self, mr, *a, **k):
            from skill_engine.models import RunResult
            seen["skill"] = mr.skill.metadata.name
            return RunResult(output="done", ctx={}, history=[])

    monkeypatch.setattr("skill_engine.execution.moa.ToolDispatchRunner", FakeRunner)
    from skill_engine.execution.moa import MoaSession
    orch._run_agent(agent, MoaSession(str(tmp_path)), "task", "query", 2, 10)

    assert seen["skill"] == "code-builder"
    # 缓存命中 → Router 从未被构造（FakeRouter 若被构造应抛错）
    class ShouldNotRun:
        def __init__(self, *a, **k):
            raise AssertionError("cache 命中时不应构造 Router")

    monkeypatch.setattr("skill_engine.execution.moa.Router", ShouldNotRun)
    from skill_engine.execution.moa import MoaSession
    orch._run_agent(agent, MoaSession(str(tmp_path)), "task", "query", 3, 10)


# ── 4.7 防早停：未行动 worker 禁止 STOP（用户场景：A2 UI 检查者被跳过） ────────
def test_commander_stop_blocked_when_worker_idle(tmp_path):
    """指挥官首轮就 STOP，但 A1 从未行动 → 硬闸拦截并改派，A1 必须执行。"""
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成了"])
    # 指挥官两轮都输出 STOP（第 1 轮被硬闸拦截，第 2 轮才真正生效）
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"STOP","task":"","rationale":"我判断完成了"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert agent_llm.call_count == 1          # A1 被强制首轮执行
    assert result["rounds"] == 2


def test_commander_stop_allowed_after_worker_acted(tmp_path):
    """worker 已行动过 → STOP 正常放行（防早停不误伤收尾）。"""
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"做","rationale":"go"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 2


def test_commander_stop_allowed_when_idle_worker_no_instruction(tmp_path):
    """未行动 worker 未声明职责（instruction 空）→ 不强制，正常 STOP。"""
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["x"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": ""}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert agent_llm.call_count == 0


def test_commander_prompt_lists_action_counts_and_idle(tmp_path):
    """prompt 必须含行动次数与「尚未行动」清单（程序显式列出，不靠模型倒推）。"""
    from skill_engine.execution.moa import MoaSession
    orch = _make_orchestrator(tmp_path)
    orch._registry = FakeRegistry({})
    session = MoaSession(str(tmp_path))
    session.action_counts = {"A1": 3, "A2": 0}
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"},
         {"alias": "A2", "model_profile": "default", "skill_name": "", "instruction": "UI 检查者，负责检查页面"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        ScriptedLLM(["x"]), ScriptedLLM(["x"]),
    )
    prompt = orch._commander_prompt(commander, session, workers, "query", 1, 8)
    assert "已行动 3 次" in prompt
    assert "已行动 0 次" in prompt
    assert "尚未行动" in prompt
    assert "A2" in prompt and "UI 检查者" in prompt
    assert "禁止输出 STOP" in prompt


def test_session_action_counts_increment_on_add_entry():
    from skill_engine.execution.moa import MoaSession
    s = MoaSession(".")
    s.add_entry("A1", "", "out1", [])
    s.add_entry("A2", "", "out2", [])
    s.add_entry("A1", "", "out3", [])
    assert s.action_counts == {"A1": 2, "A2": 1}


# ── 5. 防死循环：连续同 agent 无进展强制停止 ────────────────────────────────
def test_anti_loop_forced_stop(tmp_path):
    orch = _make_orchestrator(tmp_path)
    # worker 每次产出完全一样（黑板哈希不变）
    agent_llm = ScriptedLLM(["same output every time"])
    # 指挥官永远派 A1，从不 STOP
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"again","rationale":"loop"}</moa_decision>',
    ] * 10)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=20,
                      max_agent_iterations=5, max_llm_calls=200,
                      max_consecutive_same_agent=3)
    assert result["stopped_by"] == "anti_loop_forced_stop"
    assert result["rounds"] == 3   # 连续 3 轮同 agent 无进展即停


# ── 6. 防死循环：max_rounds 上限 ────────────────────────────────────────────
def test_max_rounds_cap(tmp_path):
    orch = _make_orchestrator(tmp_path)
    # worker 每轮产出不同（黑板变化，不触发 anti-loop），但指挥官永不 STOP
    agent_llm = ScriptedLLM([f"different output {i}" for i in range(10)])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"more","rationale":"go"}</moa_decision>',
    ] * 10)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=4,
                      max_agent_iterations=5, max_llm_calls=200)
    assert result["stopped_by"] == "max_rounds"
    assert result["rounds"] == 4


# ── 7. 防死循环：max_llm_calls 上限 ─────────────────────────────────────────
def test_max_llm_calls_cap(tmp_path):
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["out"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"x","rationale":"go"}</moa_decision>',
    ] * 10)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    # 上限 2 次：commander(1) + worker(1) 后即达上限
    result = orch.run(workers, commander, registry, query="task", max_rounds=20,
                      max_agent_iterations=5, max_llm_calls=2)
    assert result["stopped_by"] == "max_llm_calls"
    assert result["rounds"] == 1


# ── 8. CountingLLM 计数 ────────────────────────────────────────────────────
def test_counting_llm_counts_bound_invoke():
    from skill_engine.execution.moa import CountingLLM
    # 与编排器一致：dict 计数器 {calls,prompt,completion,total}
    counter = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}
    base = ScriptedLLM(["a", "b"])
    wrapped = CountingLLM(base, counter)
    bound = wrapped.bind_tools([])   # bind_tools 计入 1 次
    bound.invoke(["x"])              # invoke 计入 1 次
    wrapped.invoke(["y"])            # invoke 计入 1 次
    assert counter["calls"] == 3
    assert base.call_count == 2


# ── 9. 决策解析鲁棒性（乱序 / 含花括号的 rationale） ───────────────────────
def test_parse_decision_braces_in_rationale():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A1"})()]
    o = MoaOrchestrator(None, None)
    # rationale 内含花括号：旧的非贪婪 \{.*?\} 会误截，新实现取围栏内全文再 json.loads
    d = o._parse_decision(
        '<moa_decision>{"next":"A1","task":"x","rationale":"使用 {foo: bar} 配置"}</moa_decision>',
        agents,
    )
    assert d["next"] == "A1"


def test_parse_decision_multiline_fence():
    from skill_engine.execution.moa import MoaOrchestrator
    agents = [type("A", (), {"alias": "A2"})()]
    o = MoaOrchestrator(None, None)
    d = o._parse_decision(
        '分析一下：\n<moa_decision>\n{\n  "next": "A2",\n  "task": "审查",\n  "rationale": "补质量门禁"\n}\n</moa_decision>',
        agents,
    )
    assert d["next"] == "A2"


# ── 10. worker 异常隔离（单点失败不拖垮整轮） ──────────────────────────────
def test_worker_exception_isolated_and_antiloop(tmp_path):
    orch = _make_orchestrator(tmp_path)
    # worker 每次都抛异常；指挥官永远派 A1 → 应被反震荡闸捕获，而非向上传播
    agent_llm = RaisingLLM()
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"again","rationale":"loop"}</moa_decision>',
    ] * 10)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    registry = FakeRegistry()
    result = orch.run(workers, commander, registry, query="task", max_rounds=20,
                      max_agent_iterations=5, max_llm_calls=200,
                      max_consecutive_same_agent=3)
    # 异常被隔离，未向上抛；连续 3 轮同 agent 稳定错误指纹 → 强制停止
    assert result["stopped_by"] == "anti_loop_forced_stop"
    assert result["rounds"] == 3


# ── 11. 早退校验 ───────────────────────────────────────────────────────────
def test_no_agents_early_return(tmp_path):
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    commander = MoaAgent(alias="C", model_profile="default", role="commander",
                         llm=ScriptedLLM(["x"]))
    result = orch.run([], commander, FakeRegistry(), query="task")
    assert result["stopped_by"] == "no_agents"
    assert result["rounds"] == 0
    assert result["llm_calls"] == 0


def test_duplicate_alias_early_return(tmp_path):
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    workers = [MoaAgent(alias="A1", model_profile="default", role="worker",
                        llm=ScriptedLLM(["x"]))]
    # 指挥官与 worker 复用同一代号 → 拒绝
    commander = MoaAgent(alias="A1", model_profile="default", role="commander",
                         llm=ScriptedLLM(["x"]))
    result = orch.run(workers, commander, FakeRegistry(), query="task")
    assert result["stopped_by"] == "duplicate_alias"


def test_model_config_error(monkeypatch, tmp_path):
    import skill_engine.config as cfg

    def boom(name, **kw):
        raise ValueError(f"未知 profile: {name}")

    monkeypatch.setattr(cfg, "get_llm_by_profile", boom)
    orch = _make_orchestrator(tmp_path)
    from skill_engine.models import MoaAgent
    workers = [MoaAgent(alias="A1", model_profile="ghost", role="worker")]
    commander = MoaAgent(alias="C", model_profile="ghost", role="commander")
    result = orch.run(workers, commander, FakeRegistry(), query="task",
                      max_rounds=2, max_llm_calls=10)
    assert result["stopped_by"] == "model_config_error"


# ── 12. 多 worker 轮转（A1 → A2 → STOP） ───────────────────────────────────
def test_multi_worker_round_robin(tmp_path):
    orch = _make_orchestrator(tmp_path)
    from skill_engine.models import MoaAgent
    w1 = MoaAgent(alias="A1", model_profile="default", skill_name="",
                  instruction="i1", role="worker", llm=ScriptedLLM(["A1 实现登录函数"]))
    w2 = MoaAgent(alias="A2", model_profile="default", skill_name="",
                  instruction="i2", role="worker", llm=ScriptedLLM(["A2 编写测试"]))
    commander = MoaAgent(alias="C", model_profile="default", skill_name="",
                         instruction="cmd", role="commander",
                         llm=ScriptedLLM([
                             '<moa_decision>{"next":"A1","task":"dev","rationale":"1"}</moa_decision>',
                             '<moa_decision>{"next":"A2","task":"test","rationale":"2"}</moa_decision>',
                             '<moa_decision>{"next":"STOP","task":"","rationale":"完成"}</moa_decision>',
                         ]))
    result = orch.run([w1, w2], commander, FakeRegistry(), query="task",
                      max_rounds=8, max_llm_calls=100, final_synthesis=False)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 3
    assert w1.llm.call_count == 1
    assert w2.llm.call_count == 1
    assert "A1 实现登录函数" in result["output"]
    assert "A2 编写测试" in result["output"]


# ── 13. 最终综合兜底（指挥官崩溃 → 回退拼接黑板） ──────────────────────────
def test_final_synthesis_fallback_on_commander_error(tmp_path):
    orch = _make_orchestrator(tmp_path)
    worker_llm = ScriptedLLM(["A1 完成登录"])
    # 前 2 次决策正常（A1 / STOP），第 3 次（最终综合）抛异常 → 回退拼接
    commander_llm = RaisingAfterLLM([
        '<moa_decision>{"next":"A1","task":"x","rationale":"1"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"完成"}</moa_decision>',
    ], raise_on_call=3)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        worker_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task",
                      max_rounds=8, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert "A1" in result["output"]          # 回退拼接含 worker 产出
    assert "回退" in result["output"]         # 含回退说明


# ── 14. last_rationale 记录 ────────────────────────────────────────────────
def test_last_rationale_recorded(tmp_path):
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["out"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"x","rationale":"先做开发"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"任务完成，停止"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task",
                      max_rounds=8, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert result["last_rationale"] == "任务完成，停止"


# ── 15. MoaAgent.summary 格式与截断 ───────────────────────────────────────
def test_moa_agent_summary_format():
    from skill_engine.models import MoaAgent
    a = MoaAgent(alias="A1", model_profile="gpt4o", skill_name="code-builder",
                 instruction="实现登录页功能")
    s = a.summary()
    assert "A1" in s and "gpt4o" in s and "code-builder" in s and "实现登录页功能" in s
    # 长指示应被截断
    long_instr = "x" * 200
    s2 = MoaAgent(alias="A2", model_profile="default", instruction=long_instr).summary()
    assert len(s2) < 200 + 40
    assert s2.endswith("…")


# ── 16. last_entry_hash 稳定性 ────────────────────────────────────────────
def test_last_entry_hash_stable_vs_change(tmp_path):
    s1 = MoaSession(str(tmp_path))
    s1.add_entry("A1", "code-builder", "same text", [])
    h1 = s1.last_entry_hash()
    s1.add_entry("A2", "test", "different text", [])
    h2 = s1.last_entry_hash()
    assert h1 != h2
    # 相同内容 → 相同指纹（反震荡依赖此性质）
    s2 = MoaSession(str(tmp_path))
    s2.add_entry("A1", "code-builder", "same text", [])
    assert s2.last_entry_hash() == h1


# ── 17. 黑板摘要截断 ──────────────────────────────────────────────────────
def test_blackboard_summary_truncation(tmp_path):
    s = MoaSession(str(tmp_path))
    s.add_entry("A1", "code-builder", "z" * 5000, [])
    summary = s.blackboard_summary(max_each=1500)
    assert "截断" in summary
    assert len(summary) < 5000


# ── 18. commander prompt 含名册与黑板 ─────────────────────────────────────
def test_commander_prompt_contains_roster_and_blackboard(tmp_path):
    orch = _make_orchestrator(tmp_path)
    from skill_engine.models import MoaAgent
    agents = [
        MoaAgent(alias="A1", model_profile="default", skill_name="code-builder", role="worker"),
        MoaAgent(alias="A2", model_profile="secondary", skill_name="", role="worker"),
    ]
    commander = MoaAgent(alias="C", model_profile="default", role="commander")
    sess = MoaSession(str(tmp_path))
    sess.add_entry("A1", "code-builder", "已完成登录函数", [])
    prompt = orch._commander_prompt(commander, sess, agents, "优化登录页", 1, 8)
    # 名册只含 worker（A1/A2），指挥官自身不在可选 next 中
    assert "A1" in prompt and "A2" in prompt
    assert "优化登录页" in prompt
    assert "已完成登录函数" in prompt       # 黑板已注入


# ── 19. 异常终止（max_iterations）识别与恢复 ──────────────────────────────
def _bash_call(cid="c1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "type": "bash", "input": {"command": "echo ok"}}]}


def test_worker_status_max_iterations_forwarded(tmp_path):
    """worker 撞 max_iterations：状态必须透传（code/iterations），产出带标记。"""
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    session = MoaSession(str(tmp_path))
    agent = MoaAgent(alias="A1", model_profile="default", skill_name="",
                     role="worker", instruction="分析代码",
                     llm=ScriptedLLM([_bash_call(), _bash_call()]))
    out, files, status = orch._run_agent(agent, session, "检查文件", "总任务", 1,
                                         max_agent_iterations=2)
    assert status["code"] == "max_iterations"
    assert status["iterations"] == 2
    assert "达到最大迭代次数" in out


def test_run_aborted_worker_redispatched_and_finalized(tmp_path):
    """撞上限不拖垮流程：commander 可续派，收尾综合正常。"""
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    worker = MoaAgent(alias="A1", model_profile="default", skill_name="",
                      role="worker", instruction="完成任务 A",
                      llm=ScriptedLLM([_bash_call(), _bash_call()]))
    commander = MoaAgent(alias="C", model_profile="default", role="commander",
                         llm=ScriptedLLM([
                             '<moa_decision>{"next":"A1","task":"继续工作","rationale":"r"}</moa_decision>',
                             '<moa_decision>{"next":"A1","task":"继续推进","rationale":"r"}</moa_decision>',
                             '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
                             '最终综合输出',
                         ]))
    result = orch.run([worker], commander, FakeRegistry(), query="q",
                      max_rounds=4, max_agent_iterations=2, max_llm_calls=500)
    # 轮1 撞 max_iterations 中止 → 轮2 续派后 worker 正常完成 → 轮3 决策脚本耗尽
    # 回退为非决策文本 → 解析失败安全 STOP。完整验证「识别中止 → 续派恢复 → 收尾」。
    assert result["rounds"] == 3
    assert result["stopped_by"] == "commander_stop"
    assert result["output"] == "最终综合输出"


def test_progress_summary_two_tier(tmp_path):
    """指挥官视图两段式：异常条目全量 + 正常条目一行概览。"""
    s = MoaSession(str(tmp_path))
    s.add_entry("A1", "code-builder", "A1 已完成全部实现", [], status="ok")
    s.add_entry("A2", "vlm", "只检查了一半", [],
                status="max_iterations", iterations=60, reason="max_iterations")
    ps = s.progress_summary()
    assert "未完成 / 异常" in ps
    assert "达到最大迭代次数" in ps and "60" in ps
    assert "A1 已完成全部实现" in ps     # 正常条目进概览行
    assert "只检查了一半" in ps          # 异常条目全文保留
    # 全量视图（最终综合用）也带 ⚠ 标记
    assert "⚠" in s.blackboard_summary()


def test_commander_prompt_contains_aborted_section(tmp_path):
    """commander prompt 必须显式列出未完成 worker 并给续派规则。"""
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    agents = [MoaAgent(alias="A1", model_profile="default", role="worker")]
    commander = MoaAgent(alias="C", model_profile="default", role="commander")
    sess = MoaSession(str(tmp_path))
    sess.add_entry("A1", "code-builder", "做到一半被中断", [],
                   status="max_iterations", iterations=60)
    prompt = orch._commander_prompt(commander, sess, agents, "q", 1, 8)
    assert "未完成 / 异常 worker" in prompt
    assert "优先续派" in prompt
    assert "不要从头重做" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
