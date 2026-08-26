"""read_file 结果的行号格式化。"""


def _read_file_with_lines(content: str, offset: int = 0, limit: int = 0) -> str:
    """读取文件内容，带行号，可选分页

    Args:
        content: 文件原文
        offset: 起始行号（0-indexed），0=从头
        limit: 返回行数，0=不限

    Returns:
        带行号的文件内容字符串
    """
    lines = content.splitlines(keepends=False)
    total = len(lines)

    if offset == 0 and limit == 0:
        # 无参数：返回全文 + 行号
        numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(lines))
        if total > 200:
            numbered += f"\n(file has {total} lines, pass offset=0&limit={total} to read all)"
        return numbered

    # 显式分页
    start = offset
    end = offset + limit if limit else total
    snippet = lines[start:end]
    numbered = "\n".join(f"{i+1}:{line}" for i, line in enumerate(snippet, start=start))
    if end < total:
        numbered += f"\n(truncated, showing lines {start+1}-{end} of {total})"
    return numbered
