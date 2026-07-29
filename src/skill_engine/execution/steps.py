"""Steps DSL 确定性执行器（档位 C）

职责：
1. 解析 SKILL.md body 中的 ## Steps 部分
2. 按顺序执行步骤（exec/llm/write/read）
3. 步骤间变量传递（{variable} 引用）
4. 模板变量替换

与 Runner 的交互：
- 通过 approval_fn 回调处理安全审批
- 直接使用 executor 实例
"""

import time
import yaml
import re
from pathlib import Path
from typing import Optional, Callable

from skill_engine.models import Skill, Step
from skill_engine.execution.executor import Executor
from skill_engine.security.scanner import should_approve


def resolve_template(template: str, prev_outputs: dict, arguments: dict) -> str:
    """解析模板中的变量引用

    支持：
    - {variable}  prev_outputs[variable]
    - $VAR  arguments[VAR] 或 arguments[$VAR]
    """
    # 替换 {step_name} 引用
    for name, output in prev_outputs.items():
        template = template.replace(f"{{{name}}}", output)

    # 替换 $ 参数（先匹配完整 $KEY，再匹配 $KEY[N]）
    for key, value in arguments.items():
        if key.startswith("$"):
            # 直接匹配 $ARGUMENTS, $0, $1 等
            template = template.replace(key, str(value))
        else:
            # 命名参数，匹配 $name
            template = template.replace(f"${key}", str(value))
    # 替换 {var} 语法（与 assembler._substitute_params 对齐）
    for key, value in arguments.items():
        key_clean = key.lstrip("$")
        template = template.replace(f"{{{key_clean}}}", str(value))
    return template


def parse_steps_from_body(body: str) -> Optional[list[Step]]:
    """从 SKILL.md body 中解析 ## Steps 部分的步骤定义。

    返回 Step 列表如果找到 ## Steps 部分，否则返回 None。

    解析格式（YAML-block 列表，每个 step 以 `- name:` 开头）：
    ```
    ## Steps

    - name: fetch_problem
      type: exec
      command: python scripts/fetch_problem.py $0
      timeout: 30

    - name: save_solution
      type: write
      output_file: output/49_solution.md
      template: |
        # 题解 49
    ```
    """
    match = re.search(r'^## Steps\s*\n(.*?)(?=^## |\Z)', body, re.MULTILINE | re.DOTALL)
    if not match:
        return None

    steps_text = match.group(1).strip()
    if not steps_text:
        return None

    steps = []
    # 分割每个 step block（以 "- name:" 开头）
    step_blocks = re.split(r'\n(?=- name:)', steps_text)

    for block in step_blocks:
        block = block.strip()
        if not block:
            continue
        try:
            step_dict = yaml.safe_load(block)
            if isinstance(step_dict, list) and len(step_dict) == 1:
                step_dict = step_dict[0]
            if isinstance(step_dict, dict) and 'name' in step_dict:
                steps.append(Step(**step_dict))
        except yaml.YAMLError:
            continue

    return steps if steps else None


class StepsRunner:
    """Steps DSL 确定性执行器

    按顺序执行步骤，每步输出可作为下一步的 input_ref。
    """

    def __init__(
        self,
        executor: Executor,
        approval_fn: Optional[Callable] = None,
    ):
        self.executor = executor
        self.approval_fn = approval_fn  # Runner._check_approval 回调

    def run(
        self,
        steps: list[Step],
        arguments: dict,
        skill: Skill,
    ) -> dict:
        """按步骤执行

        Args:
            steps: 步骤列表
            arguments: 参数
            skill: Skill 对象

        Returns:
            执行结果 dict
        """
        step_outputs = {}
        step_results = []
        files_created = []

        for step in steps:
            result = self._execute_step(step, step_outputs, arguments, skill)
            step_results.append(result)

            if "output" in result:
                step_outputs[step.name] = result["output"]

            if "file_created" in result:
                files_created.append(result["file_created"])

            if getattr(step, "type", None) == "llm":
                time.sleep(1)

        if not steps:
            return {
                "skill_name": skill.metadata.name,
                "score": 1.0,
                "steps": step_results,
                "output": "",
                "files_created": files_created,
            }

        return {
            "skill_name": skill.metadata.name,
            "score": 1.0,
            "steps": step_results,
            "output": step_outputs.get(steps[-1].name, ""),
            "files_created": files_created,
        }

    def _execute_step(
        self,
        step: Step,
        prev_outputs: dict,
        arguments: dict,
        skill: Skill,
    ) -> dict:
        """执行单步"""
        if step.type == "exec":
            return self._exec_step(step, prev_outputs, arguments, skill)
        elif step.type == "llm":
            return self._llm_step(step, prev_outputs, arguments, skill)
        elif step.type == "write":
            return self._write_step(step, prev_outputs, arguments, skill)
        elif step.type == "read":
            return self._read_step(step, prev_outputs, arguments, skill)
        else:
            return {"error": f"未知 step 类型: {step.type}"}

    def _exec_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """执行 shell 命令"""
        cmd = resolve_template(step.command or "", prev_outputs, arguments)

        # 安全审批
        decision, reason = should_approve(
            cmd, skill.directory, risk_hint="step_exec"
        )
        if decision == "BLOCK":
            return {"name": step.name, "type": "exec", "command": cmd,
                    "output": "", "error": f"[安全拦截] {reason}", "exit_code": 1, "timed_out": False}
        if decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, cmd.split()[0] if cmd else "", cmd)
            else:
                approved = False
            if not approved:
                return {"name": step.name, "type": "exec", "command": cmd,
                        "output": "", "error": "[用户跳过] 操作已取消", "exit_code": 1, "timed_out": False}

        step_timeout = step.timeout
        result = self.executor.run_step(cmd, cwd=Path(skill.directory), timeout=step_timeout)
        return {
            "name": step.name,
            "type": "exec",
            "command": cmd,
            "output": result["stdout"],
            "error": result["stderr"],
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
        }

    def _llm_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """调用 LLM 生成内容（steps DSL 中的 llm 步骤）"""
        template = resolve_template(step.template or "", prev_outputs, arguments)

        try:
            from skill_engine.config import get_llm
            llm = get_llm(purpose="steps-llm")

            resp = llm.invoke(template)
            if hasattr(resp, "content"):
                output = resp.content if isinstance(resp.content, str) else str(resp.content)
            else:
                output = str(resp)

            return {
                "name": step.name,
                "type": "llm",
                "output": output,
            }
        except Exception as e:
            return {
                "name": step.name,
                "type": "llm",
                "error": str(e),
                "output": f"[LLM 调用失败: {e}]",
            }

    def _write_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """写入文件（带安全门）"""
        content = resolve_template(
            step.template or prev_outputs.get("", ""), prev_outputs, arguments
        )
        output_file = resolve_template(step.output_file or "", prev_outputs, arguments)

        # 直接检查敏感文件名（不依赖 _path_escapes 的正则提取）
        from skill_engine.security.scanner import RISKY_FILENAMES
        if Path(output_file).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, "write", output_file)
            else:
                approved = False
            if not approved:
                return {"name": step.name, "type": "write", "error": "[用户跳过] 敏感文件操作已取消"}

        # 安全门（只查路径，strict 不 BLOCK）
        decision, reason = should_approve(
            f"write:{output_file}", skill.directory, risk_hint="tool_file"
        )
        if decision == "BLOCK":
            return {"name": step.name, "type": "write", "error": f"[安全拦截] {reason}"}
        if decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, "write", output_file)
            else:
                approved = False
            if not approved:
                return {"name": step.name, "type": "write", "error": "[用户跳过] 操作已取消"}

        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(content, encoding="utf-8")

        return {
            "name": step.name,
            "type": "write",
            "file_created": output_file,
        }

    def _read_step(
        self, step: Step, prev_outputs: dict, arguments: dict, skill: Skill
    ) -> dict:
        """读取文件（带安全门）"""
        filepath = resolve_template(step.input_ref or "", prev_outputs, arguments)

        # 直接检查敏感文件名（不依赖 _path_escapes 的正则提取）
        from skill_engine.security.scanner import RISKY_FILENAMES
        if Path(filepath).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, "read", filepath)
            else:
                approved = False
            if not approved:
                return {"name": step.name, "type": "read", "error": "[用户跳过] 敏感文件操作已取消"}

        # 安全门（只查路径，strict 不 BLOCK）
        decision, reason = should_approve(
            f"read:{filepath}", skill.directory, risk_hint="tool_file"
        )
        if decision == "BLOCK":
            return {"name": step.name, "type": "read", "error": f"[安全拦截] {reason}"}
        if decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, "read", filepath)
            else:
                approved = False
            if not approved:
                return {"name": step.name, "type": "read", "error": "[用户跳过] 操作已取消"}

        try:
            content = Path(filepath).read_text(encoding="utf-8")
            return {"name": step.name, "type": "read", "output": content}
        except FileNotFoundError:
            return {"name": step.name, "type": "read", "error": f"文件不存在: {filepath}"}