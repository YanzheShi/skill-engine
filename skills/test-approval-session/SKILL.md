---
name: test-approval-session
description: 测试审批会话机制 — 多步文件操作，验证 y/Y/N/r 四种模式
when_to_use: 测试安全审批的会话缓存，需要多步文件操作触发多次弹窗
groups: [test, security]
---

## User Request

$ARGUMENTS

## Steps

- name: prepare_dir
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test').mkdir(parents=True, exist_ok=True)"
  timeout: 10

- name: write_a_first
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/a.txt').write_text('版本 1')"
  timeout: 10

- name: write_a_first
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/a.txt').write_text('版本 1')"
  timeout: 10

- name: write_b
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/b.txt').write_text('版本 1')"
  timeout: 10

- name: write_b
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/b.txt').write_text('版本 1')"
  timeout: 10

- name: write_a_second
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/a.txt').write_text('版本 2')"
  timeout: 10

- name: write_a_second
  type: exec
  command: python -c "import pathlib; pathlib.Path('temp_test/a.txt').write_text('版本 2')"
  timeout: 10

- name: read_results
  type: llm
  template: "以下是操作结果：\n\n创建目录: {prepare_dir}\n写入 a.txt 第一次: {write_a_first}\n写入 b.txt: {write_b}\n写入 a.txt 第二次: {write_a_second}\n\n请整理成清晰报告"