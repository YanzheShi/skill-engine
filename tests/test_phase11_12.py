"""
Phase 11+12 测试套件 — Skill 自动创建 + 验证

测试：
1. SkillCreator.create() — 生成 SKILL.md + scripts/
2. SkillCreator.validate() — 验证生成的 skill
3. SkillValidator.validate_compile() — 编译验证
4. SkillValidator.validate_scripts() — 脚本验证
5. Runner.create_skill() — 端到端创建 + 验证
6. Runner.register_new_skill() — 热注册
7. system-create-skill 能被 discovery 发现
"""

import pytest
from pathlib import Path
import tempfile
import shutil


class TestSkillCreator:
    """测试 SkillCreator"""

    @pytest.fixture
    def tmp_dir(self):
        """临时目录，测试后清理"""
        d = tempfile.mkdtemp(prefix="skill_engine_test_")
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def test_create_basic_skill(self, tmp_dir):
        """创建基础 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="test-skill",
            description="这是一个测试 skill",
            groups=["test"],
            when_to_use="测试时",
        )

        assert result["status"] == "success"
        assert result["validated"] is True
        assert result["valid"] is True
        assert len(result["errors"]) == 0

        # 验证文件存在
        skill_dir = Path(tmp_dir) / "test-skill"
        assert (skill_dir / "SKILL.md").exists()
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "test-skill" in content

    def test_create_skill_with_scripts(self, tmp_dir):
        """创建带脚本的 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="script-skill",
            description="带脚本的 skill",
            scripts={
                "helper.py": "print('hello')",
                "utils.sh": "#!/bin/bash\necho hi",
            },
        )

        assert result["status"] == "success"

        scripts_dir = Path(tmp_dir) / "script-skill" / "scripts"
        assert (scripts_dir / "helper.py").exists()
        assert (scripts_dir / "utils.sh").exists()
        assert "print('hello')" in (scripts_dir / "helper.py").read_text()

    def test_create_skill_with_custom_body(self, tmp_dir):
        """创建带自定义 body 的 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        body = "# Custom Body\n\n这是自定义内容"
        result = creator.create(
            name="custom-body",
            description="自定义 body",
            body_template=body,
        )

        assert result["status"] == "success"
        content = (Path(tmp_dir) / "custom-body" / "SKILL.md").read_text(encoding="utf-8")
        assert "Custom Body" in content

    def test_create_skill_duplicate(self, tmp_dir):
        """重复创建同名 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        r1 = creator.create(name="dup", description="第一次")
        r2 = creator.create(name="dup", description="第二次")

        # 允许覆盖，都应该成功
        assert r1["status"] == "success"
        assert r2["status"] == "success"

    def test_validate_missing_skill_md(self, tmp_dir):
        """验证缺少 SKILL.md"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        skill_dir = Path(tmp_dir) / "no-skill-md"
        skill_dir.mkdir()

        errors = creator.validate(skill_dir)
        assert "SKILL.md 不存在" in errors

    def test_validate_missing_description(self, tmp_dir):
        """验证缺少 description"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        skill_dir = Path(tmp_dir) / "bad-fm"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: bad\n---\nno description", encoding="utf-8")

        errors = creator.validate(skill_dir)
        assert any("description" in e for e in errors)


class TestSkillValidator:
    """测试 SkillValidator"""

    def test_validate_compile_success(self):
        """编译验证通过"""
        from skill_engine.creator import SkillValidator
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        validator = SkillValidator(assembler, executor)

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="# Test body\n",
            directory="/tmp/test",
        )

        result = validator.validate_compile(skill)
        assert result["valid"] is True
        assert result["prompt_len"] > 0

    def test_validate_compile_empty_body(self):
        """空 body 也能编译"""
        from skill_engine.creator import SkillValidator
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        validator = SkillValidator(assembler, executor)

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="",
            directory="/tmp/test",
        )

        result = validator.validate_compile(skill)
        assert result["valid"] is True

    def test_validate_scripts_missing(self, tmp_path):
        """脚本验证：引用的脚本不存在"""
        from skill_engine.creator import SkillValidator
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        validator = SkillValidator(assembler, executor)

        # body 中引用了一个不存在的脚本
        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="使用 `!python scripts/missing.py` 获取数据",
            directory=str(tmp_path),
        )

        result = validator.validate_scripts(skill)
        # missing.py 不存在，should flag it
        assert not result["valid"]
        assert "scripts/missing.py" in result["missing"]

    def test_validate_scripts_present(self, tmp_path):
        """脚本验证：脚本存在"""
        from skill_engine.creator import SkillValidator
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        validator = SkillValidator(assembler, executor)

        # 创建脚本文件
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "helper.py").write_text("print('ok')")

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body=f"使用 `!python scripts/helper.py` 获取数据",
            directory=str(tmp_path),
        )

        result = validator.validate_scripts(skill)
        assert result["valid"] is True
        assert result["missing"] == []

    def test_full_validate(self, tmp_path):
        """完整验证"""
        from skill_engine.creator import SkillValidator
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Skill, SkillMetadata

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        validator = SkillValidator(assembler, executor)

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "ok.py").write_text("pass")

        skill = Skill(
            metadata=SkillMetadata(name="test", description="测试"),
            body="使用 `!python scripts/ok.py`",
            directory=str(tmp_path),
        )

        result = validator.full_validate(skill)
        assert result["valid"] is True
        assert result["compile"]["valid"] is True
        assert result["scripts"]["valid"] is True


class TestRunnerCreateSkill:
    """测试 Runner.create_skill() 端到端"""

    def test_end_to_end_create_and_validate(self, tmp_path):
        """创建 skill 并验证"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")
        result = runner.create_skill(
            name="e2e-test",
            description="端到端测试 skill",
            groups=["testing"],
            when_to_use="用于端到端测试",
            body_template="# E2E Test\n\n这是一个端到端测试 skill。",
            skills_dir=skills_dir,
        )

        assert result["status"] == "success"
        assert result["validated"] is True
        assert result["valid"] is True
        assert result["compile_result"]["valid"] is True

        # 验证文件确实创建了
        skill_dir = Path(skills_dir) / "e2e-test"
        assert (skill_dir / "SKILL.md").exists()

    def test_create_skill_with_script(self, tmp_path):
        """创建带脚本的 skill"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")
        result = runner.create_skill(
            name="script-e2e",
            description="带脚本的 skill",
            scripts={
                "helper.py": "print('hello world')",
            },
            skills_dir=skills_dir,
        )

        assert result["status"] == "success"
        scripts_dir = Path(skills_dir) / "script-e2e" / "scripts"
        assert (scripts_dir / "helper.py").exists()

    def test_register_new_skill(self, tmp_path):
        """热注册新 skill"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        skills_dir = str(tmp_path / "skills")

        # 先创建一个 skill
        result = runner.create_skill(
            name="register-test",
            description="热注册测试",
            skills_dir=skills_dir,
        )
        assert result["status"] == "success"

        # 热注册
        name = runner.register_new_skill("register-test", skills_dir=skills_dir)
        assert name == "register-test"


class TestSystemCreateSkill:
    """测试 system-create-skill meta-skill"""

    def test_system_skill_exists(self):
        """system-create-skill 存在"""
        from pathlib import Path
        assert (Path(__file__).parent.parent / "skills" / "system-create-skill" / "SKILL.md").exists()

    def test_system_skill_discoverable(self):
        """system-create-skill 能被 discovery 发现"""
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)
        assert "system-create-skill" in index

    def test_system_skill_has_groups(self):
        """system-create-skill 有 groups"""
        from skill_engine.discovery import discover
        from skill_engine.registry import Registry

        index = discover(roots=["skills"])
        registry = Registry(index)
        fm = registry.info_full("system-create-skill")
        assert fm is not None
        assert "system" in fm.get("groups", [])
        assert "meta" in fm.get("groups", [])
