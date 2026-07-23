---
name: cta-generate-solution
description: 输入一道「题目描述 + 模板代码」，输出「文字讲解 + 最优解代码」形式的详细题解。支持两种输入：① 命令行直接传入题目描述(description)与模板代码(starter_code)；② 传入含这两个字段的 JSON 文件(problem_json)。当 code-tutor-agent 需要为某道题生成可展示给学生的详细题解时使用。
when_to_use: 当 code-tutor-agent 需要为已出好的题（给定题目描述与模板代码）生成详细题解时使用；输入为题目描述+模板代码（命令行直输），或指向含 description/starter_code 的 JSON 文件。
arguments:
- description
- starter_code
- problem_json
---

# cta-generate-solution

本 skill 通过多步骤自动完成：先归一化输入（命令行直输 或 JSON 文件），再调用 LLM 生成题解。无需手动分步。

## 输入方式（任选其一）

- **命令行直输**：`description` = 题目描述（含背景/输入/输出/示例/约束），`starter_code` = 模板代码。
- **JSON 文件**：`problem_json` = 一个 JSON 文件路径，至少含 `description` 与 `starter_code`（可选 `title`/`topic`/`difficulty`）。
- 也支持把整段 JSON 作为请求正文传入（`$ARGUMENTS`），prepare 步骤会自动解析。

> 注：`python` 在 skill-engine 风险二进制名单中，prepare 执行步骤需要 `SKILLS_ENGINE_SECURITY_MODE=off` 或自动批准（`A` / `SKILLS_ENGINE_AUTO_APPROVE=all`）。

## Steps

- name: prepare
  type: exec
  command: python scripts/prepare_solution_input.py --problem-json "$problem_json" --description "$description" --starter-code "$starter_code" --arguments "$ARGUMENTS"
  timeout: 30

- name: solve
  type: llm
  timeout: 180
  template: |
    你是一个资深算法讲师。针对下面给出的题目，输出一份**「文字讲解 + 代码最优解」**的详细题解。

    题目（已归一化为 JSON）：
    {prepare}

    ## 输出要求

    严格按以下 Markdown 格式输出，**不要**输出额外的寒暄、前言、总结，也**不要**输出你的思考过程（chain-of-thought）。每个 `## ` 二级标题都必须出现，代码块放在 ```python 围栏内。

    > 为什么固定分节：code-tutor-agent 会把本 skill 的整段 markdown 当作「详细题解」直接展示给学生，并要求题解同时包含**可理解的文字思路**与**可直接抄去跑的最优代码**。

    ## 思路讲解
    <用自然语言讲清：核心思想（用到了什么算法/数据结构）、算法流程（分步说明怎么算）、时间复杂度与空间复杂度（给出推导）、易错点（边界/常见坑）。要面向学生、易懂，不要堆术语。>

    ## OptimalSolution
    ```python
    class Solution:
        def methodName(self, ...):
            # 最优解：时间/空间最优，可直接运行并 AC
            ...
    ```

    ## BruteSolution
    ```python
    class Solution:
        def methodName(self, ...):
            # 朴素暴力解：正确但低效，作为对照与测试用例 oracle
            ...
    ```

    ## 代码要求

    - `OptimalSolution` 与 `BruteSolution` 都必须是可被 Python `compile` 的合法代码，包含 `class Solution` 与一个方法，方法名/参数签名一致。
    - `OptimalSolution` 必须正确、可直接 AC，并选用最优算法（哈希表、双指针、动态规划等），体现面试考点。
    - `BruteSolution` 朴素但正确，常用于生成测试用例时的参考解（oracle）。
    - **不要**写 `from typing import ...` 等导入到解法里（运行环境已预置 `List` 等类型）。
    - **不要**输出思考过程、候选解法列举或自我讨论。

    ## 输出限制
    - 只输出上述三个分节，不要生成 `test_cases`、`adversarial_spec` 等字段——测试用例由系统后续自动生成。
