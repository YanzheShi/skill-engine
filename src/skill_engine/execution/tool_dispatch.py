"""档位 B：Tool Dispatch 循环（Agent Loop）

职责：
1. 编译 final prompt（Assembler）
2. 绑定工具（bind_tools）
3. LLM 循环调用  解析 tool_calls  执行  追加 tool message  回到 3
4. 无 tool_calls  返回最终答案

与 Runner 的交互：
- 通过 approval_fn 回调（Runner._check_approval）处理安全审批
- 直接使用 executor 和 assembler 实例
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Callable

from skill_engine.models import Skill, MatchResult, TurnPolicy, RunResult
from skill_engine.execution.assembler import Assembler
from skill_engine.execution.executor import Executor
from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools, load_mcp_tools
from skill_engine.execution.context_manager import ContextManager, default_context_budget
from skill_engine.execution.human_io import HumanIO
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.paths import to_native_path
from skill_engine.security.scanner import should_approve
from skill_engine.config import TAVILY_API_KEY
from langchain_core.tools import tool

import re
import json
import shutil
import subprocess

logger = logging.getLogger(__name__)

# bash 工具超时参数硬上限：测试/构建等长命令由 LLM 按需传 timeout，
# 引擎守住上限，防止失控命令拖垮会话（P0 S0-4）。
BASH_MAX_TIMEOUT = 600


# ---------------------------------------------------------------------------
# 编辑应用：精确匹配优先，失败走行级宽松模糊匹配（P2-1）
# ---------------------------------------------------------------------------
def _norm_ws(line: str) -> str:
    """行内多空白归一化为单空格 + 去首尾空白"""
    return re.sub(r"\s+", " ", line).strip()


def _fuzzy_find(content: str, old: str):
    """在 content 中按"连续行窗口"做宽松匹配，返回唯一候选的 (start, end) 字符区间。

    两级比较：先整行 strip，再归一化空白；任一唯一命中即返回。
    0 个或 >1 个候选都返回 None（无法唯一确定，交由上层报错）。
    """
    old_lines = old.split("\n")
    n = len(old_lines)
    if n == 0:
        return None
    # 预计算 content 每行的起止字符索引（兼容 \\n；\\r\\n 的行尾空白由 strip 处理）
    lines = []
    start = 0
    for line in content.split("\n"):
        lines.append((start, start + len(line), line))
        start += len(line) + 1  # +1 为一个换行符

    def candidates(transform):
        target = [transform(l) for l in old_lines]
        out = []
        for i in range(len(lines) - n + 1):
            window = [transform(lines[i + k][2]) for k in range(n)]
            if window == target:
                out.append((lines[i][0], lines[i + n - 1][1]))
        return out

    for transform in (lambda x: x.strip(), _norm_ws):
        c = candidates(transform)
        if len(c) == 1:
            return c[0]
    return None


def _apply_edits(content: str, edits: list) -> tuple:
    """对 content 应用 edits，精确匹配优先，失败走行级宽松模糊匹配。

    Returns:
        (new_content, None)            成功
        (None, error_message)          失败（error 信息会回传给 LLM 以便重试）
    """
    if not edits:
        return None, "error: edits 列表为空"
    for edit in edits:
        if not edit.get("oldText"):
            return None, "error: edit 项缺少 oldText"
    # 重复歧义：模糊匹配无法消除，直接报错（与原行为一致）
    for e in edits:
        c = content.count(e["oldText"])
        if c > 1:
            return None, f"error: oldText 在文件中出现 {c} 次（需唯一）: {e['oldText'][:80]}"
    # 全精确：每个 oldText 恰出现 1 次
    exact = all(content.count(e["oldText"]) == 1 for e in edits)
    if exact:
        positioned = sorted(edits, key=lambda e: content.find(e["oldText"]))
        search_pos = 0
        out = []
        for e in positioned:
            idx = content.find(e["oldText"], search_pos)
            out.append(content[search_pos:idx])
            out.append(e["newText"])
            search_pos = idx + len(e["oldText"])
        return "".join(out) + content[search_pos:], None
    # 模糊：至少一处不存在，逐 edit 锁定区间（基于原始 content，避免串扰）
    segments = []
    for e in edits:
        old = e["oldText"]
        if content.count(old) == 1:
            idx = content.find(old)
            segments.append((idx, idx + len(old), e["newText"]))
        else:
            m = _fuzzy_find(content, old)
            if m is None:
                return None, f"error: oldText 不存在（精确与模糊匹配均失败）: {old[:80]}"
            segments.append((m[0], m[1], e["newText"]))
    segments.sort(key=lambda s: s[0])
    out = []
    pos = 0
    for s, en, new in segments:
        out.append(content[pos:s])
        out.append(new)
        pos = en
    return "".join(out) + content[pos:], None


def _resolve_path(filepath: str, base_dir: Path) -> Path:
    """解析文件路径：绝对路径透传，否则基于 base_dir

    Args:
        filepath: 用户/LLM 传入的路径字符串
        base_dir: 基准目录（working_root 或 skill.directory）

    Returns:
        解析后的 Path 对象
    """
    # 归一化：展开 ~，并在 Windows 上把 /d/x、/mnt/d/x 转成 D:\x
    # （模型常按 Git Bash 习惯给路径，不转换会被当成相对路径拼错）
    p = to_native_path(filepath)
    if p is None:
        return Path(base_dir)
    if p.is_absolute():
        return p
    # 相对路径
    return Path(base_dir) / p


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
        f"{shell_rule}\n\n"
    )


def _read_file_with_lines(content: str, offset: int = 0, limit: int = 0) -> str:
    """读取文件内容，带行号，可选分页

    Args:
        content: 文件原文
        offset: 起始行号（0-indexed），0=从头
        limit: 返回行数，0=不限

    Returns:
        带行号的文件内容字符串
    """
    lines = content.splitlines(keepends=False)
    total = len(lines)

    if offset == 0 and limit == 0:
        # 无参数：返回全文 + 行号
        numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(lines))
        if total > 200:
            numbered += f"\n(file has {total} lines, pass offset=0&limit={total} to read all)"
        return numbered

    # 显式分页
    start = offset
    end = offset + limit if limit else total
    snippet = lines[start:end]
    numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(snippet, start=start))
    if end < total:
        numbered += f"\n(truncated, showing lines {start+1}-{end} of {total})"
    return numbered


def format_observation(cmd: str, exec_result: dict) -> str:
    """格式化 bash 执行结果为结构化 observation，包含 exit_code 等关键字段

    让 LLM 能区分"成功"（exit_code: 0）和"失败"（exit_code: 非零），
    避免空 stdout 时 LLM 无谓重试。

    Args:
        cmd: 原始命令
        exec_result: executor.run_step() 返回的结果 dict

    Returns:
        格式化后的 observation 字符串（≤10000 chars）
    """
    exit_code = exec_result.get("exit_code", -1)
    stdout = exec_result.get("stdout", "")
    stderr = exec_result.get("stderr", "")
    timed_out = exec_result.get("timed_out", False)

    lines = [f"exit_code: {exit_code}"]
    if timed_out:
        lines.append("(timed_out)")
    if stdout:
        lines.append("stdout:")
        lines.append(stdout[:8000])
    if stderr:
        lines.append("stderr:")
        lines.append(stderr[:500])
    hint = _diagnose_shell_error(stderr)
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)[:10000]


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
)


def _diagnose_shell_error(stderr: str) -> str:
    """根据 stderr 匹配已知失败模式，返回一句可执行的纠正提示（无匹配返回空串）。"""
    if not stderr:
        return ""
    for needles, hint in _SHELL_ERROR_HINTS:
        if any(n in stderr for n in needles):
            return hint
    return ""


def parse_tool_calls(response) -> list:
    """解析 LLM 响应中的 tool_calls

    兼容两种输入格式：
    - LangChain AIMessage: 有 .tool_calls 属性，元素格式 {name, args, id}
    - dict: {"content": "...", "tool_calls": [{"type", "input", "id"}]}
    - str: 纯文本（无 tool_calls）

    Returns:
        tool_calls 列表，统一格式为 {"id": str, "type": str, "input": dict}
    """
    if isinstance(response, str):
        return []

    # LangChain AIMessage: 有 .tool_calls 属性
    if hasattr(response, "tool_calls"):
        tool_calls_raw = list(response.tool_calls) if response.tool_calls else []
    elif isinstance(response, dict):
        tool_calls_raw = response.get("tool_calls", [])
    else:
        return []

    if not tool_calls_raw:
        return []

    tool_calls = []
    for tc in tool_calls_raw:
        # LangChain 格式: name->type, args->input
        # 兼容两种格式：
        #   旧格式: {"type": "bash", "input": {...}, "id": "..."}
        #   新格式: {"name": "bash", "args": {...}, "id": "...", "type": "tool_call"}
        tool_type = tc.get("type", tc.get("name", "unknown"))
        # 如果 type 是 "tool_call"（LangChain 占位值），用 name 作为工具名
        if tool_type == "tool_call":
            tool_type = tc.get("name", "unknown")
        tool_calls.append({
            "id": tc.get("id", f"call_{len(tool_calls)}"),
            "type": tool_type,
            "input": tc.get("input", tc.get("args", {})),
        })

    return tool_calls



# ---------------------------------------------------------------------------
# P0(S0-2)：search_files 双实现 —— ripgrep 优先（gitignore-aware、毫秒级），
# 无 rg 二进制时回退纯 Python。独立函数实现，不进巨链（见设计文档 §7）。
# ---------------------------------------------------------------------------
_RG_TIMEOUT = 15           # ripgrep 子进程超时（秒）
_SEARCH_DEFAULT_MAX = 100  # search_files 默认结果上限（旧实现为 50）
_SEARCH_MAX_CAP = 500      # max_results 硬上限


def _format_match(rel: str, lineno, text: str) -> str:
    """统一的搜索结果行格式：rel:行号: 内容（截 120 字）。"""
    return f"{rel}:{lineno}: {text.strip()[:120]}"


def _run_ripgrep(pattern: str, search_dir: Path, file_glob: str, max_results: int):
    """ripgrep 实现。返回 None 表示 rg 不可用/执行失败（调用方回退纯 Python）。

    rg 原生尊重 .gitignore；以 search_dir 为 cwd、相对路径 '.' 执行，
    避免 Windows 绝对路径的盘符冒号破坏 'path:line:text' 解析。
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "400",
           "--no-require-git"]  # 无 git 仓库时也要尊重 .gitignore（rg 默认仅仓库内生效）
    if file_glob:
        cmd += ["--glob", file_glob]
    target = "." if search_dir.is_dir() else search_dir.name
    try:
        proc = subprocess.run(
            cmd + ["--", pattern, target],
            cwd=str(search_dir if search_dir.is_dir() else search_dir.parent),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_RG_TIMEOUT,
        )
    except Exception:
        return None
    if proc.returncode not in (0, 1):  # 0=有匹配，1=无匹配；其他视为失败 → 回退
        return None
    matches, total = [], 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        rel, lineno, text = parts
        rel = rel.lstrip("./\\")
        total += 1
        if len(matches) < max_results:
            matches.append(_format_match(rel, lineno, text))
    if not matches:
        return "no matches found"
    out = "\n".join(matches)
    if total > len(matches):
        out += f"\n... (已显示 {len(matches)} 条，共 {total} 条匹配；请收窄 pattern 或 path)"
    return out


def _python_search(pattern: str, search_dir: Path, file_glob: str, max_results: int) -> str:
    """纯 Python 回退实现（无 rg 依赖）：rglob + 逐行正则，语义与旧内联版一致。"""
    import fnmatch
    import re as re_module
    matches = []
    total_size = 0
    overflow = False
    try:
        files = [search_dir] if search_dir.is_file() else sorted(search_dir.rglob("*"))
        for f in files:
            if overflow:
                break
            if not f.is_file():
                continue
            if any(p.startswith(".") for p in f.parts):
                continue
            if file_glob and not fnmatch.fnmatch(f.name, file_glob):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.splitlines(), 1):
                    if re_module.search(pattern, line):
                        if len(matches) < max_results:
                            rel = f.relative_to(search_dir) if search_dir.is_dir() else f.name
                            match_line = _format_match(str(rel), i, line)
                            matches.append(match_line)
                            total_size += len(match_line) + 1
                            if total_size > 8000:
                                overflow = True
                                break
                        else:
                            overflow = True  # 已够数且还有更多 → 记截断
                            break
                if overflow:
                    break
            except (UnicodeDecodeError, PermissionError, OSError):
                continue
    except Exception:
        pass
    if not matches:
        return "no matches found"
    out = "\n".join(matches)
    if overflow:
        out += f"\n... (结果已截断，显示 {len(matches)} 条；请收窄 pattern 或 path)"
    return out

def _search_files(pattern: str, search_dir: Path, file_glob: str = "", max_results: int = 0) -> str:
    """search_files 统一入口：ripgrep 优先，失败回退纯 Python。"""
    mr = max_results if max_results and max_results > 0 else _SEARCH_DEFAULT_MAX
    mr = min(mr, _SEARCH_MAX_CAP)
    result = _run_ripgrep(pattern, search_dir, file_glob, mr)
    if result is None:
        result = _python_search(pattern, search_dir, file_glob, mr)
    return result


# ---------------------------------------------------------------------------
# P0(S0-4b)：verify_command 自动验证钩子 —— 轮内写盘完成后跑一次，
# 失败时把结构化信号回灌 LLM，驱动"改→验→修"闭环（不依赖 prompt 自觉）。
# 命令来自 frontmatter（作者声明、与 Steps DSL 命令同级可信），不走运行时审批。
# ---------------------------------------------------------------------------
def _extract_test_failures(output: str) -> list:
    """从 pytest 风格输出中提取 FAILED/ERROR 清单行（上限 20 条）。"""
    fails = []
    for ln in (output or "").splitlines():
        s = ln.strip()
        if s.startswith(("FAILED", "ERROR")):
            fails.append(s[:200])
            if len(fails) >= 20:
                break
    return fails


def _run_verification(executor, base_dir: Path, verify_command: str, timeout: int):
    """运行 verify_command。成功返回 None；失败返回回灌给 LLM 的反馈文本。"""
    try:
        r = executor.run_step(verify_command, cwd=base_dir, timeout=timeout)
    except Exception as e:
        return f"[自动验证执行异常] {verify_command}\n{e}"
    if r.get("exit_code", -1) == 0 and not r.get("timed_out"):
        return None
    fails = _extract_test_failures((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
    lines = [
        f"[自动验证失败] 命令: {verify_command}",
        f"exit_code: {r.get('exit_code', -1)}" + (" (timed_out)" if r.get("timed_out") else ""),
        "请根据以下失败信息诊断并修复，然后再次验证。",
    ]
    if fails:
        lines.append("失败清单:")
        lines.extend(f"  {x}" for x in fails)
    err = (r.get("stderr") or "").strip()
    if err:
        lines.append("stderr:\n" + err[:1500])
    out = (r.get("stdout") or "").strip()
    if out:
        lines.append("stdout(尾部):\n" + out[-1500:])
    return "\n".join(lines)[:4000]



# ---------------------------------------------------------------------------
# S1-3：编辑 diff 预览 —— 写盘前用 difflib 生成 unified diff（零依赖），
# 按 confirm_edits 逐次/逐文件确认。默认关闭，其他 skill 零影响。
# ---------------------------------------------------------------------------
_DIFF_MAX_LINES = 200  # diff 超过该长度截断展示（全文重写类的大 diff）


def _render_diff(path: str, old: str, new: str) -> str:
    """生成供展示/确认的 unified diff；过长时截断并附提示。"""
    import difflib
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{path} (before)", tofile=f"{path} (after)", lineterm=""))
    total = len(lines)
    if total > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES] + [f"... (diff 共 {total} 行，仅显示前 {_DIFF_MAX_LINES} 行)"]
    return "\n".join(lines) if lines else "(内容无变化)"


class ToolDispatchRunner:
    """档位 B：tool_dispatch 循环（CC 原生 skill 兼容）

    工作流程：
    1. 编译 final prompt 作为 system message
    2. 调用 LLM(llm.invoke(messages))  返回 {content, tool_calls}
    3. 有 tool_calls  Executor 执行  追加 tool message  回到 2
    4. 无 tool_calls  判断 human_in_loop  问用户 / 结束
    """

    def __init__(
        self,
        executor: Executor,
        assembler: Assembler,
        approval_fn: Optional[Callable] = None,
        human_io: Optional[HumanIO] = None,
        turn_policy: Optional[TurnPolicy] = None,
        working_root: Optional[str] = None,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn  # Runner._check_approval 回调
        self.human_io = human_io
        self.turn_policy = turn_policy
        # 归一化：Windows 下允许用户传 Git Bash / WSL 风格路径（/d/x、/mnt/d/x）
        self.working_root = to_native_path(working_root)
        # S1-3：batch 模式下会话内已批准的文件（run_repl 复用同一 runner 实例，跨轮存活）
        self._file_edit_approvals: set = set()
        self._confirm_edits_mode = "" 

    def _truncate_msg(self, content: str, max_chars: int = 5000) -> str:
        """Truncate tool result message content to prevent context overflow.

        Full content is preserved in step_results for logging.
        """
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n...(truncated, {len(content)} chars total, showing first {max_chars})"

    def _check_file_safety(self, op_type: str, filepath: str, skill: Skill) -> tuple[bool, str]:
        """检查文件操作的安全性

        Args:
            op_type: 操作类型（read/write/edit）
            filepath: 目标文件路径
            skill: 当前 skill

        Returns:
            (approved, error_message)
        """
        from skill_engine.security.scanner import RISKY_FILENAMES
        # 直接检查文件名（不依赖 _path_escapes 的正则提取）
        if Path(filepath).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 敏感文件操作已取消"

        decision, reason = should_approve(
            f"{op_type}:{filepath}", skill.directory, risk_hint="tool_file"
        )
        if decision == "BLOCK":
            return False, f"[安全拦截] {reason}"
        elif decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 操作已取消"
        return True, ""

    # ---------------- S1-3：编辑 diff 预览与确认（confirm_edits） ----------------
    def _confirm_edit(self, op: str, filepath: str, diff_text: str) -> bool:
        """diff 预览门。返回 True=放行落盘，False=用户拒绝。

        模式（frontmatter confirm_edits）：
        - "true" ：每次编辑都确认
        - "batch"：逐文件确认——某文件首次编辑询问，批准后本会话内该文件自动放行
        无 human_io（非交互场景）降级为仅展示、不阻断。
        """
        mode = self._confirm_edits_mode
        if mode == "batch" and filepath in self._file_edit_approvals:
            print(f"     [diff 预览] {op} → {filepath}（本会话已批准该文件，自动放行）")
            print(diff_text)
            return True
        if self.human_io is None:
            print(f"     [diff 预览] {op} → {filepath}（非交互模式：仅展示，直接应用）")
            print(diff_text)
            return True
        ask = ("允许吗？(y 允许 / n 拒绝)" if mode != "batch"
               else "允许吗？(y 允许并记住该文件 / n 拒绝)")
        self.human_io.emit(f"📝 编辑预览 [{op} → {filepath}]\n{diff_text}\n{ask}")
        answer = (self.human_io.read() or "").strip().lower()
        approved = answer in ("y", "yes", "是", "好", "ok")
        if approved and mode == "batch":
            self._file_edit_approvals.add(filepath)
        return approved

    def run(
        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
        initial_messages: Optional[list] = None,
        session_mode: bool = False,
        snapshot: Optional[FileSnapshot] = None,
        file_tracker: Optional[FileStateTracker] = None,
    ) -> dict:
        """执行 tool_dispatch 循环

        Args:
            match_result: 匹配结果
            llm: LLM 客户端（需支持 bind_tools）
            max_iterations: 最大迭代次数
            state_path: 可选，运行状态落盘路径（JSON）。每轮结束后写入，
                供后续 resume_from 续跑。不传则不持久化。
            resume_from: 可选，从指定状态文件续跑（载入 messages / 进度），
                继续对话而非重头来过。与 state_path 同源时即为"中断后续跑"。
            initial_messages: 可选，直接以该历史起轮（session 续轮用）。
            session_mode: 会话模式。注入 ask_user 工具，并在无 tool_calls 时
                立即以 stopped_by="session_turn_end" 返回，把控制权交还编排层。
            snapshot: 可选，外部注入的 FileSnapshot 实例。不传则每次 run() 新建
                （检查点=本次运行起点）；session 模式由 run_repl 传入同一实例，
                使 restore_file 能回滚到整个会话的起点而非本轮起点。
            file_tracker: 可选，外部注入的 FileStateTracker 实例。不传则每次 run()
                新建（软/硬约束由 skill 的 strict_file_tracking 决定）；session 模式
                由 run_repl 传入同一实例，使"已读"登记跨轮有效。

        Returns:
            执行结果 dict
        """
        skill = match_result.skill
        final_prompt = self.assembler.assemble(skill, match_result.arguments)

        # P2-2：通用文件快照（检查点），记录写文件前的原始内容供回滚
        base_dir = self.working_root or Path(skill.directory)

        # 环境头：显式告诉模型 OS / shell / 工作目录 / 路径风格。
        # 缺了这段，模型在 Windows 上会照发 ls/pwd 与 /d/... 路径，空转到迭代上限。
        final_prompt = build_env_header(base_dir, getattr(self.executor, "shell", "")) + final_prompt
        # 复用外部快照时，_recorded 集合得以跨轮保留，第 2 轮的首次写入不会
        # 覆盖第 1 轮记录的 .bak（否则只能回滚到本轮起点）。
        self._snapshot = snapshot if snapshot is not None else FileSnapshot(base_dir)
        # P0(S0-1)：文件状态跟踪（read-before-write）。外部注入时跨轮复用（session）；
        # 否则本次运行新建。软约束为默认，skill 可声明 strict_file_tracking 升级硬约束。
        if file_tracker is not None:
            self._file_tracker = file_tracker
        else:
            self._file_tracker = FileStateTracker(
                strict=bool(getattr(skill.metadata, "strict_file_tracking", False)))
        # P0(S0-4b)：skill 声明的自动验证命令（frontmatter 作者声明，可信）
        verify_command = (getattr(skill.metadata, "verify_command", "") or "").strip()
        # S1-3：编辑 diff 预览模式（''/off 关闭；'true' 逐次确认；'batch' 逐文件确认）
        self._confirm_edits_mode = str(getattr(skill.metadata, "confirm_edits", "") or "").strip().lower()
        try:
            verify_timeout = int(getattr(skill.metadata, "verify_timeout", 120) or 120)
        except (TypeError, ValueError):
            verify_timeout = 120

        # 合并内建工具 + 该 skill 自带的领域工具，再按 allowed/disallowed 过滤
        skill_tools_map: dict[str, object] = {}
        if hasattr(llm, "bind_tools"):
            skill_extra = load_skill_tools(skill)
            skill_mcp = load_mcp_tools(skill)  # 方案 A：接入 mcp.json 声明的远程 MCP 工具
            # P2-2：内置 restore_file 工具（通用文件检查点回滚，不依赖 git）
            snap = self._snapshot

            @tool
            def restore_file(path: str) -> str:
                """回滚文件到本次运行开始前的快照状态（通用文件检查点，不依赖 git）。

                引擎在每次修改文件前会自动记录其原始内容。出错时可调用本工具
                将该文件恢复到修改前的状态。

                Args:
                    path: 要回滚的文件路径（绝对，或相对工作目录）
                """
                target = Path(path)
                if not target.is_absolute():
                    target = (base_dir / path).resolve()
                else:
                    target = target.resolve()
                ok, msg = snap.restore(target)
                return msg

            # session 模式：注入 ask_user 工具（轮内暂停，向用户提问）；普通 run 不暴露
            session_tools = []
            if session_mode:
                @tool
                def ask_user(question: str = "") -> str:
                    """在 session 持续会话中向用户提问并等待回答（轮内暂停）。

                    当你需要用户的某个具体决策/确认才能继续当前任务时调用本工具，
                    例如"选择方案 A 还是 B"。引擎会暂停并读取用户输入，把回答作为本
                    工具的返回值回灌给你，你据此继续当前轮（不结束会话）。

                    若只是在汇报进度或等待用户给出下一条指令，不要调用本工具，
                    直接输出文本即可——引擎会在每轮结束后自动把控制权交还用户。
                    """
                    if self.human_io:
                        if question:
                            self.human_io.emit(question)
                        return self.human_io.read()
                    return ""
                session_tools = [ask_user]
            skill_extra_with_restore = skill_extra + [restore_file] + session_tools
            # 方案 A：MCP 远程工具并入。同名时优先保留内建工具与 restore_file，
            # 避免远程工具意外覆盖核心文件操作（bash/read_file/edit_file/...）。
            builtin_names = set(TOOL_REGISTRY.keys()) | {"restore_file"}
            skill_mcp_safe = [t for t in skill_mcp if t.name not in builtin_names]
            if len(skill_mcp_safe) != len(skill_mcp):
                logger.warning(
                    "MCP 工具存在与内建同名的项，已跳过被覆盖的 %d 个",
                    len(skill_mcp) - len(skill_mcp_safe),
                )
            skill_tools_map = {t.name: t for t in skill_extra_with_restore + skill_mcp_safe}
            tools = list(TOOL_REGISTRY.values()) + skill_extra_with_restore + skill_mcp_safe
            disallowed = getattr(skill.metadata, "disallowed_tools", None) or []
            allowed = getattr(skill.metadata, "allowed_tools", None) or []
            if disallowed:
                tools = [t for t in tools if t.name not in disallowed]
            if allowed:
                tools = [t for t in tools if t.name in allowed]
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        # 上下文管理：三级渐进压缩（P0 S0-3）。预算默认贴长会话需求
        # （SKILLS_ENGINE_CONTEXT_BUDGET 可覆盖）；L1 折叠与 L2 压缩模板可按 skill 配置。
        ctx = ContextManager(
            budget=getattr(skill.metadata, "context_budget", 0) or default_context_budget(),
            compact_tool_output=bool(getattr(skill.metadata, "compact_tool_output", True)),
            summary_prompt=(getattr(skill.metadata, "compress_template", "") or ""),
        )
        messages = ctx.messages

        iterations = 0
        step_results = []
        files_created = []

        # P2-3：todo 落盘续跑 —— 若给定 resume_from，载入上次运行状态继续对话
        save_path = state_path or resume_from
        if initial_messages is not None:
            # session 模式续轮：直接用历史起轮，final_prompt 已含在 initial_messages 中
            ctx.messages[:] = list(initial_messages)
            messages = ctx.messages
        elif resume_from:
            loaded = self._load_state(resume_from)
            if loaded is not None:
                if loaded.get("session_mode") and not session_mode:
                    logger.warning(
                        "状态文件 %s 由 session 模式产生（含多轮用户指令/ask_user 交互），"
                        "正以普通 run 载入：ask_user 工具不可用且不会在轮末交还控制权。"
                        "如需续接会话请改用 `session --resume-from`。",
                        resume_from,
                    )
                ctx.messages[:] = loaded.get("messages", [])
                messages = ctx.messages
                iterations = loaded.get("iterations", 0)
                step_results = loaded.get("step_results", [])
                files_created = loaded.get("files_created", [])
                # final_prompt 已含在 messages 中，不再重复追加
            else:
                messages.append({"role": "user", "content": final_prompt})
        else:
            messages.append({"role": "user", "content": final_prompt})

        result = None
        try:
            for i in range(max_iterations):
                iterations += 1

                print(f"\n=== Iteration {iterations}/{max_iterations} ===")
                print(f"  Messages in history: {len(messages)} items")

                # 上下文压缩：接近 token 预算时自动摘要压缩旧历史（保持首条与最近轮次）
                ctx.maybe_compress(llm)

                # 调用 LLM（带 rate limit 退避重试）
                resp = None
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        resp = llm_with_tools.invoke(messages)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower() or "exhaust" in err_str.lower():
                            wait_time = 3 * (attempt + 1)
                            time.sleep(wait_time)
                            if attempt == max_retries - 1:
                                return RunResult(
                                    output=f"[LLM 调用被限流（已重试 {max_retries} 次）: {err_str}]",
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "rate_limited"},
                                    history=messages[:] if 'messages' in dir() else [],
                                )
                        else:
                            return RunResult(
                                output=f"[LLM 调用失败: {err_str}]",
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "error"},
                                history=messages[:] if 'messages' in dir() else [],
                            )

                # 每轮 LLM 调用之间加短暂延迟，降低触发 rate limit 的概率
                time.sleep(0.5)

                # 标准化 LLM 响应为 dict（兼容 LangChain AIMessage）
                if hasattr(resp, "tool_calls"):
                    resp = {
                        "content": resp.content if hasattr(resp, "content") else str(resp),
                        "tool_calls": list(resp.tool_calls) if resp.tool_calls else [],
                    }
                elif not isinstance(resp, dict):
                    resp = {"content": str(resp), "tool_calls": []}

                # 解析 tool_calls
                tool_calls = parse_tool_calls(resp)

                print(f"  LLM response: content={len(resp.get('content', ''))} chars, tool_calls={len(tool_calls)}")
                if tool_calls:
                    for tc in tool_calls:
                        print(f"    - {tc['type']}: {tc['input']}")

                if not tool_calls:
                    text = resp.get("content", "")
                    messages.append({"role": "assistant", "content": text})

                    if session_mode:
                        # session 模式：子任务完成文本，由外层 REPL 处理后等待下条指令。
                        # 禁用内部 human_in_loop 追问循环（决策 2），避免双重提问。
                        step_results.append({"name": "llm_response", "type": "llm", "output": text})
                        return RunResult(
                            output=text,
                            ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                 "iterations": iterations, "stopped_by": "session_turn_end"},
                            history=messages,
                        )

                    if self.human_io and self.turn_policy:
                        # 多轮对话模式
                        if self.turn_policy.should_stop(text):
                            # LLM 说完了  直接结束，不追问用户
                            step_results.append({"name": "llm_response", "type": "llm", "output": text})
                            return RunResult(
                                output=text,
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "stop"},
                                history=messages,
                            )
                        else:
                            # LLM 在问用户  emit + read + 追 history + 继续
                            self.human_io.emit(text)
                            user_input = self.human_io.read()

                            # 用户退出
                            if user_input in (self.turn_policy.user_exit or []):
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "user_exit"},
                                    history=messages,
                                )

                            # 达到最大轮数
                            if iterations >= self.turn_policy.max_turns:
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "max_turns"},
                                    history=messages,
                                )

                            # 追加用户回答，继续循环
                            messages.append({"role": "user", "content": user_input})
                            continue

                    # 非多轮模式：原行为
                    step_results.append({"name": "llm_response", "type": "llm", "output": text})
                    return RunResult(
                        output=text,
                        ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                             "iterations": iterations, "stopped_by": "stop"},
                        history=messages,
                    )

                # 有 tool_calls，执行每个
                lc_tool_calls = []
                for tc in tool_calls:
                    lc_tool_calls.append({
                        "id": tc["id"],
                        "name": tc["type"],
                        "args": tc["input"],
                    })
                messages.append({
                    "role": "assistant",
                    "content": resp.get("content", ""),
                    "tool_calls": lc_tool_calls,
                })

                round_had_write = False
                for tc in tool_calls:
                    if tc["type"] == "stop":
                        return RunResult(
                            output=tc["input"].get("reason", "stopped"),
                            ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                 "iterations": iterations, "stopped_by": "tool_stop"},
                            history=messages,
                        )

                    elif tc["type"] == "bash":
                        cmd = tc["input"].get("command", "")
                        decision, reason = should_approve(cmd, skill.directory, risk_hint="tool_dispatch")
                        if decision == "BLOCK":
                            logging.getLogger("skill_engine.tool_dispatch").warning(
                                f"tool_dispatch bash 被安全拦截: {cmd[:80]}"
                            )
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": "",
                                "error": "[安全拦截] tool_dispatch 命令不自动执行",
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": "[安全拦截] tool_dispatch 命令不自动执行",
                            })
                            print(f"     BLOCKED: {cmd[:80]}")
                            continue
                        elif decision == "ATTENTION":
                            if self.approval_fn:
                                approved = self.approval_fn(
                                    skill.metadata.name,
                                    cmd.split()[0] if cmd else "",
                                    cmd,
                                )
                            else:
                                approved = False
                            if not approved:
                                step_results.append({
                                    "name": f"bash_{tc['id']}",
                                    "type": "bash",
                                    "command": cmd,
                                    "output": "",
                                    "error": "[用户跳过] 操作已取消",
                                })
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": "bash",
                                    "content": "[用户跳过] 操作已取消",
                                })
                                print(f"     REJECTED by user: {cmd[:80]}")
                                continue
                        # SAFE 或审批通过：执行命令
                        # P0(S0-4)：LLM 可为测试/构建等长命令传 timeout（秒），
                        # 引擎钳制到 BASH_MAX_TIMEOUT；不传则沿用 Executor 默认。
                        try:
                            req_timeout = int(tc["input"].get("timeout", 0) or 0)
                        except (TypeError, ValueError):
                            req_timeout = 0
                        exec_timeout = min(req_timeout, BASH_MAX_TIMEOUT) if req_timeout > 0 else None
                        try:
                            base_dir = self.working_root or Path(skill.directory)
                            exec_result = self.executor.run_step(cmd, cwd=base_dir, timeout=exec_timeout)
                            # P0(S0-1)：bash 可能改过任何文件 → 保守失效文件读取登记
                            self._file_tracker.invalidate_all()
                            obs = format_observation(cmd, exec_result)
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": obs[:500],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": self._truncate_msg(obs),
                            })
                            obs_lines = obs.split("\n")
                            print(f"     {obs_lines[0]}")
                            for ol in obs_lines[1:]:
                                if ol.strip():
                                    print(f"      {ol}")
                        except Exception as e:
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": "",
                                "error": str(e),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": f"[执行失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "read_file":
                        filepath = tc["input"].get("path", "")
                        offset = int(tc["input"].get("offset", 0))
                        limit = int(tc["input"].get("limit", 0))

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("read", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        try:
                            content = full_path.read_text(encoding="utf-8")
                            # P0(S0-1)：登记"已读版本"，供后续 edit 一致性校验
                            self._file_tracker.on_read(full_path)
                            formatted = _read_file_with_lines(content, offset, limit)
                            step_results.append({
                                "name": f"read_{tc['id']}",
                                "type": "read_file",
                                "path": str(full_path),
                                "output": formatted[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": self._truncate_msg(formatted),
                            })
                            print(f"     read {len(formatted)} chars from {filepath}")
                        except FileNotFoundError:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": f"[文件不存在: {filepath}]",
                            })
                            print(f"     FILE NOT FOUND: {filepath}")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "read_file",
                                "content": f"[读取失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "write_file":
                        filepath = tc["input"].get("path", "")
                        content = tc["input"].get("content", "")

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("write", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        # S1-3：diff 预览门（仅 confirm_edits 开启时进入）
                        if self._confirm_edits_mode in ("true", "batch"):
                            old_content = full_path.read_text(encoding="utf-8") if full_path.exists() else ""
                            diff_text = _render_diff(filepath, old_content, content)
                            if not self._confirm_edit("write_file", str(full_path), diff_text):
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": "write_file",
                                    "content": "[用户拒绝了本次写入] 文件未变更。请调整方案或与用户澄清需求。",
                                })
                                print(f"     WRITE REJECTED by user: {filepath}")
                                continue

                        try:
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            # P2-2：写回前记录快照（仅已存在文件，避免回滚到"已删除"状态）
                            if full_path.exists():
                                self._snapshot.record(full_path, full_path.read_text(encoding="utf-8"))
                            full_path.write_text(content, encoding="utf-8")
                            files_created.append(str(full_path))
                            self._file_tracker.on_write(full_path)
                            round_had_write = True
                            step_results.append({
                                "name": f"write_{tc['id']}",
                                "type": "write_file",
                                "path": str(full_path),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": f"wrote {len(content)} bytes to {filepath}",
                            })
                            print(f"     wrote {len(content)} bytes to {filepath}")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": f"[写入失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "edit_file":
                        filepath = tc["input"].get("path", "")
                        edits = tc["input"].get("edits", [])

                        if not edits:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": "error: edits 列表为空",
                            })
                            continue

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("edit", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        # 解析路径
                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)

                        if not full_path.exists():
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": f"error: 文件不存在: {filepath}（新文件请用 write_file）",
                            })
                            print(f"     FILE NOT FOUND: {filepath}")
                            continue

                        # P0(S0-1)：编辑前一致性校验。软约束（默认）注入提示不阻断；
                        # 硬约束（strict_file_tracking）拒绝执行，引导 LLM 重读后重试。
                        track_ok, track_msg = self._file_tracker.check_editable(full_path)
                        if not track_ok:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": track_msg,
                            })
                            print(f"     EDIT BLOCKED (file tracker): {filepath}")
                            continue

                        try:
                            content = full_path.read_text(encoding="utf-8")

                            # 校验 + 应用：精确优先，oldText 不存在时走行级宽松模糊匹配（P2-1）
                            new_content, err = _apply_edits(content, edits)
                            if err:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": "edit_file",
                                    "content": err + (f"\nhint: {track_msg}" if track_msg else ""),
                                })
                                print(f"     EDIT FAILED: {err[:80]}")
                                continue

                            # S1-3：diff 预览门（仅 confirm_edits 开启时进入）
                            if self._confirm_edits_mode in ("true", "batch"):
                                diff_text = _render_diff(filepath, content, new_content)
                                if not self._confirm_edit("edit_file", str(full_path), diff_text):
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "name": "edit_file",
                                        "content": "[用户拒绝了本次编辑] 文件未变更。请调整编辑方案或与用户澄清需求。",
                                    })
                                    print(f"     EDIT REJECTED by user: {filepath}")
                                    continue

                            # 写回前记录快照（P2-2：通用文件检查点，仅首次记录进入前状态）
                            self._snapshot.record(full_path, content)

                            # 写回文件
                            full_path.write_text(new_content, encoding="utf-8")
                            files_created.append(str(full_path))
                            self._file_tracker.on_write(full_path)
                            round_had_write = True

                            result_msg = f"applied {len(edits)} edits to {filepath}"
                            if track_msg:
                                result_msg += f"\n{track_msg}"
                            step_results.append({
                                "name": f"edit_{tc['id']}",
                                "type": "edit_file",
                                "path": str(full_path),
                                "edits_count": len(edits),
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": result_msg,
                            })
                            print(f"     {result_msg}")

                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "edit_file",
                                "content": f"[编辑失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "search_files":
                        pattern = tc["input"].get("pattern", "")
                        search_path = tc["input"].get("path", ".")
                        file_glob = tc["input"].get("file_glob", "")
                        if not pattern:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": "error: pattern 不能为空",
                            })
                            continue
                        base_dir = self.working_root or Path(skill.directory)
                        search_dir = _resolve_path(search_path, base_dir)
                        if not search_dir.exists():
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": f"[路径不存在: {search_path}]",
                            })
                            continue
                        try:
                            # P0(S0-2)：ripgrep 优先（gitignore-aware），无 rg 自动回退纯 Python
                            max_results_req = int(tc["input"].get("max_results", 0) or 0)
                            result = _search_files(pattern, search_dir, file_glob, max_results_req)
                            n_matches = 0 if result == "no matches found" else result.count("\n") + 1
                            step_results.append({
                                "name": f"search_{tc['id']}",
                                "type": "search_files",
                                "pattern": pattern,
                                "path": str(search_dir),
                                "matches": n_matches,
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": self._truncate_msg(result),
                            })
                            print(f"     search '{pattern}' in {search_dir}: {n_matches} matches")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "search_files",
                                "content": f"[搜索失败: {e}]",
                            })
                            print(f"     ERROR: {e}")

                    elif tc["type"] == "web_search":
                        query = tc["input"].get("query", "")
                        max_results = int(tc["input"].get("max_results", 5))
                        if not query:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "web_search",
                                "content": "error: query 不能为空",
                            })
                            print("     web_search: empty query")
                            continue
                        api_key = TAVILY_API_KEY
                        if not api_key:
                            obs = "Search failed: TAVILY_API_KEY is not set. Get a free key at https://app.tavily.com"
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "web_search",
                                "content": obs,
                            })
                            print(f"     web_search: {obs}")
                            continue
                        try:
                            from tavily import TavilyClient
                            max_results = max(1, min(10, max_results))
                            client = TavilyClient(api_key=api_key)
                            result = client.search(query, max_results=max_results)
                            results = result.get("results", [])
                            obs = json.dumps(results, ensure_ascii=False)
                            step_results.append({
                                "name": f"web_search_{tc['id']}",
                                "type": "web_search",
                                "query": query,
                                "output": obs[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "web_search",
                                "content": self._truncate_msg(obs),
                            })
                            print(f"     web_search '{query}': {len(results)} results")
                        except Exception as e:
                            obs = f"Search failed: {e}"
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "web_search",
                                "content": obs,
                            })
                            print(f"     web_search ERROR: {e}")

                    elif tc["type"] == "get_current_time":
                        timezone = tc["input"].get("timezone", "Asia/Shanghai")
                        try:
                            import urllib.request
                            import json as _json
                            url = f"https://timeapi.io/api/Time/current/zone?timeZone={urllib.request.quote(timezone)}"
                            req = urllib.request.Request(url, headers={"User-Agent": "skill-engine/1.0"})
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                data = _json.loads(resp.read().decode("utf-8"))
                            obs = _json.dumps(data, ensure_ascii=False)
                            step_results.append({
                                "name": f"get_current_time_{tc['id']}",
                                "type": "get_current_time",
                                "timezone": timezone,
                                "output": obs[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "get_current_time",
                                "content": self._truncate_msg(obs),
                            })
                            print(f"     get_current_time {timezone}: {data.get('dateTime', '?')}")
                        except Exception as e:
                            obs = f"Get time failed: {e}"
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "get_current_time",
                                "content": obs,
                            })
                            print(f"     get_current_time ERROR: {e}")

                    else:
                        # ---- skill 自带工具的通用执行分支（工具可插拔接口）----
                        # 内建工具名都已在上面的 if/elif 中处理，这里只兜底 skill 注入的领域工具。
                        # bind_tools 只负责"让 LLM 知道有这些工具"，真正执行需在此分支完成。
                        if tc["type"] in skill_tools_map:
                            tool_obj = skill_tools_map[tc["type"]]
                            base_dir = self.working_root or Path(skill.directory)
                            prev_cwd = os.getcwd()
                            try:
                                os.chdir(str(base_dir))  # 与 bash 一致的基准目录
                                result = tool_obj.invoke(tc["input"])
                            except Exception as e:
                                result = f"[工具执行失败: {e}]"
                            finally:
                                os.chdir(prev_cwd)
                            content = result if isinstance(result, str) else str(result)
                            step_results.append({
                                "name": f"skill_tool_{tc['id']}",
                                "type": tc["type"],
                                "output": content[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": tc["type"],
                                "content": self._truncate_msg(content),
                            })
                            print(f"     skill tool {tc['type']}: {len(content)} chars")
                            continue
                        # 既不是内建工具，也不是 skill 注入工具 → 真正未知
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": f"[未知工具类型: {tc['type']}]",
                        })

                # P0(S0-4b)：轮内写/改完成后跑一次声明的验证命令，
                # 只在失败时回灌结构化信号，驱动"改→验→修"闭环。
                if verify_command and round_had_write:
                    feedback = _run_verification(
                        self.executor, self.working_root or Path(skill.directory),
                        verify_command, verify_timeout)
                    if feedback:
                        messages.append({"role": "user", "content": feedback})
                        print("     VERIFY FAILED → 失败信号已回灌")
                    else:
                        print("     verify passed")

        # 达到最大迭代次数
            result = RunResult(
                output="[达到最大迭代次数]",
                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                     "iterations": iterations, "stopped_by": "max_iterations"},
                history=messages,
            )
            return result
        finally:
            # P2-3：任一退出路径（含提前 stop / error / max_iterations）都落盘，支撑续跑
            if save_path:
                self._save_state(save_path, messages, iterations, step_results, files_created,
                                 final_prompt, session_mode)
        return result

    # ---------------- P2-3：todo 落盘续跑（状态持久化） ----------------

    def _save_state(self, path, messages, iterations, step_results, files_created,
                    final_prompt, session_mode: bool = False):
        """将运行状态落盘为 JSON，供后续 resume_from 续跑。失败静默。

        session_mode 一并落盘：session 产生的历史含 ask_user 交互与多轮用户指令，
        若被普通 run --resume-from 载入，行为语义不同（无 ask_user 工具、
        无轮边界交还），载入侧据此给出提示。
        """
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "final_prompt": final_prompt,
                "messages": messages,
                "iterations": iterations,
                "step_results": step_results,
                "files_created": files_created,
                "session_mode": session_mode,
            }
            p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            # 状态持久化失败绝不影响主执行流程
            pass

    def _load_state(self, path):
        """读取状态文件；不存在或损坏返回 None。"""
        try:
            p = Path(path)
            if not p.exists():
                return None
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None