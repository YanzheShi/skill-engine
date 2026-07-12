---
name: deploy
description: 部署应用到生产环境，处理构建、测试和发布流程
when_to_use: 当用户提到部署、发布、上线、build 等关键词时使用
argument_hint: 部署目标环境（staging/prod）
arguments:
  - environment
  - version
user_invocable: true
allowed-tools:
  - Bash(git *)
  - Bash(python scripts/build.py)
  - Bash(curl *)
disallowed-tools:
  - Bash(rm *)
---

# Deploy Skill

部署应用到指定环境。

## 工作流程

1. 检查代码变更
!`git diff HEAD --stat`

2. 构建应用
!`python scripts/build.py`

3. 部署到目标环境

## 参数

- `environment`: 部署目标（staging/prod）
- `version`: 版本号
