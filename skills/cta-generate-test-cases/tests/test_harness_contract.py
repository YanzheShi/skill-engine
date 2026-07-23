"""契约测试（开发态）：证明自写 oracle_runner 与 code_tutor_agent 判题器对齐。

运行环境：装有 code_tutor_agent 的开发 venv（仅本测试 import code_tutor_agent）。
方式：直接调用判题器的 _build_harness + 本地子进程（绕过 Judge0 远程后端），
      与自写 oracle_runner 产出比对。

断言：
  1) 同批 (input, reference) 下，oracle_runner 的 actual_output 与判题器
     harness 的 actual_output 逐字节一致（序列化契约未漂移）。
  2) 用 oracle_runner 产出的 expected_output 喂回判题器 harness，正确提交解
     全部 Passed（即「正确解不会被误判 WA」）。
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

from oracle_runner import run_oracle  # noqa: E402
import code_tutor_agent.sandbox.runner as _R  # noqa: E402


def _judge_local(code: str, test_cases: list[dict], func_sig: str) -> list[dict]:
    """用判题器内部 _build_harness + 本地子进程跑用例（绕过 Judge0）。"""
    harness = _R._build_harness(code, test_cases, func_sig)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    tmp.write(harness)
    tmp.close()
    try:
        proc = subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True,
            text=True,
            timeout=20.0,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        results = []
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT:"):
                results.append(json.loads(line[len("RESULT:"):]))
        return results
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


# (参考解, 函数签名, 输入用例) 电池
BATTERY = [
    (
        "class Solution:\n"
        "    def twoSum(self, nums, target):\n"
        "        for i in range(len(nums)):\n"
        "            for j in range(i + 1, len(nums)):\n"
        "                if nums[i] + nums[j] == target:\n"
        "                    return [i, j]\n",
        "nums: List[int], target: int -> List[int]",
        [{"input_args": ["[2,7,11,15]", "9"]},
         {"input_args": ["[3,2,4]", "6"]},
         {"input_args": ["[-1,-2,-3,-4,-5]", "-8"]}],
    ),
    (
        "class Solution:\n"
        "    def maxDepth(self, root):\n"
        "        if not root:\n"
        "            return 0\n"
        "        return 1 + max(self.maxDepth(root.left), self.maxDepth(root.right))\n",
        "root: Optional[TreeNode] -> int",
        [{"input_args": ["[3,9,20,null,null,15,7]"]},
         {"input_args": ["[1,2,3,4,5]"]},
         {"input_args": ["[]"]}],
    ),
    (
        "class Solution:\n"
        "    def reverseList(self, head):\n"
        "        prev = None\n"
        "        cur = head\n"
        "        while cur:\n"
        "            nxt = cur.next\n"
        "            cur.next = prev\n"
        "            prev = cur\n"
        "            cur = nxt\n"
        "        return prev\n",
        "head: Optional[ListNode] -> Optional[ListNode]",
        [{"input_args": ["[1,2,3,4,5]"]},
         {"input_args": ["[1]"]},
         {"input_args": ["[]"]}],
    ),
    (
        "class Solution:\n"
        "    def merge(self, nums1, m, nums2, n):\n"
        "        nums1[m:m+n] = nums2[:n]\n"
        "        nums1.sort()\n",
        "nums1: List[int], m: int, nums2: List[int], n: int -> None",
        [{"input_args": ["[1,2,3,0,0,0]", "3", "[2,5,6]", "3"]},
         {"input_args": ["[0]", "0", "[1]", "1"]}],
    ),
]


def test_oracle_matches_judge():
    for code, sig, cases in BATTERY:
        # 1) 逐字节比对 actual（oracle_runner vs 判题器本地 harness）
        oracle_res = run_oracle(code, [dict(c, expected_output="") for c in cases], sig)
        judge_res = _judge_local(code, [dict(c, expected_output="") for c in cases], sig)
        assert len(oracle_res) == len(judge_res), f"长度不一致: {sig}"
        for o, j in zip(oracle_res, judge_res):
            assert o["actual_output"] == j.get("actual_output"), (
                f"序列化漂移 [{sig}] input={o['input_args']}\n"
                f"  oracle  = {o['actual_output']!r}\n"
                f"  judge   = {j.get('actual_output')!r}"
            )

        # 2) 用 oracle 产出 expected 喂回判题器，正确解应全 Passed
        fed = [dict(c, expected_output=oracle_res[i]["actual_output"]) for i, c in enumerate(cases)]
        judge_fed = _judge_local(code, fed, sig)
        for r in judge_fed:
            assert r["status"] == "Passed", (
                f"正确解被误判 [{sig}] input={r['input_args']} -> {r['status']}: {r.get('detail')}"
            )


if __name__ == "__main__":
    test_oracle_matches_judge()
    print("OK: oracle_runner 与 code_tutor_agent 判题器序列化契约完全一致，正确解全 Passed。")
