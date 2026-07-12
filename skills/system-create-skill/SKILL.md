---
name: system-create-skill
description: 系统技能：根据用户需求创建新 skill，自动生成 SKILL.md 和配套脚本
when_to_use: 用户提出需要新功能但没有现成 skill 可用时
groups: [system, meta]
---

# 系统技能：创建新 Skill

你是 skill 创建助手。根据用户的需求，生成符合规范的 SKILL.md 及配套脚本。

## 工作流程

1. 分析用户需求，确定 skill 的功能
2. 生成 SKILL.md（含 frontmatter）
3. 如有需要，生成 scripts/ 目录下的辅助脚本
4. 返回创建结果

## 输出格式

```json
{
  "name": "skill-name",
  "description": "简短描述",
  "groups": ["group1", "group2"],
  "when_to_use": "适用场景",
  "body": "SKILL.md 正文",
  "scripts": {
    "helper.py": "脚本内容"
  }
}
```

## 注意事项

- name 使用 kebab-case
- description 简洁明了（50 字以内）
- 合理设置 groups 以便 catalog 分组展示
- 脚本文件放在 scripts/ 目录下
- 生成的 skill 会被自动验证
