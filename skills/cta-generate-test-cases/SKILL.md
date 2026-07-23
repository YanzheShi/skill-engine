---
name: cta-generate-test-cases
description: >-
  为算法题生成测试用例（含 expected_output）。给定题目描述、函数签名与参考解，
  自动生成随机用例、LLM 边界用例与复杂结构（树/图）用例，并用参考解实跑回填
  expected_output，产出 test_cases.json。当用户需要「生成测试用例 / 出题测试 /
  造数据 / 给这道题配测试 / 生成测试集」时使用。
arguments:
- problem_json
- count
- boundary_json
- out_json
---

# 生成测试用例（cta-generate-test-cases）

## 目标

依据题目自带参考解（oracle），产出一批高质量测试用例：每条用例含
`input_args`（每参一个 JSON 字符串）、`expected_output`（参考解实跑结果）、
`is_hidden`（可见性）、`explanation`。产物 `test_cases.json` 可直接被
code-tutor-agent 现有判题器消费。

## 输入

- **problem_json**：题目 JSON 字符串，或指向该 JSON 的文件路径。需包含：
  - `function_signature`：如 `nums: List[int], target: int -> List[int]`
  - `optimal_solution` / `brute_solution`：参考解源码（作为 oracle）
  - `description` / `constraints` / 可选 `examples`：用于语义修正与可见示例
- **count**：随机/结构用例数量（默认 12；可见示例不计入）
- **boundary_json**：边界用例落盘路径（由 `gen_boundary` 写入、`generate` 读取），
  必须传**绝对路径**。
- **out_json**：最终 `test_cases.json` 产物路径（由 `generate` 写入、`read_result` 读取），
  必须传**绝对路径**。

## 实现说明

`scripts/gen_test_cases.py` **完全自包含**，不依赖 code_tutor_agent，运行时仅需
Python 标准库。四类候选输入统一交给 `oracle_runner` 用参考解实跑回填：

1. 题目自带可见示例（input 复用，expected 由 oracle 重算，权威）
2. 随机输入（`random_gen` 按签名/约束生成，含「有序数组」语义修正）
3. 边界用例：由 skill 原生 `type: llm` step（走引擎已配置的 LLM，如 AGNES）
   基于题目上下文生成，落盘到 `output/boundary.json` 后由本脚本读取；LLM 不可用
   /解析失败时自动跳过，不影响主流程。
4. 复杂结构（树/图）：纯随机结构生成（`random_gen` 已支持合法图边表/树层序）。

oracle runner 的序列化契约**逐字节对齐** code-tutor-agent 判题器
（`sandbox.runner._build_harness`），因此正确提交解在判题器上必判 Passed。
参考解崩溃/超时/无输出的用例会被自动丢弃。

## Steps

```steps
- name: load_problem
  type: read
  input_ref: $problem_json

- name: gen_boundary
  type: llm
  template: |
    你是算法测试设计专家。题目信息（JSON）：
    {load_problem}
    请设计 {count} 个边界/极端/易错测试用例种子（覆盖空输入、单元素、极值、重复、负数、
    已排序/逆序、图/树退化形态等）。仅输出一个 JSON 数组，每元素为
    {"input_args": ["每参一个 JSON 字符串"], "explanation": "难点说明"}。
    不要任何解释文字，不要代码围栏，直接输出数组。

- name: write_boundary
  type: write
  template: "{gen_boundary}"
  output_file: $boundary_json

- name: generate
  type: exec
  command: python scripts/gen_test_cases.py --problem $problem_json --out $out_json --count $count --boundary-json $boundary_json
  timeout: 300

- name: read_result
  type: read
  input_ref: $out_json
```

## 使用注意

- **边界用例由引擎 LLM 生成**：`gen_boundary` step 使用 skill-engine 原生的
  `type: llm` step（走引擎已配置的 LLM，如 AGNES），无需任何外部 key 或独立
  HTTP 客户端。若 LLM 不可用（无 key / 调用失败），`boundary.json` 内容非法，
  脚本解析失败后自动跳过边界用例，仅用 random+structure 产出，主流程不受影响。
- **exec 步放行**：脚本以 `python` 执行，skill-engine 安全扫描会提示批准。
  交互运行时按提示选 `A` 批准；非交互/程序化运行可设
  `SKILLS_ENGINE_AUTO_APPROVE=all` 放行。
- **调用约定（方案 B：路径参数化，引擎零改动）**：`problem_json`、`boundary_json`、
  `out_json` 三个路径**必须传绝对路径**。原因——原生 `read`/`write` 步按进程 CWD
  解析（从 skill-engine 项目根调用时 CWD=项目根），而 `exec` 步的
  `python scripts/gen_test_cases.py` 在 `skill.directory`（技能目录）下执行，其
  `--boundary-json` / `--out` 同样需绝对路径才能保证写入与读取落点一致。从项目根调用示例：

  ```powershell
  cd D:\Code\PycharmProjects\skill-engine
  $env:SKILLS_ENGINE_AUTO_APPROVE = "all"
  uv run skill-engine run cta-generate-test-cases --steps -a `
    "problem_json=D:/Code/PycharmProjects/skill-engine/output/problem.json" `
    "count=12" `
    "boundary_json=D:/Code/PycharmProjects/skill-engine/skills/cta-generate-test-cases/output/boundary.json" `
    "out_json=D:/Code/PycharmProjects/skill-engine/skills/cta-generate-test-cases/output/test_cases.json"
  ```

  有 LLM key 时产出 36 条（24 基础+12 LLM 边界），无 key 时 LLM 失败降级为 24 条，
  主流程均不受影响。
- 产物写入 `out_json` 指定路径，建议将技能目录下的 `output/` 加入 `.gitignore`
  避免污染仓库。
