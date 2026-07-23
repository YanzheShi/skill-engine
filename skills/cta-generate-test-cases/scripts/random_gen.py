#!/usr/bin/env python3
"""自包含随机输入生成器（不依赖 code_tutor_agent）。

按 function_signature 的类型与 constraints/description 生成随机合法输入，
返回与判题器 input_args 同格式的字符串列表（每参一个 JSON 字符串）。

设计目标：
  - 纯标准库（random / json / ast / re），运行时零外部依赖。
  - 生成的 input_args 能被 oracle_runner._build_harness 正确解析
    （json.loads 为值，ListNode/TreeNode/Node 经 _cta_* 还原）。
  - 处理「有序数组」类题语义（合并有序数组等）：命中风婚后对 List[int]
    真实元素升序排序，保留补零，避免随机无序输入导致参考解行为不符预期。
  - 生成器本身不保证参考解一定通过；oracle_runner 跑挂的用例由编排器丢弃，
    因此生成器的偶发缺陷只影响覆盖度，不会污染 expected_output。
"""

from __future__ import annotations

import ast
import json
import random
import re

from oracle_runner import parse_signature

DEFAULT_INT_MIN, DEFAULT_INT_MAX = -1000, 1000
DEFAULT_LEN = 8

# 提示数组应「有序」的关键词（合并有序数组 / 有序数组二分等）
_SORTED_HINTS = (
    "non-decreasing", "non-increasing", "sorted in", "ascending order",
    "descending order", "升序", "降序", "有序", "非递减", "非递增",
    "sorted array", "sorted list",
)
# 提示某 int 参数是「数组长度」的命名集合（合并有序数组的 m/n 等）
_LENGTH_HINTS = {"m", "n", "k", "l", "len", "length", "size"}


def _looks_like_length(name: str) -> bool:
    n = name.lower().strip()
    if n in _LENGTH_HINTS:
        return True
    return any(h in n for h in ("len", "length", "size", "count"))


def _is_list_type(type_str: str) -> bool:
    return bool(re.match(r"^List\[", type_str.strip()))


def _is_list_of_ints(type_str: str) -> bool:
    return re.match(r"^List\[int\]$", type_str.strip()) is not None


def _needs_sorted_inputs(*texts) -> bool:
    norm: list[str] = []
    for t in texts:
        if isinstance(t, (list, tuple)):
            norm.append(" \n ".join(str(x) for x in t if x))
        elif t:
            norm.append(str(t))
    blob = " \n ".join(norm).lower()
    return any(h in blob for h in _SORTED_HINTS)


def _rnd_int(rnd: random.Random, lo: int = DEFAULT_INT_MIN, hi: int = DEFAULT_INT_MAX) -> int:
    return rnd.randint(lo, hi)


def _rnd_str(rnd: random.Random) -> str:
    n = rnd.randint(1, 8)
    return "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))


def _gen_list_of_int(rnd: random.Random, length: int, lo: int, hi: int) -> list:
    return [_rnd_int(rnd, lo, hi) for _ in range(length)]


def _gen_list_of_str(rnd: random.Random, length: int) -> list:
    return [_rnd_str(rnd) for _ in range(length)]


def _gen_linked_list(rnd: random.Random, length: int, lo: int, hi: int) -> list:
    # ListNode 在 oracle 中由层序列表还原，直接返回 int 列表即可
    return _gen_list_of_int(rnd, length, lo, hi)


def _gen_tree_level_order(rnd: random.Random, length: int, lo: int, hi: int) -> list:
    """生成二叉树层序表示（含随机 null，root 非 null）。

    oracle 的 _cta_tree_from_list 接受层序列表（null 表示空孩子），
    任意 root 非 null 的层序列表都是合法二叉树。
    """
    vals: list = []
    for i in range(length):
        if i == 0:
            vals.append(_rnd_int(rnd, lo, hi))
        else:
            # 约 15% 概率置空，制造不规则树形
            vals.append(None if rnd.random() < 0.15 else _rnd_int(rnd, lo, hi))
    return vals


def _gen_nary_level_order(rnd: random.Random, length: int, lo: int, hi: int) -> list:
    # N 叉树（Node）：按二叉树层序编码，oracle 用 _cta_tree_from_list 还原。
    # LeetCode N 叉树编码不同，但这里仅供 oracle 实跑参考解；若参考解语义不符
    # 会被 oracle 判 Runtime Error 而丢弃。
    return _gen_tree_level_order(rnd, length, lo, hi)


def _gen_adjacency(rnd: random.Random, n: int) -> list:
    """生成无向图邻接表 List[List[int]]（节点 0..n-1，无自环，连通）。"""
    adj = [[] for _ in range(n)]
    # 随机生成一棵生成树保证连通
    order = list(range(1, n))
    rnd.shuffle(order)
    for v in order:
        u = rnd.randint(0, v - 1)
        adj[u].append(v)
        adj[v].append(u)
    # 额外随机加边
    extra = rnd.randint(0, max(0, n // 2))
    for _ in range(extra):
        a, b = rnd.randint(0, n - 1), rnd.randint(0, n - 1)
        if a != b and b not in adj[a]:
            adj[a].append(b)
            adj[b].append(a)
    return [sorted(nei) for nei in adj]


def _gen_edge_list(rnd: random.Random, n: int, m: int) -> list:
    """生成无向图**边表** List[List[int]]（每边 [u, v]，节点 0..n-1，无自环，默认连通）。

    与 _gen_adjacency（邻接表）不同： criticalConnections 等题的 connections 是边表，
    每条子列表必须恰好 [u, v] 两个元素，oracle 里 ``for u, v in connections`` 才能解包。
    """
    if n <= 0:
        return []
    edges = set()
    # 先生成一棵生成树保证连通（n 个节点的连通图至少 n-1 条边）
    if n > 1:
        order = list(range(1, n))
        rnd.shuffle(order)
        for v in order:
            u = rnd.randint(0, v - 1)
            edges.add((u, v))
    # 再随机补充额外边，直到达到目标边数 m
    attempts = 0
    while len(edges) < m and attempts < max(1, m * 6):
        a, b = rnd.randint(0, n - 1), rnd.randint(0, n - 1)
        if a != b:
            edges.add((min(a, b), max(a, b)))
        attempts += 1
    return [[u, v] for (u, v) in edges]


def _generate_param_value(name: str, type_str: str, rnd: random.Random, length: int) -> object:
    t = (type_str or "").strip()
    # "Optional[" 共 9 字符，剥前缀取 t[9:] 再去掉末尾 ]；保留嵌套 List[List[int]]
    base = t[9:-1].strip() if t.startswith("Optional[") else t
    if base in ("int",):
        return _rnd_int(rnd)
    if base in ("str",):
        return _rnd_str(rnd)
    if base in ("bool",):
        return rnd.choice([True, False])
    if base in ("float",):
        return round(rnd.uniform(-100.0, 100.0), 2)
    if base in ("ListNode",):
        return _gen_linked_list(rnd, length, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    if base in ("TreeNode",):
        return _gen_tree_level_order(rnd, length, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    if base in ("Node",):
        return _gen_nary_level_order(rnd, length, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    if base == "List[int]":
        return _gen_list_of_int(rnd, length, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    if base == "List[str]":
        return _gen_list_of_str(rnd, min(length, 6))
    if base == "List[bool]":
        return [rnd.choice([True, False]) for _ in range(length)]
    if base == "List[float]":
        return [round(rnd.uniform(-100.0, 100.0), 2) for _ in range(length)]
    if base == "List[List[int]]":
        # 图邻接表
        n = max(2, min(length, 12))
        return _gen_adjacency(rnd, n)
    if base.startswith("List["):
        # 其他 List[X]：按元素为 int 处理
        return _gen_list_of_int(rnd, length, DEFAULT_INT_MIN, DEFAULT_INT_MAX)
    # 未知类型：回退为 int
    return _rnd_int(rnd)


def _to_arg_str(v) -> str:
    if isinstance(v, str):
        return '"' + v + '"'
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def sort_sorted_inputs(func_sig: str, input_args: list[str]) -> list[str]:
    """「有序」类题目：对 List[int] 输入的真实元素升序排序，保留补零。

    例：``['[1,0,0,0,0]','3','[2,3]','2']`` -> ``['[0,0,1,0,0]','3','[2,3]','2']``
    """
    params, _ = parse_signature(func_sig)
    pairs: list[tuple[int, int]] = []
    for i, (pname, t) in enumerate(params):
        if _is_list_of_ints(t):
            for j in range(i + 1, len(params)):
                if params[j][1].strip() == "int" and _looks_like_length(params[j][0]):
                    pairs.append((i, j))
                    break
    values = [ast.literal_eval(a) for a in input_args]
    used = set()
    for li, ln in pairs:
        used.add(li)
        if isinstance(values[li], list) and ln < len(values) and isinstance(values[ln], int):
            length = values[ln]
            values[li] = sorted(values[li][:length]) + values[li][length:]
    for i, (pname, t) in enumerate(params):
        if _is_list_of_ints(t) and i not in used and isinstance(values[i], list):
            values[i] = sorted(values[i])
    return [_to_arg_str(v) for v in values]


def _maybe_inject_valid_pair(raw: list, params: list, rnd: random.Random) -> list:
    """two-sum 类问题增强：以 50% 概率把 int 型参数（如 target）设为某 List[int] 参数中
    两元素之和，保证存在有效解、能真正锻炼算法。仅改输入，oracle 仍按参考解实跑回填，
    故不会造成序列化漂移。对其他签名（无 List[int]+int 组合）直接跳过，安全无害。
    """
    list_idx = next((i for i, (n, t) in enumerate(params) if _is_list_of_ints(t)), None)
    int_idx = next((i for i, (n, t) in enumerate(params) if t.strip() == "int"), None)
    if list_idx is None or int_idx is None:
        return raw
    arr = raw[list_idx]
    if not isinstance(arr, list) or len(arr) < 2:
        return raw
    if rnd.random() < 0.5:
        i, j = rnd.sample(range(len(arr)), 2)
        new_raw = list(raw)
        new_raw[int_idx] = arr[i] + arr[j]
        return new_raw
    return raw


def generate_random_inputs(
    func_sig: str,
    constraints: str | None = None,
    description: str | None = None,
    seed: int | None = None,
    count: int = 8,
) -> list[list[str]]:
    """生成 ``count`` 组随机输入，每组是 input_args（每参一个 JSON 字符串）。

    图题特例：当签名含 ``connections``/``edges`` 这类边表参数（List[List[int]]）且存在
    整型节点数参数（名为 n/nodes/numNodes/V 等）时，整型节点数生成为正整数 ``n``，
    边表生成为 n 个节点上的合法边表（节点 ∈ [0, n-1]、连通），保证 oracle 不崩。
    """
    params, _ = parse_signature(func_sig)
    if not params:
        return []
    rnd = random.Random(seed)
    sorted_hint = _needs_sorted_inputs(constraints or "", description or "")

    # 图题检测：边表参数（名字命中）+ 节点数参数
    edge_idx = next(
        (i for i, (nm, t) in enumerate(params)
         if re.match(r"^List\[List\[int\]\]$", (t or "").strip())
         and nm.lower() in ("connections", "edges", "graph", "adjacency")),
        None,
    )
    node_idx = next(
        (i for i, (nm, t) in enumerate(params)
         if (t or "").strip() == "int"
         and nm.lower() in ("n", "nodes", "numnodes", "num_nodes", "v", "vertex", "vertices")),
        None,
    ) if edge_idx is not None else None
    is_graph = edge_idx is not None and node_idx is not None

    results: list[list[str]] = []
    for _ in range(count):
        length = rnd.randint(2, max(2, DEFAULT_LEN))
        if is_graph:
            n = length  # 正整数节点数 2..8
            m = rnd.randint(max(1, n - 1), min(n * (n - 1) // 2, n * 2))
            raw = []
            for i, (name, t) in enumerate(params):
                if i == node_idx:
                    raw.append(n)
                elif i == edge_idx:
                    raw.append(_gen_edge_list(rnd, n, m))
                else:
                    raw.append(_generate_param_value(name, t, rnd, length))
        else:
            raw = [_generate_param_value(name, t, rnd, length) for name, t in params]
        raw = _maybe_inject_valid_pair(raw, params, rnd)
        args = [_to_arg_str(v) for v in raw]
        if sorted_hint:
            args = sort_sorted_inputs(func_sig, args)
        results.append(args)
    return results


if __name__ == "__main__":
    import sys
    sig = sys.argv[1] if len(sys.argv) > 1 else "nums: List[int], target: int -> List[int]"
    for grp in generate_random_inputs(sig, "array is non-decreasing", count=3, seed=1):
        print(grp)
