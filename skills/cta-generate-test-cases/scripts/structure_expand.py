#!/usr/bin/env python3
"""把 LLM 产出的小规模结构种子随机扩充到目标规模，并做合法性校验。

策略（用户选定：LLM 种子 + 随机扩充）：
  - 树：在种子层序数组基础上，随机给已有非 null 节点补子节点，直到达到目标节点数，
    始终保持「root 非 null 的层序数组」这一合法二叉树表示。
  - 图：把种子邻接表当作初始连通分量，逐节点挂到已有节点上保证连通、无自环；
    acyclic=True 时只连一跳（保持树形，无环），否则额外随机加边。
"""

from __future__ import annotations

import random


def _rnd_int(rnd: random.Random, lo: int = -1000, hi: int = 1000) -> int:
    return rnd.randint(lo, hi)


def expand_tree(seed_value: list, target_n: int, rnd: random.Random) -> list:
    arr = list(seed_value)
    if not arr or arr[0] is None:
        arr = [_rnd_int(rnd)] + arr
    non_null = sum(1 for x in arr if x is not None)
    attempts = 0
    cap = max(target_n * 20, 200)
    while non_null < target_n and attempts < cap:
        attempts += 1
        idxs = [i for i, x in enumerate(arr) if x is not None]
        if not idxs:
            break
        i = rnd.choice(idxs)
        placed = False
        for c in (2 * i + 1, 2 * i + 2):
            if c >= len(arr):
                arr.extend([None] * (c - len(arr) + 1))
            if arr[c] is None:
                arr[c] = _rnd_int(rnd)
                non_null += 1
                placed = True
                break
        if not placed:
            # 该节点两子均存在，换一个节点再试
            continue
    return arr


def expand_graph(seed_adj: list, target_n: int, rnd: random.Random, acyclic: bool = True) -> list:
    n0 = len(seed_adj)
    adj = [list(row) for row in seed_adj]
    # 规整：确保节点 0..n0-1 连续、无自环
    for v in range(n0):
        adj[v] = sorted(set(x for x in adj[v] if x != v and 0 <= x < n0))
    for v in range(n0, target_n):
        adj.append([])
        if v == 0:
            continue
        u = rnd.randint(0, v - 1)
        adj[u].append(v)
        adj[v].append(u)
        if not acyclic:
            for _ in range(rnd.randint(0, 2)):
                a, b = rnd.randint(0, v), rnd.randint(0, v)
                if a != b and b not in adj[a]:
                    adj[a].append(b)
                    adj[b].append(a)
    return [sorted(row) for row in adj]


def validate_tree(value: list) -> bool:
    return bool(value) and value[0] is not None


def validate_graph(adj: list) -> bool:
    n = len(adj)
    if n == 0:
        return False
    # 无自环
    for v, row in enumerate(adj):
        if v in row:
            return False
    # 连通性（BFS）
    seen = {0}
    stack = [0]
    while stack:
        u = stack.pop()
        for w in adj[u]:
            if 0 <= w < n and w not in seen:
                seen.add(w)
                stack.append(w)
    return len(seen) == n


if __name__ == "__main__":
    rnd = random.Random(7)
    print("tree:", expand_tree([1, 2, 3, None, None, 4, 5], 10, rnd))
    print("graph:", expand_graph([[1], [0, 2], [1]], 6, rnd, acyclic=True))
