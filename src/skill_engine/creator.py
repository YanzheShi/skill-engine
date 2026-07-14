"""Phase 11+12: Skill 自动创建 + 自动验证

Runner 新增方法：
- create_skill(): LLM 调用，自动生成 SKILL.md + scripts/ + assets/
- validate_skill(): 验证新生成的 skill（compile + dry-run）

Registry 新增方法：
- register_new_skill(): 将新生成的 skill 注册到 index（热加载）

增强功能：
- Steps DSL 注入（## Steps 部分）
- Assets 目录支持
- 结构化 body 生成
"""

import yaml
from pathlib import Path
from typing import Optional


class SkillCreator:
    """Skill 自动创建器（纯机械，不碰 LLM）

    职责：
    1. 创建 skill 目录 + SKILL.md（含 Steps DSL 注入）
    2. 生成配套 scripts/ 目录
    3. 生成配套 assets/ 目录
    4. 验证生成的 skill 是否合法

    使用方式：
    >>> creator = SkillCreator(base_dir="skills")
    >>> result = creator.create(
    ...     name="markdown-to-pdf",
    ...     description="将 Markdown 文件转换为 PDF",
    ...     groups=["documents", "conversion"],
    ...     steps=[Step(name="convert", type="exec", command="pandoc")],
    ... )
    >>> print(result["status"])  # "success" / "failed"
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
        arguments: Optional[list[str]] = None,
        body_template: str = "",
        scripts: Optional[dict[str, str]] = None,
        assets: Optional[dict[str, str]] = None,
        steps: Optional[list] = None,
    ) -> dict:
        """创建 skill

        Args:
            name: skill 名称
            description: 描述
            groups: 分组标签
            when_to_use: 适用场景
            argument_hint: 参数提示
            arguments: 命名参数定义列表（如 ["theme", "form"]）
            body_template: SKILL.md 正文模板
                           ""（空字符串）→ 自动生成结构化 body
                           非空 → 使用自定义模板
            scripts: {文件名: 内容} 配套脚本
            assets: {相对路径: 内容} 配套资产文件（写入 assets/ 目录）
            steps: Step 对象列表或 dict 列表，序列化为 ## Steps 部分

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
        if arguments:
            fm["arguments"] = arguments

        frontmatter = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # 3. 生成 body
        if body_template:
            body = self._format_body(body_template, name, description)
        elif steps or arguments:
            body = self._generate_structured_body(name, description, steps, arguments)
        else:
            body = f"# {name}\n\n{description}"

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

        # 5. 生成 assets 目录（可选）
        if assets:
            written = self._write_assets(skill_dir / "assets", assets)
            # 报告写入失败的文件（assets 中未成功写入的）
            failed = set(assets.keys()) - set(written)
            for fname in sorted(failed):
                errors.append(f"写入 assets/{fname} 失败")

        # 6. 验证
        validation_errors = self.validate(skill_dir, body)
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

    def validate(self, skill_dir: Path, body: str = "") -> list[str]:
        """验证 skill 目录是否合法

        检查：
        1. SKILL.md 存在且 frontmatter 格式正确
        2. frontmatter 包含 name 和 description
        3. scripts/ 下的文件存在（如果声明了）
        4. assets/ 下的文件存在（如果声明了）
        5. body 中引用的脚本/资产文件存在
        """
        errors = []
        skill_md = skill_dir / "SKILL.md"

        if not skill_md.exists():
            return ["SKILL.md 不存在"]

        content = skill_md.read_text(encoding="utf-8")
        fm_dict, body_from_file = self._parse_frontmatter(content)

        if "name" not in fm_dict:
            errors.append("frontmatter 缺少 name 字段")
        if "description" not in fm_dict:
            errors.append("frontmatter 缺少 description 字段")

        # 使用传入的 body 或从文件解析
        check_body = body if body else body_from_file

        # 检查 scripts/ 引用的文件是否存在
        if check_body:
            import re
            script_refs = re.findall(r"\$\{SKILL_SCRIPTS_DIR\}/(\S+)", check_body)
            for ref in script_refs:
                if not (skill_dir / "scripts" / ref).exists():
                    errors.append(f"引用的脚本不存在: scripts/{ref}")

            # 检查 assets/ 引用
            asset_refs = re.findall(r"\$\{SKILL_ASSETS_DIR\}/(\S+)", check_body)
            for ref in asset_refs:
                if not (skill_dir / "assets" / ref).exists():
                    errors.append(f"引用的资产不存在: assets/{ref}")

            # 检查 [REF: ...] 引用
            ref_refs = re.findall(r"\[REF:\s*([^]]+)\]", check_body)
            for ref in ref_refs:
                ref_path = skill_dir / ref.strip()
                if not ref_path.exists():
                    errors.append(f"引用的支持文件不存在: {ref}")

        return errors

    def _format_body(self, body_template: str, name: str, description: str) -> str:
        """格式化 body 模板，安全处理 ${...} 占位符"""
        import re
        placeholders = {}
        counter = [0]

        def replacer(m):
            key = f'__PLACEHOLDER_{counter[0]}__'
            counter[0] += 1
            placeholders[key] = m.group(0)
            return key

        body = re.sub(r'\$\{[^}]+\}', replacer, body_template)
        body = body.format(name=name, description=description)
        for key, original in placeholders.items():
            body = body.replace(key, original)
        return body

    def _generate_structured_body(
        self,
        name: str,
        description: str,
        steps: Optional[list],
        arguments: Optional[list[str]],
    ) -> str:
        """生成结构化 body，包含 Steps DSL 部分

        当 steps 参数提供时，生成 ## Steps 部分；
        否则生成通用多轮执行指引。
        """
        lines = [
            f"# {name}",
            f"",
            f"{description}",
            f"",
            f"## 工作流程",
            f"",
            f"按以下步骤顺序执行。每步的输出可被后续步骤引用。",
            f"",
        ]

        # 添加 Steps 部分
        if steps:
            serialized = self._serialize_steps_to_body(steps)
            lines.append(serialized)
            lines.append("")

        # 添加参数部分
        if arguments:
            lines.append("## 参数")
            lines.append("")
            for arg in arguments:
                lines.append(f"- `{arg}`: 用户提供的参数")
            lines.append("")

        # 添加注意事项
        lines.append("## 注意事项")
        lines.append("")
        lines.append("- 按列表顺序确定性地执行步骤")
        lines.append("- 使用 `{step_name}` 引用上一步的输出")
        lines.append("- 使用 `$param_name` 引用命名参数")
        lines.append("- 创建目录前先确保父目录存在")

        return "\n".join(lines)

    def _serialize_steps_to_body(self, steps: list) -> str:
        """将 Step 对象列表序列化为 SKILL.md body 中的 ## Steps 部分

        格式：YAML-block 列表，每个 step 以 `- name:` 开头。
        """
        lines = ["## Steps", ""]

        for i, step in enumerate(steps):
            # 转换为 dict（如果是 Step 对象）
            if hasattr(step, "model_dump"):
                step_dict = step.model_dump(exclude_none=True)
            elif hasattr(step, "dict"):
                step_dict = step.dict(exclude_none=True)
            elif isinstance(step, dict):
                step_dict = step
            else:
                continue

            # 序列化每个 step 为 YAML block
            step_yaml = yaml.dump(
                step_dict,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            # yaml.dump 返回的格式是:
            # name: xxx
            # type: xxx
            # 需要改为列表项格式:
            # - name: xxx
            #   type: xxx
            step_lines = step_yaml.rstrip().split("\n")
            indented = []
            for line in step_lines:
                indented.append(f"  {line}")
            block = "- " + indented[0].lstrip("  ") + "\n" + "\n".join(indented[1:])

            if i > 0:
                lines.append("")
            lines.append(block)

        return "\n".join(lines)

    def _write_assets(self, asset_dir: Path, assets: dict[str, str]) -> list[str]:
        """写入 assets 目录

        Returns:
            写入的文件名列表（失败时返回空列表，错误通过 caller 处理）
        """
        written = []
        try:
            asset_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return written

        for rel_path, content in assets.items():
            target = asset_dir / rel_path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                written.append(rel_path)
            except OSError:
                pass
        return written

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