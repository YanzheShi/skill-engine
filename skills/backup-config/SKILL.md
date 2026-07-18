---
name: backup-config
description: 备份配置文件，复制到安全位置
when_to_use: 用户说备份、复制文件、存档
groups: [utility]
---

## User Request

$ARGUMENTS

## Steps

- name: backup
  type: exec
  command: cp config.yaml output/config.yaml.bak
  timeout: 10

- name: confirm
  type: llm
  template: "配置文件已备份到 output/config.yaml.bak"