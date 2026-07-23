# cta-generate-problem 使用文档

出题 skill：根据知识点（topic）与难度（difficulty）生成一道 LeetCode 风格的算法题，
输出严格对齐 code-tutor-agent 的 `Problem` 模型（标题 / 知识点 / 难度 / 描述 / 示例 /
约束 / 起始代码 / 函数签名 / 暴力解 / 最优解）。

## 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `topic` | 是 | 知识点，如 `动态规划` / `二分查找` / `数组` |
| `difficulty` | 是 | 难度，取值 `easy` / `medium` / `hard`（默认 `medium`） |
| `output_format` | 否 | 输出格式：`markdown`（默认，按分节 Markdown 输出）或 `json`（输出单个 ```` ```json ```` 代码块） |

## 运行方式

通过 skill-engine CLI 运行（档位 A：单次 LLM 调用）：

```bash
# 默认 Markdown 输出
skill-engine run cta-generate-problem --llm "topic=动态规划 difficulty=medium"

# 输出为 JSON（便于程序化消费 / 落盘）
skill-engine run cta-generate-problem --llm "topic=动态规划 difficulty=medium" -a "output_format=json"
```

### 生成题目到 JSON 文件

`output_format=json` 时，skill 只输出一个 ```` ```json ```` 代码块。把标准输出重定向即可保存为 `.json` 文件：

```bash
skill-engine run cta-generate-problem --llm "topic=Two Pointers difficulty=easy" -a "output_format=json" > problem.json
```

> 重定向会包含 skill-engine 自身的前缀日志，调用方按「最后一个 `====` 之后的 ```` ```json ```` 块」解析即可（详见 code-tutor-agent `skills/parser.py`）。

## 输出

### Markdown 模式（默认）

严格按以下 `## ` 分节顺序输出（code-tutor-agent 后端用正则按节回填 `Problem` 模型，缺节会导致解析失败）：

```
## Title
## Topic
## Difficulty
## Description
## Examples
## Constraints
## StarterCode
## FunctionSignature
## BruteSolution
## OptimalSolution
```

### JSON 模式（output_format=json）

输出单个 JSON 代码块，字段与上方各节一一对应：

```json
{
  "title": "英文题目标题",
  "topic": "知识点",
  "difficulty": "easy | medium | hard",
  "description": "完整中文题目描述",
  "examples": [{"input": "...", "output": "...", "explanation": "..."}],
  "constraints": ["约束1", "约束2"],
  "starter_code": "class Solution:\n    def methodName(self, ...):\n        ...",
  "function_signature": "name: type, ... -> rettype",
  "brute_solution": "class Solution:\n    ...",
  "optimal_solution": "class Solution:\n    ..."
}
```

## 注意事项

- 该 skill 走档位 A（单次 LLM），**不经过 `python` 执行步骤**，因此无需关闭安全审批，
  在 `strict` 安全模式下也能直接运行。
- `difficulty` 决定难度档位；`topic` 建议用简洁关键词（中文或英文均可，但英文 topic 在 argv 编码上更稳）。
- 题目 JSON 落盘后，可直接喂给 `cta-generate-test-cases`（作为 `problem_json`）一次性生成测试用例。
