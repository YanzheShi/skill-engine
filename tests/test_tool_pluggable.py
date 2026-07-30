"""
工具可插拔接口测试

验证：
1. load_skill_tools 从 skill 目录的 tools.py 加载 @tool 工具
2. 无 extra_tools 时返回空列表（对其他 skill 零影响）
3. 声明不存在的模块时安全跳过（warning 但不崩）
4. bind 合并后 skill 工具出现在工具集中，且 disallowed/allowed 过滤生效
5. 集成：带 bind_tools 的 Mock LLM 跑 runner.run，skill 自带工具被真正执行（非"未知工具类型"）
"""

import pytest
from pathlib import Path
import tempfile
import textwrap


# ---------------- 测试 1：load_skill_tools 基本加载 ----------------
class TestLoadSkillTools:
    def test_loads_tools_from_tools_py(self, tmp_path):
        from skill_engine.execution.tool_defs import load_skill_tools
        from skill_engine.models import Skill, SkillMetadata

        (tmp_path / "tools.py").write_text(textwrap.dedent("""
            from langchain_core.tools import tool

            @tool
            def cb_dummy(path: str = ".") -> str:
                '''dummy tool'''
                return "ok"
        """), encoding="utf-8")

        skill = Skill(
            metadata=SkillMetadata(
                name="demo", description="d",
                extra_tools=["tools.py"],
            ),
            body="", directory=str(tmp_path),
        )
        tools = load_skill_tools(skill)
        assert len(tools) == 1
        assert tools[0].name == "cb_dummy"

    def test_no_extra_tools_returns_empty(self, tmp_path):
        from skill_engine.execution.tool_defs import load_skill_tools
        from skill_engine.models import Skill, SkillMetadata

        skill = Skill(
            metadata=SkillMetadata(name="demo", description="d"),
            body="", directory=str(tmp_path),
        )
        assert load_skill_tools(skill) == []

    def test_missing_module_is_skipped(self, tmp_path):
        from skill_engine.execution.tool_defs import load_skill_tools
        from skill_engine.models import Skill, SkillMetadata

        skill = Skill(
            metadata=SkillMetadata(
                name="demo", description="d",
                extra_tools=["does_not_exist.py"],
            ),
            body="", directory=str(tmp_path),
        )
        # 不应抛异常，返回空
        assert load_skill_tools(skill) == []


# ---------------- 测试 2：bind 合并 + 过滤 ----------------
class TestBindMerge:
    def _make_skill(self, tmp_path, extra):
        (tmp_path / "tools.py").write_text(textwrap.dedent("""
            from langchain_core.tools import tool

            @tool
            def cb_one(x: str = "") -> str:
                '''one'''
                return x

            @tool
            def cb_two(x: str = "") -> str:
                '''two'''
                return x
        """), encoding="utf-8")
        from skill_engine.models import Skill, SkillMetadata
        return Skill(
            metadata=SkillMetadata(
                name="demo", description="d", extra_tools=extra,
            ),
            body="", directory=str(tmp_path),
        )

    def test_merged_into_registry_tools(self, tmp_path):
        from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools
        skill = self._make_skill(tmp_path, ["tools.py"])
        merged = list(TOOL_REGISTRY.values()) + load_skill_tools(skill)
        names = {t.name for t in merged}
        assert "cb_one" in names and "cb_two" in names
        # 内建工具仍在
        assert "bash" in names and "read_file" in names

    def _merge_with_filters(self, skill):
        """复现 tool_dispatch.run 中的合并+过滤逻辑，供过滤测试复用。"""
        from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools
        tools = list(TOOL_REGISTRY.values()) + load_skill_tools(skill)
        disallowed = getattr(skill.metadata, "disallowed_tools", None) or []
        allowed = getattr(skill.metadata, "allowed_tools", None) or []
        if disallowed:
            tools = [t for t in tools if t.name not in disallowed]
        if allowed:
            tools = [t for t in tools if t.name in allowed]
        return tools

    def test_disallowed_filters_skill_tool(self, tmp_path):
        from skill_engine.models import Skill, SkillMetadata
        skill = self._make_skill(tmp_path, ["tools.py"])
        skill.metadata.disallowed_tools = ["cb_one"]
        merged = self._merge_with_filters(skill)
        names = {t.name for t in merged}
        assert "cb_one" not in names
        assert "cb_two" in names
        assert "bash" in names  # 内建不受影响

    def test_allowed_whitelist(self, tmp_path):
        from skill_engine.models import Skill, SkillMetadata
        skill = self._make_skill(tmp_path, ["tools.py"])
        # allowed 白名单针对全部工具：仅 bash + cb_two 保留
        skill.metadata.allowed_tools = ["bash", "cb_two"]
        merged = self._merge_with_filters(skill)
        names = {t.name for t in merged}
        assert "cb_two" in names
        assert "cb_one" not in names
        assert "bash" in names
        assert "edit_file" not in names  # 不在白名单的内建被剔除
        assert "bash" in names


# ---------------- 测试 3：集成执行（skill 工具被真正调用） ----------------
class _MockToolLLM:
    """带 bind_tools 的 Mock LLM：第一轮调用 cb_list_files，第二轮收尾。"""

    def __init__(self):
        self.call_count = 0
        self.last_messages = None
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.last_messages = messages
        self.call_count += 1
        if self.call_count == 1:
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "cb_list_files", "input": {"path": "."}}
            ]}
        return {"content": "完成", "tool_calls": []}


class TestSkillToolExecution:
    @pytest.fixture
    def runner(self):
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler
        from skill_engine.execution.runner import Runner
        return Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))

    def test_skill_tool_executed(self, runner, tmp_path):
        from skill_engine.models import Skill, SkillMetadata, MatchResult

        # 准备 code-builder 风格的 tools.py
        (tmp_path / "tools.py").write_text(textwrap.dedent("""
            from langchain_core.tools import tool

            @tool
            def cb_list_files(path: str = ".") -> str:
                '''list files'''
                from pathlib import Path
                return "\\n".join(sorted(p.name for p in Path(path).rglob('*'))[:5])
        """), encoding="utf-8")

        skill = Skill(
            metadata=SkillMetadata(
                name="code-builder", description="d",
                extra_tools=["tools.py"],
            ),
            body="", directory=str(tmp_path),
        )
        match = MatchResult(skill=skill, score=1.0, method="name", arguments={})
        llm = _MockToolLLM()
        result = runner.run(match, tool_dispatch=llm, max_iterations=5)

        assert result["stopped_by"] == "stop"
        # skill 工具应被真正执行（出现在 step 记录里，而非 "未知工具类型"）
        skill_steps = [s for s in result["steps"] if s.get("type") == "cb_list_files"]
        assert skill_steps, "skill 自带工具未被执行"
        # 历史里不应出现 "未知工具类型"
        full = str(result.get("history", []))
        assert "未知工具类型" not in full
