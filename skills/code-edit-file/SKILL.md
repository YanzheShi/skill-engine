---
name: code-edit-file
description: 根据用户需求编辑已有代码文件，支持定向替换和修改后验证
groups:
- development
- coding
human_in_loop: true
turn_policy:
  max_turns: 20
  stop_when: "已完成|已修改|已应用|任务圆满完成|再见"
when_to_use: 用户需要修改代码文件、添加功能、修复 bug、重构代码时
arguments:
- file_path
---

# code-edit-file

根据用户需求编辑已有代码文件。

## 工作流程

用户的需求在 $ARGUMENTS 中。

你是一个编码助手，有五个工具可用：

1. **read_file** — 读文件（支持 `offset`/`limit` 分页，默认返回全文+行号）
2. **write_file** — 写新文件或全量覆盖
3. **edit_file** — 定向修改已有文件（推荐，比 write_file 更精准）
4. **search_files** — 在项目中搜索文本（支持正则和 file_glob 过滤）
5. **bash** — 执行 shell 命令（用于验证）

工作流程：
1. **读取目标文件** — 用 `read_file` 读取用户指定的文件，理解上下文
2. **规划改动** — 决定用什么工具、改什么内容
3. **执行改动** — 用 `edit_file` 做精准修改（`oldText` 必须全文唯一）
4. **验证** — 用 `bash` 运行 `python` 验证改动

## 约束

- 改完后必须验证（python 运行脚本检查）
- 如果 `edit_file` 返回 `oldText` 不唯一，用 `read_file` 重新读取文件确认具体内容
- 新文件用 `write_file`，不要用 `edit_file` 创建文件
- `edit_file` 的 `edits` 参数格式：`[{"oldText": "...", "newText": "..."}, ...]`
- 所有 edits 基于原文件按 offset 单遍应用，互不干扰

## 运行前提

验证需要 `bash` 工具，请在运行前设置：
```
SKILLS_ENGINE_SECURITY_MODE=permissive
SKILLS_ENGINE_ALLOWLIST=python,python3,git,pytest,pip
```