"""Phase 11: LLM-Native Skill Designer

职责：
1. 构造 CREATE_SKILL_PROMPT，引导 LLM 生成结构化 skill 定义
2. 从 LLM 响应中提取 JSON（3 层容错）
3. 校验 design 的完整性和合法性
"""

import json
import re
from typing import Optional

from .builtins import WRITE_TO_FILE_PY, READ_FILE_PY, SAFE_MATH_EVAL_PY

# ================================================================
# CREATE_SKILL_PROMPT
# ================================================================
# 设计原则：
# - 少说教，多举例。LLM 学样比学规则快。
# - 内置脚本源码直接嵌入，让 LLM 复用逻辑而非从零编写。
# - scripts 的 key 是相对路径（不加 scripts/ 前缀）。
# - 不包含模型名、硬编码路径、内置模板引用机制。

CREATE_SKILL_PROMPT = """你是一个精通 agentskills.io 规范的 Skill 架构师。
根据用户意图，输出一个 JSON 对象，描述一个完整的 Skill。

---

【内置脚本参考】（你可以直接复用其逻辑，请生成完整的脚本内容）
--- write_to_file.py ---
{write_to_file_content}
--- read_file.py ---
{read_file_content}
--- safe_math_eval.py ---
{safe_math_eval_content}

---

【输出格式】
仅输出 JSON，不要包含任何解释文字。

{{
  "name": "slug-style-name",
  "description": "一句话描述 skill 的作用",
  "when_to_use": "详细描述什么场景下应该触发这个 skill",
  "arguments": ["参数1", "参数2"],
  "groups": ["分组1", "分组2"],
  "steps": [
    {{
      "name": "step1",
      "type": "exec",
      "command": "具体的 shell 命令",
      "timeout": 30
    }},
    {{
      "name": "step2",
      "type": "llm",
      "template": "LLM 的 prompt 模板，清晰描述要做什么",
      "timeout": 60
    }},
    {{
      "name": "step3",
      "type": "write",
      "template": "写入文件的内容模板",
      "output_file": "output/结果文件.md"
    }},
    {{
      "name": "step4",
      "type": "read",
      "input_ref": "要读取的文件路径"
    }}
  ],
  "scripts": {{
    "脚本文件名1.py": "#!/usr/bin/env python3\\n...完整的 Python 脚本内容..."
  }},
  "assets": {{
    "参考文档.md": "# 参考文档内容..."
  }}
}}

注意：
1. scripts 的 key 是相对于 scripts/ 目录的文件名，不要加 scripts/ 前缀
2. assets 的 key 是相对于 assets/ 目录的路径，不要加 assets/ 前缀
3. exec 步骤的 command 可以用 python 脚本，也可以直接用 shell 命令
4. 所有文件路径使用相对路径，不要硬编码绝对路径
5. llm 步骤的 template 必须清晰描述要做什么

【示例】
用户意图：帮我写一个统计 Python 代码行数的 skill

输出：
{{
  "name": "count-lines",
  "description": "统计指定目录下所有 Python 文件的代码行数，生成报告",
  "when_to_use": "用户需要分析代码库规模、统计项目代码量、生成项目报告时",
  "arguments": ["path"],
  "groups": ["development", "analysis"],
  "steps": [
    {{
      "name": "scan_files",
      "type": "exec",
      "command": "python scripts/find_py_files.py $path",
      "timeout": 30
    }},
    {{
      "name": "count_lines",
      "type": "exec",
      "command": "python scripts/count_lines.py",
      "timeout": 30
    }},
    {{
      "name": "generate_report",
      "type": "llm",
      "template": "根据以下代码统计结果生成一份简洁的代码分析报告：\\n\\n扫描结果：{{scan_files}}\\n行数统计：{{count_lines}}",
      "timeout": 60
    }}
  ],
  "scripts": {{
    "find_py_files.py": "#!/usr/bin/env python3\\nimport sys\\nimport os\\n\\npath = sys.argv[1] if len(sys.argv) > 1 else '.'\\npy_files = []\\nfor root, dirs, files in os.walk(path):\\n    for f in files:\\n        if f.endswith('.py'):\\n            py_files.append(os.path.join(root, f))\\nfor f in py_files:\\n    print(f)"
  }},
  "assets": {{}}
}}

---

【当前用户意图】
{intent}
"""


def extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON，3 层容错

    1. 直接 json.loads()
    2. 匹配 ```json ... ``` 代码块
    3. 贪婪匹配第一个 { 到最后一个 }

    Args:
        text: LLM 的原始输出

    Returns:
        dict 如果成功提取，否则 None
    """
    # Tier 1: 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Tier 2: 匹配 ```json ... ``` 代码块
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Tier 3: 贪婪匹配第一个 { 到最后一个 }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


_VALID_STEP_TYPES = {"exec", "llm", "read", "write"}

_REQUIRED_DESIGN_FIELDS = ["name", "description", "steps"]


def validate_design(design: dict) -> list[str]:
    """校验 LLM 生成的 design 是否完整合法

    Args:
        design: LLM 返回的设计 dict

    Returns:
        错误列表，空列表表示校验通过
    """
    errors = []

    # 必填字段
    for field in _REQUIRED_DESIGN_FIELDS:
        if field not in design:
            errors.append(f"缺少必要字段: {field}")

    if errors:
        return errors

    # steps 校验
    steps = design.get("steps", [])
    if not isinstance(steps, list):
        errors.append("steps 必须是列表")
        return errors

    if not steps:
        errors.append("steps 不能为空列表")
        return errors

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{i}] 必须是对象")
            continue
        if "name" not in step:
            errors.append(f"steps[{i}] 缺少 name 字段")
        if "type" not in step:
            errors.append(f"steps[{i}] 缺少 type 字段")
        elif step["type"] not in _VALID_STEP_TYPES:
            errors.append(f"steps[{i}] 无效的 type: {step['type']}（合法值: {', '.join(sorted(_VALID_STEP_TYPES))}）")

    # name 格式校验
    name = design.get("name", "")
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', name):
        errors.append(f"name 格式无效: '{name}'（应为小写字母、数字、连字符、下划线组成）")

    return errors


class SkillDesigner:
    """LLM 驱动的 Skill 设计师

    职责：
    1. 接收自然语言意图
    2. 构造 prompt 调 LLM 生成 JSON design
    3. 提取和校验 JSON
    """

    def __init__(self):
        self._prompt_template = CREATE_SKILL_PROMPT

    def design(self, intent: str, llm) -> dict:
        """根据自然语言意图，让 LLM 生成完整的 skill 定义

        Args:
            intent: 自然语言描述（如"帮我写一个分析代码质量的 skill"）
            llm: LangChain LLM 客户端实例

        Returns:
            校验通过的 design dict

        Raises:
            ValueError: 如果 JSON 提取失败或校验失败
        """
        # 1. 构造 prompt
        prompt = self._prompt_template.format(
            intent=intent,
            write_to_file_content=WRITE_TO_FILE_PY,
            read_file_content=READ_FILE_PY,
            safe_math_eval_content=SAFE_MATH_EVAL_PY,
        )

        # 2. 调 LLM
        print("[INFO] 正在发送请求到 LLM...")
        import sys
        sys.stdout.flush()
        resp = llm.invoke(prompt)

        # 3. 提取文本
        raw_text = ""
        if hasattr(resp, "content"):
            raw_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        else:
            raw_text = str(resp)

        # 4. 提取 JSON
        design = extract_json(raw_text)
        if design is None:
            raise ValueError(
                f"LLM 返回格式错误，无法提取有效 JSON。原始输出:\n{raw_text[:1000]}"
            )

        # 5. 校验
        errors = validate_design(design)
        if errors:
            error_msg = "; ".join(errors)
            raise ValueError(
                f"LLM 生成的 design 校验失败: {error_msg}\n"
                f"原始输出:\n{raw_text[:1000]}"
            )

        return design