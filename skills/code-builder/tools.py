"""code-builder 自带领域工具（仅在该 skill 通过 frontmatter 声明 extra_tools 时被加载）。

这些工具是 *代码专用* 能力，按设计留在 skill 层，不污染通用引擎核心。
工具名统一加 cb_ 前缀，避免与其他 skill 的工具名冲突。

约定（见 docs/large-code-capability-design.md）：
- 用 @tool 装饰器定义，引擎经 importlib 从绝对路径隔离加载；
- 执行时引擎会把基准目录 chdir 到 working_root / skill.directory，故相对路径按项目根解析。
"""

from pathlib import Path
import ast
import subprocess

from langchain_core.tools import tool

# 常见忽略目录（简易 .gitignore 近似）
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode",
}


@tool
def cb_list_files(path: str = ".") -> str:
    """列出目录树（code-builder 专用）。

    自动跳过 .git/node_modules/__pycache__ 等常见忽略目录，
    让 LLM 先看懂项目结构再决定读哪些文件。

    Args:
        path: 要列举的目录（绝对或相对项目根）
    """
    root = Path(path)
    if not root.exists():
        return f"[错误] 路径不存在: {path}"
    lines = []
    for p in sorted(root.rglob("*")):
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        depth = len(p.relative_to(root).parts) - 1
        lines.append("  " * depth + p.name + ("/" if p.is_dir() else ""))
    return "\n".join(lines) if lines else "(空目录)"


@tool
def cb_ast_map(path: str) -> str:
    """提取目录下所有 .py 文件的类/函数签名地图（code-builder 专用）。

    用 python ast 静态解析，零运行时依赖；帮助 LLM 快速定位
    "哪个文件定义了哪个类/函数"，无需逐个 read_file。

    Args:
        path: 目标目录（绝对或相对项目根）
    """
    root = Path(path)
    if not root.exists():
        return f"[错误] 路径不存在: {path}"
    out = []
    for py in sorted(root.rglob("*.py")):
        if any(part in _IGNORE_DIRS for part in py.parts):
            continue
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception as e:
            out.append(f"# {py} (解析失败: {e})")
            continue
        out.append(f"# {py}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [ast.unparse(b) for b in node.bases]
                out.append(f"  class {node.name}({', '.join(bases)})")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                out.append(f"  def {node.name}({', '.join(args)})")
    return "\n".join(out) if out else "(无 .py 文件)"


@tool
def cb_git_checkpoint(action: str, message: str = "") -> str:
    """对当前项目做 git 快照/回滚（code-builder 专用）。

    依赖 "被操作目录是 git 仓库" 这一代码域假设。

    Args:
        action: "save" 提交一个检查点 / "log" 列出检查点 / "restore" 回滚到最近检查点
        message: save 时的提交信息
    """
    try:
        if action == "save":
            subprocess.run(["git", "add", "-A"], cwd=Path.cwd(), check=True,
                           capture_output=True)
            msg = message or "code-builder checkpoint"
            r = subprocess.run(["git", "commit", "-m", msg], cwd=Path.cwd(),
                              capture_output=True, text=True)
            return (r.stdout or r.stderr or "已提交检查点").strip()
        elif action == "log":
            r = subprocess.run(["git", "log", "--oneline", "-n", "10"], cwd=Path.cwd(),
                              capture_output=True, text=True)
            return (r.stdout or "(无提交)").strip()
        elif action == "restore":
            r = subprocess.run(["git", "checkout", "HEAD", "--", "."], cwd=Path.cwd(),
                              capture_output=True, text=True)
            return (r.stdout or r.stderr or "已回滚到最近检查点").strip()
        return "[错误] action 必须是 save/log/restore"
    except Exception as e:
        return f"[git 操作失败: {e}]"
