---
name: write-output
description: 生成报告并写入 skills/output/ 目录
when_to_use: 用户说保存结果、导出报告、生成文件
groups: [utility]
---

## User Request

$ARGUMENTS

## Steps

- name: generate_report
  type: exec
  command: python -c "print('report content')" > output/report.md
  timeout: 10

- name: confirm
  type: llm
  template: "报告已生成至 output/report.md"