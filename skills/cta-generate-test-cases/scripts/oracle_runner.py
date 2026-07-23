#!/usr/bin/env python3
"""自包含 oracle 执行器 —— 复刻 code_tutor_agent.sandbox.runner 的序列化契约。

用途（cta-generate-test-cases）：
    用题目自带的参考解（optimal_solution / brute_solution）作为 oracle，
    对生成的 (input_args) 实跑出 expected_output，回填进测试用例。

为什么能「独立」又「不误判」：
    本模块把判题器 harness 的「输入解析 + 输出序列化」逐字段复刻
    （ds.INJECT_PROLOGUE 的 ListNode/TreeNode/Node 类 +
     struct_convert.HARNESS_STRUCT_SRC 的 _cta_* 转换 + runner._fmt +
     void 题读回 args[0]）。由于 oracle 把参考解当「待测解」跑，
    产出的 expected_output 字符串与判题器对「正确提交解」的输出逐字节一致，
    因此正确提交在判题器上必判 Passed。

纯标准库（subprocess / json / ast / re / tempfile / sys / os），
运行时不 import code_tutor_agent。仅测试脚本（test_harness_contract.py）
在开发态可 import code_tutor_agent 做对齐校验。

harness 行为说明：
    run_oracle 调用时每条用例的 expected_output 恒为空，harness 走
    「Skipped」分支，把参考解实跑的 actual_output 作为权威期望输出返回；
    参考解崩溃 / TLE 的用例 status 为 Runtime Error / TLE，actual_output
    为空，由编排器丢弃。
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)


# ── 复刻 ds.INJECT_PROLOGUE（与判题器逐字一致） ──
INJECT_PROLOGUE = """# ===== 平台预置类型注入 =====
from typing import *

# --- 链表节点 ---
class ListNode:
    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next

# --- 二叉树节点 ---
class TreeNode:
    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right

# --- N 叉树节点 ---
class Node:
    def __init__(self, val=None, children=None):
        self.val = val
        self.children = children if children is not None else []

"""

# ── 复刻 struct_convert.HARNESS_STRUCT_SRC（与判题器逐字一致） ──
HARNESS_STRUCT_SRC = r'''
def _cta_ll_from_list(vals):
    if vals is None:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    dummy = ListNode(0)
    cur = dummy
    for v in vals:
        cur.next = ListNode(v)
        cur = cur.next
    return dummy.next

def _cta_ll_to_list(node):
    out = []
    while node:
        out.append(node.val)
        node = node.next
    return out

def _cta_tree_from_list(vals):
    if not vals:
        return None
    if not isinstance(vals, list):
        vals = [vals]
    root = TreeNode(vals[0])
    queue = [root]
    i = 1
    while queue and i < len(vals):
        node = queue.pop(0)
        if i < len(vals) and vals[i] is not None:
            node.left = TreeNode(vals[i]); queue.append(node.left)
        i += 1
        if i < len(vals) and vals[i] is not None:
            node.right = TreeNode(vals[i]); queue.append(node.right)
        i += 1
    return root

def _cta_tree_to_list(root):
    if not root:
        return []
    out = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        if node is None:
            out.append(None)
        else:
            out.append(node.val)
            queue.append(node.left)
            queue.append(node.right)
    while out and out[-1] is None:
        out.pop()
    return out

def _cta_coerce_arg(raw, type_str):
    if not type_str:
        return raw
    t = type_str.replace("Optional[", "").rstrip("]")
    if "ListNode" in t:
        return _cta_ll_from_list(raw)
    if "TreeNode" in t or "Node" in t:
        return _cta_tree_from_list(raw)
    return raw

def _cta_coerce_result(val, type_str):
    if not type_str:
        return val
    t = type_str.replace("Optional[", "").rstrip("]")
    if "ListNode" in t:
        return _cta_ll_to_list(val)
    if "TreeNode" in t or "Node" in t:
        return _cta_tree_to_list(val)
    return val

def _cta_is_void_result(result, return_type):
    """判断是否为「原地修改 / 无返回值」型题目。"""
    if result is not None:
        return False
    rt = (return_type or "").strip().lower()
    return rt in ("", "none", "void")
'''

# ── 复刻 runner.parse_signature（input_generator 内同名函数） ──
_SIG_RE = re.compile(r"^(.*?)\s*->\s*(.+)$")
_PARAM_RE = re.compile(r"(\w+)\s*:\s*([^,]+)")


def parse_signature(sig: str) -> tuple[list[tuple[str, str]], str]:
    """Parse a function signature into parameter types and return type."""
    m = _SIG_RE.search(sig)
    if not m:
        logger.warning("Cannot parse signature: %s", sig)
        return [], ""
    params_str = m.group(1).strip()
    return_type = m.group(2).strip()
    params = _PARAM_RE.findall(params_str)
    return params, return_type


# ── 复刻 runner._extract_python_code / _clean_error ──
def _extract_python_code(text: str) -> str:
    if match := re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL):
        return match.group(1).strip()
    return text.strip()


_RE_TEMP = re.compile(r'File "[^"]+[\\/]tmp[^"]+\.py"')


def _clean_error(stderr: str) -> str:
    if not stderr:
        return ""
    cleaned = _RE_TEMP.sub("line", stderr)
    lines = cleaned.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line and not line.startswith(("Traceback", "File ", "  ", "^", "~")):
            return line[:200]
    return cleaned[-200:]


# ── harness 函数段：定义 _eval_arg/_fmt，发现 Solution 方法并跑用例 ──
# 与 runner._build_harness 的对应段逐字一致（不含 import 与 test_cases 赋值，
# 那部分在 _build_harness 中以内联字面量方式拼接，避免 json.loads 引号问题）。
_HARNESS_FUNCS = '''def _eval_arg(s):
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception:
        return s

def _fmt(val):
    if isinstance(val, (list, tuple)):
        return json.dumps(list(val), separators=(",", ":"))
    if isinstance(val, set):
        return json.dumps(sorted(val), separators=(",", ":"))
    return str(val)

sol = Solution()
members = inspect.getmembers(sol, predicate=inspect.ismethod)
public = [(n, fn) for n, fn in members if not n.startswith('_')]
if not public:
    print('RESULT: ' + json.dumps({"test_case_id": -1, "status": "Runtime Error", "detail": "no public method"}))
    sys.exit(0)

method_name, method_fn = public[0]

for idx, tc in enumerate(test_cases):
    args = [
        _cta_coerce_arg(_eval_arg(a), _param_types[i] if i < len(_param_types) else "")
        for i, a in enumerate(tc['input_args'])
    ]
    expected = tc['expected_output']
    start = time.perf_counter()
    try:
        result = method_fn(*args)
        elapsed = (time.perf_counter() - start) * 1000
        if _cta_is_void_result(result, _return_type):
            actual = _fmt(args[0]) if args and isinstance(args[0], (list, dict, set)) else _fmt(None)
        else:
            actual = _fmt(_cta_coerce_result(result, _return_type))
        if expected is None or (isinstance(expected, str) and expected.strip() == ""):
            print('RESULT: ' + json.dumps({"test_case_id": idx, "status": "Skipped", "detail": actual, "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": "", "actual_output": actual}))
            continue
        try:
            exp_val = json.loads(expected) if isinstance(expected, str) else expected
            exp_fmt = _fmt(exp_val)
        except (json.JSONDecodeError, TypeError, ValueError):
            exp_fmt = _fmt(expected)
        if actual == exp_fmt:
            print('RESULT: ' + json.dumps({"test_case_id": idx, "status": "Passed", "detail": actual, "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": exp_fmt, "actual_output": actual}))
        else:
            print('RESULT: ' + json.dumps({"test_case_id": idx, "status": "Wrong Answer", "detail": f"expected={exp_fmt} got={actual}", "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": exp_fmt, "actual_output": actual}))
    except Exception as exc:
        logger.error("Exception: %s", exc)
        elapsed = (time.perf_counter() - start) * 1000
        print('RESULT: ' + json.dumps({"test_case_id": idx, "status": "Runtime Error", "detail": str(exc)[:200], "runtime_ms": round(elapsed, 2), "input_args": tc['input_args'], "expected_output": "", "actual_output": ""}))
'''


def _build_harness(code: str, test_cases: list[dict], function_signature: str | None = None) -> str:
    """构建独立 Python 脚本（与 runner._build_harness 等价）。

    拼接顺序严格照搬判题器：prologue + struct 源 + 用户/参考解代码
    + import/日志 + test_cases/_param_types/_return_type 内联字面量 + 函数段。
    test_cases 等以内联 JSON 字面量给出（json.dumps 产物即合法 Python 字面量），
    不使用 json.loads 占位符，规避引号转义问题。
    """
    param_types: list[str] = []
    return_type = ""
    if function_signature:
        params, return_type = parse_signature(function_signature)
        param_types = [t for _, t in params]

    # harness 只需 input_args + expected_output；只序列化这两个字段，
    # 避免候选 dict 的 is_hidden(bool)/explanation 等被 json.dumps 成
    # 非法 Python（false/true/null）导致整段 harness 崩溃。
    tc_min = [
        {"input_args": tc.get("input_args"), "expected_output": tc.get("expected_output", "")}
        for tc in test_cases
    ]
    tc_json = json.dumps(tc_min)                  # 合法 Python 列表字面量
    param_types_json = json.dumps(param_types)  # 合法 Python 列表字面量
    return_type_json = json.dumps(return_type)   # 合法 Python 字符串字面量

    return (
        INJECT_PROLOGUE
        + HARNESS_STRUCT_SRC
        + "\n# --- User / reference code ---\n"
        + _extract_python_code(code)
        + "\n\n"
        + "import ast, json, sys, time, inspect\n"
        + "import logging\n\n"
        + "logger = logging.getLogger(__name__)\n\n"
        + "test_cases = " + tc_json + "\n"
        + "_param_types = " + param_types_json + "\n"
        + "_return_type = " + return_type_json + "\n\n"
        + _HARNESS_FUNCS
    )


def run_oracle(
    code: str,
    test_cases: list[dict],
    function_signature: str | None = None,
    timeout: float = 12.0,
) -> list[dict]:
    """用参考解 ``code`` 跑 test_cases（每条含 input_args），回填 expected_output。

    返回与判题器 RunnerResult 同形 dict 列表，每条含：
        test_case_id / status / detail / actual_output / input_args
    编排器应：status 为 Runtime Error / TLE / Judge Error 或 actual_output
    为空 -> 丢弃；否则取 actual_output 作为该用例的 expected_output。

    序列化与 code_tutor_agent.sandbox.runner 完全一致，确保正确提交解在
    判题器上判 Passed。
    """
    harness = _build_harness(code, test_cases, function_signature)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    n = len(test_cases) or 1
    try:
        tmp.write(harness)
        tmp.close()

        proc = subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True,
            text=True,
            timeout=timeout + 2.0,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

        results: list[dict] = []
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                data = json.loads(line[len("RESULT:"):])
                results.append(data)

        if not results and proc.returncode != 0:
            detail = _clean_error(proc.stderr[:500])
            return [_err_result(i, "Runtime Error", detail, test_cases) for i in range(n)]
        if not results:
            return [_err_result(i, "Judge Error", "No structured output", test_cases) for i in range(n)]
        return results

    except subprocess.TimeoutExpired:
        return [_err_result(i, "TLE", f"timed out after {timeout}s", test_cases) for i in range(n)]
    except Exception as exc:
        logger.error("Exception: %s", exc)
        return [_err_result(i, "Runtime Error", str(exc), test_cases) for i in range(n)]
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


def _err_result(i: int, status: str, detail: str, test_cases: list[dict]) -> dict:
    inp = test_cases[i].get("input_args") if i < len(test_cases) else []
    return {
        "test_case_id": i,
        "status": status,
        "detail": detail,
        "actual_output": "",
        "input_args": inp,
    }


def oracle_one(oracle_code: str, tc: dict, func_sig: str, timeout: float = 12.0) -> dict | None:
    """用 oracle 验证单条用例，返回回填了 expected_output 的用例或 None（丢弃）。"""
    results = run_oracle(oracle_code, [tc], function_signature=func_sig, timeout=timeout)
    if not results:
        return None
    r = results[0]
    if r.get("status") in ("Runtime Error", "TLE", "Judge Error"):
        return None
    actual = r.get("actual_output") or ""
    if not actual:
        return None
    tc = dict(tc)
    tc["expected_output"] = actual
    return tc


if __name__ == "__main__":
    # 简易自检：用一段参考解跑一条用例，打印 RESULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--sig", required=True)
    ap.add_argument("--input", required=True, help='JSON 数组字符串，如 \'["[1,2,3]","5"]\'')
    args = ap.parse_args()
    tc = {"input_args": json.loads(args.input), "expected_output": ""}
    out = run_oracle(args.code, [tc], function_signature=args.sig)
    print(json.dumps(out, ensure_ascii=False, indent=2))
