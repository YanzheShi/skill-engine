"""code-analyzer 自带领域工具（仅在该 skill 通过 frontmatter 声明 extra_tools 时被加载）。

这些工具是 *代码审查* 专用能力，按设计留在 skill 层，不污染通用引擎核心。
工具名统一加 ca_ 前缀，避免与其他 skill 的工具名冲突。

约定（与 code-builder 的 cb_* 一致）：
- 用 @tool 装饰器定义，引擎经 importlib 从绝对路径隔离加载；
- 执行时引擎会把基准目录 chdir 到 working_root / skill.directory，故相对路径按项目根解析。
"""

from pathlib import Path
import ast
import re

from langchain_core.tools import tool

# 常见忽略目录（简易 .gitignore 近似）
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode",
    ".next", ".nuxt", "coverage", "target",
}

_PY_SUFFIXES = {".py"}
_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
_JAVA_SUFFIXES = {".java"}
_HTML_SUFFIXES = {".html", ".htm", ".vue", ".svelte"}


def _walk(root: Path):
    """递归列出文件，跳过忽略目录。"""
    for p in sorted(root.rglob("*")):
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        yield p


@tool
def ca_list_files(path: str = ".") -> str:
    """列出目录树（code-analyzer 专用）。

    自动跳过 .git/node_modules/__pycache__/dist 等常见忽略目录，
    文件附带大小（KB），让审查先看懂项目结构、判断哪些文件值得细读。

    Args:
        path: 要列举的目录（绝对或相对项目根）
    """
    root = Path(path)
    if not root.exists():
        return f"[错误] 路径不存在: {path}"
    lines = []
    for p in _walk(root):
        depth = len(p.relative_to(root).parts) - 1
        if p.is_dir():
            lines.append("  " * depth + p.name + "/")
        else:
            size = ""
            try:
                size = f"  ({p.stat().st_size / 1024:.1f} KB)"
            except OSError:
                pass
            lines.append("  " * depth + p.name + size)
    return "\n".join(lines) if lines else "(空目录)"


def _py_map(py: Path) -> list[str]:
    """Python：用 ast 解析类/函数签名（零运行时依赖）。"""
    out = []
    try:
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception as e:
        return [f"# {py} (解析失败: {e})"]
    out.append(f"# {py}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            out.append(f"  class {node.name}({', '.join(bases)})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            out.append(f"  def {node.name}({', '.join(args)})")
    return out


# JS/TS/Java 用轻量正则提取签名（不引第三方依赖，覆盖常见写法即可）
_JS_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function(?:\*)?\s+"
    r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
)
_JS_ARROW_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:\(([^)]*)\)|[A-Za-z_$][\w$]*)\s*=>",
)
_JS_CLASS_RE = re.compile(
    r"(?:^|\n)\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)",
)
_JS_METHOD_RE = re.compile(
    r"(?:^|\n)\s{2,}(?:async\s+)?(?:get\s+|set\s+)?"
    r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{",
)


def _js_map(path: Path) -> list[str]:
    """JS/TS：正则提取函数/箭头函数/类/方法。"""
    out = [f"# {path}"]
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        return [f"# {path} (读取失败: {e})"]
    for m in _JS_FUNC_RE.finditer(src):
        out.append(f"  function {m.group(1)}({m.group(2).strip()})")
    for m in _JS_ARROW_RE.finditer(src):
        out.append(f"  const {m.group(1)} = ({m.group(2).strip()}) =>")
    for m in _JS_CLASS_RE.finditer(src):
        out.append(f"  class {m.group(1)}")
    for m in _JS_METHOD_RE.finditer(src):
        out.append(f"    method {m.group(1)}({m.group(2).strip()})")
    return out


_JAVA_TYPE_RE = re.compile(
    r"(?:public|private|protected|static|final|abstract|synchronized|\s)+"
    r"(class|interface|enum|record)\s+(\w+)",
)
_JAVA_METHOD_RE = re.compile(
    r"(?:public|private|protected|static|final|synchronized|\s)+"
    r"(?:[\w<>?\[\],. ]+\s+)?(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[\w, ]+)?\{",
)


def _java_map(path: Path) -> list[str]:
    """Java：正则提取类/接口/方法。"""
    out = [f"# {path}"]
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        return [f"# {path} (读取失败: {e})"]
    for m in _JAVA_TYPE_RE.finditer(src):
        out.append(f"  {m.group(1)} {m.group(2)}")
    for m in _JAVA_METHOD_RE.finditer(src):
        out.append(f"  method {m.group(1)}({m.group(2).strip()})")
    return out


_HTML_ID_RE = re.compile(r'id="([^"]*)"')
_HTML_CLASS_RE = re.compile(r'class="([^"]*)"')
_HTML_ELEMENT_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


def _html_map(path: Path) -> list[str]:
    """HTML/Vue/Svelte：提取带 id/class 的元素与表单控件，便于对照 UI 稿。"""
    out = [f"# {path}"]
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        return [f"# {path} (读取失败: {e})"]
    for m in _HTML_ELEMENT_RE.finditer(src):
        tag = m.group(1)
        if tag in ("html", "head", "body", "script", "style", "meta", "link"):
            continue
        tag_end = src.find(">", m.end())
        seg = src[m.start(): tag_end + 1]
        id_m = _HTML_ID_RE.search(seg)
        cls_m = _HTML_CLASS_RE.search(seg)
        detail = ""
        if id_m:
            detail += f" id={id_m.group(1)}"
        if cls_m:
            detail += f" class={cls_m.group(1)}"
        out.append(f"  <{tag}>{detail}")
    return out


@tool
def ca_ast_map(path: str) -> str:
    """提取目录下代码文件的类/函数签名地图（code-analyzer 专用）。

    支持语言：
    - Python：ast 静态解析（精确）
    - JS/TS/JSX/TSX、Java、HTML/Vue/Svelte：正则提取（覆盖常见写法）

    帮助审查者快速定位"哪个文件定义了哪个类/函数/组件"，无需逐个 read_file。
    注意：正则提取是近似结果，复杂写法可能漏报，精读时仍需 read_file。

    Args:
        path: 目标目录（绝对或相对项目根）
    """
    root = Path(path)
    if not root.exists():
        return f"[错误] 路径不存在: {path}"
    out = []
    for p in _walk(root):
        if p.suffix in _PY_SUFFIXES:
            out.extend(_py_map(p))
        elif p.suffix in _JS_SUFFIXES:
            out.extend(_js_map(p))
        elif p.suffix in _JAVA_SUFFIXES:
            out.extend(_java_map(p))
        elif p.suffix in _HTML_SUFFIXES:
            out.extend(_html_map(p))
    return "\n".join(out) if out else "(无受支持的代码文件)"