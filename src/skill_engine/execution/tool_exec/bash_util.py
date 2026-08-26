"""bash 相关工具函数：命令路径提取 / 输出格式化 / 报错诊断 / 环境头生成。"""

import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

from skill_engine.execution.paths import to_native_path


# ---------------------------------------------------------------------------
# bash 后文件登记选择性失效
# ---------------------------------------------------------------------------
# 引号包裹的 token，或裸的相对/盘符路径 token（要求含目录分隔符或形如
# 盘符开头，避免把 "python"、"git" 等普通词误判为路径）。
_PATH_TOKEN_RE = re.compile(
    r"""(?:"(?P<dq>[^"]+)"|'(?P<sq>[^']+)')
        |(?P<raw>(?:\.{1,2}[/\\]|[A-Za-z]:[/\\]|[\w.+-]+[/\\])[\w .\-@+()\[\]{}~#%'"\\]*)
    """,
    re.VERBOSE,
)


def _extract_cmd_paths(cmd: str, base_dir):
    """从 bash 命令中尽力提取命令可能涉及的文件/目录路径。

    Returns:
        list[Path]: 解析到且位于工作目录内的路径；token 含目录分隔符或
            命令执行后路径真实存在（touch 新建等）才保留——引号包裹的普通
            字符串（如 echo "hi"）不会误判成文件
        None: 命令中提取不到任何路径 token → 影响面未知，调用方应保守
            全量失效（invalidate_all，与旧行为一致）。
    """
    base = Path(base_dir).resolve()
    tokens = []
    saw_pathish = False   # 出现过「形似路径」的 token（含引号包裹或分隔符）
    for m in _PATH_TOKEN_RE.finditer(cmd):
        t = (m.group("dq") or m.group("sq") or m.group("raw") or "").strip()
        if not t or t in (".", ".."):
            continue
        saw_pathish = True
        # 含 .. 组件的 token（../../etc/hosts 等）：可能逃逸工作目录，直接跳过
        if ".." in t.replace("\\", "/").split("/"):
            continue
        quoted = m.group("dq") is not None or m.group("sq") is not None
        native = to_native_path(t)
        p = Path(native) if Path(native).is_absolute() else (base / t)
        try:
            rp = p.resolve()
            rp.relative_to(base)
        except (OSError, ValueError):
            continue
        # 引号包裹的 token 一律保留（echo "x" > "out.txt" 的重定向目标靠它命中）；
        # 裸 token 要求含分隔符或命令执行后真实存在（touch 新建等）。
        if quoted or "/" in t or "\\" in t or rp.exists():
            tokens.append(rp)
    if not saw_pathish:
        # 命令里压根没有路径形 token（echo hi / git status）→ 影响面未知，保守全失效
        return None
    return tokens


def format_observation(cmd: str, exec_result: dict) -> str:
    """格式化 bash 执行结果为结构化 observation，包含 exit_code 等关键字段

    让 LLM 能区分"成功"（exit_code: 0）和"失败"（exit_code: 非零），
    避免空 stdout 时 LLM 无谓重试。

    Args:
        cmd: 原始命令
        exec_result: executor.run_step() 返回的结果 dict

    Returns:
        格式化后的 observation 字符串（≤20000 chars）
    """
    exit_code = exec_result.get("exit_code", -1)
    stdout = exec_result.get("stdout", "")
    stderr = exec_result.get("stderr", "")
    timed_out = exec_result.get("timed_out", False)

    lines = [f"exit_code: {exit_code}"]
    if timed_out:
        lines.append("(timed_out)")
        # 测试超时（非失败）专属提示：日志实证 pytest 全量超时后模型反复无分析重跑。
        # 此处硬提示收窄到单测，避免空转。
        combined = (stdout + "\n" + stderr)
        if "pytest" in combined or "test session" in combined or "collected" in combined:
            lines.append(
                "hint: 测试超时（非失败），不是用例报错。先收窄范围再跑："
                "`pytest tests/xxx.py::test_y -x`（单文件/单用例），"
                "或检查 DB 锁 / 死循环 /  fixture 慢；不要无分析地重复跑全量。"
            )
        else:
            lines.append(
                "hint: 命令执行超时。先确认是否死循环/长耗时操作，或主动传 timeout 参数；"
                "不要在没改代码的情况下原样重跑。"
            )
    if stdout:
        lines.append("stdout:")
        if len(stdout) > 8000:
            lines.append(stdout[:8000] + f"\n... (stdout 已截断，原长 {len(stdout)} 字符)")
        else:
            lines.append(stdout)
    if stderr:
        lines.append("stderr:")
        # stderr 不静默截断：完整回灌，仅在超长时截断并明确标注截断量，让模型能看到真实报错。
        if len(stderr) > 8000:
            lines.append(stderr[:8000] + f"\n... (stderr 已截断，原长 {len(stderr)} 字符)")
        else:
            lines.append(stderr)
    hint = _diagnose_shell_error(stderr)
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)[:20000]


# stderr 特征 -> 可执行的纠正提示。命中即在 observation 里补一行 hint，
# 打断"模型看不懂报错 -> 换个花样再试 -> 又失败"的空转循环。
_SHELL_ERROR_HINTS = (
    (("WinError 267", "目录名称无效", "The directory name is invalid"),
     "工作目录路径无效。Windows 上不要用 /d/... 或 /mnt/d/... 这种 Git Bash / WSL 写法，"
     "改用 D:\\path\\to\\dir。命令已在工作目录中执行，通常根本不需要 cd。"),
    (("不是内部或外部命令", "is not recognized as an internal or external command"),
     "当前 shell 是 cmd.exe，不是 bash。ls/pwd/cat/grep/touch 都不可用，"
     "对应改用 dir/cd/type/findstr，或直接改用 read_file / search_files 等跨平台工具。"),
    (("WinError 2", "系统找不到指定的文件", "The system cannot find the file specified"),
     "可执行文件或路径不存在。先用 search_files / read_file 确认路径，再执行。"),
    # Pydantic 字段错误专属提示（放在 Python 错误组最前，优先匹配）。
    # 典型报错：ValueError: <field> is not a valid field for <Model>
    #           / AttributeError: <Model> has no attribute '<field>'
    #           / ValidationError: extra fields not permitted
    # 根因：先改了业务逻辑但数据模型尚未声明该字段 → Pydantic 拒动态属性。
    # 应先改 models 再改业务。
    (("is not a valid field", "extra fields not permitted", "has no attribute", "ValidationError"),
     "Pydantic 模型没有该字段——需**先在模型类（models.py / schema.py）中声明字段**，"
     "再在业务代码中使用。典型顺序：先改数据模型 → 再改业务逻辑（database/service）→ 最后改接口（router/api）。"
     "不要反过来（先改逻辑导致模型缺字段而验证失败空转）。"),
    (("ModuleNotFoundError", "No module named"),
     "模块未安装或 PYTHONPATH 未覆盖。用 pip install 安装缺失模块，"
     "或确认脚本在正确的虚拟环境（.venv）中运行——引擎已自动注入 venv 环境。"),
    (("no such column", "OperationalError", "sqlite3.OperationalError"),
     "SQL 查询引用了不存在的列。先用 PRAGMA table_info(表名) 或 .schema 确认表结构列名，再修正查询。"),
    (("SyntaxError", "syntax error", "IndentationError"),
     "Python 语法/缩进错误。检查括号匹配、缩进、引号闭合；"
     "在 cmd.exe 上 python -c 的多层引号易出错，改用 write_file 写完整脚本再执行。"),
    (("KeyError", "TypeError", "AttributeError"),
     "运行时对象访问错误。先用 read_file 确认相关变量/字段的真实结构，"
     "不要凭记忆假设字段存在（压缩后旧轮思考可能已折叠）。"),
)


def _diagnose_shell_error(stderr: str) -> str:
    """根据 stderr 匹配已知失败模式，返回一句可执行的纠正提示（无匹配返回空串）。"""
    if not stderr:
        return ""
    for needles, hint in _SHELL_ERROR_HINTS:
        if any(n in stderr for n in needles):
            return hint
    return ""


def build_env_header(base_dir: Path, shell: str = "") -> str:
    """生成注入给 LLM 的环境说明块。

    没有这段说明时，模型只能靠猜：在 Windows + cmd.exe 上照样发 `ls -la`、`pwd`，
    再拿 Git Bash 风格的 `/d/...` 路径去 `cd`，一路失败还看不出原因。
    把 OS / shell / 工作目录 / 路径风格显式告诉它，这类空转就消失了。

    Args:
        base_dir: 本次运行的工作目录（已归一化为原生路径）
        shell: Executor 实际使用的 shell（"cmd" / "bash" / "wsl"）

    Returns:
        <env> 块 + 环境约定的文本，供拼在 final_prompt 之前
    """
    is_win = os.name == "nt"
    os_name = "Windows" if is_win else ("macOS" if sys.platform == "darwin" else "Linux")

    if shell == "cmd":
        shell_desc = "cmd.exe（Windows 命令提示符，不是 bash）"
        shell_rule = (
            "- Shell 是 cmd.exe：不要用 ls / pwd / cat / grep / touch / rm / "
            "`mkdir -p`，对应改用 dir / cd / type / findstr / del / mkdir。"
        )
    elif shell == "wsl":
        shell_desc = "WSL bash（命令在 WSL 中执行，路径为 /mnt/<盘符>/... 形式）"
        shell_rule = "- Shell 是 WSL bash：可用标准 Unix 命令。"
    else:
        shell_desc = "bash"
        shell_rule = "- Shell 是 bash：可用标准 Unix 命令。"

    path_rule = (
        "- 路径一律用 Windows 原生写法（D:\\a\\b 或 D:/a/b）。不要用 /d/... 或 "
        "/mnt/d/... —— 那是 Git Bash / WSL 的写法，本机 Python 无法识别，"
        "会直接报 [WinError 267] 目录名称无效。"
        if is_win else
        "- 路径用 POSIX 写法（/home/x/proj）。"
    )

    return (
        "<env>\n"
        f"操作系统: {os_name}\n"
        f"Shell: {shell_desc}\n"
        f"工作目录: {base_dir}\n"
        "</env>\n\n"
        "环境约定（务必遵守）:\n"
        "- bash 工具的命令**已经**在上述「工作目录」中执行，不需要也不要 cd 过去。\n"
        f"{path_rule}\n"
        "- 相对路径直接以工作目录为基准，例如 src/demo/main.py。\n"
        "- 读写/搜索文件优先用 read_file / write_file / edit_file / search_files 工具，"
        "它们跨平台且比 bash 可靠；bash 只用于跑测试、构建等真正需要 shell 的场景。\n"
        f"{shell_rule}\n"
        "- 涉及当前日期/时间/年份的问题，必须先调用 get_current_time 获取，"
        "不要凭训练知识猜测（训练数据有截止时间）。\n"
        "- 禁止用 `taskkill /IM <镜像名>`、`pkill <名称>`、`kill` 按镜像名/进程名"
        "批量终止进程——这会误杀 skill-engine 自身（它也是 python 进程），导致任务中断。"
        "需要重启/停止某服务时，用该服务的**显式 PID** 定向操作"
        "（如 `taskkill /PID 1234`），不要按镜像名全量杀。\n\n"
    )
