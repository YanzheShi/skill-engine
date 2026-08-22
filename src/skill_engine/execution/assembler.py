"""
Assembler — Skill 编译器

职责：
1. 动态注入 `` !`command` `` 的执行结果（预处理型）
2. 参数替换（$ARGUMENTS, $0, $1, 命名参数）
3. 注入 skill 目录路径变量（${SKILL_DIR}, ${SKILL_SCRIPTS_DIR} 等）
4. 加载支持文件内容（refs）
5. 注入宪法切片（宪法 = 全局约束规则）
6. 注入平台说明（Windows cmd 环境自动检测）

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


# 整改 B 层（B1/B2/B3）：默认宪法约束切片，编译进每个 skill 的 system prompt 顶部。
# 三条通用纪律（源自 agent-loop 整改终稿），对所有 skill 生效、零额外调用：
# - B1 收敛/收尾协议：判定满足即 stop，禁止已完成时继续探索。
# - B2 搜索纪律：search_files 已默认带上下文+←MATCH，读数用 read_file 完整区间。
# - B3 测试失败自愈阶梯：测失败先读清单→定位→修→重测，≤3 次仍失败则停手报告（不空转）。
DEFAULT_CONSTITUTION = """\
# 通用执行纪律（引擎强制，非建议）

## 1. 收敛与收尾协议（B1）
- 当你已确认任务完成（读到了所需信息 / 改完了目标 / 验证通过），立即调用 stop 并给总结，**不要继续发起新工具调用**。
- 上下文压缩后早前轮次的思考过程会被折叠，若需要某段旧信息，用 read_file / search_files 重新读取，不要用"我记得之前看到过"作为依据。
- 引擎会在进度提示里告知"已用 N / 上限 M 步"。剩余 ≤3 步时必须立即停止探索、用已有信息输出总结或调用 stop。

## 2. 搜索与读取纪律（B2）
- search_files 已默认带命中行前后上下文并标注 `← MATCH`，直接读结果即可，不要为"看上下文"再发起无谓的 search。
- 需要文件内容时用 read_file 一次性读完整函数区间（≤800 行的文件直接全文读），不要只盲读单行片段再反复分页。
- 同一文件不要发起超过 3 次 read_file；第一次就该读全文。

## 3. 测试失败自愈阶梯（B3）
- 运行测试失败后：①读失败清单（FAILED/ERROR 行）→ ②定位根因 → ③修复 → ④重新运行验证。
- 同一测试连续失败 ≤3 次仍不通：停止盲目重试，调用 stop 并报告具体失败信息 + 你的诊断，等待人工决策。
- 禁止"换个花样再试一次"式空转——每次重试都应基于新的根因判断。

## 4. 关键结论固化（A4b pinned block）
- 当你定位到关键根因 / 作出重要决定 / 确认某事实时，在输出中用 `<key_finding>...</key_finding>` 标签包裹这段文字。
- 上下文压缩时会原样保留这些标签内的内容（不被重新摘要、不丢失），后续轮次可直接读到，避免"压缩后忘记根因又重蹈覆辙"。
- 示例：`<key_finding>根因：get_profile 不返回 ac_rate 字段，需补 ELO 计算</key_finding>`
"""
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
        self.constitution = constitution or DEFAULT_CONSTITUTION

    def assemble(
        self,
        skill: Skill,
        arguments: Optional[dict] = None,
        plain_text: bool = False,
    ) -> str:
        """编译 skill 为 final prompt

        Args:
            skill: 完整的 Skill 对象
            arguments: 解析后的参数
            plain_text: 纯文本终端模式。为 True 时注入「不要使用 Markdown 语法」约束，
                适用于 CLI 等不渲染 Markdown 的输出环境；Web UI 等支持 Markdown 渲染的
                场景应保持默认 False。

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

        # 5.5 注入平台说明（Windows cmd 环境自动检测）
        if hasattr(self.executor, 'shell') and self.executor.shell == "cmd":
            body += (
                "\n\n## 平台说明\n"
                "注意：当前运行环境是 Windows cmd，不是 Linux bash。\n"
                "- 使用 `dir` 而不是 `ls` 列出目录\n"
                "- 使用 `python` 而不是 `python3` 运行脚本\n"
                "- 路径使用反斜杠 \\ 或正斜杠 / 均可\n"
            )

        # 5.7 注入输出格式约束（纯文本终端场景）
        if plain_text:
            body += (
                "\n\n## 输出格式要求\n"
                "注意：当前运行环境是纯文本终端（CLI），**不支持 Markdown 渲染**，"
                "你的输出会被原样打印。\n"
                "- 禁止使用 Markdown 语法：`# 标题`、`**加粗**`、`` `行内代码` ``、"
                "`| 表格 |`、`- 列表项`、`> 引用` 等标记都不会被渲染。\n"
                "- 用纯文本 + 缩进 + 空行组织内容；需要强调时用【】或全角括号标注。\n"
                "- 表格类信息改用「键：值」逐行罗列，或简单对齐的纯文本。\n"
                "- 每轮调用工具前，先用一两句纯文本简述你打算做什么、为什么（便于用户观察思考过程）。\n"
                "- 这是硬性约束：违反会让终端直接显示 Markdown 原始标记，严重损害可读性。\n"
            )

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