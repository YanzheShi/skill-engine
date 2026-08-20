"""多轮会话（Session/REPL）模式测试

覆盖：
- 续轮上下文：首轮结束后 messages 进 session，次轮 run 能续上下文
- ask_user 暂停：LLM 调 ask_user，引擎暂停读输入，回答回灌后继续当前轮
- stop 工具：LLM 调 stop = 子任务完成，run 返回 REPL 等下条指令
- /exit 退出：REPL 干净结束
- 落盘往返：exit 后状态文件存在，resume_from 能载入历史续接
- 空输入（直接回车）：追加续写指令而非重跑原始 query
- Ctrl+C 中断：stopped_by=interrupted 且状态已落盘
- max_iterations：达到上限时把结果直接返回给调用方
- 多轮 ask_user：同一轮内连续多次提问都能拿到对应回答
- 无 human_in_loop 属性的 skill：session 模式不依赖该字段
- 快照跨轮保持：整个 session 共用一个 FileSnapshot（可回滚到会话起点）
"""

import pytest
from unittest.mock import MagicMock


def _make_skill(tmp_path):
    from skill_engine.models import Skill, SkillMetadata

    return Skill(
        metadata=SkillMetadata(name="demo", description="d"),
        body="",
        directory=str(tmp_path),
    )


def _make_registry(skill):
    class _Reg:
        def load_skill(self, name):
            return skill
    return _Reg()


def _make_plan(skill):
    from skill_engine.models import MatchResult

    class _Plan:
        primary = MatchResult(skill=skill, score=1.0, method="name", arguments={})
        selections = []
        score = 1.0
        method = "name"
        reason = None
        uncertain = False
    return _Plan()


class _FakeLLM:
    """按序返回预设响应；记录每次 invoke 收到的 messages。

    响应用 dict 格式 {"content": ..., "tool_calls": [{"name","args","id"}]}，
    由 tool_dispatch.parse_tool_calls 归一化为 {"id","type","input"}
    （name→type、args→input，见其 docstring 的"新格式"分支）。
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.last_tools = None
        self._idx = 0

    def bind_tools(self, tools):
        self.last_tools = tools
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return {"content": "[done]"}


class _FakeHumanIO:
    def __init__(self, queue):
        self.queue = list(queue)
        self.reads = []

    def emit(self, text):
        self.reads.append(("emit", text))

    def read(self, prompt=None):
        v = self.queue.pop(0) if self.queue else "/exit"
        self.reads.append(("read", v))
        return v


def _run_session(tmp_path, responses, io_queue, resume_from=None, state_path=None,
                 max_iterations=5, skill=None, llm=None, query="do X", assembler=None):
    from skill_engine.execution.runner import Runner

    skill = skill or _make_skill(tmp_path)
    registry = _make_registry(skill)
    plan = _make_plan(skill)
    executor = MagicMock()
    if assembler is None:
        assembler = MagicMock()
        assembler.assemble.return_value = "FINAL_PROMPT"
    runner = Runner(assembler, executor)
    llm = llm or _FakeLLM(responses)
    hio = _FakeHumanIO(io_queue)
    sp = state_path or str(tmp_path / "session.json")
    result = runner.run_repl(
        plan, registry, query=query, llm=llm,
        max_iterations=max_iterations, working_root=str(tmp_path),
        state_path=sp, resume_from=resume_from, human_io=hio,
    )
    return result, llm, hio


def test_continuation_across_turns(tmp_path):
    responses = [
        {"content": "turn1 plan"},
        {"content": "turn2 done"},
    ]
    result, llm, hio = _run_session(tmp_path, responses, io_queue=["refactor Y", "/exit"])

    # LLM 被调用两次（两个子任务）
    assert len(llm.calls) == 2
    # 第二轮 invoke 的 messages 应包含第一轮的助手文本（上下文续接）
    second_msgs = llm.calls[1]
    joined = " ".join(str(m.get("content", "")) for m in second_msgs)
    assert "turn1 plan" in joined, "第二轮未继承第一轮上下文"
    # 最终因 /exit 退出
    assert result.get("stopped_by") == "user_exit"


def test_ask_user_pauses_for_input(tmp_path):
    responses = [
        {"content": "", "tool_calls": [
            {"name": "ask_user", "args": {"question": "which?"}, "id": "c1"}]},
        {"content": "did A"},
    ]
    result, llm, hio = _run_session(tmp_path, responses, io_queue=["choose A", "/exit"])

    # ask_user 工具确实被注入并出现在 bind_tools 中
    names = {t.name for t in llm.last_tools}
    assert "ask_user" in names
    # 用户回答被 ask_user 读取（轮内暂停）
    read_values = [v for _, v in hio.reads]
    assert "choose A" in read_values
    # 最终输出是 ask_user 之后的续写
    assert "did A" in (result.get("output") or "")
    assert result.get("stopped_by") == "user_exit"


def test_stop_tool_ends_subtask(tmp_path):
    responses = [
        {"content": "", "tool_calls": [
            {"name": "stop", "args": {"reason": "finished"}, "id": "s1"}]},
        {"content": "after stop"},
    ]
    result, llm, hio = _run_session(tmp_path, responses, io_queue=["/exit"])

    names = {t.name for t in llm.last_tools}
    assert "stop" in names
    # stop 后 REPL 继续等待下条指令，用户 /exit 退出
    assert result.get("stopped_by") == "user_exit"


def test_exit_immediately(tmp_path):
    responses = [{"content": "turn1 plan"}]
    result, llm, hio = _run_session(tmp_path, responses, io_queue=["/exit"])
    assert result.get("stopped_by") == "user_exit"
    # 只执行了一轮
    assert len(llm.calls) == 1


def test_session_state_persisted_and_resumable(tmp_path):
    sp = str(tmp_path / "sess.json")
    # 第一轮：执行后 /exit
    r1, llm1, hio1 = _run_session(tmp_path, [{"content": "turn1 plan"}], io_queue=["/exit"], state_path=sp)
    assert r1.get("stopped_by") == "user_exit"
    # 状态文件应已落盘且含历史
    import json
    from pathlib import Path
    assert Path(sp).exists()
    # JSONL（append-only）：取最后一行的完整快照
    last_line = [ln for ln in Path(sp).read_text(encoding="utf-8").splitlines() if ln.strip()][-1]
    saved = json.loads(last_line)
    assert any("turn1 plan" in str(m.get("content", "")) for m in saved["messages"])

    # 第二轮：resume_from 续接（先给一条续写指令，再 /exit）
    r2, llm2, hio2 = _run_session(
        tmp_path, [{"content": "turn2 after resume"}], io_queue=["continue from history", "/exit"],
        resume_from=sp, state_path=str(tmp_path / "sess2.json"),
    )
    assert r2.get("stopped_by") == "user_exit"
    # resume 后首轮 invoke 应能看到第一轮的历史（续接生效）
    assert len(llm2.calls) >= 1
    resumed_joined = " ".join(str(m.get("content", "")) for m in llm2.calls[0])
    assert "turn1 plan" in resumed_joined, "resume 未载入历史"


def test_run_repl_accepts_matchplan_selectedskill(tmp_path):
    """真实 CLI 路径：router 返回 MatchPlan，其 primary 是 SelectedSkill（只有 .name），
    而非旧 API 的 MatchResult。run_repl 必须能解析并跑通，不能 AttributeError。"""
    from skill_engine.models import SelectedSkill
    from skill_engine.execution.runner import Runner
    from skill_engine.execution.assembler import Assembler
    from skill_engine.execution.executor import Executor
    from unittest.mock import MagicMock

    skill = _make_skill(tmp_path)
    registry = _make_registry(skill)

    class _Plan:
        # 真实 MatchPlan 结构：primary 为 SelectedSkill（无 .skill 属性）
        primary = SelectedSkill(name="demo")
        selections = []
        score = 0.95
        method = "keyword"
        reason = None
        uncertain = False

    executor = MagicMock()
    assembler = MagicMock()
    assembler.assemble.return_value = "FINAL_PROMPT"
    runner = Runner(assembler, executor)
    llm = _FakeLLM([{"content": "turn1 plan"}])
    hio = _FakeHumanIO(["/exit"])
    sp = str(tmp_path / "sel.json")
    result = runner.run_repl(
        _Plan(), registry, query="do X", llm=llm,
        max_iterations=5, working_root=str(tmp_path),
        state_path=sp, human_io=hio,
    )
    # SelectedSkill 分支解析成功，首轮 LLM 被调用且正常退出
    assert len(llm.calls) == 1
    assert result.get("stopped_by") == "user_exit"


# ---------------- 边界与回归覆盖 ----------------


def test_empty_input_continues_instead_of_rerunning_query(tmp_path):
    """空输入（直接回车）应追加「继续」指令让 LLM 基于历史续写，
    而不是回退成重跑原始 query（否则第一轮请求会被重做一遍）。"""
    from skill_engine.execution.runner import Runner

    responses = [{"content": "turn1 plan"}, {"content": "turn2 continued"}]
    result, llm, hio = _run_session(tmp_path, responses, io_queue=["", "/exit"])

    assert len(llm.calls) == 2
    second = llm.calls[1]
    user_msgs = [m for m in second if m.get("role") == "user"]
    assert user_msgs[-1]["content"] == Runner._CONTINUE_HINT, "空输入未追加续写指令"
    # 原始 query 编译出的 FINAL_PROMPT 只应出现一次（没有被重跑）
    # 用「包含」而非「相等」：首轮 prompt 前会拼上 <env> 环境头
    assert sum(1 for m in second if "FINAL_PROMPT" in str(m.get("content", ""))) == 1
    assert result.get("stopped_by") == "user_exit"


def test_keyboard_interrupt_marks_interrupted_and_persists(tmp_path):
    """Ctrl+C 中断：run_repl 捕获后返回 interrupted，且状态已在 finally 中落盘。"""
    from pathlib import Path

    class _BoomLLM(_FakeLLM):
        def invoke(self, messages):
            self.calls.append(messages)
            raise KeyboardInterrupt()

    sp = str(tmp_path / "boom.json")
    result, llm, hio = _run_session(
        tmp_path, [], io_queue=["/exit"], state_path=sp, llm=_BoomLLM([]))

    assert result.get("stopped_by") == "interrupted"
    assert Path(sp).exists(), "中断时状态未落盘，无法 --resume-from 续接"


def test_max_iterations_does_not_exit_session(tmp_path):
    """轮内一直调工具直到达到 max_iterations：本轮被中断，但会话不退出、继续等下条指令
    （d8da353 引入的"达到最大循环后不退出会话"特性）。用户随后可用 /exit 正常结束。"""
    loop_resp = {"content": "", "tool_calls": [
        {"name": "no_such_tool", "args": {}, "id": "x"}]}
    result, llm, hio = _run_session(
        tmp_path, [loop_resp, loop_resp], io_queue=["/exit"], max_iterations=2)

    # 内层被 max_iterations 限制，只跑了 2 轮工具循环（不会无限循环）
    assert len(llm.calls) == 2
    # 达到 max_iterations 后本轮中断但会话继续，最终由用户 /exit 结束
    # （若旧行为"直接以 max_iterations 退出"，stopped_by 会是 "max_iterations" 而非 "user_exit"）
    assert result.get("stopped_by") == "user_exit"


def test_multiple_ask_user_in_one_turn(tmp_path):
    """同一轮内连续两次 ask_user：两个问题都被 emit，两个回答都被读入并回灌。"""
    responses = [
        {"content": "", "tool_calls": [
            {"name": "ask_user", "args": {"question": "q1?"}, "id": "a1"}]},
        {"content": "", "tool_calls": [
            {"name": "ask_user", "args": {"question": "q2?"}, "id": "a2"}]},
        {"content": "final answer"},
    ]
    result, llm, hio = _run_session(
        tmp_path, responses, io_queue=["ans1", "ans2", "/exit"])

    emitted = [v for kind, v in hio.reads if kind == "emit"]
    read_vals = [v for kind, v in hio.reads if kind == "read"]
    assert "q1?" in emitted and "q2?" in emitted
    assert "ans1" in read_vals and "ans2" in read_vals
    assert "final answer" in (result.get("output") or "")
    assert result.get("stopped_by") == "user_exit"


def test_skill_without_human_in_loop_attribute(tmp_path):
    """session 模式在轮末直接交还控制权，不应依赖 skill.metadata.human_in_loop。
    用连该属性都没有的 skill 元数据跑通，防止未来加回隐式依赖。"""
    from skill_engine.models import SelectedSkill
    from skill_engine.execution.runner import Runner

    skill = _make_skill(tmp_path)

    class _MetaNoHIL:
        """转发到真实 metadata，但 human_in_loop 一律 AttributeError。"""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, k):
            if k == "human_in_loop":
                raise AttributeError("human_in_loop")
            return getattr(self._inner, k)

    # 直接换掉 pydantic 实例的字段值（绕过校验），skill 仍是合法 Skill 实例，
    # 这样 MatchResult(skill=skill) 不会因类型校验失败。
    skill.__dict__["metadata"] = _MetaNoHIL(skill.metadata)
    with pytest.raises(AttributeError):
        _ = skill.metadata.human_in_loop  # 前提：该属性确实不存在
    proxy = skill

    class _Plan:
        primary = SelectedSkill(name="demo")
        selections = []
        score = 1.0
        method = "name"
        reason = None
        uncertain = False

    runner = Runner(MagicMock(), MagicMock())
    runner.assembler.assemble.return_value = "FINAL_PROMPT"
    llm = _FakeLLM([{"content": "ok"}])
    hio = _FakeHumanIO(["/exit"])
    result = runner.run_repl(
        _Plan(), _make_registry(proxy), query="do X", llm=llm,
        max_iterations=5, working_root=str(tmp_path),
        state_path=str(tmp_path / "nohil.json"), human_io=hio,
    )
    assert len(llm.calls) == 1
    assert result.get("stopped_by") == "user_exit"


def test_snapshot_shared_across_turns(tmp_path, monkeypatch):
    """会话内所有轮共用同一 FileSnapshot 实例，restore_file 才能回滚到会话起点。"""
    from skill_engine.execution import tool_dispatch as td
    from skill_engine.execution.snapshot import FileSnapshot

    seen = []
    orig_run = td.ToolDispatchRunner.run

    def _spy(self, match_result, llm, *args, **kwargs):
        seen.append(kwargs.get("snapshot"))
        return orig_run(self, match_result, llm, *args, **kwargs)

    monkeypatch.setattr(td.ToolDispatchRunner, "run", _spy)
    _run_session(tmp_path, [{"content": "t1"}, {"content": "t2"}],
                 io_queue=["next", "/exit"])

    assert len(seen) == 2
    assert all(isinstance(s, FileSnapshot) for s in seen), "run() 未收到会话级快照"
    assert seen[0] is seen[1], "各轮快照实例不同，检查点会被覆盖"


def test_filesnapshot_shared_instance_keeps_session_start_checkpoint(tmp_path):
    """回归：共用实例时检查点停留在会话起点；每轮新建实例则被覆盖成上一轮结果。"""
    from skill_engine.execution.snapshot import FileSnapshot

    f = tmp_path / "a.txt"

    # 共用实例（修复后的 session 行为）：回滚拿到会话起点 v0
    f.write_text("v0", encoding="utf-8")
    shared = FileSnapshot(tmp_path)
    shared.record(f, "v0")
    f.write_text("v1", encoding="utf-8")
    shared.record(f, "v1")          # 同实例 → 已记录，不覆盖
    f.write_text("v2", encoding="utf-8")
    ok, _ = shared.restore(f)
    assert ok and f.read_text(encoding="utf-8") == "v0"

    # 对照：每轮新建实例（修复前的行为）→ 检查点被覆盖成 v1
    f.write_text("v0", encoding="utf-8")
    FileSnapshot(tmp_path).record(f, "v0")
    f.write_text("v1", encoding="utf-8")
    FileSnapshot(tmp_path).record(f, "v1")
    f.write_text("v2", encoding="utf-8")
    ok2, _ = FileSnapshot(tmp_path).restore(f)
    assert ok2 and f.read_text(encoding="utf-8") == "v1"


def test_state_records_session_mode_flag(tmp_path):
    """落盘状态需带 session_mode 标记，供 run --resume-from 侧识别来源。"""
    import json
    from pathlib import Path

    sp = str(tmp_path / "flag.json")
    _run_session(tmp_path, [{"content": "t1"}], io_queue=["/exit"], state_path=sp)
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved.get("session_mode") is True


# ---------------------------------------------------------------- 无初始 query


def _make_rich_skill(tmp_path):
    """带完整 frontmatter 的 skill，用于校验提示渲染。"""
    from skill_engine.models import Skill, SkillMetadata

    return Skill(
        metadata=SkillMetadata(
            name="code-builder",
            description="按需求增改代码并补测试",
            when_to_use="需要在既有项目里新增函数/模块时",
            argument_hint="给 src/demo/utils.py 加一个 greet(name) 函数",
            arguments=["target", "style"],
            allowed_tools=["bash", "write_file"],
        ),
        body="",
        directory=str(tmp_path),
    )


def test_session_without_query_shows_hint_and_waits(tmp_path):
    """不传初始 query：先打印 skill 用法提示，不自动起轮，等用户第一条指令。"""
    import io
    import sys
    import contextlib
    _old = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        skill = _make_rich_skill(tmp_path)

        # 真实 Assembler 会把用户指令经 $ARGUMENTS 替换进 final_prompt；
        # 用等价 mock 模拟（旧行为把裸指令当历史，新行为走 $ARGUMENTS 注入）
        mock_assembler = MagicMock()
        mock_assembler.assemble.side_effect = (
            lambda skill_, args_, plain_text=False:
            "FINAL_PROMPT|" + (args_ or {}).get("$ARGUMENTS", "")
        )
        result, llm, hio = _run_session(
            tmp_path, [{"content": "done"}], io_queue=["加一个 greet 函数", "/exit"],
            skill=skill, query="", assembler=mock_assembler,
        )
    finally:
        sys.stdout = _old
    out = buf.getvalue()

    # 提示内容：名称 / 用途 / 适用 / 参数 / 命名参数 / 会话命令
    assert "code-builder" in out
    assert "按需求增改代码并补测试" in out
    assert "需要在既有项目里新增函数/模块时" in out
    assert "--target=<值>" in out and "--style=<值>" in out
    assert "/exit" in out

    # 首轮不自动执行：LLM 只被用户那条指令触发一次
    assert len(llm.calls) == 1
    first_msgs = llm.calls[0]
    joined = " ".join(str(m.get("content", "")) for m in first_msgs)
    assert "加一个 greet 函数" in joined
    assert result.get("stopped_by") == "user_exit"


def test_session_without_query_empty_input_reprompts(tmp_path):
    """无 query 且无历史时直接回车：重新提示，不空跑一轮 LLM。"""
    import io
    import sys
    _old = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    try:
        result, llm, hio = _run_session(
            tmp_path, [{"content": "done"}], io_queue=["", "", "/exit"], query="",
        )
    finally:
        sys.stdout = _old
    out = buf.getvalue()

    assert out.count("请输入一条指令") == 2
    assert len(llm.calls) == 0, "无历史的空输入不应触发 LLM"
    assert result.get("stopped_by") == "user_exit"


def test_skill_hint_tolerates_missing_fields(tmp_path):
    """frontmatter 只填必填项时，提示仍能渲染（缺字段只是少一行）。"""
    from skill_engine.execution.runner import Runner

    text = Runner._format_skill_hint(_make_skill(tmp_path))

    # 不硬判 "demo" —— name 走 pyfiglet ASCII art 标题区，不会以原样
    # 字符串出现在纯文本里。这里只校验：渲染不出错、必要 UI 文案在。
    assert "/exit" in text
    assert "用途" in text
    assert "命名参数" not in text  # 未配置命名参数时不渲染该行


def test_first_turn_injects_skill_prompt(tmp_path):
    """回归：交互式 session 首轮必须把组装好的 skill 指令（final_prompt）
    传给 LLM，而不是只传用户裸指令导致模型看不到技能上下文。

    复现路径：没有初始 query、也没有续接历史时，首条用户指令曾以
    initial_messages=[裸指令] 起轮，tool_dispatch 直接丢弃组装好的
    final_prompt → 只产生工具调用、看不到思考/指令。
    """
    from skill_engine.execution.runner import Runner

    skill = _make_rich_skill(tmp_path)
    registry = _make_registry(skill)

    # final_prompt 会被解析拼进 content；必须能取到 Plan 内部跑的路径
    executor = MagicMock()
    mock_assembler = MagicMock()
    mock_assembler.assemble.return_value = "FINAL_SKILL_PROMPT"
    runner = Runner(mock_assembler, executor)
    llm = _FakeLLM([{"content": "ok"}])
    hio = _FakeHumanIO(["帮我加个函数", "/exit"])
    result = runner.run_repl(
        _make_plan(skill), registry, query="", llm=llm,
        max_iterations=5, working_root=str(tmp_path),
        state_path=str(tmp_path / "fresh.json"), human_io=hio,
    )

    assert len(llm.calls) == 1
    first = llm.calls[0]
    joined = " ".join(str(m.get("content", "")) for m in first)
    assert "FINAL_SKILL_PROMPT" in joined, "首轮未注入组装好的 skill 指令"
    assert result.get("stopped_by") == "user_exit"


def test_run_plan_multi_no_selected_score(tmp_path):
    """回归：multi 协同 run_plan 不再因 SelectedSkill 缺 score 字段而崩溃。

    之前 runner.run_plan 访问 selected.score（SelectedSkill 只有
    name/role/args_override）→ AttributeError，多 skill 协同完全不可用。
    修复后应以 plan.score 兜底，整体返回 all_outputs。
    """
    from unittest.mock import patch
    from skill_engine.models import SelectedSkill
    from skill_engine.execution.runner import Runner

    class _Plan:
        mode = "multi"
        selections = [SelectedSkill(name="demo")]
        primary = SelectedSkill(name="demo")
        score = 0.9
        method = "llm"
        reason = ""
        uncertain = False

    skill = _make_skill(tmp_path)
    registry = _make_registry(skill)
    executor = MagicMock()
    assembler = MagicMock()
    assembler.assemble.return_value = "FINAL_MULTI"
    llm = _FakeLLM([{"content": "run1"}])
    runner = Runner(assembler, executor)

    def _noop_run(mr, *a, **kw):
        return {"output": "ran", "iterations": 1, "stopped_by": "tool_stop",
                "steps": [], "files_created": [], "skill_name": mr.skill.metadata.name}

    with patch.object(runner, "run", MagicMock(side_effect=_noop_run)) as mock_run:
        result = runner.run_plan(
            _Plan(), registry, query="do multi", llm=llm,
            tool_dispatch=llm, max_iterations=2, working_root=str(tmp_path),
        )

    assert mock_run.called, "multi 分支应逐个 skill 调 run()"
    assert result.get("all_outputs"), "修复后应产出 all_outputs"
    assert result["all_outputs"][0]["skill_name"] == "demo"

