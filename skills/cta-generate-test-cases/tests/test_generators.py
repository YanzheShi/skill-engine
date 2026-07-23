"""离线单测：random_gen / structure_expand 产出合法且可解析。

不依赖 code_tutor_agent，纯标准库运行。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import random_gen as rg  # noqa: E402
import structure_expand as se  # noqa: E402
import random  # noqa: E402


def test_random_inputs_parseable():
    groups = rg.generate_random_inputs(
        "nums: List[int], target: int -> List[int]", "array is non-decreasing",
        count=5, seed=1,
    )
    assert len(groups) == 5
    for grp in groups:
        assert isinstance(grp, list)
        parsed = [json.loads(a) for a in grp]
        assert isinstance(parsed[0], list) and isinstance(parsed[1], int)
        # 有序提示：nums 应为升序
        assert parsed[0] == sorted(parsed[0])


def test_random_tree_and_graph():
    trees = rg.generate_random_inputs("root: Optional[TreeNode] -> int", count=3, seed=2)
    for t in trees:
        val = json.loads(t[0])
        assert val and val[0] is not None  # root 非 null

    graphs = rg.generate_random_inputs("graph: List[List[int]] -> bool", count=3, seed=3)
    for g in graphs:
        adj = json.loads(g[0])
        assert isinstance(adj, list)
        for row in adj:
            assert isinstance(row, list)


def test_struct_expand():
    r = random.Random(7)
    tree = se.expand_tree([1, 2, 3, None, None, 4, 5], 12, r)
    assert se.validate_tree(tree)
    assert sum(1 for x in tree if x is not None) <= 12

    graph = se.expand_graph([[1], [0, 2], [1]], 8, r, acyclic=True)
    assert se.validate_graph(graph)
    assert len(graph) == 8


if __name__ == "__main__":
    test_random_inputs_parseable()
    test_random_tree_and_graph()
    test_struct_expand()
    print("OK: generators 单测通过")
