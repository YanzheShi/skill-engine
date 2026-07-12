#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LeetCode 题目信息获取脚本（纯 Python 实现，无外部依赖）

用法:
    python fetch_problem.py <problem_id>
    python fetch_problem.py --slug <title_slug>
    python fetch_problem.py --file <path_to_cpp_file>

示例:
    python fetch_problem.py 49
    python fetch_problem.py --file ~/.leetcode/49.字母异位词分组.cpp
"""

import sys
import os
import re
import json
import argparse
from typing import Dict, Any, Optional

# 使用标准库 urllib
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def parse_problem_id_from_file(filepath: str) -> Optional[str]:
    """
    从 LeetCode 题目文件中解析题号

    Args:
        filepath: .cpp/.py/.java 文件路径

    Returns:
        题号字符串，如果解析失败返回 None
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        # 匹配 @lc app=leetcode.cn id=49 或类似格式
        match = re.search(r'@lc\s+app=leetcode\.cn\s+id=(\d+)', content)
        if match:
            return match.group(1)

        # 从文件名提取题号 (如 "49.字母异位词分组.cpp")
        filename = os.path.basename(filepath)
        match = re.match(r'(\d+)\.', filename)
        if match:
            return match.group(1)

        return None

    except Exception as e:
        print(f"[WARNING] 无法解析文件: {e}", file=sys.stderr)
        return None


def fetch_problem_list(skip: int, limit: int = 100) -> list:
    """
    从 LeetCode 中文站获取题目列表

    Args:
        skip: 跳过数量
        limit: 返回数量

    Returns:
        题目列表
    """
    query = {
        "operationName": "problemsetQuestionList",
        "query": """
        query problemsetQuestionList($categorySlug: String, $skip: Int, $limit: Int) {
          problemsetQuestionList(
            categorySlug: $categorySlug
            skip: $skip
            limit: $limit
          ) {
            questions {
              frontendQuestionId
              title
              titleSlug
              difficulty
              topicTags {
                name
                slug
              }
            }
          }
        }
        """,
        "variables": {
            "categorySlug": "",
            "skip": skip,
            "limit": limit
        }
    }

    req = Request(
        "https://leetcode.cn/graphql/",
        data=json.dumps(query).encode('utf-8'),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://leetcode.cn/problemset/"
        },
        method="POST"
    )

    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data.get('data', {}).get('problemsetQuestionList', {}).get('questions', [])
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[WARNING] 获取题目列表失败: {e}", file=sys.stderr)
        return []


def fetch_problem_detail(title_slug: str) -> Optional[Dict[str, Any]]:
    """
    从 LeetCode 中文站获取题目详情

    Args:
        title_slug: 题目的 title slug

    Returns:
        题目信息字典，失败返回 None
    """
    query = {
        "operationName": "questionData",
        "query": """
        query questionData($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            questionId
            questionFrontendId
            title
            translatedTitle
            titleSlug
            content
            translatedContent
            difficulty
            topicTags {
              name
              slug
              translatedName
            }
            codeSnippets {
              lang
              langSlug
              code
            }
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://leetcode.cn/problems/{title_slug}/"
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


def find_problem_by_id(problem_id: str) -> Optional[Dict[str, Any]]:
    """
    通过题号查找题目

    Args:
        problem_id: 题号

    Returns:
        题目基本信息，失败返回 None
    """
    try:
        pid = int(problem_id)
    except ValueError:
        return None

    # 估算 skip 位置（题号通常接近 skip 值）
    # LeetCode 题号从 1 开始，skip 从 0 开始
    estimated_skip = max(0, pid - 10)

    # 获取估算位置附近的数据
    questions = fetch_problem_list(estimated_skip, 50)

    for q in questions:
        if q.get('frontendQuestionId') == problem_id:
            return q

    # 如果没找到，尝试更宽的范围
    questions = fetch_problem_list(0, min(pid + 50, 500))
    for q in questions:
        if q.get('frontendQuestionId') == problem_id:
            return q

    return None


def fetch_using_leetcode_api(problem_id: str) -> Optional[Dict[str, Any]]:
    """
    使用 LeetCode GraphQL API 获取题目信息

    Args:
        problem_id: 题号

    Returns:
        题目信息字典，失败返回 None
    """
    # 第一步：查找题目基本信息
    problem_info = find_problem_by_id(problem_id)

    if not problem_info:
        print(f"[WARNING] 未找到题号 {problem_id}", file=sys.stderr)
        return None

    title_slug = problem_info.get('titleSlug')
    if not title_slug:
        return None

    # 第二步：获取详细信息
    detail = fetch_problem_detail(title_slug)

    if not detail:
        # 如果详情获取失败，返回基本信息
        return {
            "id": problem_info.get('frontendQuestionId'),
            "title": problem_info.get('title'),
            "slug": title_slug,
            "difficulty": problem_info.get('difficulty'),
            "tags": [tag.get('name') for tag in problem_info.get('topicTags', [])],
            "content": None,
            "codeSnippets": [],
            "link": f"https://leetcode.cn/problems/{title_slug}/"
        }

    # 使用翻译后的标题和内容（如果有）
    title = detail.get('translatedTitle') or detail.get('title')
    content = detail.get('translatedContent') or detail.get('content')

    # 转换为与原来类似的输出格式
    result = {
        "id": detail.get('questionFrontendId'),
        "title": title,
        "slug": detail.get('titleSlug'),
        "difficulty": detail.get('difficulty'),
        "content": content,
        "tags": [tag.get('translatedName') or tag.get('name')
                 for tag in detail.get('topicTags', [])],
        "codeSnippets": detail.get('codeSnippets', []),
        "link": f"https://leetcode.cn/problems/{detail.get('titleSlug')}/"
    }

    return result


def format_simple_problem_info(problem_id: str, title: str = "") -> str:
    """
    格式化简单的题目信息（当 API 不可用时）

    Args:
        problem_id: 题号
        title: 题目标题（可选）

    Returns:
        Markdown 格式的基本信息
    """
    lines = []

    if title:
        lines.append(f"## {problem_id}. {title}")
    else:
        lines.append(f"## 题目 {problem_id}")

    lines.append("")
    lines.append("[题目描述待补充 - 请手动从 LeetCode 网站获取]")
    lines.append("")
    lines.append("LeetCode 链接: ")
    if title:
        slug = title.lower().replace(' ', '-').replace('(', '').replace(')', '')
        lines.append(f"- 中文站: https://leetcode.cn/problems/{slug}/")
    lines.append(f"- 国际站: https://leetcode.com/problems/problem-{problem_id}/")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="获取 LeetCode 题目信息")
    parser.add_argument("problem_id", nargs="?", help="LeetCode 题号")
    parser.add_argument("--slug", help="题目的 title slug")
    parser.add_argument("--file", help="从题目文件解析题号")
    parser.add_argument("--title", help="题目标题（用于离线模式）")

    args = parser.parse_args()

    # 确定题号
    problem_id = args.problem_id
    title = args.title

    if args.file:
        problem_id = parse_problem_id_from_file(args.file)
        if not problem_id:
            print("[ERROR] 无法从文件中解析题号", file=sys.stderr)
            sys.exit(1)

    if not problem_id:
        parser.print_help()
        sys.exit(1)

    # 使用 API 获取题目信息
    question_data = fetch_using_leetcode_api(problem_id)

    if question_data:
        print(json.dumps(question_data, ensure_ascii=False, indent=2))
    else:
        # 降级到简单模式
        print(format_simple_problem_info(problem_id, title or ""))
        print("[INFO] API 请求失败，请手动补充题目描述", file=sys.stderr)


if __name__ == "__main__":
    main()
