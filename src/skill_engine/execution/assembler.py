"""
Assembler — Skill 编译器

职责：
1. 动态注入 `` !`command` `` 的执行结果（预处理型）
2. 参数替换（$ARGUMENTS, $0, $1, 命名参数）
3. 注入 skill 目录路径变量（${SKILL_DIR}, ${SKILL_SCRIPTS_DIR} 等）
4. 加载支持文件内容（refs）
5. 注入宪法切片（宪法 = 全局约束规则）

编译后的 final prompt 格式：
```
[SKILL: leetcode-solution-writer]
<编译后的 body>
```

注意：
- `` !`cmd` `` 是 CC 规范中的预处理语法
- CC 文档语义：substitution runs once over the original file,
  Claude only sees the final result
- 所有 spawn 委托给 Executor，Executor 是唯一 spawn 门神
"""

from pathlib import Path
from typing import Optional
import re
from skill_engine.models import Skill
from skill_engine.execution.executor import Executor


class Assembler:
    """Skill 编译器

    使用方式：
    >>> executor = Executor(timeout=30)
    >>> assembler = Assembler(executor=executor)
    >>> prompt = assembler.assemble(skill, arguments)
    """

    def __init__(
        self,
        executor: Optional[Executor] = None,
        command_timeout: int = 10,
        shell: str = "bash",
        constitution: Optional[str] = None,
    ):
        """初始化编译器

        Args:
            executor: Executor 实例，None 则创建默认
            command_timeout: 命令超时秒数
            shell: 默认 shell
            constitution: 全局约束规则（宪法切片）
        """
        self.executor = executor or Executor(timeout=command_timeout)
        self.shell = shell
        self.constitution = constitution or ""

    def assemble(
        self,
        skill: Skill,
        arguments: Optional[dict] = None,
    ) -> str:
        """编译 skill 为 final prompt

        Args:
            skill: 完整的 Skill 对象
            arguments: 解析后的参数

        Returns:
            编译后的 final prompt 字符串
        """
        arguments = arguments or {}
        body = skill.body
        skill_dir = Path(skill.directory)

        # 1. 注入目录路径变量
        body = self._inject_paths(body, skill_dir)

        # 2. 动态注入 !command（预处理型，委托 Executor）
        body = self._inject_commands(body, skill_dir)

        # 3. 参数替换
        body = self._substitute_params(body, arguments)

        # 4. 加载支持文件 refs
        body = self._inject_refs(body, skill_dir)

        # 5. 注入宪法切片
        if self.constitution:
            body = f"# 宪法约束\n{self.constitution}\n\n{body}"

        # 6. 包装
        return f"[SKILL: {skill.metadata.name}]\n{body}"

    def _inject_paths(self, body: str, skill_dir: Path) -> str:
        """注入 skill 目录路径变量"""
        body = body.replace("${SKILL_DIR}", str(skill_dir))
        body = body.replace("${SKILL_SCRIPTS_DIR}", str(skill_dir / "scripts"))
        body = body.replace("${SKILL_ASSETS_DIR}", str(skill_dir / "assets"))
        return body

    def _inject_commands(self, body: str, skill_dir: Path) -> str:
        """动态注入 !command 的执行结果（预处理型）

        CC 规范中的 `` !`cmd` `` 语法：
        - 行内: !`git diff HEAD`
        - 代码块: ```! \n 多行命令 \n ```

        所有 spawn 委托给 Executor，Executor 是唯一 spawn 门神。
        """
        # 行内: !`cmd`
        def replace_inline(match):
            cmd = match.group(1).strip()
            result = self.executor.run_preprocess(cmd, cwd=skill_dir)
            if result["exit_code"] == 0:
                return result["stdout"]
            else:
                return f"[命令失败: {result['stderr']}]"

        body = re.sub(r"!\`([^`]+)\`", replace_inline, body)

        # 代码块: ```! \n cmd \n ```
        def replace_block(match):
            cmd = match.group(1).strip()
            result = self.executor.run_preprocess(cmd, cwd=skill_dir, multiline=True)
            if result["exit_code"] == 0:
                return result["stdout"]
            else:
                return "[命令失败]"

        body = re.sub(r"```!\s*\n(.*?)```", replace_block, body, flags=re.DOTALL)

        return body

    def _substitute_params(self, body: str, arguments: dict) -> str:
        """参数替换

        替换 $ARGUMENTS, $0, $1, $name 等变量，以及 {var} 语法。
        """
        for key, value in arguments.items():
            if key.startswith("$"):
                # 替换 $N, $ARGUMENTS, $ARGUMENTS[N]
                body = body.replace(key, str(value))
            else:
                # 替换命名参数
                body = body.replace(f"${key}", str(value))
        # 替换 {var} 语法（兼容 Steps DSL 风格的模板变量）
        for key, value in arguments.items():
            key_clean = key.lstrip("$")
            body = body.replace(f"{{{key_clean}}}", str(value))
        return body

    def _inject_refs(self, body: str, skill_dir: Path) -> str:
        """加载支持文件内容并注入

        匹配模式：[REF: filename] 或 [REF: assets/template.md]
        注意：[REF:...] 是 engine 原生扩展，非 CC 规范。
        CC 规范中引用文件靠 agent 自己读，这里是 engine 增强。
        """
        def replace_ref(match):
            ref_path = match.group(1).strip()
            full_path = skill_dir / ref_path
            if full_path.exists():
                return full_path.read_text(encoding="utf-8")
            return f"[引用文件不存在: {ref_path}]"

        body = re.sub(r"\[REF:\s*([^]]+)\]", replace_ref, body)
        return body
