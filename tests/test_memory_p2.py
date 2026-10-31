"""长期记忆 P2 测试：episode 溢出、蒸馏、INBOX、supersede、导入导出。"""

import io
import json
from contextlib import redirect_stdout

import pytest
from typer.testing import CliRunner

from skill_engine.execution.context_manager import ContextManager
from skill_engine.memory.repl import handle_repl_command
from skill_engine.memory.service import MemoryService
from skill_engine.memory.settings import MemoryConfig


@pytest.fixture
def svc(tmp_path):
    cfg = MemoryConfig(
        enabled=True,
        user_store=tmp_path / "user",
        fts_enabled=True,
        auto_distill=False,
    )
    service = MemoryService(working_root=tmp_path / "project", config=cfg)
    yield service
    service.close()


class _StubLLM:
    def invoke(self, prompt):
        return "压缩摘要"


def _build_rounds(n: int, per_round_chars: int = 300) -> list[dict]:
    msgs = [{"role": "user", "content": "FINAL_PROMPT"}]
    for i in range(n):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"c{i}", "name": "bash", "args": {}}],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "name": "bash",
            "content": f"round {i} " + "x" * per_round_chars,
        })
    return msgs


def test_compression_calls_episode_writer_before_l2():
    cm = ContextManager(budget=300, keep_recent=2)
    cm.messages = _build_rounds(6)
    seen: list[list[dict]] = []
    assert cm.maybe_compress(_StubLLM(), episode_writer=lambda seg: seen.append(seg)) is True
    assert seen and seen[0], "L2 之前应把待压缩段交给 episode writer"


def test_write_episode_is_searchable_but_not_injected(svc):
    episode = svc.write_episode(
        [
            {"role": "assistant", "content": "<key_finding>缓存根因已定位</key_finding>"},
            {"role": "assistant", "content": "已修复并验证"},
        ],
        skill_name="demo",
        session_id="s1",
        reason="session_end",
    )
    assert episode is not None and episode.type == "episode"
    assert svc.search("缓存")
    assert "episode" not in svc.render_context()
    assert "缓存根因" not in svc.render_context()


def test_rule_distill_auto_commit_and_review(svc):
    messages = [
        {"role": "user", "content": "排查缓存问题"},
        {"role": "assistant", "content": "<key_finding>根因：缓存未失效</key_finding> 已修复"},
        {"role": "user", "content": "以后先给方案再动手，不要直接改"},
    ]
    result = svc.distill_session(messages, skill_name="demo", session_id="s1")
    assert result["episode"]
    assert result["committed"], "项目高置信 key_finding 应自动提交"
    assert result["pending"], "用户纠正应进入 INBOX"

    pending = svc.pending_records()
    assert pending
    accepted = svc.review_accept(pending[0].id)
    assert accepted is not None and accepted.status == "active"

    # 再放一条候选，测试 reject
    extra = svc.commit_candidate({
        "type": "feedback", "title": "待拒绝", "body": "这是一条待拒绝的候选",
        "scope": "user", "confidence": "medium", "source": "test",
    }, auto=False)
    assert extra.status == "pending"
    assert svc.review_reject(extra.id) is True
    assert svc.get(extra.id) is None


def test_llm_distill_candidates(tmp_path):
    class FakeLLM:
        def invoke(self, prompt):
            return """```json
{"records": [
  {"type": "fact", "title": "FTS 检索顺序", "body": "先精确解析再 FTS", "scope": "project", "confidence": "high", "sensitivity": "L2"},
  {"type": "preference", "title": "汇报风格", "body": "一屏密度", "scope": "user", "confidence": "medium", "sensitivity": "L1"}
]}
```"""

    svc = MemoryService(
        working_root=tmp_path / "project",
        config=MemoryConfig(
            enabled=True,
            user_store=tmp_path / "user",
            fts_enabled=True,
            auto_distill=False,
        ),
    )
    try:
        result = svc.distill_session(
            [
                {"role": "user", "content": "讨论一下长期记忆的检索顺序应该怎么设计"},
                {"role": "assistant", "content": "我们决定：先用精确解析，再用 FTS5，最后才考虑向量"},
            ],
            skill_name="demo", session_id="s-llm", llm=FakeLLM(),
        )
        assert result["committed"] or result["pending"]
        all_records = svc.list_records()
        assert any(r.title == "FTS 检索顺序" for r in all_records)
        assert any(r.title == "汇报风格" for r in all_records)
    finally:
        svc.close()


def test_supersede_auto_commit(svc):
    old = svc.add(type="fact", title="构建命令", body="mvn test",
                  scope="project", confidence="medium")
    new = svc.commit_candidate({
        "type": "fact", "title": "构建命令", "body": "mvn -q test",
        "scope": "project", "confidence": "high", "source": "test",
    }, auto=True)
    assert new.status == "active"
    assert new.supersedes == old.id
    assert svc.get(old.id).status == "superseded"
    titles = {h.record.title for h in svc.search("构建命令")}
    assert "构建命令" in titles
    history = svc.search("构建命令", include_history=True)
    assert {h.record.status for h in history} >= {"active", "superseded"}


def test_import_markdown_goes_to_inbox(svc, tmp_path):
    import_dir = tmp_path / "old-memory"
    import_dir.mkdir()
    (import_dir / "feedback.md").write_text(
        "---\nname: 沟通偏好\ntype: feedback\ndescription: 先给方案\n---\n"
        "以后先给方案再动手，不要直接改代码。\n",
        encoding="utf-8",
    )
    (import_dir / "log.md").write_text(
        "# 2026-01-01 工作日志\n\n今天修复了缓存失效问题，并记录了验证步骤。\n",
        encoding="utf-8",
    )
    candidates = svc.import_markdown(import_dir, dry_run=True)
    assert len(candidates) == 2
    assert {c["type"] for c in candidates} == {"feedback", "episode"}

    saved = svc.import_markdown(import_dir, dry_run=False)
    assert len(saved) == 2
    assert all(r.status == "pending" for r in svc.pending_records())
    assert svc.search("沟通偏好", include_private=True) == []


def test_export_excludes_l3(svc):
    svc.add(type="fact", title="公开事实", body="L2", scope="user", sensitivity="L2")
    svc.add(type="fact", title="私密事实", body="L3", scope="user", sensitivity="L3")
    exported = svc.export_records()
    titles = {r["title"] for r in exported}
    assert "公开事实" in titles and "私密事实" not in titles
    assert "私密事实" in {r["title"] for r in svc.export_records(include_private=True)}


def test_repl_review_accept_reject(svc):
    pending = svc.commit_candidate({
        "type": "feedback", "title": "REPL 候选", "body": "待评审",
        "scope": "user", "confidence": "medium", "source": "test",
    }, auto=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command("/memory review", svc) is True
    assert "REPL 候选" in buf.getvalue()

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command(f"/memory accept {pending.id}", svc) is True
    assert svc.get(pending.id).status == "active"

    second = svc.commit_candidate({
        "type": "feedback", "title": "REPL 拒绝", "body": "待拒绝",
        "scope": "user", "confidence": "medium", "source": "test",
    }, auto=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert handle_repl_command(f"/memory reject {second.id}", svc) is True
    assert svc.get(second.id) is None


def test_cli_import_review_export(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_ENABLED", "1")
    monkeypatch.setenv("SKILLS_ENGINE_MEMORY_DIR", str(tmp_path / "user"))
    import_dir = tmp_path / "old"
    import_dir.mkdir()
    (import_dir / "note.md").write_text(
        "---\nname: 旧偏好\ntype: user\n---\n以后先给方案再动手。\n",
        encoding="utf-8",
    )
    from skill_engine.cli import app

    runner = CliRunner()
    imported = runner.invoke(app, [
        "memory", "import", str(import_dir), "--yes", "-w", str(tmp_path),
    ])
    assert imported.exit_code == 0, imported.output
    assert "INBOX" in imported.output

    reviewed = runner.invoke(app, ["memory", "review", "-w", str(tmp_path)])
    assert reviewed.exit_code == 0, reviewed.output
    assert "旧偏好" in reviewed.output

    out_file = tmp_path / "export.json"
    exported = runner.invoke(app, [
        "memory", "export", "--output", str(out_file), "-w", str(tmp_path),
    ])
    assert exported.exit_code == 0, exported.output
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert any(r["title"] == "旧偏好" for r in data["records"])
