"""read_file 去重缓存测试：打断「读-压缩-遗忘-重读」循环。

- file_tracker 单元级：区间命中 / 覆盖 / 全文 / mtime 失效 / on_write 失效 / invalidate_all
- Runner 集成级：同一区间二次读 → 缓存命中提示；force_refresh → 全文
"""

import time

import pytest

from skill_engine.models import Skill, SkillMetadata, MatchResult


@pytest.fixture(autouse=True)
def _permissive_security(monkeypatch):
    """隔离全局安全模式：bash 失效缓存测试需真正执行命令以触发缓存失效。"""
    monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "permissive")


def _make_file(tmp_path, lines=60):
    p = tmp_path / "app.js"
    p.write_text("\n".join(f"line {i}" for i in range(lines)) + "\n", encoding="utf-8")
    return p


class MockLLM:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.last_messages = []

    def invoke(self, messages):
        self.call_count += 1
        self.last_messages = messages
        resp = self.responses[self.call_count - 1]
        if isinstance(resp, str):
            return {"content": resp, "tool_calls": []}
        return resp


def _runner():
    from skill_engine.execution.runner import Runner
    from skill_engine.execution.executor import Executor
    from skill_engine.execution.assembler import Assembler
    return Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))


def _read_call(path, offset=0, limit=0, force_refresh=False, cid="c1"):
    inp = {"path": str(path)}
    if offset:
        inp["offset"] = offset
    if limit:
        inp["limit"] = limit
    if force_refresh:
        inp["force_refresh"] = True
    return {"content": "", "tool_calls": [{"id": cid, "type": "read_file", "input": inp}]}


def _match(tmp_path) -> MatchResult:
    skill = Skill(metadata=SkillMetadata(name="t", description="d"),
                  body="b", directory=str(tmp_path))
    return MatchResult(skill=skill, score=1.0, method="name", arguments={})


def _tool_msgs(llm):
    return [m for m in llm.last_messages if m.get("role") == "tool"]


# ── file_tracker 单元级 ────────────────────────────────────────────────
class TestFileTrackerCache:
    def test_hit_same_interval(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        content = "".join(f"{i}\n" for i in range(60))
        ft.cache_read(p, 0, 60, 60, content)
        hit = ft.cache_lookup(p, 0, 60)
        assert hit is not None
        assert hit["content"] == content
        assert hit["full"] is False

    def test_hit_subrange_covered(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 60, 60, "whole")
        hit = ft.cache_lookup(p, 10, 20)  # 10-30 行被子区间覆盖
        assert hit is not None
        assert hit["start"] == 10 and hit["end"] == 30

    def test_miss_out_of_range(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 30, 60, "first-half")
        assert ft.cache_lookup(p, 30, 30) is None  # 30-60 未读过
        assert ft.cache_lookup(p, 0, 60) is None   # 非整读

    def test_full_read_hit(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 0, 60, "whole")
        hit = ft.cache_lookup(p, 0, 0)
        assert hit is not None and hit["full"] is True
        assert ft.cache_lookup(p, 5, 10) is not None  # 整读覆盖任意子区间

    def test_mtime_change_invalidates(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 60, 60, "v1")
        time.sleep(0.02)
        p.write_text("changed\n", encoding="utf-8")
        assert ft.cache_lookup(p, 0, 60) is None

    def test_on_write_invalidates(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 60, 60, "v1")
        ft.on_write(p)
        assert ft.cache_lookup(p, 0, 60) is None

    def test_invalidate_all_clears(self, tmp_path):
        from skill_engine.execution.file_tracker import FileStateTracker
        p = _make_file(tmp_path)
        ft = FileStateTracker()
        ft.cache_read(p, 0, 60, 60, "v1")
        ft.invalidate_all()
        assert ft.cache_lookup(p, 0, 60) is None


# ── Runner 集成级 ──────────────────────────────────────────────────────
class TestReadCacheIntegration:
    def test_second_read_returns_cache_hit_notice(self, tmp_path):
        """同一区间二次 read_file → 缓存命中提示，不再返回全文。"""
        p = _make_file(tmp_path)
        runner = _runner()
        llm = MockLLM([_read_call(p, cid="c1"),
                       _read_call(p, cid="c2"),
                       "done"])
        runner.run(_match(tmp_path), tool_dispatch=llm)
        tool_msgs = _tool_msgs(llm)
        assert len(tool_msgs) == 2
        assert "line 0" in tool_msgs[0]["content"]            # 首次全文
        assert "缓存命中" in tool_msgs[1]["content"]           # 二次提示
        assert "line 0" not in tool_msgs[1]["content"]

    def test_force_refresh_returns_full_content(self, tmp_path):
        """force_refresh=true 跳过缓存，始终返回完整内容。"""
        p = _make_file(tmp_path)
        runner = _runner()
        llm = MockLLM([_read_call(p, cid="c1"),
                       _read_call(p, force_refresh=True, cid="c2"),
                       "done"])
        runner.run(_match(tmp_path), tool_dispatch=llm)
        tool_msgs = _tool_msgs(llm)
        assert "缓存命中" not in tool_msgs[1]["content"]
        assert "line 0" in tool_msgs[1]["content"]

    def test_file_modified_after_bash_reloads(self, tmp_path):
        """文件被改后重读（bash 失效缓存）→ 返回新内容而非命中提示。"""
        p = _make_file(tmp_path)
        runner = _runner()
        bash_call = {"content": "", "tool_calls": [
            {"id": "cb", "type": "bash", "input": {"command": "echo modify"}}]}
        llm = MockLLM([_read_call(p, cid="c1"),
                       bash_call,
                       _read_call(p, cid="c2"),
                       "done"])
        runner.run(_match(tmp_path), tool_dispatch=llm)
        tool_msgs = [m for m in llm.last_messages if m.get("role") == "tool"]
        read_msgs = [m for m in tool_msgs if m.get("name") == "read_file"]
        assert len(read_msgs) == 2
        assert "缓存命中" not in read_msgs[1]["content"]  # bash 后缓存已失效