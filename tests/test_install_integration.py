"""
Install 命令集成测试

测试三种安装来源：
1. 本地路径（含 SKILL.md 的目录）
2. Git URL（跳过，需要网络）
3. 嵌套目录（递归查找 SKILL.md）
"""

import pytest
from pathlib import Path


class TestInstallLocalPath:
    """测试本地路径安装"""

    def test_install_single_skill(self, tmp_path):
        """安装单个 skill 目录（含 SKILL.md）"""
        from skill_engine.cli import app
        import typer.testing

        # 创建测试 skill
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: test\n---\nTest", encoding="utf-8")

        target = tmp_path / "installed"
        runner = typer.testing.CliRunner()
        result = runner.invoke(app, ["install", str(skill_dir), "-t", str(target)])

        assert result.exit_code == 0
        installed = target / "test-skill"
        assert installed.exists()
        assert (installed / "SKILL.md").exists()

    def test_install_nested_skills(self, tmp_path):
        """安装嵌套目录，自动找到所有 SKILL.md"""
        from skill_engine.cli import app
        import typer.testing

        # 创建嵌套结构（模拟 ~/.claude/skills/xxx/SKILL.md）
        base = tmp_path / "source"
        skill1 = base / "skill-a"
        skill2 = base / "skill-b"
        base.mkdir(parents=True)
        skill1.mkdir()
        skill2.mkdir()
        (skill1 / "SKILL.md").write_text("---\nname: a\n---\nA", encoding="utf-8")
        (skill2 / "SKILL.md").write_text("---\nname: b\n---\nB", encoding="utf-8")

        target = tmp_path / "installed"
        runner = typer.testing.CliRunner()
        result = runner.invoke(app, ["install", str(base), "-t", str(target)])

        assert result.exit_code == 0
        assert (target / "skill-a" / "SKILL.md").exists()
        assert (target / "skill-b" / "SKILL.md").exists()

    def test_install_without_skill_md(self, tmp_path):
        """没有 SKILL.md 应报错"""
        from skill_engine.cli import app
        import typer.testing

        bad_dir = tmp_path / "bad-skill"
        bad_dir.mkdir()
        (bad_dir / "README.md").write_text("No SKILL.md here")

        target = tmp_path / "installed"
        runner = typer.testing.CliRunner()
        result = runner.invoke(app, ["install", str(bad_dir), "-t", str(target)])

        assert result.exit_code != 0

    def test_install_force_overwrite(self, tmp_path):
        """--force 覆盖已存在的 skill"""
        from skill_engine.cli import app
        import typer.testing

        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: test\n---\nOriginal", encoding="utf-8")

        target = tmp_path / "installed"
        target.mkdir()
        existing = target / "test-skill"
        existing.mkdir()
        (existing / "SKILL.md").write_text("---\nname: old\n---\nOld", encoding="utf-8")

        runner = typer.testing.CliRunner()
        result = runner.invoke(app, ["install", str(skill_dir), "-t", str(target), "--force"])

        assert result.exit_code == 0
        content = (existing / "SKILL.md").read_text(encoding="utf-8")
        assert "Original" in content

    def test_install_skip_existing_without_force(self, tmp_path):
        """不加 --force 应跳过已存在的 skill"""
        from skill_engine.cli import app
        import typer.testing

        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: test\n---\nNew", encoding="utf-8")

        target = tmp_path / "installed"
        target.mkdir()
        existing = target / "test-skill"
        existing.mkdir()
        (existing / "SKILL.md").write_text("---\nname: old\n---\nOld", encoding="utf-8")

        runner = typer.testing.CliRunner()
        result = runner.invoke(app, ["install", str(skill_dir), "-t", str(target)])

        assert result.exit_code == 0
        content = (existing / "SKILL.md").read_text(encoding="utf-8")
        assert "Old" in content  # 未被覆盖