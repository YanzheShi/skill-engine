---
name: cta-generate-problem
description: 根据指定的知识点(topic)和难度(difficulty)生成一道 LeetCode 风格的算法题，输出严格对齐 code-tutor-agent 的 Problem 模型，包含标题、知识点、难度、描述、示例、约束、起始代码、函数签名、暴力解与最优解。
when_to_use: 当 code-tutor-agent 需要程序化生成一道新的编程练习题时使用；输入为知识点(topic)与难度(difficulty)两个参数。
arguments:
- topic
- difficulty
---

# cta-generate-problem

你是一个资深的算法题出题专家。请根据以下参数生成一道 LeetCode 风格的算法题：

- 知识点(topic)：{topic}
- 难度(difficulty)：{difficulty}（取值 easy / medium / hard，默认 medium）

## 输出要求

你必须**严格按以下 Markdown 格式**输出，**不要**输出任何额外的解释、前言或总结，也**不要**输出你的思考过程（chain-of-thought）。每一个二级标题（`## `）都必须出现，且顺序保持一致。代码块必须放在 ```python 围栏内。

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
- `OptimalSolution` 必须正确解决问题，可直接 AC（Accept）。
- 三个代码块的方法名、类名、参数签名应保持一致。
