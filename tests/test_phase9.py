"""
Phase 9 测试套件 — CLI 增强

测试 run --dry-run、install/uninstall/update 命令。
"""

import pytest
from pathlib import Path
import subprocess
import sys
import shutil


PROJECT_ROOT = Path(__file__).parent.parent
TEST_SKILLS_DIR = Path(__file__).parent / "fixtures" / "cli-skills"


def _run_cli(*args):
    """运行 CLI 命令，设置 UTF-8 编码"""
    import os
    env = {**os.environ, "PYTHONUTF8": "1"}
    # 使用 skill-engine 入口点（已安装到 venv Scripts）
    entry_point = os.path.join(str(PROJECT_ROOT), ".venv", "Scripts", "skill-engine.exe")
    if os.path.exists(entry_point):
        cmd = [entry_point] + list(args)
    else:
        cmd = [sys.executable, "-m", "skill_engine.cli"] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True, timeout=10,
        cwd=str(PROJECT_ROOT), env=env,
        encoding="utf-8", errors="replace",
    )


class TestCLIDryRun:
    """测试 run --dry-run"""

    def test_dry_run_output_contains_prompt(self):
        """--dry-run 输出编译后的 prompt"""
        result = _run_cli("run", "leetcode-solution-writer", "--dry-run")
        assert result.returncode == 0
        assert "[SKILL: leetcode-solution-writer]" in result.stdout
        assert "题解" in result.stdout

    def test_dry_run_no_execution(self):
        """--dry-run 不执行任何命令"""
        result = _run_cli("run", "leetcode-solution-writer", "--dry-run")
        assert result.returncode == 0
        # 不应有文件创建输出
        assert "创建的文件:" not in result.stdout or not result.stdout.split("创建的文件:")[-1].strip()


class TestCLISkillInstall:
    """测试 skill install/uninstall/update"""

    @pytest.fixture
    def temp_target(self, tmp_path):
        """临时目标目录"""
        target = tmp_path / "test_skills"
        target.mkdir()
        yield target
        if target.exists():
            shutil.rmtree(target)

    def test_install_local_path(self, temp_target):
        """从本地路径安装 skill"""
        skill_dir = TEST_SKILLS_DIR / "test-installer"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Test\n\nTest skill", encoding="utf-8")

        result = _run_cli("install", str(skill_dir), "--target", str(temp_target))
        assert result.returncode == 0
        assert "已安装" in result.stdout
        assert (temp_target / "test-installer" / "SKILL.md").exists()

    def test_install_force_overwrite(self, temp_target):
        """不加 --force 应跳过已存在的 skill，加 --force 应覆盖"""
        skill_dir = TEST_SKILLS_DIR / "test-force"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Test", encoding="utf-8")

        result = _run_cli("install", str(skill_dir), "--target", str(temp_target))
        assert result.returncode == 0

        # 再次安装不加 --force，应 warn 跳过（exit 0）
        result = _run_cli("install", str(skill_dir), "--target", str(temp_target))
        assert result.returncode == 0
        assert "已存在" in result.stdout or "跳过" in result.stdout

        # 加 --force 应覆盖
        result = _run_cli("install", str(skill_dir), "--target", str(temp_target), "--force")
        assert result.returncode == 0

    def test_uninstall(self, temp_target):
        """卸载 skill"""
        skill_dir = TEST_SKILLS_DIR / "test-uninstaller"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Test", encoding="utf-8")

        _run_cli("install", str(skill_dir), "--target", str(temp_target))
        assert (temp_target / "test-uninstaller").exists()

        result = _run_cli("uninstall", "test-uninstaller", "--target", str(temp_target))
        assert result.returncode == 0
        assert "已卸载" in result.stdout
        assert not (temp_target / "test-uninstaller").exists()

    def test_update_non_git_skill(self, temp_target):
        """更新非 git skill 应给出提示"""
        skill_dir = TEST_SKILLS_DIR / "test-updater"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Test", encoding="utf-8")

        _run_cli("install", str(skill_dir), "--target", str(temp_target))

        result = _run_cli("update", "test-updater", "--target", str(temp_target))
        assert result.returncode == 0
        assert "更新失败" in result.stdout or "已更新" in result.stdout
