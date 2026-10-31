"""长期记忆 P0/P1 测试。

覆盖：
- 记录写入/读取/更新/遗忘/去重；
- FTS 检索、敏感度、时间有效性、历史查询；
- PROFILE/NOW/PROJECT 视图；
- 实体/资源解析（含缺失路径/歧义）；
- 项目初始化；
- REPL / CLI / Agent 工具；
- 注入块的保存与轨迹剥离。
"""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handlers.memory import (
    MemoryForgetHandler,
    MemoryGetHandler,
    MemorySearchHandler,
    MemoryWriteHandler,
    ResourceResolveHandler,
)
from skill_engine.memory.models import MemoryRecord
from skill_engine.memory.redact import (
    contains_secret,
    redact_for_trace,
    strip_memory_from_messages,
    strip_memory_from_state,
)
from skill_engine.memory.repl import handle_repl_command
from skill_engine.memory.service import MemoryService
from skill_engine.memory.settings import MemoryConfig


@pytest.fixture
def svc(tmp_path):
    cfg = MemoryConfig(
        enabled=True,
        user_store=tmp_path / "user",
        fts_enabled=True,
    )
    service = MemoryService(working_root=tmp_path, config=cfg)
    yield service
    service.close()


def _ctx(tmp_path, svc):
    return ToolContext(
        executor=None,
        base_dir=tmp_path,
        skill=None,
        file_tracker=None,
        snapshot=None,
        memory=svc,
    )


# ---------------- store / search / views ----------------

def test_add_search_and_profile_view(svc):
    record = svc.add(
        type="preference",
        title="汇报偏好",
        body="一屏密度，大数字",
        scope="user",
        pinned=True,
    )
    hits = svc.search("汇报", top_k=3)
    assert hits and hits[0].record.id == record.id
    assert Path(record.path).exists()

    context = svc.render_context()
    assert "<profile>" in context and "</profile>" in context
    assert "汇报偏好" in context
    assert "memory_context" in context


def test_resource_resolution_and_missing_path(svc, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    svc.add(
        type="resource", title="笔记目录", body="只存指针", scope="user",
        data={"path": str(notes), "aliases": ["笔记", "面经"]}, pinned=True,
    )
    result = svc.resolve_resource("笔记")
    assert result["status"] == "ok"
    assert result["candidate"]["exists"] is True
    # 别名/路径应进入 FTS，而不只是精确解析
    assert any(h.record.id == result["candidate"]["id"] for h in svc.search("面经"))

    svc.add(
        type="resource", title="不存在的目录", body="",
        scope="user", data={"path": str(tmp_path / "missing"), "aliases": ["缺失"]},
    )
    missing = svc.resolve_resource("缺失")
    assert missing["status"] == "missing_path"


def test_resource_ambiguity(svc, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    svc.add(type="resource", title="A", body="", scope="user",
            data={"path": str(tmp_path / "a"), "aliases": ["notes"]})
    svc.add(type="resource", title="B", body="", scope="user",
            data={"path": str(tmp_path / "b"), "aliases": ["notes"]})
    result = svc.resolve_resource("notes")
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2


def test_entity_resolution(svc):
    svc.add(
        type="organization", title="示例公司", body="",
        scope="user", data={"aliases": ["示例"]}, pinned=True,
    )
    result = svc.resolve_entity("示例")
    assert result["status"] == "ok"
    assert result["candidates"][0]["title"] == "示例公司"

    svc.add(
        type="term", title="示例术语", body="",
        scope="user", data={"aliases": ["示例别名"], "definition": "示例别名"},
    )
    term = svc.resolve_entity("示例别名")
    assert term["status"] == "ok"
    assert term["candidates"][0]["title"] == "示例术语"
    assert any(h.record.title == "示例术语" for h in svc.search("示例别名"))


def test_sensitivity_filtering(svc):
    record = svc.add(
        type="fact", title="隐私事实", body="L3 内容",
        scope="user", sensitivity="L3",
    )
    assert svc.get(record.id) is None
    assert svc.get(record.id, include_private=True) is not None
    assert svc.search("隐私") == []
    assert svc.search("隐私", include_private=True)
    assert "隐私事实" not in svc.render_context()


def test_ascii_search_is_case_insensitive(svc):
    svc.add(type="fact", title="Demo Project", body="SQLite FTS5", scope="project")
    assert svc.search("demo")
    assert svc.search("sqlite")


def test_state_ttl_and_as_of(svc):
    current = svc.add(type="state", title="进行中", body="", scope="user")
    assert current.valid_until  # state 自动带 TTL

    svc.add(
        type="state", title="旧状态", body="",
        scope="user", valid_until="2020-01-02",
    )
    assert svc.search("旧状态") == []
    old = svc.search("旧状态", as_of="2020-01-01", include_history=True)
    assert old


def test_phase_lifecycle(svc):
    phase = svc.phase_start("测试阶段")
    assert "测试阶段" in svc.render_now()

    ended = svc.phase_end(phase.id)
    assert ended is not None and ended.status == "archived"
    assert "测试阶段" not in svc.render_now()
    assert svc.search("测试阶段", include_history=True)


def test_record_id_path_traversal_rejected(svc):
    assert svc.get("../config") is None
    assert svc.forget("../config") is False
    with pytest.raises(ValueError):
        svc.user_store.save(
            MemoryRecord(id="../escape", type="fact", title="x")
        )


def test_update_forget_and_dedupe(svc):
    first = svc.add(type="fact", title="事实", body="旧内容", scope="project")
    duplicate = svc.add(type="fact", title="事实", body="旧内容", scope="project")
    assert duplicate.id == first.id

    updated = svc.update(first.id, body="新内容")
    assert updated is not None and updated.body == "新内容"
    assert svc.forget(first.id) is True
    assert svc.get(first.id) is None
    assert svc.forget(first.id) is False


def test_project_init(svc, tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n\nA demo project.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndescription = "demo project"\n', encoding="utf-8"
    )
    candidates = svc.project_init(dry_run=True)
    assert len(candidates) >= 3
    saved = svc.project_init(dry_run=False)
    assert saved
    assert any(r.type == "project" for r in svc.list_records("project"))


# ---------------- redact ----------------

def test_redact_helpers():
    messages = [
        {"role": "user", "content": "<memory_context>secret</memory_context> 保留指令"},
        {"role": "user", "content": "<memory_context>only</memory_context>"},
    ]
    out = strip_memory_from_messages(messages)
    assert len(out) == 1
    assert out[0]["content"] == "保留指令"

    state = {"messages": messages, "iterations": 1}
    safe = strip_memory_from_state(state)
    assert len(safe["messages"]) == 1
    assert state["messages"][0]["content"].startswith("<memory_context>")

    assert contains_secret("sk-" + "a" * 24)
    assert "sk-" not in redact_for_trace("sk-" + "a" * 24)
    assert "<memory_context>" not in redact_for_trace(
        "<memory_context>x</memory_context> hi"
    )
    assert redact_for_trace('{"sensitivity": "L3", "body": "secret"}') == (
        "[L3 memory omitted from trace]"
    )


# ---------------- Agent tools ----------------

def test_agent_tool_handlers(tmp_path, svc):
    ctx = _ctx(tmp_path, svc)

    write = MemoryWriteHandler().execute({
        "id": "w1",
        "input": {
            "type": "fact", "title": "工具写入", "body": "内容",
            "scope": "project", "domain": "coding",
        },
    }, ctx)
    payload = json.loads(write.content)
    assert payload["status"] == "ok"

    search = MemorySearchHandler().execute({
        "id": "s1", "input": {"query": "工具写入"},
    }, ctx)
    assert json.loads(search.content)["status"] == "ok"

    record_id = payload["id"]
    get = MemoryGetHandler().execute({
        "id": "g1", "input": {"id": record_id},
    }, ctx)
    assert json.loads(get.content)["status"] == "ok"

    notes = tmp_path / "notes"
    notes.mkdir()
    svc.add(type="resource", title="笔记", body="", scope="user",
            data={"path": str(notes), "aliases": ["笔记"]})
    resolve = ResourceResolveHandler().execute({
        "id": "r1", "input": {"name": "笔记"},
    }, ctx)
    assert json.loads(resolve.content)["status"] == "ok"

    needs_confirm = MemoryForgetHandler().execute({
        "id": "f1", "input": {"id": record_id},
    }, ctx)
    assert json.loads(needs_confirm.content)["status"] == "confirmation_required"
    confirm = MemoryForgetHandler().execute({
        "id": "f2", "input": {"id": record_id, "confirm": True},
    }, ctx)
    assert json.loads(confirm.content)["status"] == "ok"

    user_needs_confirm = MemoryWriteHandler().execute({
        "id": "u1",
        "input": {"type": "preference", "title": "用户偏好", "body": "x", "scope": "user"},
    }, ctx)
    assert json.loads(user_needs_confirm.content)["status"] == "confirmation_required"
    user_ok = MemoryWriteHandler().execute({
        "id": "u2",
        "input": {
            "type": "preference", "title": "用户偏好", "body": "x",
            "scope": "user", "confirm": True,
        },
    }, ctx)
    assert json.loads(user_ok.content)["status"] == "ok"


def test_cross_project_isolation_and_shared_user_memory(tmp_path):
    cfg = MemoryConfig(enabled=True, user_store=tmp_path / "user")
    a = MemoryService(working_root=tmp_path / "a", config=cfg)
    b = MemoryService(working_root=tmp_path / "b", config=cfg)
    try:
        a.add(type="fact", title="A 项目事实", body="only A", scope="project")
        assert b.search("A 项目事实") == []
        a.add(type="fact", title="共享用户事实", body="user", scope="user")
        assert b.search("共享用户事实")
    finally:
        a.close()
        b.close()


# ---------------- REPL / CLI ----------------

def test_repl_commands(svc):
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command("/remember --user 喜欢简洁汇报", svc) is True
    assert "已记录" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command("/memory search 简洁", svc) is True
    assert "喜欢简洁汇报" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command("/phase start 测试阶段", svc) is True
    assert "阶段已开始" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command("/now", svc) is True
    assert "测试阶段" in buf.getvalue()

    assert handle_repl_command("/exit", svc) is False


def test_cli_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_ENABLED", "1")
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_DIR", str(tmp_path / "user"))

    from skill_engine.cli import app

    runner = CliRunner()
    added = runner.invoke(app, [
        "memory", "add", "--type", "preference", "--title", "CLI 偏好",
        "--body", "简洁", "--user", "--pinned", "-w", str(tmp_path),
    ])
    assert added.exit_code == 0, added.output
    assert "已写入" in added.output

    searched = runner.invoke(app, [
        "memory", "search", "CLI 偏好", "-w", str(tmp_path),
    ])
    assert searched.exit_code == 0, searched.output
    assert "CLI 偏好" in searched.output

    profile = runner.invoke(app, ["profile", "show", "-w", str(tmp_path)])
    assert profile.exit_code == 0, profile.output
    assert "CLI 偏好" in profile.output

    phase = runner.invoke(app, ["phase", "start", "CLI 阶段", "-w", str(tmp_path)])
    assert phase.exit_code == 0, phase.output
    now = runner.invoke(app, ["now", "-w", str(tmp_path)])
    assert now.exit_code == 0, now.output
    assert "CLI 阶段" in now.output


# ---------------- injection / persistence ----------------

def test_tool_dispatch_injects_and_strips(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_ENABLED", "1")
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_DIR", str(tmp_path / "user"))
    from skill_engine.execution.tool_dispatch import ToolDispatchRunner
    from skill_engine.models import MatchResult, Skill, SkillMetadata

    svc = MemoryService(working_root=tmp_path)
    svc.add(type="preference", title="注入偏好", body="简洁",
            scope="user", pinned=True)

    import skill_engine.execution.tool_dispatch as td_mod
    monkeypatch.setattr(td_mod, "load_skill_tools", lambda skill: [])
    monkeypatch.setattr(td_mod, "load_mcp_tools", lambda skill: [])

    class FakeExec:
        shell = "bash"
        def prewarm_sandbox(self, *a, **k):
            pass

    class FakeAsm:
        def assemble(self, skill, arguments, plain_text=False):
            return "DO TASK"

    class FakeLLM:
        def __init__(self):
            self.messages = []
        def bind_tools(self, tools):
            return self
        def invoke(self, messages):
            self.messages.append(messages)
            class R:
                content = "done"
                def __init__(self):
                    self.tool_calls = []
                    self.additional_kwargs = {}
            return R()

    skill = Skill(
        metadata=SkillMetadata(name="demo", description="d"),
        body="", directory=str(tmp_path),
    )
    mr = MatchResult(skill=skill, score=1.0, method="name", arguments={})
    llm = FakeLLM()
    state = tmp_path / "state.jsonl"
    runner = ToolDispatchRunner(
        executor=FakeExec(), assembler=FakeAsm(),
        working_root=str(tmp_path), memory_service=svc,
    )
    runner.run(mr, llm, max_iterations=2, state_path=str(state))
    svc.close()

    first = json.dumps(llm.messages[0], ensure_ascii=False, default=str)
    assert "<memory_context" in first
    assert "注入偏好" in first

    last = json.loads(state.read_text(encoding="utf-8").splitlines()[-1])
    assert "<memory_context" not in json.dumps(last, ensure_ascii=False)
