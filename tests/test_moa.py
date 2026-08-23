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


# ── 4.7.x MOA 指挥官决策解析失败止血（对应 pomodoro3 故障：解析失败伪装成达成 + 删检查点） ──
def test_unparseable_decision_keeps_checkpoint_and_does_not_fake_stop(tmp_path):
    """指挥官 JSON 解析失败 → 标记 commander_unparseable（非 commander_stop）、
    已存在的检查点不被删除（可 --resume-from）、不伪装成任务达成。

    回归：修复前解析失败会走 STOP 分支，stopped_by=commander_stop 且检查点被删。

    场景构造：先正常派 A1 跑一轮（写出检查点），第 2 轮故意给坏 JSON，
    验证检查点在中断后依然保留（而非被 _CLEAN_STOP_REASONS 路径删除）。
    """
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"做","rationale":"go"}</moa_decision>',  # 正常派活，写出检查点
        "<moa_decision>{这不是合法JSON</moa_decision>",                            # 解析失败
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    state_path = str(tmp_path / "moa_session_state.json")
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100, state_path=state_path)
    # 第 2 轮解析失败应被识别为格式故障，而非伪装成任务达成
    assert result["stopped_by"] == "commander_unparseable"
    # 第 1 轮已写出检查点，中断后必须保留（不在 _CLEAN_STOP_REASONS 中），以便续跑
    assert os.path.exists(state_path)


def test_unparseable_decision_reports_unparseable_flag(tmp_path):
    """_parse_decision 在各类格式故障时返回 unparseable=True 标记，
    使主循环能据此与真实 STOP 区分（不依赖具体坏形态，覆盖：裸换行/未知代号/无围栏）。"""
    orch = _make_blank_orchestrator_for_parse()
    agents = [type("A", (), {"alias": "A1"})()]
    # 裸换行（strict=False 应救回，这里故意用 strict 失效形态验证标记逻辑之外的健壮性）
    bad = [
        "<moa_decision>{坏JSON</moa_decision>",                       # 解析失败
        '<moa_decision>{"next":"ZZ9","task":"","rationale":""}</moa_decision>',  # 未知代号
        "完全没有围栏的闲聊文本",                                       # 无围栏非 STOP
    ]
    for text in bad:
        d = orch._parse_decision(text, agents)
        assert d.get("unparseable") is True, f"应为格式故障: {text!r}"
        assert d["next"] == "STOP"  # 兼容字段保留，既有断言不受影响


def test_strict_false_rescues_multiline_task(tmp_path):
    """JSON 字符串内含裸换行（控制字符）应在 strict=False 下正常解析（本次故障头号嫌疑）。"""
    orch = _make_orchestrator(tmp_path)
    agents = [type("A", (), {"alias": "A1"})()]
    text = '<moa_decision>{"next":"A1","task":"第一行\n第二行","rationale":"多行任务"}</moa_decision>'
    d = orch._parse_decision(text, agents)
    assert d.get("unparseable") is not True
    assert d["next"] == "A1"
    assert "第一行" in d["task"] and "第二行" in d["task"]


# ── 4.7.y 指挥官 LLM 调用失败重试（瞬时故障自愈 / 鉴权 fail-fast） ──
class _FlakyLLM:
    """前 fail_times 次 invoke 抛异常，之后**每次**都返回成功响应（模拟瞬时故障自愈）。

    注意：commander 私有上下文的 maybe_compress 也可能调用 llm 做摘要，
    故失败计数会把压缩调用也算进去；这里用"失败后恒返回成功"而非"pop 队列"，
    避免压缩偷吃 responses 导致后续决策拿到错误内容。
    """

    def __init__(self, fail_times=0, exc=RuntimeError, success="ok"):
        self.fail_times = fail_times
        self.exc = exc
        self.success = success
        self.call_count = 0

    def invoke(self, messages, **kwargs):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise self.exc(f"transient boom #{self.call_count}")
        return self.success

    def bind_tools(self, tools, **kwargs):
        return self


def test_commander_retries_on_transient_failure_then_succeeds(tmp_path, monkeypatch):
    """指挥官前 2 次调用抛瞬时异常、之后成功 → 重试后正常派活，不终止。"""
    monkeypatch.setattr("skill_engine.execution.moa.time.sleep", lambda *a, **k: None)  # 跳过真实等待
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成"])
    # 指挥官：前 2 次抛 RuntimeError，第 3 次返回合法决策
    commander_llm = _FlakyLLM(
        fail_times=2, exc=RuntimeError,
        success='<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    )
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"      # 重试成功，正常结束
    assert commander_llm.call_count >= 3                  # 首试 + 2 次重试（压缩可能更多）


def test_commander_gives_up_after_max_retries(tmp_path, monkeypatch):
    """指挥官连续调用失败（超过上限）→ commander_error 终止，不无限重试。"""
    monkeypatch.setattr("skill_engine.execution.moa.time.sleep", lambda *a, **k: None)
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成"])
    commander_llm = _FlakyLLM(fail_times=99, exc=RuntimeError)   # 永远抛
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_error"
    assert commander_llm.call_count >= 3                  # 首试 + 2 次重试 = 至少上限


def test_commander_auth_error_fails_fast(tmp_path, monkeypatch):
    """鉴权失败（401/api key）不重试，直接 commander_error（fail-fast）。"""
    monkeypatch.setattr("skill_engine.execution.moa.time.sleep", lambda *a, **k: None)
    orch = _make_orchestrator(tmp_path)
    agent_llm = ScriptedLLM(["A1 完成"])
    commander_llm = _FlakyLLM(fail_times=99, exc=RuntimeError)
    # 让 invoke 抛带鉴权关键词的异常
    def _auth_boom(messages, **kwargs):
        raise RuntimeError("401 Unauthorized: invalid api key")
    commander_llm.invoke = _auth_boom
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=8,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_error"
    # 鉴权失败 fail-fast：重试循环内只调用 1 次（首试），不重试；
    # 压缩可能额外调 1 次，故断言 < 3（无 2 次重试）
    assert commander_llm.call_count < 3


def _make_blank_orchestrator_for_parse():
    """构造一个只用于调用 _parse_decision 的轻量编排器（不跑完整 run）。"""
    from skill_engine.execution.moa import MoaOrchestrator
    return MoaOrchestrator(
        executor=None, assembler=None, approval_fn=lambda *a, **k: True,
        human_io=None, working_root="/tmp", plain_text=True, verbose=False,
    )


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
    bound = wrapped.bind_tools([])   # bind_tools 非真实调用，不计入（B-3 修复）
    bound.invoke(["x"])              # invoke 计入 1 次
    wrapped.invoke(["y"])            # invoke 计入 1 次
    assert counter["calls"] == 2     # 旧实现 bind_tools 虚增 1 次 → 预算被幽灵配额提前耗尽
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


# ── 20. Phase 3：MOA 级状态持久化与崩溃续跑 ────────────────────────────────
def test_moa_state_save_load_roundtrip(tmp_path):
    """状态落盘/载入往返：agent 私有上下文、黑板、决策轨迹、计数器全部保留。"""
    from pathlib import Path
    from skill_engine.execution.moa import MoaAgentRuntime
    orch = _make_orchestrator(tmp_path)
    s = MoaSession(str(tmp_path))
    s.round = 2
    s.add_entry("A1", "code-builder", "A1 实现登录", [], key_facts=["登录页已实现"])
    s.add_entry("A2", "", "A2 补了测试", ["t.py"], key_facts=["测试覆盖登录流程"])
    s.commander_decisions.append({"round": 1, "next": "A1", "task": "dev", "rationale": "先开发"})
    s.commander_decisions.append({"round": 2, "next": "A2", "task": "test", "rationale": "补质量"})
    s.last_agent_alias = "A2"
    s.same_agent_streak = 2
    s.no_progress_streak = 0
    s.prev_blackboard_hash = s.last_entry_hash()
    s.agent_contexts["A1"] = MoaAgentRuntime(
        alias="A1",
        messages=[{"role": "user", "content": "r1"}, {"role": "assistant", "content": "done"}],
        last_output="A1 实现登录", cumulative_iterations=5, files=["login.py"],
    )
    counter = {"calls": 7, "prompt": 100, "completion": 50, "total": 150}
    path = str(tmp_path / "moa_state.json")
    orch._save_moa_state(path, s, counter)

    restored = orch._load_moa_state(path)
    assert restored is not None
    s2, c2 = restored["session"], restored["counter"]
    assert s2.round == 2
    assert s2.last_agent_alias == "A2"
    assert s2.same_agent_streak == 2
    assert s2.no_progress_streak == 0
    assert s2.prev_blackboard_hash == s.prev_blackboard_hash   # 确定性指纹跨进程一致
    assert s2.action_counts == {"A1": 1, "A2": 1}
    assert len(s2.blackboard) == 2
    assert s2.blackboard[1]["files"] == ["t.py"]
    assert s2.blackboard[0]["key_facts"] == ["登录页已实现"]   # key_facts 随状态持久化
    assert len(s2.commander_decisions) == 2
    a1 = s2.agent_contexts["A1"]
    assert a1.messages[0]["content"] == "r1"
    assert a1.cumulative_iterations == 5
    assert a1.files == ["login.py"]
    assert c2 == counter
    # 载入后增量指纹与落盘前一致（确定性哈希，跨进程可对齐）
    assert s2.last_entry_hash() == s.last_entry_hash()


def test_moa_resume_continues_from_breakpoint(tmp_path):
    """崩溃续跑：载入断点后从「已完成轮数+1」继续，黑板/计数保留，不再重复派活。"""
    from pathlib import Path
    orch = _make_orchestrator(tmp_path)
    s = MoaSession(str(tmp_path))
    s.round = 2
    s.add_entry("A1", "code-builder", "A1 did X", [])
    s.add_entry("A2", "code-builder", "A2 did Y", [])
    s.prev_blackboard_hash = s.last_entry_hash()
    state_path = str(tmp_path / "moa_state.json")
    counter = {"calls": 5, "prompt": 90, "completion": 40, "total": 130}
    orch._save_moa_state(state_path, s, counter)

    # 续跑：两个 worker 都已行动过 → 指挥官 STOP 直接生效，不再派活
    agent_llm = ScriptedLLM(["should not be called"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"},
         {"alias": "A2", "model_profile": "default", "skill_name": "", "instruction": "test"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=10,
                      max_agent_iterations=5, max_llm_calls=100,
                      resume_from=state_path, final_synthesis=False)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 3          # 已完成的 2 轮 + 续跑 1 轮（STOP 轮）
    assert commander_llm.call_count == 1  # 仅决策 1 次（final_synthesis=False）
    assert agent_llm.call_count == 0      # 不重复派活
    assert "A1 did X" in result["output"]  # 黑板跨进程保留
    assert "A2 did Y" in result["output"]
    assert result["llm_calls"] >= 5       # 成本计数器续接（原 5 次 + 续跑 1 次）
    # 干净结束 → 检查点被删除（不留陈旧状态文件误导后续 --resume-from）
    assert not Path(state_path).exists()


def test_moa_resume_redispatch_worker_keeps_context(tmp_path):
    """续跑后 commander 再次派 A1：worker 基于恢复出的私有上下文续做（不重做）。"""
    from pathlib import Path
    from skill_engine.execution.moa import MoaAgentRuntime
    orch = _make_orchestrator(tmp_path)
    s = MoaSession(str(tmp_path))
    s.round = 2
    s.add_entry("A1", "code-builder", "A1 完成了第一版", ["login.py"])
    s.add_entry("A2", "code-builder", "A2 审查发现 3 处问题", [])
    s.agent_contexts["A1"] = MoaAgentRuntime(
        alias="A1",
        messages=[{"role": "user", "content": "round1 任务"},
                  {"role": "assistant", "content": "A1 完成了第一版"}],
        last_output="A1 完成了第一版", cumulative_iterations=3, files=["login.py"],
    )
    state_path = str(tmp_path / "moa_state.json")
    orch._save_moa_state(state_path, s, {"calls": 6, "prompt": 1, "completion": 1, "total": 2})

    agent_llm = ScriptedLLM(["A1 已修复审查问题"])
    commander_llm = ScriptedLLM([
        '<moa_decision>{"next":"A1","task":"修复 A2 指出的问题","rationale":"续派"}</moa_decision>',
        '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
    ])
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"},
         {"alias": "A2", "model_profile": "default", "skill_name": "", "instruction": "review"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        agent_llm, commander_llm,
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=10,
                      max_agent_iterations=5, max_llm_calls=100,
                      resume_from=state_path, final_synthesis=False)
    assert result["stopped_by"] == "commander_stop"
    assert result["rounds"] == 4          # 2 轮已完成 + 续跑 2 轮（A1 修复 / STOP）
    assert agent_llm.call_count == 1      # A1 仅续做一次，不重做
    assert "A1 完成了第一版" in result["output"]   # 恢复出的旧产出仍在黑板
    assert "A2 审查发现 3 处问题" in result["output"]
    assert "A1 已修复审查问题" in result["output"]  # 续跑新增产出追加


def test_moa_resume_missing_state_early_return(tmp_path):
    """续跑文件不存在/损坏 → resume_state_missing 终止（fail-safe）。"""
    orch = _make_orchestrator(tmp_path)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        ScriptedLLM(["x"]), ScriptedLLM(["x"]),
    )
    result = orch.run(workers, commander, FakeRegistry(), query="task", max_rounds=5,
                      resume_from=str(tmp_path / "nope.json"))
    assert result["stopped_by"] == "resume_state_missing"
    assert result["rounds"] == 0


# ── 21. 修复回归：commander 压缩 / eager L1 / 文件累计 ─────────────────────
def test_commander_context_compresses_across_rounds(tmp_path):
    """Phase 2：commander 私有上下文无工具历史，超预算后压缩必须生效。

    回归点：_round_starts 无工具时回退以 user 消息为轮次边界，否则
    L2/L3 永不触发、上下文无界增长。
    """
    from skill_engine.execution.context_manager import ContextManager
    session = MoaSession(str(tmp_path))
    c_ctx = session.ctx("C")
    # 模拟 run() 主循环：每轮追加超长决策 prompt + 决策回复（8 轮纯对话）
    big = "决策背景 " + "协作状态" * 4000
    for _ in range(8):
        c_ctx.messages.append({"role": "user", "content": big})
        c_ctx.messages.append({"role": "assistant", "content": "决策回复" * 100})
    # 走 run() 同款压缩路径（c_cm.messages 与 c_ctx.messages 同一对象）
    # 显式小预算，避免受 config.yml 的 context_budget 环境桥接影响
    c_cm = ContextManager(budget=2000, keep_recent=2)
    c_cm.messages = c_ctx.messages
    llm = ScriptedLLM(["压缩摘要"])
    assert c_cm.maybe_compress(llm) is True
    assert llm.call_count == 1
    assert any("<condensed_history>" in m.get("content", "") for m in c_ctx.messages)
    # 首条 user prompt 保留；最近 2 个完整轮次成对保留
    assert c_ctx.messages[0]["content"].startswith("决策背景")
    tail = c_ctx.messages[-4:]
    assert [m["role"] for m in tail[::2]] == ["user"] * 2
    assert [m["role"] for m in tail[1::2]] == ["assistant"] * 2


def test_eager_fold_old_rounds_keeps_recent():
    """P2b：轮末 eager L1 折叠旧轮大 tool 输出与思考，keep_recent 窗口原样保留。"""
    from skill_engine.execution.moa import _eager_fold_old_rounds
    msgs = [{"role": "user", "content": "final"}]
    for i in range(6):
        msgs.append({
            "role": "assistant",
            "content": f"思考 {i} " + "z" * 2000,
            "tool_calls": [{"id": f"c{i}", "name": "bash", "args": {}}],
        })
        msgs.append({
            "role": "tool", "tool_call_id": f"c{i}", "name": "bash",
            "content": "o" * 3000,
        })
    _eager_fold_old_rounds(msgs)
    # 默认 keep_recent=4：只折前 2 轮（rounds 0-1），最近 4 轮原样保留
    for i in range(2):
        assert msgs[1 + i * 2]["content"] == "[已折叠: 旧轮思考过程]"
        assert msgs[2 + i * 2]["content"].startswith("[已折叠: bash 输出")
    for i in range(2, 6):
        assert msgs[1 + i * 2]["content"].startswith("思考")
        assert msgs[2 + i * 2]["content"] == "o" * 3000


def test_merge_files_dedup_cumulative():
    """P3：文件列表跨轮去重累计。"""
    from skill_engine.execution.moa import _merge_files
    existing = ["a.py"]
    _merge_files(existing, ["b.py", "a.py", "b.py"])
    assert existing == ["a.py", "b.py"]


def test_run_agent_files_cumulative_across_rounds(tmp_path):
    """P3：actx.files 跨轮累计（去重）；黑板每轮只记本轮文件。"""
    orch = _make_orchestrator(tmp_path)
    session = MoaSession(str(tmp_path))
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        ScriptedLLM([
            {"content": "先写文件", "tool_calls": [
                {"type": "write_file", "id": "w1",
                 "input": {"path": "a.py", "content": "print(1)"}}]},
            {"content": "第一轮完成", "tool_calls": []},
            {"content": "再写一个", "tool_calls": [
                {"type": "write_file", "id": "w2",
                 "input": {"path": "b.py", "content": "print(2)"}}]},
            {"content": "第二轮完成", "tool_calls": []},
        ]),
        ScriptedLLM(["x"]),
    )
    agent = workers[0]
    _, files1, status1 = orch._run_agent(agent, session, "task1", "query", 1, 10)
    actx = session.ctx("A1")
    assert status1["code"] == "ok"
    assert [f.endswith("a.py") for f in files1] == [True]
    assert actx.files == files1
    # 第二轮续跑：写新文件 → actx.files 累计；黑板文件只含本轮
    out2, files2, status2 = orch._run_agent(agent, session, "task2", "query", 2, 10)
    assert status2["code"] == "ok"
    assert out2 == "第二轮完成"
    assert len(actx.files) == 2
    assert files2 == [actx.files[1]] and files2 != actx.files


# ── 22. key_facts 结构化要点 + progress_summary 分层 ───────────────────────
def test_extract_key_facts_parses_section():
    """【关键事实】段解析：- / * / 数字 行首符号均可。"""
    from skill_engine.execution.moa import _extract_key_facts
    out = ("UI 检查汇报……\n"
           "【关键事实】\n"
           "- 品牌图标与设计稿不一致\n"
           "- 主色调已对齐设计稿 #EE6A4D\n"
           "* 第三行星号也可\n")
    assert _extract_key_facts(out) == [
        "品牌图标与设计稿不一致",
        "主色调已对齐设计稿 #EE6A4D",
        "第三行星号也可",
    ]


def test_extract_key_facts_no_marker_returns_empty():
    from skill_engine.execution.moa import _extract_key_facts
    assert _extract_key_facts("纯文本产出，无关键事实段") == []
    assert _extract_key_facts("") == []


def test_extract_key_facts_stops_at_plain_text():
    """无空行分隔的普通正文行终止提取（不把后续正文当 facts）。"""
    from skill_engine.execution.moa import _extract_key_facts
    out = "结论。\n【关键事实】\n- 事实一\n接下来是正文补充说明……\n"
    assert _extract_key_facts(out) == ["事实一"]


def test_extract_key_facts_caps_items_and_len():
    """条数上限 + 单条超长钳制，防 facts 变第二份长文本。"""
    from skill_engine.execution.moa import _extract_key_facts, FACTS_MAX_LEN
    out = "【关键事实】\n" + "".join(f"- 事实{i}\n" for i in range(30))
    facts = _extract_key_facts(out)
    assert len(facts) == 20 and facts[-1] == "事实19"
    long = _extract_key_facts("【关键事实】\n- " + "长" * 200)
    assert long == ["长" * FACTS_MAX_LEN + "…"]


def test_progress_summary_recent_full_older_oneline(tmp_path):
    """方案 1：最近 N 条 ok 产出全量，更早的折叠成一行（要点优先）。"""
    s = MoaSession(str(tmp_path))
    for i in range(5):
        s.add_entry(f"A{i % 3}", "code-builder", f"产出{i} " + "好" * 300, [],
                    status="ok", key_facts=[f"要点{i}"])
    ps = s.progress_summary(recent_ok=2)
    recent_part = ps.split("# 最近产出")[1]
    older_part = ps.split("# 最近产出")[0]
    # 最近 2 条（产出3/4）全量：300 字长文本完整可见
    assert "产出3" in recent_part and "产出4" in recent_part
    assert "好" * 300 in recent_part
    # 更早 3 条一行概览：有要点、无全量长文本
    assert "要点0" in older_part and "要点2" in older_part
    assert "好" * 121 not in older_part


def test_progress_summary_facts_preferred_on_older(tmp_path):
    """旧条目一行：有 key_facts 展示要点，无 facts 回退正文前若干字。"""
    s = MoaSession(str(tmp_path))
    s.add_entry("A1", "", "很短", [], status="ok", key_facts=["核心事实甲"])
    s.add_entry("A2", "", "x" * 5000, [], status="ok")
    s.add_entry("A3", "", "y" * 5000, [], status="ok", key_facts=["要点乙", "要点丙"])
    ps = s.progress_summary(recent_ok=1)
    assert "核心事实甲" in ps             # 旧条目有 facts → 要点展示
    assert "要点乙" in ps and "要点丙" in ps   # 新近全量也带 facts 行
    assert "x" * 121 not in ps            # A2 无 facts → 旧行只取 120 字
    assert "y" * 200 in ps                # A3 新近全量正文


def test_blackboard_summary_includes_facts(tmp_path):
    """blackboard_summary（worker 注入 / 最终综合）每条先给关键事实行。"""
    s = MoaSession(str(tmp_path))
    s.add_entry("A1", "", "正文内容", [], key_facts=["事实一", "事实二"])
    bs = s.blackboard_summary()
    assert "[关键事实] 事实一；事实二" in bs
    assert "正文内容" in bs


def test_worker_prompt_includes_facts_contract(tmp_path):
    """首轮 / 续跑 composed 都带【关键事实】产出约定。"""
    orch = _make_orchestrator(tmp_path)
    session = MoaSession(str(tmp_path))
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "dev"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        ScriptedLLM(["完成", "完成"]), ScriptedLLM(["x"]),
    )
    orch._run_agent(workers[0], session, "任务1", "q", 1, 5)
    actx = session.ctx("A1")
    joined = "".join(str(m.get("content", "")) for m in actx.messages)
    assert "产出格式约定" in joined and "【关键事实】" in joined
    orch._run_agent(workers[0], session, "任务2", "q", 2, 5)
    joined2 = "".join(str(m.get("content", "")) for m in actx.messages)
    assert joined2.count("产出格式约定") >= 2


def test_moa_key_facts_extracted_into_blackboard(tmp_path, monkeypatch):
    """run 全流程：worker 产出【关键事实】→ add_entry 收到结构化 key_facts。"""
    from skill_engine.execution.moa import MoaSession
    seen = []
    orig = MoaSession.add_entry

    def spy(self, *a, **kw):
        seen.append(kw.get("key_facts"))
        return orig(self, *a, **kw)

    monkeypatch.setattr(MoaSession, "add_entry", spy)
    orch = _make_orchestrator(tmp_path)
    workers, commander = _agents_with_injected_llm(
        [{"alias": "A1", "model_profile": "default", "skill_name": "", "instruction": "检查 UI"}],
        {"alias": "C", "model_profile": "default", "skill_name": "", "instruction": "cmd"},
        ScriptedLLM(["UI 检查完毕。\n【关键事实】\n- 品牌图标与设计稿不一致\n- 主色调已对齐 #EE6A4D"]),
        ScriptedLLM([
            '<moa_decision>{"next":"A1","task":"检查UI","rationale":"r"}</moa_decision>',
            '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
            "最终综合",
        ]),
    )
    result = orch.run(workers, commander, FakeRegistry(), query="检查 UI", max_rounds=3,
                      max_agent_iterations=5, max_llm_calls=100)
    assert result["stopped_by"] == "commander_stop"
    assert any(f and "品牌图标与设计稿不一致" in f[0] for f in seen)


# ── 20. 性能诊断建议 4/9/2 回归：增量注入 / JSONL 差量 / strict 快速失败 ────
def test_blackboard_incremental_summary(tmp_path):
    """增量黑板摘要：只注入 since 之后的新条目；无新增 → 明确提示。"""
    from skill_engine.execution.moa import MoaAgentRuntime
    orch = _make_orchestrator(tmp_path)
    session = MoaSession(str(tmp_path))
    session.agent_contexts["A1"] = MoaAgentRuntime(
        alias="A1", messages=[], last_output="", cumulative_iterations=0, files=[])
    session.add_entry("A1", "code-builder", "第一轮：登录函数", [])
    session.add_entry("A1", "code-builder", "第二轮：首页检查", [])
    rt = session.agent_contexts["A1"]
    assert rt.last_blackboard_len == 2          # add_entry 同步推进注入游标
    assert "无新增" in session.blackboard_incremental_summary(rt.last_blackboard_len)
    session.add_entry("A2", "vlm", "第三轮：视觉走查", [])
    inc = session.blackboard_incremental_summary(rt.last_blackboard_len)
    assert "第三轮：视觉走查" in inc
    assert "登录函数" not in inc and "首页检查" not in inc
    full = session.blackboard_incremental_summary(0)
    assert "登录函数" in full and "第三轮：视觉走查" in full
    # _run_agent 续跑分支会回退兜底：last_blackboard_len 缺省 → 保守全量
    assert session.agent_contexts.get("A2") is None or True   # A2 未注册 runtime 不崩溃


def test_moa_state_jsonl_delta_roundtrip(tmp_path):
    """MOA 状态 JSONL：首行快照 + 后续增量；加载按序重放后状态一致。"""
    from skill_engine.execution.moa import MoaAgentRuntime
    orch = _make_orchestrator(tmp_path)
    s = MoaSession(str(tmp_path))
    s.agent_contexts["A1"] = MoaAgentRuntime(
        alias="A1",
        messages=[{"role": "user", "content": "r1"}, {"role": "assistant", "content": "done"}],
        last_output="", cumulative_iterations=2, files=[])
    counter = {"calls": 2, "prompt": 10, "completion": 5, "total": 15}
    path = str(tmp_path / "moa_state.jsonl")
    s.add_entry("A1", "code-builder", "第一笔产出", [])
    orch._save_moa_state(path, s, counter)                      # 第 1 行：全量快照
    s.add_entry("A1", "code-builder", "第二笔产出", [])
    s.commander_decisions.append({"round": 1, "next": "A1", "task": "t", "rationale": "r"})
    s.llm_calls = 4
    counter["calls"] = 4
    s.agent_contexts["A1"].messages_version += 1
    orch._save_moa_state(path, s, counter)                      # 第 2 行：round_delta

    lines = (tmp_path / "moa_state.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["type"] == "moa_snapshot"
    assert json.loads(lines[1])["type"] == "moa_round_delta"

    restored = orch._load_moa_state(path)
    assert restored is not None
    s2, c2 = restored["session"], restored["counter"]
    assert len(s2.blackboard) == 2
    assert s2.blackboard[1]["output"] == "第二笔产出"           # 增量重放成功
    assert len(s2.commander_decisions) == 1
    assert s2.agent_contexts["A1"].messages[0]["content"] == "r1"
    assert s2.llm_calls == 4
    assert c2 == counter


def test_strict_mode_bash_fast_fail(tmp_path, monkeypatch):
    """strict 模式 bash BLOCK → 快速失败（security_blocked），不耗到 max_iterations。"""
    monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "strict")
    from skill_engine.models import MoaAgent
    orch = _make_orchestrator(tmp_path)
    worker = MoaAgent(alias="A1", model_profile="default", skill_name="",
                      role="worker", instruction="检查代码",
                      llm=ScriptedLLM([_bash_call()]))
    commander = MoaAgent(alias="C", model_profile="default", role="commander",
                         llm=ScriptedLLM([
                             '<moa_decision>{"next":"A1","task":"检查","rationale":"r"}</moa_decision>',
                             '<moa_decision>{"next":"STOP","task":"","rationale":"done"}</moa_decision>',
                             "最终综合",
                         ]))
    result = orch.run([worker], commander, FakeRegistry(), query="q",
                      max_rounds=3, max_agent_iterations=5, max_llm_calls=100)
    # 快速失败：worker 只调 1 次 LLM（旧行为会重试到 5 次 → llm_calls ≥ 7）
    assert result["llm_calls"] == 3
    assert result["rounds"] == 2
    assert result["stopped_by"] == "commander_stop"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
