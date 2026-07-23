#!/usr/bin/env python3
"""cta-generate-test-cases 自包含编排器（不依赖 code_tutor_agent）。

流程：
  1. 从 problem_json 取参考解（optimal_solution / brute_solution）、
     function_signature、description、constraints、可见示例。
  2. 生成候选输入（四类，可独立失败）：
       a. 题目自带可见示例（input 复用，expected 由 oracle 重算，权威）；
       b. 随机输入（random_gen）；
       c. LLM 边界用例（边界种子来自 --boundary-json，由 skill 的 llm step 生成；
          文件缺失/解析失败则跳过，仅用 random+structure）；
       d. 复杂结构（树/图）：纯随机结构生成（random_gen，已支持合法图边表/树层序）。
  3. 全部候选交 oracle_runner 用参考解实跑，回填 expected_output；
     参考解崩溃/超时/无输出的用例丢弃。
  4. 写出 test_cases.json（字段与判题器一致：input_args / expected_output /
     is_hidden / explanation），可见示例标记为 is_hidden=False。

纯标准库；不 import code_tutor_agent，也不依赖任何外部 LLM 客户端
（边界用例种子由 skill 的 type:llm step 生成并落盘到 --boundary-json）。
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from oracle_runner import oracle_one, parse_signature  # noqa: E402
import random_gen as rg  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("gen_test_cases")

DROP_STATUSES = ("Runtime Error", "TLE", "Judge Error")


def _load_problem(raw: str) -> dict:
    """raw 是 JSON 字符串或文件路径。"""
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as f:
            return json.load(f)
    # 尝试当作 JSON 文件路径（去引号）
    p = raw.strip().strip('"').strip("'")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    raise ValueError("无法解析 problem_json（既非 JSON 也非文件路径）")


def _to_arg_str(v) -> str:
    if isinstance(v, str):
        return '"' + v + '"'
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def _example_to_input_args(func_sig: str, raw) -> list[str] | None:
    """把题目示例的 input 解析为 input_args（每参一个 JSON 字符串）。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                elems = json.loads(s)
            except Exception:
                return None
        else:
            # 形如 "nums = [1,2,3], target = 9"：剥离前缀后按行/逗号拆太脆弱，
            # 直接尝试整体 ast.literal_eval（常见导出格式为 JSON 数组）。
            return None
    elif isinstance(raw, list):
        elems = raw
    else:
        return None
    if not isinstance(elems, list):
        return None
    params, _ = parse_signature(func_sig)
    if params and len(elems) != len(params):
        # 元素数不匹配签名：尝试整体当作单参
        if len(params) == 1:
            return [_to_arg_str(elems)]
        return None
    return [_to_arg_str(e) for e in elems]


def _struct_kind(func_sig: str) -> str | None:
    params, _ = parse_signature(func_sig)
    for _, t in params:
        base = t.strip()
        if "TreeNode" in base or "Node" in base:
            return "tree"
        if "List[List[int]]" in base:
            return "graph"
    return None


def _gen_struct_inputs(func_sig: str, problem: dict, rnd: random.Random, count: int) -> list[list[str]]:
    """树/图类：纯随机结构生成（random_gen 已支持合法图边表/树层序）。"""
    kind = _struct_kind(func_sig)
    if not kind:
        return []
    out: list[list[str]] = []
    attempts = 0
    while len(out) < count and attempts < count * 3:
        attempts += 1
        # 复用 random_gen 对结构参数（TreeNode / Node / List[List[int]] 图）的随机生成
        grp = rg.generate_random_inputs(func_sig, problem.get("constraints"),
                                        problem.get("description"), seed=rnd.randint(0, 10**9), count=1)
        if grp:
            out.append(grp[0])
    return out


def _load_boundary(path: str | None) -> list[dict]:
    """读取 LLM 落盘的 boundary.json，容错解析为候选列表；任何失败返回 []。

    期望格式：JSON 数组，每元素 {"input_args": [每参一个 JSON 字符串], "explanation": "..."}。
    兼容 LLM 常见的 ```json 围栏与多余文字；文件缺失/解析失败/内容是失败文本 → 返回 []。
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            raw = f.read()
        # 容错多种编码（GBK 常见于中文 Windows 的默认写盘）
        text = None
        for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", errors="replace")
        text = text.strip()
    except Exception:
        return []
    if not text or text.startswith("[LLM 调用失败"):
        return []
    # 去 ```json / ``` 围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 抽取首个 [...] 数组
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    cands: list[dict] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        ia = item.get("input_args")
        if isinstance(ia, str):
            ia = [ia]
        if not isinstance(ia, list) or not ia:
            continue
        # 元素非字符串则转成 JSON 字符串
        norm = [_to_arg_str(x) if not isinstance(x, str) else x for x in ia]
        cands.append({
            "input_args": norm,
            "is_hidden": True,
            "explanation": item.get("explanation") or "LLM 边界用例",
            "expected_output": "",
        })
    return cands


def _run_oracle_batch(oracle_code: str, func_sig: str, candidates: list[dict], timeout: float = 12.0) -> list[dict]:
    """对候选用例批量跑 oracle，返回回填了 expected_output 的用例。"""
    results = []
    for cand in candidates:
        tc = oracle_one(oracle_code, cand, func_sig, timeout=timeout)
        if tc is None:
            logger.info("  丢弃用例（参考解崩溃/无输出）: %s", cand.get("input_args"))
            continue
        results.append(tc)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--problem", required=True, help="题目 JSON 字符串或文件路径")
    ap.add_argument("--out", required=True, help="输出 test_cases.json 路径")
    ap.add_argument("--count", type=str, default=None, help="随机/结构用例数量（不含示例；省略则用默认 12）")
    ap.add_argument("--boundary-json", type=str, default=None,
                   help="LLM 落盘的边界用例 JSON 路径（可选，缺失/解析失败则跳过）")
    ap.add_argument("--visible-count", type=int, default=4, help="可见示例保留数量")
    ap.add_argument("--seed", type=str, default=None, help="随机种子（可传整数或任意字符串）")
    ap.add_argument("--timeout", type=float, default=12.0)
    args = ap.parse_args()

    # count 容错：skill-engine 未显式传 count 时，$count 会保留为字面量 "$count"，
    # 此时退化为默认 12，避免 argparse 因非整数崩溃。
    _c = args.count
    count = 12
    if isinstance(_c, str) and _c.strip().lstrip("-").isdigit():
        count = int(_c)
    seed = args.seed  # random.Random 接受 int 或 str 种子；未传则为 None

    problem = _load_problem(args.problem)
    func_sig = problem.get("function_signature") or ""
    oracle_code = problem.get("optimal_solution") or problem.get("brute_solution") or ""
    if not oracle_code or not func_sig:
        logger.error("题目缺少 function_signature 或参考解（optimal_solution/brute_solution），无法生成。")
        sys.exit(2)

    rnd = random.Random(seed)
    candidates: list[dict] = []

    # a. 可见示例
    examples = problem.get("examples") or problem.get("example_test_cases") or problem.get("visible_test_cases") or []
    for ex in examples:
        raw_input = ex.get("input") if isinstance(ex, dict) else ex
        ia = _example_to_input_args(func_sig, raw_input)
        if ia:
            candidates.append({
                "input_args": ia,
                "is_hidden": False,
                "explanation": (ex.get("explanation") if isinstance(ex, dict) else "") or "题目示例",
                "expected_output": str(ex.get("output", "")) if isinstance(ex, dict) else "",
            })

    # b. 随机输入
    random_inputs = rg.generate_random_inputs(
        func_sig, problem.get("constraints"), problem.get("description"),
        seed=rnd.randint(0, 10**9), count=count,
    )
    for ia in random_inputs:
        candidates.append({"input_args": ia, "is_hidden": True,
                           "explanation": "随机生成", "expected_output": ""})

    # c. LLM 边界用例（来自 --boundary-json，由 skill 的 llm step 生成）
    boundary = _load_boundary(args.boundary_json)
    if boundary:
        logger.info("载入 LLM 边界用例 %d 条", len(boundary))
        candidates.extend(boundary)
    else:
        logger.info("无 LLM 边界用例（--boundary-json 缺失/解析失败），跳过")

    # d. 复杂结构（树/图）：LLM 种子 + 扩充，否则随机
    struct_inputs = _gen_struct_inputs(func_sig, problem, rnd, count=count)
    for ia in struct_inputs:
        candidates.append({"input_args": ia, "is_hidden": True,
                           "explanation": "结构用例（种子+扩充）", "expected_output": ""})

    logger.info("候选用例 %d 条，开始用参考解实跑回填 expected_output ...", len(candidates))
    filled = _run_oracle_batch(oracle_code, func_sig, candidates, timeout=args.timeout)
    logger.info("成功回填 %d / %d 条", len(filled), len(candidates))

    # 拆分可见/隐藏
    visible = [c for c in filled if not c.get("is_hidden")]
    hidden = [c for c in filled if c.get("is_hidden")]
    # 可见示例数量上限
    visible = visible[: max(args.visible_count, len(visible))]
    # 清理内部字段
    def _clean(c):
        return {
            "input_args": c["input_args"],
            "expected_output": c["expected_output"],
            "is_hidden": c.get("is_hidden", True),
            "explanation": c.get("explanation", ""),
        }
    out_obj = {
        "generated_at": "",
        "function_signature": func_sig,
        "count": len(visible) + len(hidden),
        "visible_test_cases": [_clean(c) for c in visible],
        "test_cases": [_clean(c) for c in visible] + [_clean(c) for c in hidden],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_obj, f, ensure_ascii=False, indent=2)
    logger.info("已写出 %s（可见 %d，隐藏 %d）", args.out, len(visible), len(hidden))


if __name__ == "__main__":
    main()
