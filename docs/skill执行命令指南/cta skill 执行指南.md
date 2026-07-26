# CTA Skill 执行指南

记录 code-tutor-agent 三个出题相关 skill（`cta-generate-problem` / `cta-generate-solution` / `cta-generate-test-cases`）在 skill-engine 里的运行方式。

## 前提

- `skill-engine run` 固定扫描「当前目录下的 `skills/`」，所以运行前 **必须 `cd` 到 skill-engine 根目录**（其根下有 `skills/`）。在 code-tutor-agent 目录直接跑会扫不到 skill、直接 exit 1。
- `--llm` 模式需要 skill-engine 已配置可用 LLM（默认 `sensenova-deepseek`，见 `skill_engine.config.get_llm()`）。
- 不想调 LLM、只看 prompt 装配时，把 `--llm` 换成 `--dry-run` 即可。
- **题解 / 测试用例是 Steps DSL**，含 `python` exec 步骤，运行前需放行安全审批（二选一）：
  ```powershell
  $env:SKILLS_ENGINE_SECURITY_MODE = "off"      # 关闭审批（调试用）
  # 或
  $env:SKILLS_ENGINE_AUTO_APPROVE = "all"        # 自动批准所有步骤
  ```
- **测试用例 skill 已完全自包含**：`scripts/gen_test_cases.py` 及配套 `oracle_runner` / `random_gen` / `llm_gen` / `structure_expand` **不 import code_tutor_agent**，运行时仅需 Python 标准库；oracle 的序列化契约逐字节对齐判题器，正确提交解必判 Passed。LLM 边界/结构用例为可选增强：配置 `CTA_LLM_API_KEY` / `OPENAI_API_KEY` 时启用，未配置自动跳过、不影响主流程。

## 1. 出题 — `cta-generate-problem`

```powershell
cd D:/Code/PycharmProjects/skill-engine
uv run skill-engine run cta-generate-problem -a "topic=二分查找 difficulty=easy" --llm
```

- `-a` 后跟占位符 `topic` / `difficulty`（空格分隔的 `k=v`，值不带空格；含空格的长描述建议走 JSON 模式，见下）。
- 输出全部分节（Title / Topic / Difficulty / Description / Examples / Constraints / StarterCode / FunctionSignature / BruteSolution / OptimalSolution），可被 `code_tutor_agent.skills.parser.parse_problem_markdown` 解析。
- 只看装配、不调 LLM：`--llm` 换成 `--dry-run`。
- 该 skill 走档位 A（单次 LLM），**不经 `python` 执行步骤**，无需关闭安全审批，在 `strict` 模式下也能直接跑。

### 输出为 JSON / 落盘

新增 `output_format` 参数（`markdown` 默认 / `json`）。传 `output_format=json` 时只输出一个 ```` ```json ```` 代码块，字段与上方各节一一对应（title/topic/difficulty/description/examples/constraints/starter_code/function_signature/brute_solution/optimal_solution）。

```powershell
# 直接输出 JSON 块到终端
uv run skill-engine run cta-generate-problem -a "topic=双指针 difficulty=easy output_format=json" --llm

# 重定向落盘为 .json 文件（调用方按最后一个 ```json 块解析）
uv run skill-engine run cta-generate-problem -a "topic=双指针 difficulty=easy output_format=json" --llm > problem.json
```

## 2. 出题解（文字讲解 + 代码最优解 + 暴力解）— `cta-generate-solution`

本 skill 已改为 **Steps DSL 一次跑完**（prepare → solve）。输入方式（任选其一）：

- **命令行直输**：`description`（题目描述）+ `starter_code`（模板代码）同时给出；
- **JSON 文件**：`problem_json` 指向含 `description` / `starter_code`（可选 `title`/`topic`/`difficulty`）的 JSON 文件；
- 也可把整段题目 JSON 作为请求正文 `$ARGUMENTS` 传入，prepare 步骤自动解析。

```powershell
cd D:/Code/PycharmProjects/skill-engine
$env:SKILLS_ENGINE_SECURITY_MODE = "off"

# 方式一（推荐）：从 JSON 文件解析
uv run skill-engine run cta-generate-solution -a "problem_json=D:/path/to/problem.json" --llm

# 方式二：命令行直输（description 中文无空格可写内联；starter_code 多行建议改用 JSON 文件）
uv run skill-engine run cta-generate-solution -a "description=给定一个整数数组nums和目标值target，找出和为target的两个整数并返回下标 starter_code=class Solution:`n    def twoSum(self, nums, target):`n        pass" --llm
```

- `prepare` 步骤是 `python` exec，务必先放行安全审批（见前提）。
- `solve` 步骤走 skill-engine 的 `get_llm()`（默认 sensenova）生成题解。
- 输出结构（固定三节）：`## 思路讲解`（核心思想 / 算法流程 / 复杂度 / 易错点）+ `## OptimalSolution`（可 AC 最优代码，class Solution 风格）+ `## BruteSolution`（暴力对照）。`OptimalSolution` / `BruteSolution` 均为可被 Python `compile` 的合法代码。

## 3. 出测试用例（参考解 oracle 回填）— `cta-generate-test-cases` ✅ 一步到位

该 skill **完全自包含**（不依赖 code-tutor-agent），用 Steps DSL 一次跑完（`run` 命令自动检测 `## Steps` 执行，无需 `--llm`；LLM 边界 / 结构用例由脚本按环境变量在内部调用）：

- `generate`（exec）：`python scripts/gen_test_cases.py --problem $problem_json --out output/test_cases.json --count $count`，四类候选（题目示例 / 随机 / LLM 边界 / 树图结构种子扩充）统一交给自带 `oracle_runner` 用参考解实跑回填 `expected_output`。
- `read_result`（read）：读回 `output/test_cases.json` 作为 skill 最终产物。

oracle 的序列化契约**逐字节对齐** `code-tutor-agent` 现有判题器（`sandbox.runner._build_harness`），因此正确提交解在判题器上必判 Passed；参考解崩掉（Runtime Error / TLE）的用例会被自动丢弃。

```powershell
cd D:/Code/PycharmProjects/skill-engine
$env:SKILLS_ENGINE_SECURITY_MODE = "off"

# 方式一（推荐）：从题目 JSON 文件一次性生成
uv run skill-engine run cta-generate-test-cases -a "problem_json=D:/path/to/problem.json count=12"

# 方式二：命令行直输（把整段题目 JSON 作为请求正文 $ARGUMENTS 传入）
uv run skill-engine run cta-generate-test-cases -a "problem_json={\"function_signature\":\"nums: List[int], target: int -> List[int]\",\"optimal_solution\":\"...\"} count=12"
```

输入参数（任选其一）：

- `problem_json`：指向题目 JSON 文件（推荐，字段最全：至少含 `description` 与 `optimal_solution`/`brute_solution` 作 oracle，`function_signature` 决定参数还原方式；`count` / `seed` 也可写进该 JSON）。
- `description` + `function_signature` + `optimal_solution`（或 `brute_solution`）：命令行直输。
- `count` / `seed`：可选。`count` 默认 12；`seed` 默认按 `title` 哈希（同题可复现）。优先级：显式 CLI 参数 > 题目 JSON 字段 > 内置默认。

输出结构（与 `output/test_cases.json` 一致）：

```json
{
  "test_cases": [
    {"input_args": [...], "expected_output": "...", "is_hidden": false, "explanation": "随机生成测试 1"}
  ],
  "visible_test_cases": [ "...前 4 条可见用例..." ]
}
```

- 每条用例的 `expected_output` 都是用参考解（oracle）实跑回填的，参考解崩掉（Runtime Error / TLE / Judge Error）的用例会被丢弃，保证数字可信、可复现。
- 题目自带的示例 / 可见用例（`test_cases` 字段）会经 oracle 复核后并入最终结果。

> 仍想手动分步跑脚本（`gen_test_cases.py`）也行，参数：`--problem <题目json> --out <输出json> --count <n> --seed <n>`；`count` / `seed` 留空即从题目 JSON / 默认读取。该脚本完全自包含，任意标准库 Python 即可运行；仅 `tests/test_harness_contract.py` 这类开发态契约测试需要 `import code_tutor_agent`，须在 code-tutor-agent 的 venv 下跑。

## 一句话总结

| Skill | 怎么跑 |
|---|---|
| `cta-generate-problem` | `cd` skill-engine 根目录，`uv run skill-engine run cta-generate-problem -a "topic=.. difficulty=.." --llm`；加 `-a "output_format=json"` 可输出 JSON（可 `>` 落盘） |
| `cta-generate-solution` | 同上换 skill 名；入参 `problem_json=路径` 或 `description=.. starter_code=..`；需 `SKILLS_ENGINE_SECURITY_MODE=off` |
| `cta-generate-test-cases` | 同上换 skill 名；入参 `problem_json=路径`（或命令行直输）；需 `SKILLS_ENGINE_SECURITY_MODE=off`（或 `SKILLS_ENGINE_AUTO_APPROVE=all` 放行 exec 步），一步出 `test_cases.json`（自包含，无需 code_tutor_agent） |

## 附：测试用 problem.json 样例

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
