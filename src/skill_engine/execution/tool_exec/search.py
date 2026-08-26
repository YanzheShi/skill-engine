"""search_files 双实现：ripgrep 优先（gitignore-aware、毫秒级），无 rg 二进制时回退纯 Python。"""

import re
import shutil
import subprocess
from pathlib import Path

_RG_TIMEOUT = 15           # ripgrep 子进程超时（秒）
_SEARCH_DEFAULT_MAX = 100  # search_files 默认结果上限（旧实现为 50）
_SEARCH_MAX_CAP = 500      # max_results 硬上限


def _format_match(rel: str, lineno, text: str, is_match: bool = False) -> str:
    """统一的搜索结果行格式：rel:行号: 内容（截 120 字）。

    匹配行标注 `← MATCH`，便于模型一眼定位命中行、减少二次 read_file。
    """
    marker = "  ← MATCH" if is_match else ""
    return f"{rel}:{lineno}: {text.strip()[:120]}{marker}"


def _run_ripgrep(pattern: str, search_dir: Path, file_glob: str, max_results: int,
                context_lines: int = 3):
    """ripgrep 实现。返回 None 表示 rg 不可用/执行失败（调用方回退纯 Python）。

    rg 原生尊重 .gitignore；以 search_dir 为 cwd、相对路径 '.' 执行，
    避免 Windows 绝对路径的盘符冒号破坏 'path:line:text' 解析。
    -C context_lines 输出命中行前后上下文，匹配行标注 ← MATCH。
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", "400",
           "--no-require-git", "-C", str(context_lines)]
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
        # is_match 用 Python re 判定（与 rg 的 Rust regex 对常见 pattern 兼容）；
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

    匹配行带前后各 context_lines 行上下文，匹配行标注 ← MATCH。
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
                        # 仅对匹配行计数 max_results（而非含上下文的总行数），
                        # 保证返回 ~max_results 个匹配，恢复搜索覆盖率。
                        if match_count >= max_results:
                            overflow = True
                            break
                        match_count += 1
                        # 输出命中行 + 前后上下文（上下文行 is_match=False）
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

    context_lines 默认 3，命中行带前后上下文、标注 ← MATCH。
    """
    mr = max_results if max_results and max_results > 0 else _SEARCH_DEFAULT_MAX
    mr = min(mr, _SEARCH_MAX_CAP)
    result = _run_ripgrep(pattern, search_dir, file_glob, mr, context_lines)
    if result is None:
        result = _python_search(pattern, search_dir, file_glob, mr, context_lines)
    return result
