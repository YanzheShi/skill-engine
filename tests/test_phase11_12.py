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


class TestSkillCreatorEnhanced:
    """测试增强版 SkillCreator：steps、assets、script_templates"""

    @pytest.fixture
    def tmp_dir(self):
        d = tempfile.mkdtemp(prefix="skill_engine_enhanced_")
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def test_create_with_steps(self, tmp_dir):
        """创建带 steps 的 skill"""
        from skill_engine.creator import SkillCreator
        from skill_engine.models import Step

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="steps-skill",
            description="带 steps 的 skill",
            groups=["test"],
            steps=[
                Step(name="step_one", type="exec", command="echo hello", timeout=10),
                Step(name="step_two", type="llm", model="test", template="Hello {step_one}"),
                Step(name="step_three", type="write", output_file="output/result.txt", template="Result: {step_two}"),
            ],
        )

        assert result["status"] == "success"

        # 验证 SKILL.md 中包含 ## Steps
        skill_md = Path(tmp_dir) / "steps-skill" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        assert "## Steps" in content
        assert "step_one" in content
        assert "step_two" in content
        assert "step_three" in content

        # 验证每个 step 的 type 都被序列化
        assert "type: exec" in content
        assert "type: llm" in content
        assert "type: write" in content

    def test_create_with_arguments(self, tmp_dir):
        """创建带 arguments 的 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="args-skill",
            description="带参数的 skill",
            arguments=["theme", "form", "output_dir"],
        )

        assert result["status"] == "success"

        skill_md = Path(tmp_dir) / "args-skill" / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        # arguments 应出现在 frontmatter 中
        assert "arguments:" in content
        assert "theme" in content

    def test_create_with_assets(self, tmp_dir):
        """创建带 assets 的 skill"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="assets-skill",
            description="带资产的 skill",
            assets={
                "template.md": "# Template\n\nThis is a template file.",
                "styles/default.css": "body { font-family: serif; }",
            },
        )

        assert result["status"] == "success"

        # 验证 assets 目录
        assets_dir = Path(tmp_dir) / "assets-skill" / "assets"
        assert assets_dir.exists()
        assert (assets_dir / "template.md").exists()
        assert (assets_dir / "styles" / "default.css").exists()
        assert "This is a template file." in (assets_dir / "template.md").read_text(encoding="utf-8")

    def test_create_with_script_templates(self, tmp_dir):
            """创建带脚本的 skill（引用内置脚本源码）"""
            from skill_engine.creator import SkillCreator
            from skill_engine.builtins import WRITE_TO_FILE_PY, READ_FILE_PY

            creator = SkillCreator(base_dir=tmp_dir)
            result = creator.create(
                name="template-skill",
                description="带脚本的 skill",
                scripts={
                    "write_to_file.py": WRITE_TO_FILE_PY,
                    "read_file.py": READ_FILE_PY,
                },
            )

            assert result["status"] == "success"

            # 验证脚本被生成
            scripts_dir = Path(tmp_dir) / "template-skill" / "scripts"
            assert scripts_dir.exists()
            assert (scripts_dir / "write_to_file.py").exists()
            assert (scripts_dir / "read_file.py").exists()

            # 验证脚本内容包含预期的逻辑
            write_content = (scripts_dir / "write_to_file.py").read_text(encoding="utf-8")
            assert "os.makedirs" in write_content
            assert "DEST_PATH" in write_content

    def test_create_with_all_features(self, tmp_dir):
        """创建包含所有增强功能的 skill（端到端）"""
        from skill_engine.creator import SkillCreator
        from skill_engine.models import Step

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="full-feature-skill",
            description="测试所有功能的 skill",
            groups=["writing", "creative"],
            when_to_use="用户需要创作古诗时",
            arguments=["theme", "form"],
            steps=[
                Step(name="analyze", type="llm", model="agnes",
                     template="分析主题 {theme}，建议诗词体裁"),
                Step(name="compose", type="llm", model="agnes",
                     template="写一首古诗\n主题: {theme}\n体裁: {form}\n分析: {analyze}"),
                Step(name="save", type="write",
                     output_file="${DEST_PATH}",
                     template="# {theme}\n\n{compose}"),
            ],
            assets={
                "form-reference.md": "诗词格律参考...",
            },
        )

        assert result["status"] == "success"

        skill_dir = Path(tmp_dir) / "full-feature-skill"
        # 验证所有文件存在
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "assets" / "form-reference.md").exists()

        # 验证 SKILL.md 内容
        content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert "## Steps" in content
        assert "analyze" in content
        assert "compose" in content
        assert "save" in content
        assert "arguments:" in content
        assert "theme" in content

    def test_create_backward_compatible(self, tmp_dir):
        """验证向后兼容：不带新参数的创建仍然工作"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)

        # 旧方式调用
        result = creator.create(
            name="legacy-skill",
            description="旧方式创建的 skill",
            groups=["test"],
            body_template="# Legacy\n\n{description}",
            scripts={"helper.py": "print('hello')"},
        )

        assert result["status"] == "success"
        assert (Path(tmp_dir) / "legacy-skill" / "SKILL.md").exists()
        assert (Path(tmp_dir) / "legacy-skill" / "scripts" / "helper.py").exists()

    def test_structured_body_without_steps(self, tmp_dir):
        """有 arguments 但无 steps 时，生成带 arguments 部分的 body"""
        from skill_engine.creator import SkillCreator

        creator = SkillCreator(base_dir=tmp_dir)
        result = creator.create(
            name="args-only-skill",
            description="只有参数的 skill",
            arguments=["topic"],
        )

        assert result["status"] == "success"
        content = (Path(tmp_dir) / "args-only-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert "## 参数" in content
        assert "topic" in content

    def test_serialize_steps_preserves_order(self, tmp_dir):
        """验证步骤序列化保持顺序"""
        from skill_engine.creator import SkillCreator
        from skill_engine.models import Step

        creator = SkillCreator(base_dir=tmp_dir)
        steps = [
            Step(name="first", type="exec", command="echo 1"),
            Step(name="second", type="llm", template="{first}"),
            Step(name="third", type="write", output_file="out.txt", template="{second}"),
        ]

        result = creator.create(
            name="order-test",
            description="测试步骤顺序",
            steps=steps,
        )

        assert result["status"] == "success"
        content = (Path(tmp_dir) / "order-test" / "SKILL.md").read_text(encoding="utf-8")

        # 验证步骤顺序
        first_pos = content.index("first")
        second_pos = content.index("second")
        third_pos = content.index("third")
        assert first_pos < second_pos < third_pos


class TestRunnerStepAutoDetection:
    """测试 runner 自动检测 body 中的 steps"""

    def test_parse_steps_from_body(self, tmp_path):
        """从 body 中解析 steps"""
        from skill_engine.creator import SkillCreator
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler
        from skill_engine.models import Step

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        # 构造包含 ## Steps 的 body
        body = """\
# test-skill

Test description

## Steps

- name: step_one
  type: exec
  command: echo hello
  timeout: 10

- name: step_two
  type: llm
  model: test
  template: "Result of {step_one}"

- name: step_three
  type: write
  output_file: output.txt
  template: "{step_two}"
"""

        steps = runner._parse_steps_from_body(body)
        assert steps is not None
        assert len(steps) == 3
        assert steps[0].name == "step_one"
        assert steps[0].type == "exec"
        assert steps[1].name == "step_two"
        assert steps[1].type == "llm"
        assert steps[2].name == "step_three"
        assert steps[2].type == "write"

    def test_parse_steps_from_body_no_steps(self, tmp_path):
        """没有 ## Steps 时返回 None"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        body = "# test-skill\n\nThis is a simple skill without steps."
        steps = runner._parse_steps_from_body(body)
        assert steps is None

    def test_parse_steps_from_body_empty(self, tmp_path):
        """空 body 返回 None"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        steps = runner._parse_steps_from_body("")
        assert steps is None

    def test_parse_steps_from_body_with_extra_sections(self, tmp_path):
        """## Steps 后有 ## Notes 等其他 section 时正确截断"""
        from skill_engine.runner import Runner
        from skill_engine.executor import Executor
        from skill_engine.assembler import Assembler

        executor = Executor(timeout=10, allow_all=True)
        assembler = Assembler(executor=executor)
        runner = Runner(assembler, executor)

        body = """\
# test-skill

## Steps

- name: step_one
  type: exec
  command: echo hello

## Notes

- Do not forget to test

## Conclusion

Done.
"""

        steps = runner._parse_steps_from_body(body)
        assert steps is not None
        assert len(steps) == 1
        assert steps[0].name == "step_one"


class TestDesignerExtractJson:
    """测试 designer.py 的 JSON 提取"""

    def test_extract_json_direct(self):
        """直接合法 JSON"""
        from skill_engine.designer import extract_json
        json_str = '{"name": "test-skill", "description": "a test skill", "steps": [{"name": "s1", "type": "exec", "command": "echo hi"}]}'
        result = extract_json(json_str)
        assert result is not None
        assert result["name"] == "test-skill"
        assert len(result["steps"]) == 1

    def test_extract_json_codeblock(self):
        """```json 代码块包裹"""
        from skill_engine.designer import extract_json
        text = 'Some intro...\n```json\n{"name": "cb-skill", "description": "from codeblock", "steps": [{"name": "s1", "type": "exec", "command": "ls"}]}\n```\nSome outro...'
        result = extract_json(text)
        assert result is not None
        assert result["name"] == "cb-skill"

    def test_extract_json_greedy(self):
        """有前后废话，贪婪提取"""
        from skill_engine.designer import extract_json
        text = 'Here is the skill:\n{"name": "greedy-skill", "description": "extracted", "steps": [{"name": "s1", "type": "exec", "command": "echo ok"}]}\nHope this helps!'
        result = extract_json(text)
        assert result is not None
        assert result["name"] == "greedy-skill"

    def test_extract_json_invalid(self):
        """无合法 JSON 返回 None"""
        from skill_engine.designer import extract_json
        assert extract_json("This is plain text.") is None

    def test_extract_json_empty_string(self):
        """空字符串返回 None"""
        from skill_engine.designer import extract_json
        assert extract_json("") is None

    def test_extract_json_malformed(self):
        """不完整 JSON 返回 None"""
        from skill_engine.designer import extract_json
        assert extract_json('{"name": "broken"') is None


class TestDesignerValidateDesign:
    """测试 designer.py 的 design 校验"""

    def test_validate_design_ok(self):
        """合法 design 通过校验"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "valid-skill", "description": "a valid skill",
                    "steps": [{"name": "s1", "type": "exec", "command": "echo hello"}],
        })
        assert errors == []

    def test_validate_design_missing_name(self):
        """缺少 name"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "description": "no name",
                    "steps": [{"name": "s1", "type": "exec", "command": "echo hi"}],
        })
        assert any("name" in e for e in errors)

    def test_validate_design_missing_description(self):
        """缺少 description"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "no-desc",
                    "steps": [{"name": "s1", "type": "exec", "command": "echo hi"}],
        })
        assert any("description" in e for e in errors)

    def test_validate_design_missing_steps(self):
        """缺少 steps"""
        from skill_engine.designer import validate_design
        errors = validate_design({"name": "no-steps", "description": "missing steps"})
        assert any("steps" in e for e in errors)

    def test_validate_design_steps_empty(self):
        """steps 为空列表"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "empty-steps", "description": "empty", "steps": [],
        })
        assert any("空" in e or "empty" in e.lower() for e in errors)

    def test_validate_design_bad_step_type(self):
        """step type 非法"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "bad-type", "description": "invalid type",
                    "steps": [{"name": "s1", "type": "fetch", "url": "http://x.com"}],
        })
        assert any("type" in e and "fetch" in e for e in errors)

    def test_validate_design_bad_name_format(self):
        """name 非 slug 格式"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "Bad Name With Spaces", "description": "invalid",
                    "steps": [{"name": "s1", "type": "exec", "command": "echo hi"}],
        })
        assert len(errors) > 0

    def test_validate_design_step_missing_name(self):
        """step 缺少 name"""
        from skill_engine.designer import validate_design
        errors = validate_design({
                    "name": "step-no-name", "description": "step missing name",
                    "steps": [{"type": "exec", "command": "echo hi"}],
        })
        assert any("name" in e for e in errors)
