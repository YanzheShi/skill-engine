#!/usr/bin/env python3
"""LeetCode 题目信息获取脚本（纯 Python 实现）"""

import sys
import os
import re
import json
import argparse
from typing import Dict, Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def parse_problem_id_from_file(filepath: str) -> Optional[str]:
    """从 LeetCode 题目文件中解析题号"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r'@lc\s+app=leetcode\.cn\s+id=(\d+)', content)
        if match:
            return match.group(1)
        filename = os.path.basename(filepath)
        match = re.match(r'(\d+)\.', filename)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        print(f"[WARNING] 无法解析文件: {e}", file=sys.stderr)
        return None


def fetch_problem_detail(title_slug: str) -> Optional[Dict[str, Any]]:
    """从 LeetCode 中文站获取题目详情"""
    query = {
        "operationName": "questionData",
        "query": """
        query questionData($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            questionFrontendId
            title
            titleSlug
            content
            translatedContent
            difficulty
            topicTags { name slug }
            codeSnippets { lang langSlug code }
          }
        }
        """,
        "variables": {"titleSlug": title_slug}
    }

    req = Request(
        "https://leetcode.cn/graphql/",
        data=json.dumps(query).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://leetcode.cn/problemset/"
        },
        method="POST"
    )

    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get('data', {}).get('question', {})
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[WARNING] 获取题目详情失败: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="获取 LeetCode 题目信息")
    parser.add_argument("problem_id", nargs="?", help="LeetCode 题号")
    parser.add_argument("--slug", help="题目的 title slug")
    parser.add_argument("--file", help="从题目文件解析题号")
    args = parser.parse_args()

    problem_id = args.problem_id
    if args.file:
        problem_id = parse_problem_id_from_file(args.file)
        if not problem_id:
            print("[ERROR] 无法从文件中解析题号", file=sys.stderr)
            sys.exit(1)

    if not problem_id:
        parser.print_help()
        sys.exit(1)

    print(json.dumps({"id": problem_id, "title": f"Problem {problem_id}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
