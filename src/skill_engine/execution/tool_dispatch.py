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
from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools, load_mcp_tools, _take_screenshot
from skill_engine.execution.context_manager import ContextManager, default_context_budget
from skill_engine.execution.human_io import HumanIO
from skill_engine.execution.tracer import DebugTracer, truncate
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.paths import to_native_path
from skill_engine.security.scanner import should_approve
from skill_engine.config import TAVILY_API_KEY, llm_call_interval
from langchain_core.tools import tool

import re
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# bash 工具超时参数硬上限：测试/构建等长命令由 LLM 按需传 timeout，
# 引擎守住上限，防止失控命令拖垮会话（P0 S0-4）。
BASH_MAX_TIMEOUT = 600

# IO 工具（read_file / search_files）并行执行的工作线程上限（性能诊断建议 8）。
_IO_MAX_WORKERS = 4


# ---------------------------------------------------------------------------
# 整改 A1：预算可见注入（agent-loop-redesign-final.md §3 A1）
# 模型在循环中看不到自己用了多少步 / 还剩多少步，导致「根因已定位仍无脑探索」
# 的迭代爆炸。每轮把剩余预算作为一条 system 提示临时注入，invoke 后立即 pop，
# 不进 messages 历史（不污染压缩 / 重放 / 续跑）。
# ---------------------------------------------------------------------------
def _progress_hint(iterations: int, max_iterations: int, step_results: "list|None" = None) -> str:
    """生成本轮迭代的预算进度提示（供模型自我调节收敛用）。

    三段式紧迫度（对应 trace 痛点：根因常在第 14 轮找到后仍探索 16 轮）：
    - 剩余 <= 3：紧急，立即停止新工具调用。
    - 已用 >= 一半：尽快收敛（覆盖 15~27 轮，根因定位后立刻给强信号）。
    - 否则：温和提示在剩余步内收敛。
    分阶段约束（整改 A2c）：探索预算（前 1/3）用尽但 step_results 仍无 write/edit
    → 追加「立即停止探索、转入执行」强信号，防探索阶段无限膨胀。
    """
    remaining = max(max_iterations - iterations, 0)
    pct = int(iterations * 100 / max_iterations) if max_iterations else 0
    extra = ""
    if step_results is not None:
        has_write = any(s.get("type") in ("write_file", "edit_file") for s in step_results)
        if not has_write and iterations >= max_iterations // 3:
            extra = " 你已用尽探索预算却尚未开始编辑任何文件——请立即总结已发现信息、输出改动计划并转入执行阶段；若信息不足，用一句话说明缺什么后做一次定向搜索补全。"
    if remaining <= 3:
        urgency = "⚠️ 预算即将耗尽：请立即停止发起新工具调用，用当前已有信息输出最终总结或调用 stop。"
    elif iterations >= max_iterations // 2:
        urgency = "请尽快收敛：完成判定满足即调用 stop 并给总结，禁止在已完成时继续探索。"
    else:
        urgency = "请在剩余步数内收敛：完成判定满足即调用 stop 并给总结，避免无关探索。"
    return (
        f"[进度] 已用 {iterations} / 上限 {max_iterations} 步（剩余 {remaining}，已用 {pct}%）。"
        f"{urgency}{extra}"
    )


def _build_handoff(messages: "list[dict]", step_results: "list[dict]", skill_name: str) -> str:
    """整改 A2a：硬中断（max_iterations）时的结构化交接摘要。

    从当前上下文抽取三段（已确认结论 / 进行到哪 / 建议下一步），供 MOA commander
    或人工 resume 时直接续上，避免无人值守任务静默丢失成果。
    不依赖 LLM（纯规则抽取），保证中断路径零额外调用。
    """
    # 1. 已确认结论：优先取压缩历史 <condensed_history>（role 可能是 user 或 system，
    #    取决于 ContextManager.maybe_compress 的存储；此处两者都查，避免找不到）。
    condensed = ""
    for m in messages:
        if m.get("role") in ("system", "user") and "<condensed_history>" in str(m.get("content", "")):
            condensed = str(m["content"])
            break
    # 2. 进行到哪：最近若干轮 assistant 文本（去重截断）
    recent = []
    for m in messages[-8:]:
        if m.get("role") == "assistant" and m.get("content"):
            snippet = str(m["content"]).strip()[:200]
            if snippet and snippet not in recent:
                recent.append(snippet)
    recent_txt = "；".join(recent[-3:]) if recent else "（无）"
    # 3. 已执行的工具步骤（动作轨迹）
    actions = [s.get("type") for s in step_results if s.get("type")]
    actions_txt = "、".join(actions[-8:]) if actions else "（无）"
    summary = (
        f"【已确认结论/压缩历史】{condensed[:300] or '（无压缩历史）'}\n"
        f"【进行到哪】最近动作：{actions_txt}\n最近 assistant 文本：{recent_txt}\n"
        f"【建议下一步】基于已执行步骤续做未完成的编辑/验证；如需恢复根因上下文请阅读上方压缩历史。"
    )
    return summary[:800]


def _pop_progress_hint(messages: list) -> None:
    """移除本轮临时注入的预算进度提示（invoke 后调用，保证不残留进 history）。

    仅当末尾确为进度提示时才 pop，避免误删真实消息。进度提示可能是 system 或
    user 角色（历史实现用 system，现改用 user 以兼容拒绝 mid-conversation system
    消息的 LLM），两者都识别。
    """
    if messages and messages[-1].get("role") in ("system", "user") and \
            str(messages[-1].get("content", "")).startswith("[进度]"):
        messages.pop()


# ---------------------------------------------------------------------------
# 性能诊断建议 6：bash 后文件登记选择性失效
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
    """从 bash 命令中尽力提取命令可能涉及的文件/目录路径（性能诊断建议 6）。

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

    支持两种 edit：
    - 普通 edit：``{"oldText", "newText"}``，要求 oldText 在**全局**唯一（与原行为一致）。
    - 区间 edit：``{"oldText", "newText", "line_range": [start, end]}``（1-indexed 闭区间），
      仅在指定行范围内定位 oldText，**不要求全局唯一**——消除「oldText 出现 2 次」歧义。
      这让模型在重复行场景下用 line_range 精确锚定，避免反复 read 找唯一 oldText。

    Returns:
        (new_content, None)            成功
        (None, error_message)          失败（error 信息会回传给 LLM 以便重试）
    """
    if not edits:
        return None, "error: edits 列表为空"
    for edit in edits:
        if not edit.get("oldText"):
            return None, "error: edit 项缺少 oldText"

    # 第一步：先应用「区间 edit」（按 line_range 锚定，消除全局歧义）
    lines = content.splitlines(keepends=True)
    # 按 line_range 处理（可能多次修改同一文件的不同区间，逐 edit 应用）
    ranged_errors = []
    plain_edits = []
    for e in edits:
        lr = e.get("line_range")
        if not lr or not isinstance(lr, (list, tuple)) or len(lr) != 2:
            plain_edits.append(e)
            continue
        start, end = int(lr[0]), int(lr[1])  # 1-indexed 闭区间
        if start < 1 or end < start or end > len(lines):
            ranged_errors.append(
                f"error: line_range {lr} 越界（文件共 {len(lines)} 行）: {e['oldText'][:60]}"
            )
            continue
        seg = "".join(lines[start - 1:end])
        cnt = seg.count(e["oldText"])
        if cnt == 0:
            ranged_errors.append(
                f"error: line_range {lr} 内未找到 oldText: {e['oldText'][:60]}"
            )
            continue
        if cnt > 1:
            ranged_errors.append(
                f"error: line_range {lr} 内 oldText 出现 {cnt} 次（区间内仍需唯一）: {e['oldText'][:60]}"
            )
            continue
        # 区间内唯一 → 替换并写回对应行
        new_seg = seg.replace(e["oldText"], e["newText"], 1)
        lines[start - 1:end] = [new_seg]
    if ranged_errors:
        # 区间 edit 有错直接返回（不部分应用，避免半成品）
        return None, "\n".join(ranged_errors)
    content = "".join(lines)

    # 第二步：对「普通 edit」走原逻辑（全局唯一检查 + 精确/模糊）
    edits = plain_edits
    if not edits:
        return content, None
    # 重复歧义：模糊匹配无法消除，直接报错（与原行为一致）
    for e in edits:
        c = content.count(e["oldText"])
        if c > 1:
            return None, f"error: oldText 在文件中出现 {c} 次（需唯一，或改用 line_range 锚定）: {e['oldText'][:80]}"
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
        f"{shell_rule}\n"
        "- 涉及当前日期/时间/年份的问题，必须先调用 get_current_time 获取，"
        "不要凭训练知识猜测（训练数据有截止时间）。\n\n"
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
        if len(stdout) > 8000:
            lines.append(stdout[:8000] + f"\n... (stdout 已截断，原长 {len(stdout)} 字符)")
        else:
            lines.append(stdout)
    if stderr:
        lines.append("stderr:")
        # 整改 A6：stderr 不再静默截断到 500 字（trace 实证：模型只看到 2/6 行报错而瞎猜重试）。
        # 完整回灌，仅在超长时截断并明确标注截断量，让模型能看到真实报错。
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
    # 整改 B7（来自审计）：Python 执行错误提示，打断"看不懂报错→换个花样再试"空转
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


def _format_match(rel: str, lineno, text: str, is_match: bool = False) -> str:
    """统一的搜索结果行格式：rel:行号: 内容（截 120 字）。

    整改 A5：匹配行标注 `← MATCH`，便于模型一眼定位命中行、减少二次 read_file。
    """
    marker = "  ← MATCH" if is_match else ""
    return f"{rel}:{lineno}: {text.strip()[:120]}{marker}"


def _run_ripgrep(pattern: str, search_dir: Path, file_glob: str, max_results: int,
                context_lines: int = 3):
    """ripgrep 实现。返回 None 表示 rg 不可用/执行失败（调用方回退纯 Python）。

    rg 原生尊重 .gitignore；以 search_dir 为 cwd、相对路径 '.' 执行，
    避免 Windows 绝对路径的盘符冒号破坏 'path:line:text' 解析。
    整改 A5：加 -C context_lines 输出命中行前后上下文，匹配行标注 ← MATCH。
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "400",
           "--no-require-git", "-C", str(context_lines)]  # 整改 A5：上下文行
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
    match_count = 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("--"):  # rg 文件分组分隔符，跳过
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        rel, lineno, text = parts
        rel = rel.lstrip("./\\")
        total += 1
        # 整改 A5/Bug4：is_match 用 Python re 判定（与 rg 的 Rust regex 对常见 pattern 兼容）；
        # 仅对「匹配行」计数 max_results（而非含上下文的总行数），保证返回 ~max_results 个匹配。
        is_match = bool(re.search(pattern, text))
        if is_match:
            match_count += 1
        if match_count <= max_results:
            matches.append(_format_match(rel, lineno, text, is_match))
    if not matches:
        return "no matches found"
    out = "\n".join(matches)
    if total > len(matches):
        out += f"\n... (已显示 {len(matches)} 条，共 {total} 条匹配；请收窄 pattern 或 path)"
    return out


def _python_search(pattern: str, search_dir: Path, file_glob: str, max_results: int,
                   context_lines: int = 3) -> str:
    """纯 Python 回退实现（无 rg 依赖）：rglob + 逐行正则，语义与旧内联版一致。

    整改 A5：匹配行带前后各 context_lines 行上下文，匹配行标注 ← MATCH。
    """
    import fnmatch
    import re as re_module
    matches = []
    total_size = 0
    match_count = 0
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
                all_lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                rel = f.relative_to(search_dir) if search_dir.is_dir() else f.name
                for i, line in enumerate(all_lines, 1):
                    if re_module.search(pattern, line):
                        # 整改 A5/Bug4：仅对匹配行计数 max_results（而非含上下文的总行数），
                        # 保证返回 ~max_results 个匹配，恢复搜索覆盖率。
                        if match_count >= max_results:
                            overflow = True
                            break
                        match_count += 1
                        # 整改 A5：输出命中行 + 前后上下文（上下文行 is_match=False）
                        lo = max(0, i - 1 - context_lines)
                        hi = min(len(all_lines), i + context_lines)
                        for j in range(lo, hi):
                            is_match = (j == i - 1)
                            ctx_line = _format_match(str(rel), j + 1, all_lines[j], is_match)
                            matches.append(ctx_line)
                            total_size += len(ctx_line) + 1
                        if total_size > 8000:
                            overflow = True
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

def _search_files(pattern: str, search_dir: Path, file_glob: str = "", max_results: int = 0,
                  context_lines: int = 3) -> str:
    """search_files 统一入口：ripgrep 优先，失败回退纯 Python。

    整改 A5：context_lines 默认 3，命中行带前后上下文、标注 ← MATCH。
    """
    mr = max_results if max_results and max_results > 0 else _SEARCH_DEFAULT_MAX
    mr = min(mr, _SEARCH_MAX_CAP)
    result = _run_ripgrep(pattern, search_dir, file_glob, mr, context_lines)
    if result is None:
        result = _python_search(pattern, search_dir, file_glob, mr, context_lines)
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
        # 整改 A6：验证失败 stderr 也不静默截断，超长时标注截断量
        if len(err) > 8000:
            lines.append("stderr:\n" + err[:8000] + f"\n... (stderr 已截断，原长 {len(err)} 字符)")
        else:
            lines.append("stderr:\n" + err)
    out = (r.get("stdout") or "").strip()
    if out:
        lines.append("stdout(尾部):\n" + out[-1500:])
    return "\n".join(lines)[:4000]



# ---------------------------------------------------------------------------
# S1-3：编辑 diff 预览 —— 写盘前用 difflib 生成 unified diff（零依赖），
# 按 confirm_edits 逐次/逐文件确认。默认关闭，其他 skill 零影响。
# ---------------------------------------------------------------------------
_DIFF_MAX_LINES = 200  # diff 超过该长度截断展示（全文重写类的大 diff）
_DIFF_MAX_CHARS = 500  # diff 总字符上限（行数限制之外的双保险，防单行超长刷屏）
_DIFF_NEW_FILE_PREVIEW_LINES = 40  # 新建文件时仅预览前 N 行


def _render_diff(path: str, old: str, new: str) -> str:
    """生成供展示/确认的 unified diff；过长时截断并附提示。"""
    import difflib
    # 新建文件：整份 diff 都是新增行，预览前几行 + 行数摘要即可，避免 709 行刷屏
    if not old:
        lines = new.splitlines()
        total = len(lines)
        preview = lines[:_DIFF_NEW_FILE_PREVIEW_LINES]
        text = (f"[将新建文件 {path}，共 {total} 行，预览前 {len(preview)} 行]\n+"
                + "\n+".join(preview))
        if len(preview) < total:
            text += f"\n... (其余 {total - len(preview)} 行省略)"
        if len(text) > _DIFF_MAX_CHARS:
            text = (text[:_DIFF_MAX_CHARS]
                    + f"\n...(diff 共 {len(text)} 字符，仅显示前 {_DIFF_MAX_CHARS} 字符)")
        return text
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{path} (before)", tofile=f"{path} (after)", lineterm=""))
    total = len(lines)
    if total > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES] + [f"... (diff 共 {total} 行，仅显示前 {_DIFF_MAX_LINES} 行)"]
    text = "\n".join(lines) if lines else "(内容无变化)"
    if len(text) > _DIFF_MAX_CHARS:
        text = (text[:_DIFF_MAX_CHARS]
                + f"\n...(diff 共 {len(text)} 字符，仅显示前 {_DIFF_MAX_CHARS} 字符)")
    return text


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
        plain_text: bool = False,
        verbose: bool = False,
        trusted_root: Optional[str] = None,
        tracer: Optional["DebugTracer"] = None,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn  # Runner._check_approval 回调
        self.human_io = human_io
        self.turn_policy = turn_policy
        # 归一化：Windows 下允许用户传 Git Bash / WSL 风格路径（/d/x、/mnt/d/x）
        self.working_root = to_native_path(working_root)
        self.plain_text = plain_text  # CLI 纯文本终端：禁用 Markdown 输出
        self.verbose = verbose  # 是否显示引擎内部调试态（迭代/历史条数/LLM 响应）
        self.tracer = tracer  # 可选 DebugTracer；None / 未开启时全程 no-op
        # trusted_root：用户显式指定的受信任工作目录（如 MOA -w）。
        # 目录内的文件读写自动放行（免审批/免 diff 确认）；目录外维持原审批。
        self.trusted_root = to_native_path(trusted_root)
        # S1-3：batch 模式下会话内已批准的文件（run_repl 复用同一 runner 实例，跨轮存活）
        self._file_edit_approvals: set = set()
        self._confirm_edits_mode = ""
        # view_image 的 R2 上传缓存：(path, size, mtime_ns) → 公网 URL，同文件只传一次
        self._view_image_urls: dict = {}

    def _trace_finish(self, res) -> "RunResult":
        """包装一次 RunResult 返回：顺带记一条 stop 事件（debug 模式）。

        同时把 stopped_by 存到实例上：run() 的 finally 收尾 dump_context 需要它，
        但 finally 里的局部变量 ``result`` 可能被主循环里的工具输出字符串覆盖
        （Python 无块级作用域），不能直接引用——这里是最可靠的采集点，
        因为所有 RunResult 退出路径（正常完成/提前 stop/error/max_iterations）
        都经过 _trace_finish。
        """
        if self.tracer and self.tracer.enabled():
            stopped_by = res.ctx.get("stopped_by")
            self.tracer.event(
                "stop",
                stopped_by=stopped_by,
                iterations=res.ctx.get("iterations"),
            )
            self._dbg_stopped_by = stopped_by
        return res

    def _is_trusted_path(self, full_path: Path) -> bool:
        """路径是否位于受信任工作目录（trusted_root）内。规范化后前缀匹配，防 .. 逃逸。"""
        if not self.trusted_root:
            return False
        try:
            root = os.path.normcase(str(Path(self.trusted_root).resolve()))
            target = os.path.normcase(str(Path(full_path).resolve()))
        except OSError:
            return False
        return target == root or target.startswith(root + os.sep) 

    def _truncate_msg(self, content: str, max_chars: int = 30000) -> str:
        """Truncate tool result message content to prevent context overflow.

        Full content is preserved in step_results for logging.
        """
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n...(truncated, {len(content)} chars total, showing first {max_chars})"

    # ---- 用户态执行轨迹：语义化输出（步骤 1+2 重构）----
    # 统一经 human_io 的 emit_* 语义通道；human_io 为 None（非交互/Web）或
    # 未实现新语义方法（如旧版 Fake/测试替身）时，回退为纯文本 print，
    # 保持与原有无条件 print 行为一致，不破坏 Web 端/测试。
    def _emit_tool(self, label: str, detail: str = "") -> None:
        m = getattr(self.human_io, "emit_tool", None) if self.human_io is not None else None
        if callable(m):
            m(label, detail)
        else:
            print(f"  [tool] {label}  {detail}" if detail else f"  [tool] {label}")

    def _emit_result(self, out: str) -> None:
        m = getattr(self.human_io, "emit_result", None) if self.human_io is not None else None
        if callable(m):
            m(out)
        elif out:
            # 回退路径（human_io 无 emit_result）：按 2 行截断，与 CliHumanIO 行为一致
            lines = out.splitlines()
            for line in lines[:2]:
                print(f"  {line}")
            if len(lines) > 2:
                print(f"  ...(还有 {len(lines) - 2} 行未显示，共 {len(lines)} 行)")

    def _emit_thinking(self, text: str) -> None:
        """模型本轮的「思考文字」（content）实时展示给用户（步骤 4）。"""
        m = getattr(self.human_io, "emit_thinking", None) if self.human_io is not None else None
        if callable(m):
            m(text)
        elif text:
            print(f"\n{text}")

    def _check_file_safety(self, op_type: str, filepath: str, skill: Skill) -> tuple[bool, str]:
        """检查文件操作的安全性

        Args:
            op_type: 操作类型（read/write/edit）
            filepath: 目标文件路径
            skill: 当前 skill

        Returns:
            (approved, error_message)
        """
        # 防御：目标路径若为已存在目录，不能当文件做 read/write/edit。
        # 空 path 会被 _resolve_path 解析成 base_dir 本身（目录），且 _resolve_path 不会对
        # 已存在目录做特殊处理；Windows 上对目录 read_text/write_text 抛 PermissionError，
        # 会让整个 worker 崩溃。这里在信任目录判断之前就拦下，优先级最高。
        base_dir = self.working_root or Path(skill.directory)
        if _resolve_path(filepath, base_dir).is_dir():
            return False, f"[拒绝] 目标路径是目录，不能进行{op_type}操作：{filepath}"

        from skill_engine.security.scanner import RISKY_FILENAMES
        # 直接检查文件名（不依赖 _path_escapes 的正则提取）
        if Path(filepath).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 敏感文件操作已取消"

        # 受信任工作目录内的文件操作自动放行（用户显式指定 trusted_root 时）
        if self.trusted_root:
            base_dir = self.working_root or Path(skill.directory)
            if self._is_trusted_path(_resolve_path(filepath, base_dir)):
                return True, ""

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
        # 受信任工作目录内的文件操作自动放行（不展示 diff、不询问）
        if self._is_trusted_path(Path(filepath)):
            print(f"     [diff 预览] {op} → {filepath}（工作目录内，自动放行）")
            return True
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
        pencil = "📝" if getattr(self.human_io, "_emoji", True) else "[diff]"
        self.human_io.emit(f"{pencil} 编辑预览 [{op} → {filepath}]\n{diff_text}\n{ask}")
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
        append_final_prompt: bool = False,
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
        final_prompt = self.assembler.assemble(
            skill, match_result.arguments, plain_text=self.plain_text)

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
            # 按模型能力过滤：文本模型不暴露视觉工具（view_image / shot_web），
            # 从根源杜绝「截图 → 看图 → 被告知无视觉」的无效步骤。
            from skill_engine.config import model_supports_vision
            self._model_has_vision = model_supports_vision(llm)
            if not self._model_has_vision:
                tools = [t for t in tools if t.name not in ("view_image", "shot_web")]
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

        # P2-3：落盘续跑 —— 若给定 resume_from，载入上次运行状态继续对话
        save_path = state_path or resume_from
        if initial_messages is not None:
            # session 模式续轮：直接用历史起轮，final_prompt 已含在 initial_messages 中
            ctx.messages[:] = list(initial_messages)
            messages = ctx.messages
            if append_final_prompt:
                # MOA 续跑：在已有私有历史上追加本轮 final_prompt（含 env 头）作为新 user 轮
                messages.append({"role": "user", "content": final_prompt})
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
            _fp = final_prompt
            if not getattr(self, "_model_has_vision", True):
                _fp = (
                    "【能力声明】当前模型是**无视觉的文本模型**：view_image / shot_web "
                    "工具不可用。需要验证 UI 渲染等视觉任务时，请改用读取 HTML/CSS/JS "
                    "源码、或运行自动化测试（node / pytest 等）等方式完成，不要尝试截图看图。\n\n"
                    + _fp
                )
            messages.append({"role": "user", "content": _fp})

        result = None
        # 执行开始标题头（语义通道；无 human_io 时静默——Web 端/测试回退默认实现）
        if self.human_io is not None:
            hdr = getattr(self.human_io, "emit_header", None)
            if callable(hdr):
                hdr(f"Running in {skill.metadata.name} @ {base_dir}")

        # debug 轨迹：run 开始（含 skill / 模式 / 工作目录）
        if self.tracer and self.tracer.enabled():
            self.tracer.event(
                "run_start",
                skill=skill.metadata.name,
                session_mode=session_mode,
                working_root=str(base_dir),
            )
        try:
            for i in range(max_iterations):
                iterations += 1
                # debug 轨迹：每轮迭代起点
                if self.tracer and self.tracer.enabled():
                    self.tracer.event("iteration", n=iterations, max=max_iterations)

                # 隐藏迭代轮次计数（正常输出不显示）；每轮之间用空行分割，方便观察。
                # 仅在 --verbose 调试模式保留「迭代 N/max」与历史条数。
                if self.verbose:
                    print(f"  > 迭代 {iterations}/{max_iterations}")
                    print(f"  Messages in history: {len(messages)} items")
                elif iterations > 1:
                    print()

                # 上下文压缩移到轮末执行（性能诊断建议 3）：本轮工具结果全部追加
                # 完毕、下轮 invoke 之前压缩——旧实现在轮首，压缩对象滞后一轮，
                # 且压缩本身抢在 LLM 调用前白白占用热路径。
                # 每轮 LLM 调用之间的人为节流（性能诊断建议 1）：旧版无条件
                # sleep(3) 拖慢所有会话；改为可配置（SKILLS_ENGINE_LLM_CALL_INTERVAL /
                # config.yml settings.llm_call_interval），默认 0 = 关闭，
                # 429 限流由上方指数退避兜底。测试环境下跳过（PYTEST_CURRENT_TEST）。
                interval = llm_call_interval()
                if interval > 0 and not os.environ.get("PYTEST_CURRENT_TEST"):
                    time.sleep(interval)

                # 整改 A1：预算可见注入。临时把进度提示作为 user 消息追加到
                # messages（用 user 角色而非 system，避免部分 LLM 拒绝 mid-conversation
                # system 消息导致 invoke 抛异常、循环直接挂掉），让本轮模型能看到剩余
                # 预算并自我调节收敛；invoke 后立即 pop 掉，不残留进 history。
                messages.append({"role": "user", "content": _progress_hint(iterations, max_iterations, step_results)})
                resp = None
                max_retries = 5
                for attempt in range(max_retries):
                    try:
                        resp = llm_with_tools.invoke(messages)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower() or "exhaust" in err_str.lower():
                            wait_time = 20 * (attempt + 1)
                            # 测试环境下跳过退避等待（PYTEST_CURRENT_TEST 由 pytest 自动设置），加速用例
                            if not os.environ.get("PYTEST_CURRENT_TEST"):
                                time.sleep(wait_time)
                            if attempt == max_retries - 1:
                                _pop_progress_hint(messages)  # 清理临时进度提示
                                return self._trace_finish(RunResult(
                                    output=f"[LLM 调用被限流（已重试 {max_retries} 次）: {err_str}]",
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "rate_limited"},
                                    history=messages[:],
                                ))
                        else:
                            _pop_progress_hint(messages)  # 清理临时进度提示
                            return self._trace_finish(RunResult(
                                output=f"[LLM 调用失败: {err_str}]",
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "error"},
                                history=messages[:],
                            ))

                # 整改 A1：移除本轮临时预算进度提示，避免残留进 history / 压缩 / 续跑。
                _pop_progress_hint(messages)

                # 标准化 LLM 响应为 dict（兼容 LangChain AIMessage）。
                # 关键：保留推理字段 reasoning_content —— 推理模型（DeepSeek-R1 /
                # V4-thinking / Qwen-thinking 等）把"思考过程"吐在的独立字段，
                # LangChain ChatOpenAI 将其放在 additional_kwargs 里；flash 等非
                # 推理模型通常不返回，此时 reasoning 为空，无思考可展示（正常现象）。
                reasoning = ""
                if isinstance(resp, dict):
                    reasoning = resp.get("reasoning_content") or resp.get("reasoning") or ""
                elif hasattr(resp, "additional_kwargs"):
                    ak = resp.additional_kwargs or {}
                    reasoning = ak.get("reasoning_content") or ak.get("reasoning") or ""
                if hasattr(resp, "tool_calls"):
                    resp = {
                        "content": resp.content if hasattr(resp, "content") else str(resp),
                        "tool_calls": list(resp.tool_calls) if resp.tool_calls else [],
                        "reasoning": reasoning,
                    }
                elif not isinstance(resp, dict):
                    resp = {"content": str(resp), "tool_calls": [], "reasoning": ""}
                else:
                    resp.setdefault("reasoning", "")

                # 解析 tool_calls
                tool_calls = parse_tool_calls(resp)

                # 模型本轮的「思考文字」：实时展示。
                # 优先级：reasoning_content（推理模型思考）→ content（模型写在工具调用前的说明）。
                # 两者皆空 → 该模型在调工具时不输出思考（如 DeepSeek-v4-flash），属正常现象，
                # 无内容可展示。仅在有 tool_calls 时 emit，避免与最终回答（无 tool_calls 的
                # content）重复打印。
                thinking = resp.get("reasoning") or resp.get("content", "")
                if thinking and tool_calls:
                    self._emit_thinking(thinking)

                # 内部调试态仅在 --verbose 显示；工具调用改走语义通道 emit_tool
                if self.verbose:
                    print(f"  LLM response: content={len(resp.get('content', ''))} chars, tool_calls={len(tool_calls)}")
                # debug 轨迹：LLM 响应概览（content/推理长度 + 本轮工具调用名）
                if self.tracer and self.tracer.enabled():
                    self.tracer.event(
                        "llm_response",
                        content_len=len(resp.get("content", "") or ""),
                        reasoning_len=len(resp.get("reasoning", "") or ""),
                        tool_calls=[tc.get("type") for tc in tool_calls],
                    )
                for tc in tool_calls:
                    self._emit_tool(tc['type'], str(tc['input']))
                    # debug 轨迹：引擎层工具调用（raw input，human_io 为 None 时也能捕获）
                    if self.tracer and self.tracer.enabled():
                        self.tracer.event(
                            "tool_call",
                            type=tc.get("type"),
                            input=truncate(str(tc.get("input", "")), 1000),
                        )

                if not tool_calls:
                    text = resp.get("content", "")
                    messages.append({"role": "assistant", "content": text})

                    if session_mode:
                        # session 模式：子任务完成文本，由外层 REPL 处理后等待下条指令。
                        # 禁用内部 human_in_loop 追问循环（决策 2），避免双重提问。
                        step_results.append({"name": "llm_response", "type": "llm", "output": text})
                        return self._trace_finish(RunResult(
                            output=text,
                            ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                 "iterations": iterations, "stopped_by": "session_turn_end"},
                            history=messages,
                        ))

                    if self.human_io and self.turn_policy:
                        # 多轮对话模式
                        if self.turn_policy.should_stop(text):
                            # LLM 说完了  直接结束，不追问用户
                            step_results.append({"name": "llm_response", "type": "llm", "output": text})
                            return self._trace_finish(RunResult(
                                output=text,
                                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "stop"},
                                history=messages,
                            ))
                        else:
                            # LLM 在问用户  emit + read + 追 history + 继续
                            self.human_io.emit(text)
                            user_input = self.human_io.read()

                            # 用户退出
                            if user_input in (self.turn_policy.user_exit or []):
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return self._trace_finish(RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "user_exit"},
                                    history=messages,
                                ))

                            # 达到最大轮数
                            if iterations >= self.turn_policy.max_turns:
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return self._trace_finish(RunResult(
                                    output=text,
                                    ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                         "iterations": iterations, "stopped_by": "max_turns"},
                                    history=messages,
                                ))

                            # 追加用户回答，继续循环
                            messages.append({"role": "user", "content": user_input})
                            continue

                    # 非多轮模式：原行为
                    step_results.append({"name": "llm_response", "type": "llm", "output": text})
                    return self._trace_finish(RunResult(
                        output=text,
                        ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                             "iterations": iterations, "stopped_by": "stop"},
                        history=messages,
                    ))

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
                io_batch = []
                for tc in tool_calls:
                    # 性能诊断建议 8：read_file / search_files 是纯磁盘读、互不依赖、
                    # 无副作用，入批并行执行（工作线程只做磁盘读/ripgrep，共享状态
                    # 操作留在主线程）；遇到任何串行工具先 flush 批，保证工具消息
                    # 严格按 tool_calls 顺序回灌（OpenAI 协议要求）。
                    if tc["type"] == "read_file":
                        io_batch.append(tc)
                        continue
                    if tc["type"] == "search_files":
                        io_batch.append(tc)
                        continue
                    self._flush_io_batch(io_batch, messages, step_results, skill, base_dir)
                    io_batch = []
                    if tc["type"] == "stop":
                        return self._trace_finish(RunResult(
                            output=tc["input"].get("reason", "stopped"),
                            ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                                 "iterations": iterations, "stopped_by": "tool_stop"},
                            history=messages,
                        ))

                    elif tc["type"] == "bash":
                        cmd = tc["input"].get("command", "")
                        # debug 轨迹：实际执行的 shell 命令
                        if self.tracer and self.tracer.enabled():
                            self.tracer.event("command", cmd=cmd, tool_id=tc["id"])
                        decision, reason = should_approve(cmd, skill.directory, risk_hint="tool_dispatch")
                        if decision == "BLOCK":
                            # 性能诊断建议 2（strict 快速失败）：BLOCK 只会出现在 strict
                            # 模式下（LLM 侧 bash 一律不自动执行）。继续循环只会让模型一遍遍
                            # 撞墙、空转耗尽迭代上限——直接终止本轮并明确告知原因。
                            logging.getLogger("skill_engine.tool_dispatch").warning(
                                f"tool_dispatch bash 被安全拦截（strict 快速失败）: {cmd[:80]}"
                            )
                            err = (
                                "[安全拦截] 当前安全模式为 strict：LLM 发起的 bash 命令一律不自动执行。"
                                "继续尝试 bash 只会耗尽迭代上限。请设置环境变量 "
                                "SKILLS_ENGINE_SECURITY_MODE=permissive 后重试，"
                                "或改用 read_file / write_file / edit_file / search_files 等文件工具。"
                            )
                            step_results.append({
                                "name": f"bash_{tc['id']}",
                                "type": "bash",
                                "command": cmd,
                                "output": "",
                                "error": err,
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "bash",
                                "content": err,
                            })
                            print(f"     BLOCKED (strict 快速失败): {cmd[:80]}")
                            return self._trace_finish(RunResult(
                                output=err,
                                ctx={"steps": step_results, "files_created": files_created,
                                     "skill_name": skill.metadata.name,
                                     "iterations": iterations, "stopped_by": "security_blocked"},
                                history=messages,
                            ))
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
                            # 性能诊断建议 6：bash 可能改过文件 → 按命令中实际出现的路径
                            # 选择性失效（文件级/目录级），未涉及的登记保留，消除「每次
                            # bash 后全部文件回到未读」的迭代放大；无法提取路径 token 时
                            # 保守全失效（与旧行为一致，如 echo hi）。
                            touched = _extract_cmd_paths(cmd, base_dir)
                            if touched is None:
                                self._file_tracker.invalidate_all()
                            else:
                                self._file_tracker.invalidate_paths(touched)
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
                            # 步骤 2：bash 真实输出改走语义通道（行截断），替代原先裸 print 全打
                            self._emit_result(obs)
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
                        # 已在上方批入 io_batch 并行执行（性能诊断建议 8），此分支不可达
                        pass

                    elif tc["type"] == "view_image":
                        filepath = tc["input"].get("path", "")

                        # 安全门（只查路径，strict 不 BLOCK）
                        approved, err_msg = self._check_file_safety("read", filepath, skill)
                        if not approved:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "view_image",
                                "content": err_msg,
                            })
                            print(f"     {err_msg}")
                            continue

                        base_dir = self.working_root or Path(skill.directory)
                        full_path = _resolve_path(filepath, base_dir)
                        if not full_path.is_file():
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "view_image",
                                "content": f"[图片不存在: {filepath}]",
                            })
                            print(f"     view_image FILE NOT FOUND: {filepath}")
                            continue

                        # 模态区分：仅 vision 模型注入图片；文本模型返回提示（省 token）
                        from skill_engine.config import model_supports_vision
                        if not model_supports_vision(llm):
                            notice = (
                                f"[当前模型为文本模态，无法查看图片] {filepath} 是图片文件"
                                f"（{full_path.stat().st_size} bytes）。请由支持视觉的模型"
                                f"（vision: true）查看，或手动打开文件确认。"
                            )
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "view_image",
                                "content": notice,
                            })
                            self._emit_tool(f"view_image {filepath}")
                            self._emit_result(notice)
                            continue

                        try:
                            import base64
                            ext = full_path.suffix.lower()
                            mime = {".png": "image/png", ".jpg": "image/jpeg",
                                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                                    ".webp": "image/webp"}.get(ext, "image/png")
                            # 进模型前自适应压缩：按"面积线性计费"决策（sensenova 实测：
                            # image_tokens ≈ 面积(px)/1024，纯线性、无瓦片取整边界）。
                            # 只要发生缩放，面积必然严格减小 → token 必然减少，直接采纳；
                            # 未缩放（最长边 ≤ 1568）则保持原样，避免无谓重编码损失画质。
                            # 缩放后取 JPEG/PNG 较小者；Pillow 缺失或解码失败回退原样字节（零新硬依赖）。
                            import io
                            raw_bytes = full_path.read_bytes()
                            orig_size = len(raw_bytes)
                            size = orig_size
                            note = ""
                            payload = raw_bytes  # 最终发送字节（压缩后或原样）
                            try:
                                from PIL import Image
                                try:
                                    resample = Image.Resampling.LANCZOS
                                except AttributeError:
                                    resample = Image.LANCZOS
                                img = Image.open(io.BytesIO(raw_bytes))
                                if max(img.size) > 1568:
                                    scale = 1568 / max(img.size)
                                    img = img.resize(
                                        (int(img.size[0] * scale), int(img.size[1] * scale)),
                                        resample,
                                    )
                                    if img.mode in ("RGBA", "P", "LA"):
                                        img = img.convert("RGB")
                                    # 缩放后面积减小→token 减少；取 JPEG/PNG 较小者
                                    buf_jpeg = io.BytesIO()
                                    img.save(buf_jpeg, format="JPEG", quality=85)
                                    pj = buf_jpeg.getvalue()
                                    buf_png = io.BytesIO()
                                    img.save(buf_png, format="PNG")
                                    pp = buf_png.getvalue()
                                    if len(pj) <= len(pp):
                                        payload = pj
                                        mime = "image/jpeg"
                                        size = len(pj)
                                        note = f"（压缩至 {size} bytes，原 {orig_size} bytes）"
                                    else:
                                        payload = pp
                                        mime = "image/png"
                                        size = len(pp)
                                        note = f"（缩放至 {size} bytes，原 {orig_size} bytes）"
                            except Exception:
                                pass
                            b64 = base64.b64encode(payload).decode("ascii")
                            # R2 外链模式：配置了 R2 时上传压缩后字节换公网 URL（sensenova
                            # 等不吃 base64 的模型）。失败/未配置回退 base64 内联（fail-soft）。
                            # 同文件（大小+mtime 不变）会话内只传一次，避免重复上传。
                            image_url = ""
                            cache_key = (str(full_path), orig_size,
                                         int(full_path.stat().st_mtime_ns))
                            if cache_key in self._view_image_urls:
                                image_url = self._view_image_urls[cache_key]
                            else:
                                try:
                                    from skill_engine.execution.image_hosting import (
                                        upload_image_to_r2,
                                    )
                                    got = upload_image_to_r2(payload, mime)
                                    if got:
                                        image_url = got
                                        self._view_image_urls[cache_key] = got
                                except Exception:
                                    image_url = ""
                            if image_url:
                                url = image_url
                                url_note = f"，已上传 R2 公网 URL"
                            else:
                                url = f"data:{mime};base64,{b64}"
                                url_note = ""
                            # 先落 tool 文本消息（保持 OpenAI 协议顺序），
                            # 再注入 user 多模态消息：下一轮调用时模型即可"看见"图片
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "view_image",
                                "content": f"[图片已加载] {filepath}（{size} bytes{note}）"
                                           f"，多模态消息已注入{url_note}。",
                            })
                            messages.append({
                                "role": "user",
                                "content": [
                                    {"type": "text",
                                     "text": f"已加载图片 {filepath}（{size} bytes{note}），请仔细查看图片内容，"
                                             "据此检查实现是否符合要求。"},
                                    {"type": "image_url",
                                     "image_url": {"url": url}},
                                ],
                            })
                            self._emit_tool(f"view_image {filepath}")
                            self._emit_result(f"图片已注入多模态消息（{size} bytes, {mime}{note}{url_note}）")
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "view_image",
                                "content": f"[读取图片失败: {e}]",
                            })
                            print(f"     view_image ERROR: {e}")

                    elif tc["type"] == "write_file":
                        filepath = tc["input"].get("path", "")
                        content = tc["input"].get("content", "")

                        # 防御：LLM（尤其纯文本模型）可能漏传 path 键，导致 filepath 为空。
                        # 空 path 会被 _resolve_path 解析成 base_dir 本身（目录），
                        # 后续 read_text/write_text 在 Windows 上抛 PermissionError，使整个 worker 崩溃。
                        # 这里提前拦截，回一条错误让模型补全 path 重试，而不是把异常冒泡成崩溃。
                        if not filepath:
                            msg = ("[错误] write_file 缺少 path 参数，已跳过本次写入。"
                                   "请补全要写入的文件路径（如 action_track/index.html）后重试。")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "write_file",
                                "content": msg,
                            })
                            print("     [SKIP] write_file 缺少 path 参数，已跳过")
                            continue

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
                        # 已在上方批入 io_batch 并行执行（性能诊断建议 8），此分支不可达
                        pass

                    elif tc["type"] == "update_plan":
                        # 整改 B4：结构化任务追踪。仅记入 step_results，不污染 messages 历史、
                        # 不参与压缩、不消耗迭代预算；续跑/压缩时可被引用。
                        plan = tc["input"].get("plan", "")
                        plan_status = tc["input"].get("status", "in_progress")
                        step_results.append({
                            "name": f"plan_{tc['id']}",
                            "type": "update_plan",
                            "plan": plan,
                            "status": plan_status,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": "update_plan",
                            "content": f"[计划已更新] status={plan_status}\n{plan}",
                        })
                        print(f"     [计划更新] status={plan_status}")

                    elif tc["type"] == "query_db":
                        # 整改 二轮 P0-3：只读 SQL 查询工具。消临时脚本（write_file+bash 验证）。
                        import sqlite3 as _sqlite3
                        sql = tc["input"].get("sql", "").strip()
                        db_path_in = tc["input"].get("db_path", "").strip()
                        if not sql:
                            obs = "[query_db] sql 不能为空"
                        elif not re.match(r"^(SELECT|PRAGMA|EXPLAIN|WITH)\b", sql, re.IGNORECASE):
                            obs = ("[query_db] 仅允许只读语句（SELECT / PRAGMA / EXPLAIN / WITH 开头）。"
                                   "拒绝执行 DDL/DML，避免误改数据。")
                        else:
                            # 解析 db 路径：显式指定优先，否则在 base_dir 递归找第一个 *.db
                            db_file = None
                            if db_path_in:
                                db_file = _resolve_path(db_path_in, base_dir)
                            else:
                                candidates = sorted(base_dir.rglob("*.db"))
                                if candidates:
                                    db_file = candidates[0]
                            if not db_file or not db_file.exists():
                                obs = f"[query_db] 未找到数据库文件（指定={db_path_in or '无'}）。"
                            else:
                                try:
                                    conn = _sqlite3.connect(str(db_file))
                                    cur = conn.cursor()
                                    cur.execute(sql)
                                    rows = cur.fetchall()
                                    cols = [d[0] for d in cur.description] if cur.description else []
                                    if not rows:
                                        obs = f"[query_db] 查询成功，0 行（列：{cols}）"
                                    else:
                                        width = max([len(str(c)) for c in cols] + [8])
                                        header = " | ".join(str(c).ljust(width) for c in cols)
                                        sep = "-+-".join("-" * width for _ in cols)
                                        body = "\n".join(
                                            " | ".join(str(v).ljust(width) for v in r) for r in rows
                                        )
                                        obs = f"[query_db] {len(rows)} 行（表：{db_file.name}）\n{header}\n{sep}\n{body}"
                                    conn.close()
                                except Exception as e:
                                    obs = f"[query_db] 执行失败：{e}"
                        step_results.append({"name": f"query_db_{tc['id']}", "type": "query_db", "sql": sql})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": "query_db",
                            "content": format_observation("query_db", {"exit_code": 0, "stdout": obs, "stderr": "", "timed_out": False}),
                        })
                        print(f"     [query_db] {sql[:60]}")

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

                    elif tc["type"] == "shot_web":
                        url = tc["input"].get("url", "")
                        if not url:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "shot_web",
                                "content": "error: url 不能为空（支持 http(s):// 或本地文件路径）",
                            })
                            print("     shot_web: empty url")
                            continue
                        try:
                            width = int(tc["input"].get("width", 1280))
                            height = int(tc["input"].get("height", 800))
                        except (TypeError, ValueError):
                            width, height = 1280, 800
                        full_page = bool(tc["input"].get("full_page", False))
                        out = tc["input"].get("out", "screenshot.png")
                        base_dir = self.working_root or Path(skill.directory)
                        try:
                            obs = _take_screenshot(
                                url, width, height, full_page, out, str(base_dir))
                            step_results.append({
                                "name": f"shot_web_{tc['id']}",
                                "type": "shot_web",
                                "url": url,
                                "output": obs[:1000],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "shot_web",
                                "content": self._truncate_msg(obs),
                            })
                            self._emit_tool("shot_web", url)
                            self._emit_result(self._truncate_msg(obs, max_chars=800))
                        except Exception as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "name": "shot_web",
                                "content": f"[截图失败: {e}]",
                            })
                            print(f"     shot_web ERROR: {e}")

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
                            # 步骤 2：技能注入工具的真实输出原本只打 "N chars"，
                            # 现改走语义通道，展示实际内容（超长截断）而非仅字符数。
                            display = self._truncate_msg(content, max_chars=800)
                            self._emit_tool(tc['type'])
                            self._emit_result(display)
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

                # 收尾：flush 批内剩余的 IO 结果（工具消息顺序与 tool_calls 对齐）
                self._flush_io_batch(io_batch, messages, step_results, skill, base_dir)

                # 上下文压缩移到轮末（性能诊断建议 3）：本轮全部工具结果已追加完毕、
                # 即将进入下一轮 invoke——轮首压缩的旧实现压缩对象滞后一轮，且压缩
                # 抢在 LLM 调用前占热路径。
                ctx.maybe_compress(llm)

        # 达到最大迭代次数
            result = self._trace_finish(RunResult(
                output="[达到最大迭代次数]",
                ctx={"steps": step_results, "files_created": files_created, "skill_name": skill.metadata.name,
                     "iterations": iterations, "stopped_by": "max_iterations",
                     # 整改 A2a：硬中断结构化移交，供 resume / 人工续跑，避免静默丢失成果
                     "handoff_summary": _build_handoff(messages, step_results, skill.metadata.name)},
                history=messages,
            ))
            return result
        finally:
            # P2-3：任一退出路径（含提前 stop / error / max_iterations）都落盘，支撑续跑
            if save_path:
                self._save_state(save_path, messages, iterations, step_results, files_created,
                                 final_prompt, session_mode)
            # debug 轨迹：run 收尾，把完整上下文（含 messages）单独 dump 到 .ctx.json
            if self.tracer and self.tracer.enabled():
                self.tracer.dump_context(
                    messages=messages,
                    step_results=step_results,
                    files_created=files_created,
                    skill_name=skill.metadata.name,
                    iterations=iterations,
                    # stopped_by 从 _trace_finish 采集（所有 RunResult 退出路径都经过它）。
                    # 不能引用局部变量 result：主循环里的工具输出会把 result 覆盖成 str，
                    # 正常完成路径（不经过 max_iterations 赋值）下会 AttributeError。
                    stopped_by=getattr(self, "_dbg_stopped_by", None) or "unknown",
                )
        return result

    # ---------------- 性能诊断建议 8：IO 工具并行执行 ----------------

    def _flush_io_batch(self, io_batch, messages, step_results, skill, base_dir):
        """并行执行 IO 工具批（read_file / search_files）。

        IO 工具是纯磁盘读、互不依赖、无副作用，可并行；安全门、路径解析、
        read 缓存、tracker 登记、消息回灌等共享状态操作留在主线程，工作线程
        只做真正的磁盘读取/ripgrep 搜索。结果严格按 tool_calls 顺序回灌，
        保证 OpenAI 协议消息顺序不变。串行工具（bash/write/edit/...）执行前
        必须调用本方法。
        """
        if not io_batch:
            return
        base = base_dir or Path(skill.directory)
        prepared = []   # (kind, tc, payload)：主线程预处理后的批项
        for tc in io_batch:
            inp = tc["input"]
            if tc["type"] == "read_file":
                filepath = inp.get("path", "")
                approved, err_msg = self._check_file_safety("read", filepath, skill)
                if not approved:
                    prepared.append(("deny", tc, err_msg))
                    continue
                full_path = _resolve_path(filepath, base)
                offset = int(inp.get("offset", 0))
                limit = int(inp.get("limit", 0))
                force_refresh = bool(inp.get("force_refresh", False))
                # 整改 A4c（二轮 P0-1）：重复读取检测。
                # 对所有 read（含 force_refresh）累计同文件次数；仅当本次是「分页切片读」
                # （给了 offset/limit，而非全文）且已达阈值时注入提示——无论是否 force_refresh。
                # 关键修复：原逻辑用 `not force_refresh` 豁免，导致模型滥用 force_refresh 切片读大文件
                # 绕过检测（实测 database.py 被读 15+ 次）。全文读（无 offset/limit）不触发，避免干扰正常全文读。
                is_paged = (offset > 0 or limit > 0)
                read_count = sum(
                    1 for s in step_results
                    if s.get("type") == "read_file" and s.get("path") == str(full_path)
                )
                if read_count >= 3 and is_paged:
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "name": "read_file",
                        "content": (
                            f"[提示] 你已第 {read_count + 1} 次读取 {filepath}（含 force_refresh 切片读）。"
                            f"反复分页/切片读取同一大文件会快速消耗迭代预算。请改用 "
                            f"force_refresh=true 且**不带 offset/limit** 一次性取全文，"
                            f"或若之前已读过则直接基于上文历史内容工作，不要继续分页重试。"
                        ),
                    })
                    # 仍继续本次读取（不阻断），让模型拿到内容后收敛
                if not force_refresh and self._file_tracker is not None:
                    hit = self._file_tracker.cache_lookup(full_path, offset, limit)
                    if hit is not None:
                        prepared.append(("cache_hit", tc, (full_path, hit, filepath)))
                        continue
                prepared.append(("read", tc, (full_path, offset, limit, filepath)))
            elif tc["type"] == "search_files":
                pattern = inp.get("pattern", "")
                search_path = inp.get("path", ".")
                file_glob = inp.get("file_glob", "")
                if not pattern:
                    prepared.append(("deny", tc, "error: pattern 不能为空"))
                    continue
                search_dir = _resolve_path(search_path, base)
                if not search_dir.exists():
                    prepared.append(("deny", tc, f"[路径不存在: {search_path}]"))
                    continue
                prepared.append(("search", tc,
                                 (pattern, search_dir, file_glob,
                                  int(inp.get("max_results", 0) or 0))))

        # 工作线程只做磁盘读/搜索（纯函数，无共享状态）
        def _worker(item):
            kind = item[0]
            payload = item[2]
            try:
                if kind == "read":
                    full_path = payload[0]
                    return ("read", payload, full_path.read_text(encoding="utf-8"))
                if kind == "search":
                    pattern, search_dir, file_glob, max_results_req = payload
                    return ("search", payload,
                            _search_files(pattern, search_dir, file_glob, max_results_req))
            except FileNotFoundError:
                return ("err", payload, f"[文件不存在: {payload[3]}]")
            except Exception as e:
                return ("err", payload, f"[读取失败: {e}]")
            return ("err", payload, "[未知错误]")

        to_run = [it for it in prepared if it[0] in ("read", "search")]
        outcomes = []
        if to_run:
            with ThreadPoolExecutor(max_workers=min(_IO_MAX_WORKERS, len(to_run))) as pool:
                outcomes = list(pool.map(_worker, to_run))
        run_iter = iter(outcomes)

        for item in prepared:
            kind = item[0]
            tc = item[1]
            payload = item[2]
            if kind == "deny":
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "name": tc["type"], "content": payload,
                })
                print(f"     {payload}")
                continue
            if kind == "cache_hit":
                full_path, hit, filepath = payload
                lo, hi = hit["start"], hit["end"]
                where = ("全文" if hit["full"] else f"第 {lo + 1}-{hi} 行")
                note = (
                    f"[read_file 缓存命中] {filepath} {where} 已在本会话早前"
                    f"读取过且文件未被修改，内容见上文历史。"
                    f"若上文内容已不可见，请带 force_refresh=true 重新调用以获取完整内容。"
                )
                step_results.append({
                    "name": f"read_{tc['id']}",
                    "type": "read_file",
                    "path": str(full_path),
                    "output": note[:1000],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": "read_file",
                    "content": note,
                })
                self._emit_tool(f"read_file {filepath} (缓存命中 {where})")
                continue
            out = next(run_iter)
            if out[0] == "err":
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "name": tc["type"], "content": out[2],
                })
                print(f"     {out[2]}")
                continue
            _, out_payload, content = out
            if kind == "read":
                full_path, offset, limit, filepath = out_payload
                # P0(S0-1)：登记"已读版本"，供后续 edit 一致性校验
                self._file_tracker.on_read(full_path)
                formatted = _read_file_with_lines(content, offset, limit)
                self._file_tracker.cache_read(
                    full_path, offset, limit, len(content.splitlines()), formatted)
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
                self._emit_tool(f"read_file {filepath}")
                self._emit_result(self._truncate_msg(formatted, max_chars=800))
            elif kind == "search":
                pattern, search_dir, file_glob, max_results_req = out_payload
                result = content
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


    def _save_state(self, path, messages, iterations, step_results, files_created,
                    final_prompt, session_mode: bool = False):
        """将运行状态落盘为 append-only JSONL（性能诊断建议 9）。

        每次调用追加一行完整快照（type=snapshot）。JSONL 天然防半写损坏：
        崩溃/中断只损失最后一行，之前的历史快照仍可续跑；加载侧取最后一行。
        旧版单 JSON 整文件覆写（一次崩溃即全损）自动迁移兼容。

        session_mode 一并落盘：session 产生的历史含 ask_user 交互与多轮用户指令，
        若被普通 run --resume-from 载入，行为语义不同（无 ask_user 工具、
        无轮边界交还），载入侧据此给出提示。
        """
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "type": "snapshot",
                "final_prompt": final_prompt,
                "messages": messages,
                "iterations": iterations,
                "step_results": step_results,
                "files_created": files_created,
                "session_mode": session_mode,
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False) + "\n")
        except Exception:
            # 状态持久化失败绝不影响主执行流程
            pass

    def _load_state(self, path):
        """读取状态文件；不存在或损坏返回 None。

        JSONL（append-only）：取最后一行的完整快照；兼容旧版单 JSON 对象。
        """
        try:
            p = Path(path)
            if not p.exists():
                return None
            text = p.read_text(encoding="utf-8")
            if "\n" in text.strip():
                last_line = [ln for ln in text.splitlines() if ln.strip()][-1]
                return json.loads(last_line)
            return json.loads(text)
        except Exception:
            return None