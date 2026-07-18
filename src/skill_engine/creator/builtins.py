"""内置脚本模板 — 仅用于 Prompt 上下文 + Validator 语法检查

这三个脚本的源码被注入到 CREATE_SKILL_PROMPT 中作为参考实现，
让 LLM 可以复用其逻辑而非从零编写。

不在运行时参与任何流程。
"""

# ================================================================
# write_to_file.py
# ================================================================
WRITE_TO_FILE_PY = '''#!/usr/bin/env python3
"""Write content to a file, auto-creating parent directories.

Usage:
    python write_to_file.py
Environment variables:
    DEST_PATH   - Target file path
    CONTENT     - Content to write
"""
import sys
import os

dest = os.environ.get("DEST_PATH", "")
content = os.environ.get("CONTENT", "")

if not dest:
    print("Error: DEST_PATH not set", file=sys.stderr)
    sys.exit(1)

os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
with open(dest, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Written {len(content)} bytes to {dest}")
'''

# ================================================================
# read_file.py
# ================================================================
READ_FILE_PY = '''#!/usr/bin/env python3
"""Read a file and print its contents to stdout.

Usage:
    python read_file.py <path>            # via command-line argument
    FILE_PATH=<path> python read_file.py  # via environment variable
"""
import sys
import os

# Try command-line argument first, then environment variable
path = ""
if len(sys.argv) > 1:
    path = sys.argv[1]
else:
    path = os.environ.get("FILE_PATH", "")

if not path:
    print("Error: no file path provided", file=sys.stderr)
    sys.exit(1)

try:
    content = open(path, encoding="utf-8").read()
    print(content, end="")
except FileNotFoundError:
    print(f"Error: file not found: {path}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
'''

# ================================================================
# safe_math_eval.py
# ================================================================
SAFE_MATH_EVAL_PY = '''#!/usr/bin/env python3
"""Safely evaluate a mathematical expression using AST parsing.

Usage:
    python safe_math_eval.py
Environment variables:
    EXPRESSION   - Mathematical expression to evaluate

Supported operations: +, -, *, /, ** (power), unary - (negation)
Constants: numbers only (integers and floats)
"""
import ast
import operator
import sys
import os

expr = os.environ.get("EXPRESSION", "").strip()
if not expr:
    print("Error: EXPRESSION not set", file=sys.stderr)
    sys.exit(1)

SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def eval_expr(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPS:
        return SAFE_OPS[type(node.op)](eval_expr(node.left), eval_expr(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPS:
        return SAFE_OPS[type(node.op)](eval_expr(node.operand))
    raise ValueError("Unsafe expression")


try:
    result = eval_expr(ast.parse(expr, mode="eval").body)
    print(result)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
'''

# ================================================================
# Index
# ================================================================
BUILTIN_SCRIPTS = {
    "write_to_file": WRITE_TO_FILE_PY,
    "read_file": READ_FILE_PY,
    "safe_math_eval": SAFE_MATH_EVAL_PY,
}

BUILTIN_SCRIPT_NAMES = set(BUILTIN_SCRIPTS.keys())


def get_builtin_script(name: str) -> str | None:
    """获取内置脚本的源码

    Args:
        name: 脚本名（不含 .py），如 "write_to_file"

    Returns:
        脚本源码字符串，不存在则返回 None
    """
    return BUILTIN_SCRIPTS.get(name)