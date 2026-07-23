# cta-generate-test-cases 使用文档

测试用例 skill：为一道已出好的算法题**一次性生成完整测试用例**（随机用例 + LLM 边界用例 +
复杂结构用例），所有用例都用题目自带参考解（oracle）实跑回填 `expected_output`。

本 skill 通过 **Steps DSL 一次性跑完**（`generate` → `read_result`），**完全自包含**，
不依赖 code-tutor-agent：运行仅需 Python 标准库，`gen_test_cases.py` 等脚本不 `import`
`code_tutor_agent`，oracle 的序列化契约逐字节对齐 code-tutor-agent 判题器
（`sandbox.runner._build_harness`），因此正确提交解在判题器上必判 Passed。

## 输入方式（任选其一）

| 参数 | 必填 | 说明 |
|---|---|---|
| `problem_json` | 二选一 | 指向题目 JSON 文件的路径（推荐，字段最全）；也可直接传整段题目 JSON 字符串 |
| `description` + `function_signature` + `optimal_solution` | 二选一 | 命令行直输的题目核心字段 |
| `count` | 否 | 随机 / 结构用例数量（默认 12） |
| `seed` | 否 | 随机种子（默认按 title 哈希，保证可复现） |

- 题目 JSON 字段：`function_signature`（如 `nums: List[int], target: int -> List[int]`）、
  `optimal_solution` / `brute_solution`（参考解，作 oracle，至少其一）、`description`、
  `constraints`、可选 `examples`（可见示例）。
- `count` / `seed` 也可直接写进题目 JSON 文件。

## 题目 JSON 示例

```json
{
  "title": "两数之和",
  "description": "给定一个整数数组 nums 和一个目标值 target，请在数组中找出和为目标值的两个整数并返回下标。",
  "difficulty": "easy",
  "function_signature": "nums: List[int], target: int -> List[int]",
  "optimal_solution": "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        seen = {}\n        for i, n in enumerate(nums):\n            if target - n in seen:\n                return [seen[target - n], i]\n            seen[n] = i\n        return []",
  "brute_solution": "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        for i in range(len(nums)):\n            for j in range(i + 1, len(nums)):\n                if nums[i] + nums[j] == target:\n                    return [i, j]\n        return []",
  "constraints": ["2 <= nums.length <= 10^4", "-10^9 <= nums[i] <= 10^9"],
  "test_cases": []
}
```

## 运行方式

```bash
cd D:/Code/PycharmProjects/skill-engine
export SKILLS_ENGINE_SECURITY_MODE=off      # 或 SKILLS_ENGINE_AUTO_APPROVE=all 放行 exec 步

# 方式一（推荐）：从题目 JSON 文件生成
skill-engine run cta-generate-test-cases --args "problem_json=/abs/path/to/problem.json count=12"

# 方式二：命令行直输（把整段题目 JSON 作为 problem_json 传入）
skill-engine run cta-generate-test-cases --args 'problem_json={"function_signature":"nums: List[int], target: int -> List[int]","optimal_solution":"..."} count=12'
```

> 该 skill 是 Steps DSL，`run` 命令会**自动检测 `## Steps` 执行，无需 `--llm`**。`--llm` 是
> 档位 A「单次 LLM 调用」开关，对本 skill 无效；LLM 边界 / 结构用例由 `gen_test_cases.py`
> 内部按环境变量（`CTA_LLM_*` / `OPENAI_*`）调用，未配置则自动跳过、不影响主流程。

## 安全模式要求

`generate` 是 `python` exec 步骤，运行前需放行安全审批（同上「方式一 / 方式二」前的
`export`）。交互运行时按提示选 `A` 批准即可。

## 内部步骤

1. `generate`（exec）：`python scripts/gen_test_cases.py --problem $problem_json --out output/test_cases.json --count $count`。
   四类候选（题目示例 / 随机 / LLM 边界 / 树图种子扩充）统一交给自带 `oracle_runner`
   用参考解实跑回填 `expected_output`；参考解崩溃 / 超时 / 无输出的用例自动丢弃。
2. `read_result`（read）：读回 `output/test_cases.json` 作为 skill 最终产物。

`gen_test_cases.py` **完全自包含**（纯标准库 + 可选 LLM），不 `import code_tutor_agent`。

## 输出结构（test_cases.json）

```json
{
  "test_cases": [
    {"input_args": [...], "expected_output": "...", "is_hidden": false, "explanation": "随机生成测试 1"}
  ],
  "visible_test_cases": ["前 4 条可见用例"]
}
```

- `test_cases`：全量（可见示例 + 随机/边界/结构隐藏用例）。
- `visible_test_cases`：至多 4 条经 oracle 验证的可见用例。
- 每条 `input_args` 是「每参一个 JSON 字符串」的列表，与 code-tutor-agent 判题器一致。

## 注意事项

- `expected_output` 由参考解实跑回填，参考解崩溃的用例会被丢弃，保证用例可复现、数字可信。
- 题目自带的示例（`examples` 字段）会经 oracle 复核后并入最终结果，并标记为 `is_hidden=False`。
- 仅 `tests/test_harness_contract.py` 这类开发态契约测试需要 `import code_tutor_agent`，
  须在 code-tutor-agent 的 venv 下运行；线上生成流程无需该依赖。
