#!/usr/bin/env python3
"""cta-generate-solution 伴随脚本：把题解输入归一化为统一 JSON。

输入来源（按优先级，第一个有效的胜出）：
1. --problem-json <path>  含 description / starter_code 的 JSON 文件
2. --arguments <json>     $ARGUMENTS 整体是一段 JSON 字符串
3. --description + --starter-code  命令行直输的题目描述与模板代码

输出：归一化 JSON（含 title/topic/difficulty/description/starter_code）到 stdout。
纯标准库，不依赖 code_tutor_agent。
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def _clean(value: str) -> str:
    """把 Steps DSL 未解析的残留占位符（如 $description）当作空值处理。

    resolve_template 仅替换 arguments 中存在的键；未提供的命名参数会原样保留
    '$name'，若直接传给脚本会被误当成真实内容。这里把形如 '$xxx' 的残留清掉。
    """
    if value is None:
        return ""
    value = value.strip()
    if re.fullmatch(r"\$[A-Za-z_]\w*", value):
        return ""
    return value


def _load_json_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _try_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--problem-json", default="")
    p.add_argument("--description", default="")
    p.add_argument("--starter-code", default="")
    p.add_argument("--arguments", default="")
    args = p.parse_args(argv)

    problem_json = _clean(args.problem_json)
    description = _clean(args.description)
    starter_code = _clean(args.starter_code)
    arguments = _clean(args.arguments)

    data: dict | None = None
    if problem_json and (data := _load_json_file(problem_json)):
        pass
    elif (data := _try_json(arguments)):
        pass
    elif description or starter_code:
        data = {"description": description, "starter_code": starter_code}

    if not data or not (data.get("description") or "").strip():
        sys.stderr.write(
            "[prepare] 未找到有效输入：请提供 --problem-json 指向的 JSON 文件，"
            "或传入含 description/starter_code 的 JSON（--arguments），"
            "或同时给定 --description 与 --starter-code。\n"
        )
        return 1

    out = {
        "title": (data.get("title") or "").strip(),
        "topic": (data.get("topic") or "").strip(),
        "difficulty": (data.get("difficulty") or "").strip(),
        "description": data.get("description", "").strip(),
        "starter_code": data.get("starter_code", "").strip(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
