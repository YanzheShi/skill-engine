# cta-generate-solution 使用文档

题解 skill：输入一道「题目描述 + 模板代码」，输出「文字讲解 + 最优解代码 + 暴力解代码」
形式的详细题解。本 skill 通过 **Steps DSL 一次性跑完**（prepare → solve），无需手动分步。

## 输入方式（任选其一）

| 参数 | 必填 | 说明 |
|---|---|---|
| `description` | 二选一 | 题目描述（含背景 / 输入 / 输出 / 示例 / 约束） |
| `starter_code` | 二选一 | 模板代码（如 `class Solution: ...`） |
| `problem_json` | 二选一 | 指向题目 JSON 文件的路径，至少含 `description` 与 `starter_code`（可选 `title` / `topic` / `difficulty`） |

- 命令行直输：`description` + `starter_code` 同时给出。
- JSON 文件：`problem_json` 指向含上述字段的 JSON 文件。
- 也可把整段题目 JSON 作为请求正文传入（`$ARGUMENTS`），prepare 步骤会自动解析。

> `prepare` / `solve` 步骤里 `description` 等为空时，脚本会把 Steps DSL 未解析的残留占位符（如 `$description`）当作空值处理，不会误当成真实内容。

## 运行方式

```bash
# 方式一：命令行直输 description + starter_code
skill-engine run cta-generate-solution --llm \
  --args "description=给定一个整数数组nums和目标值target，找出和为target的两个整数并返回下标 \
starter_code=class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        pass"

# 方式二：从 JSON 文件解析
skill-engine run cta-generate-solution --llm --args "problem_json=/abs/path/to/problem.json"
```

> 长文本 / 多行内容（题目描述、代码）建议走 **JSON 文件**方式，避免命令行 argv 编码与空格转义问题。

## 安全模式要求

`prepare` 步骤是 `python` exec，且 `python` 在 skill-engine 风险二进制名单中，运行前需放行：

```bash
# 方式 A：关闭安全审批（调试用）
export SKILLS_ENGINE_SECURITY_MODE=off
# 方式 B：自动批准所有步骤
export SKILLS_ENGINE_AUTO_APPROVE=all
```

否则 `prepare` 步骤会被交互式审批拦截（非交互环境下输出为空）。

## 输出

一份 Markdown 题解，固定三个 `## ` 分节（code-tutor-agent 直接展示给学生）：

```
## 思路讲解
## OptimalSolution
## BruteSolution
```

- `思路讲解`：核心思想 / 算法流程 / 复杂度推导 / 易错点，面向学生、通俗易懂。
- `OptimalSolution` / `BruteSolution`：均可被 Python `compile` 的合法代码（含 `class Solution` 与方法），
  `BruteSolution` 常用作后续测试用例生成的 oracle。

## 注意事项

- `solve` 步骤走 skill-engine 自身的 `get_llm()`（默认 `sensenova-deepseek`）。在 code-tutor-agent 生产主通道中，
  该题解由 `engine_adapter` 用本系统 LLM 别名（agnes）调用，不依赖本 skill 的 Steps `llm` 步骤。
- 不要在本 skill 输出里生成 `test_cases` / `adversarial_spec`——测试用例由 `cta-generate-test-cases` 另行生成。
