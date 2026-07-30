"""mind-map skill 测试

验证 mind-map skill 能正常被发现、匹配、编译和执行。
因为是纯 LLM 生成型 skill，测试重点在编译阶段（不需要真实 LLM 调用）。
"""

import pytest
from pathlib import Path


# 项目 skills 目录
SKILLS_DIR = Path(__file__).parent.parent / "skills"


class TestMindMapDiscovery:
    """mind-map 能被发现和加载"""

    def test_skill_exists_in_skills_dir(self):
        assert (SKILLS_DIR / "mind-map" / "SKILL.md").exists()

    def test_skill_has_meta_file(self):
        assert (SKILLS_DIR / "mind-map" / ".skill-meta.yaml").exists()

    def test_skill_is_discoverable(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        skill = registry.load_skill("mind-map")
        assert skill is not None
        assert skill.metadata.name == "mind-map"

    def test_skill_has_arguments_in_body(self):
        """SKILL.md body 必须包含 $ARGUMENTS，否则 -a 参数无法注入"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        skill = registry.load_skill("mind-map")
        assert skill is not None
        assert "$ARGUMENTS" in skill.body, "SKILL.md 缺少 $ARGUMENTS 占位符"


class TestMindMapCompile:
    """编译后的 prompt 包含用户参数"""

    def test_compiled_prompt_contains_user_input(self):
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        skill = registry.load_skill("mind-map")
        assert skill is not None

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        args = {"$ARGUMENTS": "topic: Go语言, material: 基础语法、并发"}
        prompt = assembler.assemble(skill, args)

        assert "topic: Go语言" in prompt
        assert "material: 基础语法、并发" in prompt
        assert "[SKILL: mind-map]" in prompt

    def test_compiled_prompt_has_mermaid_instructions(self):
        """编译后的 prompt 保留 Mermaid 输出格式说明"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.execution.executor import Executor
        from skill_engine.execution.assembler import Assembler

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        skill = registry.load_skill("mind-map")
        assert skill is not None

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        prompt = assembler.assemble(skill, {"$ARGUMENTS": "test"})

        assert "```mermaid" in prompt
        assert "mindmap" in prompt
        assert "root((" in prompt


class TestMindMapRouting:
    """mind-map 能被路由匹配命中"""

    def test_match_by_exact_name(self):
        """精确名称匹配"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry
        from skill_engine.routing.router import Router

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        router = Router(registry)

        plan = router.match("mind-map")
        assert plan.primary is not None
        assert plan.primary.name == "mind-map"
        assert plan.method == "exact"

    def test_skill_is_listed(self):
        """skill-engine list 能列出 mind-map"""
        from skill_engine.routing.discovery import discover
        from skill_engine.routing.registry import Registry

        roots = [str(SKILLS_DIR)] if SKILLS_DIR.exists() else []
        index = discover(roots=roots)
        registry = Registry(index)
        active = registry.list_active()
        assert "mind-map" in active