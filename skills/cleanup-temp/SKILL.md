---
name: cleanup-temp
description: 清理临时文件，删除 skills/temp/ 下的缓存
when_to_use: 用户说清理缓存、删除临时文件、磁盘清理
groups: [utility]
---

## User Request

$ARGUMENTS

## Steps

- name: clean_te
  type: exec
  command: python -c "import shutil,pathlib,os;shutil.rmtree(pathlib.Path('temp/cache'),ignore_errors=True);(pathlib.Path('temp/cache').parent).mkdir(parents=True,exist_ok=True)"
  timeout: 10

- name: report
  type: llm
  template: "临时文件已清理完毕，结果如下：\n{clean_temp}"