"""run_python：Python 代码执行工具。

替代 write_file 临时脚本 + bash python，或 bash python -c（cmd 引号易错）。
用临时文件 + executor（自动注入 venv）执行，规避 shell 引号转义。
"""

from skill_engine.execution.tool_exec.bash_util import format_observation
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class RunPythonHandler(BaseHandler):
    name = "run_python"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        import tempfile as _tempfile
        code = tc["input"].get("code", "").strip()
        try:
            timeout = int(tc["input"].get("timeout", 30))
        except (TypeError, ValueError):
            timeout = 30
        if not code:
            obs = "[run_python] code 不能为空"
        else:
            _tmp_path = None
            try:
                # 临时文件写到系统 temp（不污染工作目录）；离开上下文时清理
                with _tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False, encoding="utf-8"
                ) as _tf:
                    _tf.write(code)
                    _tmp_path = _tf.name
                cmd = f'python "{_tmp_path}"'
                exec_result = ctx.executor.run_step(cmd, cwd=ctx.base_dir, timeout=timeout)
                obs = format_observation("run_python", exec_result)
            except Exception as e:
                obs = f"[run_python] 执行准备失败：{e}"
            finally:
                if _tmp_path is not None:
                    try:
                        import os as _os
                        _os.unlink(_tmp_path)
                    except Exception:
                        pass
        print(f"     [run_python] {code[:60].replace(chr(10), ' ')}")
        return ToolResult(
            tool_call_id=tc["id"], name="run_python",
            content=obs,
            step={"name": f"run_python_{tc['id']}", "type": "run_python"},
        )
