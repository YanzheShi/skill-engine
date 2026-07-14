---
name: code-analyzer
description: 分析 Python/JS/Java 代码的质量、复杂度、潜在 bug 和改进建议
groups:
- development
- code-review
- analysis
when_to_use: 用户需要分析代码质量、查找 bug、评估复杂度、获取重构建议时
arguments:
- file_path
- language
- focus_area
---

# code-analyzer

分析 Python/JS/Java 代码的质量、复杂度、潜在 bug 和改进建议

## 工作流程

按以下步骤顺序执行。每步的输出可被后续步骤引用。

## Steps

- name: read_code
  type: exec
  command: python scripts/read_file.py $FILE_PATH
  timeout: 10

- name: analyze_quality
  type: llm
  model: sensenova-deepseek
  template: '请分析以下代码，重点关注: $FOCUS_AREA
  
  
    语言: $LANGUAGE
  
    文件路径: $FILE_PATH
  
  
    代码内容:
  
    {read_code}
  
  
    请从以下维度分析:
  
    1. 代码质量和可读性
  
    2. 潜在 bug 和风险
  
    3. 复杂度评估
  
    4. 改进建议
  
    5. 安全漏洞检查'
  timeout: 30

- name: generate_report
  type: write
  template: '# 代码分析报告
  
  
    **文件**: $FILE_PATH
  
    **语言**: $LANGUAGE
  
    **分析重点**: $FOCUS_AREA
  
  
    ## 分析结果
  
  
    {analyze_quality}
  
  
    ## 总结
  
  
    - 整体质量评分: (根据上述分析给出)
  
    - 关键改进建议: (列出最重要的 3 条)
  
    - 风险评估: (高/中/低)'
  output_file: analysis_output/analysis.md
  timeout: 30

## 参数

- `file_path`: 用户提供的参数
- `language`: 用户提供的参数
- `focus_area`: 用户提供的参数

## 注意事项

- 按列表顺序确定性地执行步骤
- 使用 `{step_name}` 引用上一步的输出
- 使用 `$param_name` 引用命名参数
- 创建目录前先确保父目录存在