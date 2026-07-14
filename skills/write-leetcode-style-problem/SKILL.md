---
name: write-leetcode-style-problem
description: 根据用户指定的主题和难度，生成一道 LeetCode 风格的算法题目，包括标题、标签、描述、示例和约束条件，并保存到文件
groups:
- code
- education
- leetcode
when_to_use: 当用户需要快速生成一道 LeetCode 风格的算法题用于练习、教学或面试准备时；或者希望根据特定主题（如动态规划、二叉树）和难度（Easy/Medium/Hard）获得一道完整的题目描述
arguments:
- topic
- difficulty
---

# write-leetcode-style-problem

根据用户指定的主题和难度，生成一道 LeetCode 风格的算法题目，包括标题、标签、描述、示例和约束条件，并保存到文件

## 工作流程

按以下步骤顺序执行。每步的输出可被后续步骤引用。

## Steps

- name: generate_problem
  type: llm
  template: '你是一个 LeetCode 题目生成专家。请根据以下要求生成一道 LeetCode 风格的算法题目，使用 Markdown 格式输出。
  
  
    主题：{topic}
  
    难度：{difficulty}（默认为 Medium）
  
  
    请输出以下部分（按顺序）：
  
    1. **标题**：一个简洁的英文标题，如 "Two Sum"
  
    2. **难度**：{difficulty}
  
    3. **标签**：相关的算法标签，如 "Array, Hash Table"
  
    4. **题目描述**：清晰的问题描述，说明输入输出和具体要求
  
    5. **示例 1**：包含输入、输出和解释（如果有）
  
    6. **示例 2**：包含输入、输出和解释（如果有）
  
    7. **约束条件**：用列表形式列出，如输入范围、时间/空间复杂度要求等
  
  
    确保题目风格、格式与 LeetCode 官方题目保持一致。'
  timeout: 60

- name: save_problem
  type: write
  template: '{generate_problem}'
  output_file: output/problem.md

## 参数

- `topic`: 用户提供的参数
- `difficulty`: 用户提供的参数

## 注意事项

- 按列表顺序确定性地执行步骤
- 使用 `{step_name}` 引用上一步的输出
- 使用 `$param_name` 引用命名参数
- 创建目录前先确保父目录存在