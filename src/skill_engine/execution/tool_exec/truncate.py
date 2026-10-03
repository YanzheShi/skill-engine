"""超长内容的中段折叠：保头 + 保尾 + 标注折叠量。

工业级 Agent（Claude Code 等）对超长工具输出的通行做法是两端保留、
中段折叠并明确标注折叠量——因为高价值内容经常在**尾部**：

- pytest 的 summary / 失败清单在输出最后；
- traceback 的异常行在堆栈最后；
- JSON/XML 的闭合结构在文本最后。

只保头会把最关键的尾部信息切掉；本模块提供统一实现，
供 `_truncate_msg`（工具结果总闸）与 `format_observation`
（stdout/stderr）共用。
"""

# 标注行自身也要占字符预算（含数字位数波动），预留固定余量
_MARKER_BUDGET = 160


def truncate_middle(content: str, max_chars: int, label: str = "content") -> str:
    """超限时保留头部约 70% + 尾部约 30%，中段折叠并标注。

    Args:
        content: 原始文本
        max_chars: 结果允许的最大字符数（含标注行）
        label: 标注行里的内容名（如 stdout / stderr / tool result）

    Returns:
        不超过 max_chars 的字符串；未超限时原样返回。
    """
    if not isinstance(content, str):
        content = str(content)
    if max_chars <= 0 or len(content) <= max_chars:
        return content

    keep = max_chars - _MARKER_BUDGET
    if keep <= 0:
        # 预算极小（理论边界），退化为纯头部截断并标注
        return content[:max_chars] + f"...({label} truncated)"

    head = int(keep * 0.7)
    tail = keep - head
    omitted = len(content) - head - tail
    marker = (
        f"\n...({label} truncated: omitted {omitted} chars, "
        f"showing first {head} and last {tail} of {len(content)})...\n"
    )
    return content[:head] + marker + (content[-tail:] if tail > 0 else "")
