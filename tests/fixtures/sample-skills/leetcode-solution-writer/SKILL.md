---
name: leetcode-solution-writer
description: LeetCode 题解生成助手，用于创建题解文档。配合斜杠命令调用，在刷完题并通过 OJ 后使用
when_to_use: 刷题完成后说"生成题解"或"写题解"
argument_hint: LeetCode 题号
arguments:
  - problem_id
user_invocable: true
---

# LeetCode 题解生成助手

用于在 `~/.leetcode/docs/` 目录下创建题解文档。

## 核心工作流程

### 第一步：识别题目

从以下来源确定题号和题目标题：
1. 当前打开的题目文件
2. 从文件头部注释解析题号：`@lc app=leetcode.cn id=49`
3. 从文件名提取题号：正则匹配 `(\d+)\.`

### 第二步：获取题目信息

使用 `scripts/fetch_problem.py` 获取题目数据：

```bash
python scripts/fetch_problem.py 49
```

### 第三步：创建题解

在 `~/.leetcode/docs/` 下创建目录结构：
```
{题号}. {题目标题}/
└── 题解.md
```

### 第四步：基于模板填充

使用 `assets/solution-template.md` 作为模板。
