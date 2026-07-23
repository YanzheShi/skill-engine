---
name: cta-generate-problem
description: 根据指定的知识点(topic)和难度(difficulty)生成一道 LeetCode 风格的算法题，输出严格对齐 code-tutor-agent 的 Problem 模型，包含标题、知识点、难度、描述、示例、约束、起始代码、函数签名、暴力解与最优解。支持以 JSON 格式输出（output_format=json）以便程序化消费，也可重定向保存为 .json 文件。当 code-tutor-agent 需要程序化生成一道新的编程练习题、或用 skill 通道补充题库时使用；输入为知识点(topic)与难度(difficulty)两个参数。如果用户提到"出一道题""生成练习题""按某知识点出题"，优先使用本 skill。
when_to_use: 当 code-tutor-agent 需要程序化生成一道新的编程练习题时使用；输入为知识点(topic)与难度(difficulty)两个参数。可通过 output_format=json 输出结构化 JSON（便于落盘为 .json 文件）。
arguments:
- topic
- difficulty
- output_format
---

# cta-generate-problem

你是一个资深的算法题出题专家。请根据以下参数生成一道 LeetCode 风格的算法题：

- 知识点(topic)：{topic}
- 难度(difficulty)：{difficulty}（取值 easy / medium / hard，默认 medium）
- 输出格式(output_format)：{output_format}（空 / `markdown` 按下方 Markdown 输出；`json` 按下方 JSON 块输出）

## 输出格式分支（重要）

- 如果 `output_format` 为 **`json`**（即参数里传了 `output_format=json`）：**不要**按下面的 Markdown 分节输出，而是输出**一个且只有一个** ```json 代码块，字段与下方 Markdown 各节一一对应：
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
  不要输出任何额外解释文字，只输出该 JSON 代码块。调用方通常会把标准输出重定向保存为 `.json` 文件（例如 `skill-engine run cta-generate-problem --llm "topic=DP difficulty=medium" -a "output_format=json" > problem.json`）。
- 否则（output_format 为空或 `markdown`）：严格按下面的 Markdown 格式输出。

## 输出要求

你必须**严格按以下 Markdown 格式**输出（仅当 output_format 非 `json` 时），**不要**输出任何额外的解释、前言或总结，也**不要**输出你的思考过程（chain-of-thought）。每一个二级标题（`## `）都必须出现，且顺序保持一致。代码块必须放在 ```python 围栏内。

> 为什么这么严格：code-tutor-agent 的后端会用正则从输出里按 `## 标题` 抠出每个字段回填到 `Problem` 模型。只要缺一个节或顺序乱掉，解析就会拿不到对应字段，导致题目残缺、测试用例生成失败。所以务必逐节输出。

## Title
<题目标题，简洁的英文标题，例如 "Two Sum">

## Topic
<对应的知识点，例如 动态规划 / 二分查找 / 图遍历>

## Difficulty
<只能是 easy、medium、hard 三者之一，根据 {difficulty} 输出>

## Description
<完整的中文题目描述：包含背景说明、输入定义、输出定义、以及明确的函数行为要求。描述要自洽、可独立理解。>

## Examples
### Example 1
<输入：...>
<输出：...>
<解释：...>
---
### Example 2
<输入：...>
<输出：...>
<解释：...>

## Constraints
- <约束条件 1，例如 1 <= nums.length <= 10^4>
- <约束条件 2>

## StarterCode
```python
class Solution:
    def methodName(self, ...):
        ...
```

## FunctionSignature
<参数类型签名，例如 nums: List[int], target: int -> List[int]>

## BruteSolution
```python
class Solution:
    def methodName(self, ...):
        # 暴力解，朴素但正确，可用于生成测试用例的 oracle
        ...
```

## OptimalSolution
```python
class Solution:
    def methodName(self, ...):
        # 最优解，时间/空间最优，可直接运行并通过测试用例
        ...
```

## 代码要求

- `StarterCode`、`BruteSolution`、`OptimalSolution` 中的代码必须是可以被 Python `compile` 的合法代码，且都包含 `class Solution` 与一个方法。
- `OptimalSolution` 必须正确解决问题，可直接 AC（Accept），并尽量选用最优算法（如哈希表、双指针、动态规划），体现面试考点。
- 三个代码块的方法名、类名、参数签名应保持一致。
- **`StarterCode` 不要写 `from typing import ...`** 等导入语句，只保留 `class Solution` 与方法签名（方法体仅 `pass` 或 `...`）。
- **数据结构定义规则**（很重要，避免后续判题框架还原参数失败）：
  - 树 / 图 / 链表题：`StarterCode` 开头必须包含对应的结构定义（`class TreeNode` / `class ListNode` / `class GraphNode`），放在 `class Solution` 之前。
  - 非树/图/链表题（数组、字符串、数学、哈希、动态规划、贪心、双指针、模拟等）：`StarterCode` **只保留** `class Solution` 与方法签名，**禁止**写 `class TreeNode` / `class ListNode` / `class GraphNode` 或 `# Definition for a binary tree node.` 这类注释。
- **`FunctionSignature` 格式**：`name: type, ... -> rettype`，类型只能是以下之一：`int` / `float` / `str` / `bool` / `List[int]` / `List[str]` / `List[List[int]]`。例如 `nums: List[int], target: int -> List[int]`。这个签名会被测试用例生成器用来还原参数，务必准确。

## 输出限制
- 不要生成 `test_cases`、`adversarial_spec` 等字段——测试用例和校验会由系统后续自动生成。
- **不要输出思考过程、候选题目列举、或自我讨论**。
