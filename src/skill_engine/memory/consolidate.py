"""主题相似度与合并候选分组。

P2.5 只做轻量、可解释的分组：
- 英文按词、中文按 bigram，计算 Jaccard；
- 同 type、同 store 的 pending 记录才可能合并；
- 不在这里删除数据，由 MemoryService.auto_review_pending 决定接受/拒绝。

这不是语义聚类；只是防止同一规则被多次蒸馏后重复激活。
"""

from __future__ import annotations

import re

from .models import CONFIDENCE_RANK, MemoryRecord

DEFAULT_SIMILARITY = 0.22


def _tokens(text: str) -> set[str]:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())
    words = set(re.findall(r"[a-z0-9_]+", text))
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    grams = {zh[i:i + 2] for i in range(len(zh) - 1)}
    return words | grams


def similarity(a: str, b: str) -> float:
    left = _tokens(a)
    right = _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def group_records(
    records: list[MemoryRecord],
    *,
    threshold: float = DEFAULT_SIMILARITY,
    same_type_only: bool = True,
) -> list[list[MemoryRecord]]:
    """按 store + 文本相似度做贪心分组。

    same_type_only=False 时只允许 preference/feedback 跨类型合并，
    避免把“UI 事实”和“UI 偏好”这类不同性质的内容误合并。
    """
    groups: list[list[MemoryRecord]] = []
    for record in records:
        text = f"{record.title} {record.body}"
        placed = False
        for group in groups:
            head = group[0]
            if head.store_scope != record.store_scope:
                continue
            if same_type_only:
                if head.type != record.type:
                    continue
            elif head.type != record.type and {head.type, record.type} != {"preference", "feedback"}:
                continue
            if similarity(text, f"{head.title} {head.body}") >= threshold:
                group.append(record)
                placed = True
                break
        if not placed:
            groups.append([record])
    return groups


def pick_canonical(group: list[MemoryRecord]) -> MemoryRecord:
    """选信息最完整、置信度最高的一条；同分时保留较早记录。"""
    return max(
        group,
        key=lambda r: (
            CONFIDENCE_RANK.get(r.confidence, 2),
            len(r.body or ""),
            r.importance,
            r.updated_at or "",
        ),
    )
