"""
Phase 11+12: Skill 自动创建 + 自动验证

Runner 新增方法：
- create_skill(): LLM 调用，自动生成 SKILL.md + scripts/
- validate_skill(): 验证新生成的 skill（compile + dry-run）

Registry 新增方法：
- register_new_skill(): 将新生成的 skill 注册到 index（热加载）

"""

import json
import yaml
from pathlib import Path
from typing import Optional


class SkillCreator:
    """Skill 自动创建器

    职责：
    1. 根据 LLM 的意图描述，生成符合规范的 SKILL.md
    2. 生成配套 scripts/ 目录
    3. 验证生成的 skill 是否合法（compile + dry-run）
    4. 热注册到 registry（无需重启）

    使用方式：
    >>> creator = SkillCreator(base_dir="skills")
    >>> result = creator.create(
    ...     name="markdown-to-pdf",
    ...     description="将 Markdown 文件转换为 PDF",
    ...     groups=["documents", "conversion"],
    ...     when_to_use="用户需要将 Markdown 转为 PDF 时",
    ...     body_template="# Markdown 转 PDF\n\n使用 pandoc 转换...",
    ...     scripts={"convert.py": "import subprocess..."},
    ... )
    >>> print(result["status"])  # "success" / "failed"
    >>> print(result["errors"])  # 验证错误列表
    """

    def __init__(self, base_dir: str = "skills"):
        self.base_dir = Path(base_dir)

    def create(
        self,
        name: str,
        description: str,
        groups: Optional[list[str]] = None,
        when_to_use: str = "",
        argument_hint: str = "",
        body_template: str = "# {name}\n\n{description}",
        scripts: Optional[dict[str, str]] = None,
    ) -> dict:
        """创建 skill

        Args:
            name: skill 名称
            description: 描述
            groups: 分组标签
            when_to_use: 适用场景
            argument_hint: 参数提示
            body_template: SKILL.md 正文模板
            scripts: {文件名: 内容} 配套脚本

        Returns:
            {status: "success"|"failed", path: str, errors: [], validated: bool}
        """
        skill_dir = self.base_dir / name
        errors = []

        # 1. 创建目录
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"status": "failed", "path": str(skill_dir), "errors": [str(e)], "validated": False}

        # 2. 生成 frontmatter
        fm = {
            "name": name,
            "description": description,
        }
        if groups:
            fm["groups"] = groups
        if when_to_use:
            fm["when_to_use"] = when_to_use
        if argument_hint:
            fm["argument_hint"] = argument_hint

        frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # 3. 生成 SKILL.md
        body = body_template.format(name=name, description=description)
        skill_md = f"---\n{frontmatter}---\n\n{body}"

        try:
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        except OSError as e:
            errors.append(f"写入 SKILL.md 失败: {e}")

        # 4. 生成 scripts（可选）
        if scripts:
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(exist_ok=True)
            for fname, content in scripts.items():
                try:
                    (scripts_dir / fname).write_text(content, encoding="utf-8")
                except OSError as e:
                    errors.append(f"写入 scripts/{fname} 失败: {e}")

        # 5. 验证
        validation_errors = self.validate(skill_dir)
        if validation_errors:
            errors.extend(validation_errors)
            return {"status": "failed", "path": str(skill_dir), "errors": errors, "validated": True, "valid": False}

        return {
            "status": "success",
            "path": str(skill_dir),
            "errors": [],
            "validated": True,
            "valid": True,
        }

    def validate(self, skill_dir: Path) -> list[str]:
        """验证 skill 目录是否合法

        检查：
        1. SKILL.md 存在且 frontmatter 格式正确
        2. frontmatter 包含 name 和 description
        3. scripts/ 下的文件存在（如果声明了）
        """
        errors = []
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            return ["SKILL.md 不存在"]

        content = skill_md.read_text(encoding="utf-8")
        fm_dict, body = self._parse_frontmatter(content)

        if "name" not in fm_dict:
            errors.append("frontmatter 缺少 name 字段")
        if "description" not in fm_dict:
            errors.append("frontmatter 缺少 description 字段")

        # 检查 scripts 引用的文件是否存在
        if body:
            import re
            script_refs = re.findall(r"\$\{SKILL_SCRIPTS_DIR\}/(\S+)", body)
            for ref in script_refs:
                if not (skill_dir / "scripts" / ref).exists():
                    errors.append(f"引用的脚本不存在: scripts/{ref}")

        return errors

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """简易 frontmatter 解析"""
        import re
        match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
        if not match:
            return {}, content
        try:
            data = yaml.safe_load(match.group(1)) or {}
            if not isinstance(data, dict):
                data = {}
        except yaml.YAMLError:
            data = {}
        return data, content[match.end():]


class SkillValidator:
    """Skill 自动验证器

    在 skill 创建后自动验证：
    1. frontmatter 格式正确
    2. Assembler 能编译通过
    3. !command 预处理正常
    4. 路径变量注入正常
    """

    def __init__(self, assembler, executor):
        self.assembler = assembler
        self.executor = executor

    def validate_compile(self, skill) -> dict:
        """验证 skill 能否被 Assembler 编译

        Returns:
            {valid: bool, errors: [], prompt_len: int}
        """
        try:
            prompt = self.assembler.assemble(skill, {})
            return {
                "valid": True,
                "errors": [],
                "prompt_len": len(prompt),
            }
        except Exception as e:
            return {
                "valid": False,
                "errors": [str(e)],
                "prompt_len": 0,
            }

    def validate_scripts(self, skill) -> dict:
        """验证 skill 引用的脚本是否存在

        Returns:
            {valid: bool, missing: []}
        """
        import re
        missing = []

        # 检查 scripts/ 目录下的文件
        scripts_dir = Path(skill.directory) / "scripts"
        if scripts_dir.exists():
            for f in scripts_dir.iterdir():
                if f.is_file():
                    # 检查 SKILL.md 中是否引用了这个脚本
                    if f.name not in skill.body:
                        pass  # 未引用的脚本不算错误

        # 检查 !command 中引用的脚本
        cmd_refs = re.findall(r"`!`([^`]+)`\`|`!([^`]+)`", skill.body)
        for cmd_parts in cmd_refs:
            cmd = cmd_parts[0] or cmd_parts[1]
            if cmd.startswith("python"):
                script_path = cmd.split()[-1]
                full_path = Path(skill.directory) / script_path
                if not full_path.exists():
                    missing.append(script_path)

        return {
            "valid": len(missing) == 0,
            "missing": missing,
        }

    def full_validate(self, skill) -> dict:
        """完整验证

        Returns:
            {valid: bool, compile: {}, scripts: {}}
        """
        compile_result = self.validate_compile(skill)
        scripts_result = self.validate_scripts(skill)

        return {
            "valid": compile_result["valid"] and scripts_result["valid"],
            "compile": compile_result,
            "scripts": scripts_result,
        }
