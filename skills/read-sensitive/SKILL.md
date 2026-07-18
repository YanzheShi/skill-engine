---
name: read-sensitive
description: 读取系统配置文件（仅演示，不会实际执行）
when_to_use: 用户说检查配置、查看系统信息
groups: [utility]
---

## User Request

$ARGUMENTS

## Steps

- name: read_config
  type: exec
  command: cat /etc/hosts
  timeout: 10

- name: display
  type: llm
  template: "系统配置如下：\n{read_config}"