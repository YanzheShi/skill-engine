"""
Skill 创建关键链路测试（create_skill，纯机械、不依赖 LLM）

验证 runner.create_skill 能把 name/description/groups/body/scripts 落地为合法 SKILL.md。
用 tempfile.mkdtemp 自建目录，避开 pytest tmp_path fixture（沙箱环境下该 fixture 会触发
Bad file descriptor 假红）；在真实环境中 tmp_path 同样可用。
"""

import os
import tempfile

from skill_engine.execution.runner import Runner
from skill_engine.execution.executor import Executor
from skill_engine.execution.assembler import Assembler


def _runner():
    return Runner(Assembler(executor=Executor(timeout=10)), Executor(timeout=10))


def test_create_skill_writes_skill_md():
    base = tempfile.mkdtemp(prefix="sktest_")
    skills_dir = os.path.join(base, "skills")
    runner = _runner()

    result = runner.create_skill(
        name="demo-create",
        description="演示创建 skill 的关键链路",
        groups=["demo"],
        body_template="# demo-create\n\n创建测试。",
        skills_dir=skills_dir,
    )
    skill_md = os.path.join(skills_dir, "demo-create", "SKILL.md")
    assert os.path.exists(skill_md)
    with open(skill_md, encoding="utf-8") as f:
        text = f.read()
    assert "demo-create" in text
    assert "演示创建 skill 的关键链路" in text
    assert result.get("status") == "success"


def test_create_skill_with_script():
    base = tempfile.mkdtemp(prefix="sktest_")
    skills_dir = os.path.join(base, "skills")
    runner = _runner()

    result = runner.create_skill(
        name="img-resize",
        description="调整图片尺寸",
        groups=["images"],
        scripts={"resize.py": "print('resize')"},
        skills_dir=skills_dir,
    )
    script = os.path.join(skills_dir, "img-resize", "scripts", "resize.py")
    assert os.path.exists(script)
    assert result.get("status") == "success"
