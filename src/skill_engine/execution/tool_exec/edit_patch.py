"""edit_file 的编辑应用与 diff 预览。

精确匹配优先，失败走行级宽松模糊匹配。
"""

import re


def _norm_ws(line: str) -> str:
    """行内多空白归一化为单空格 + 去首尾空白"""
    return re.sub(r"\s+", " ", line).strip()


def _fuzzy_find(content: str, old: str):
    """在 content 中按"连续行窗口"做宽松匹配，返回唯一候选的 (start, end) 字符区间。

    两级比较：先整行 strip，再归一化空白；任一唯一命中即返回。
    0 个或 >1 个候选都返回 None（无法唯一确定，交由上层报错）。
    """
    old_lines = old.split("\n")
    n = len(old_lines)
    if n == 0:
        return None
    # 预计算 content 每行的起止字符索引（兼容 \n；\r\n 的行尾空白由 strip 处理）
    lines = []
    start = 0
    for line in content.split("\n"):
        lines.append((start, start + len(line), line))
        start += len(line) + 1  # +1 为一个换行符

    def candidates(transform):
        target = [transform(l) for l in old_lines]
        out = []
        for i in range(len(lines) - n + 1):
            window = [transform(lines[i + k][2]) for k in range(n)]
            if window == target:
                out.append((lines[i][0], lines[i + n - 1][1]))
        return out

    for transform in (lambda x: x.strip(), _norm_ws):
        c = candidates(transform)
        if len(c) == 1:
            return c[0]
    return None


def _apply_edits(content: str, edits: list) -> tuple:
    """对 content 应用 edits，精确匹配优先，失败走行级宽松模糊匹配。

    支持两种 edit：
    - 普通 edit：``{"oldText", "newText"}``，要求 oldText 在**全局**唯一（与原行为一致）。
    - 区间 edit：``{"oldText", "newText", "line_range": [start, end]}``（1-indexed 闭区间），
      仅在指定行范围内定位 oldText，**不要求全局唯一**——消除「oldText 出现 2 次」歧义。
      这让模型在重复行场景下用 line_range 精确锚定，避免反复 read 找唯一 oldText。

    Returns:
        (new_content, None)            成功
        (None, error_message)          失败（error 信息会回传给 LLM 以便重试）
    """
    if not edits:
        return None, "error: edits 列表为空"
    for edit in edits:
        if not edit.get("oldText"):
            return None, "error: edit 项缺少 oldText"

    # 第一步：先应用「区间 edit」（按 line_range 锚定，消除全局歧义）
    lines = content.splitlines(keepends=True)
    # 按 line_range 处理（可能多次修改同一文件的不同区间，逐 edit 应用）
    ranged_errors = []
    plain_edits = []
    ranged = []  # 收集带 line_range 的 edit
    for e in edits:
        lr = e.get("line_range")
        if not lr or not isinstance(lr, (list, tuple)) or len(lr) != 2:
            plain_edits.append(e)
            continue
        ranged.append(e)
    # 按 line_range[0] **降序**处理（从文件末尾向前改），避免前面的区间改了行数后，
    # 后面 edit 的 line_range 指向错位行。
    # LLM 给的行号基于原始文件，不会预知前一个 edit 减少/增加了行数。
    for e in sorted(ranged, key=lambda x: int(x["line_range"][0]), reverse=True):
        lr = e["line_range"]
        start, end = int(lr[0]), int(lr[1])  # 1-indexed 闭区间
        if start < 1 or end < start or end > len(lines):
            ranged_errors.append(
                f"error: line_range {lr} 越界（文件共 {len(lines)} 行）: {e['oldText'][:60]}"
            )
            continue
        seg = "".join(lines[start - 1:end])
        cnt = seg.count(e["oldText"])
        if cnt == 0:
            ranged_errors.append(
                f"error: line_range {lr} 内未找到 oldText: {e['oldText'][:60]}"
            )
            continue
        if cnt > 1:
            ranged_errors.append(
                f"error: line_range {lr} 内 oldText 出现 {cnt} 次（区间内仍需唯一）: {e['oldText'][:60]}"
            )
            continue
        # 区间内唯一 → 替换并写回对应行
        new_seg = seg.replace(e["oldText"], e["newText"], 1)
        lines[start - 1:end] = [new_seg]
    if ranged_errors:
        # 区间 edit 有错直接返回（不部分应用，避免半成品）
        return None, "\n".join(ranged_errors)
    content = "".join(lines)

    # 第二步：对「普通 edit」走原逻辑（全局唯一检查 + 精确/模糊）
    edits = plain_edits
    if not edits:
        return content, None
    # 重复歧义：模糊匹配无法消除，直接报错（与原行为一致）
    for e in edits:
        c = content.count(e["oldText"])
        if c > 1:
            return None, f"error: oldText 在文件中出现 {c} 次（需唯一，或改用 line_range 锚定）: {e['oldText'][:80]}"
    # 全精确：每个 oldText 恰出现 1 次
    exact = all(content.count(e["oldText"]) == 1 for e in edits)
    if exact:
        positioned = sorted(edits, key=lambda e: content.find(e["oldText"]))
        search_pos = 0
        out = []
        for e in positioned:
            idx = content.find(e["oldText"], search_pos)
            out.append(content[search_pos:idx])
            out.append(e["newText"])
            search_pos = idx + len(e["oldText"])
        return "".join(out) + content[search_pos:], None
    # 模糊：至少一处不存在，逐 edit 锁定区间（基于原始 content，避免串扰）
    segments = []
    for e in edits:
        old = e["oldText"]
        if content.count(old) == 1:
            idx = content.find(old)
            segments.append((idx, idx + len(old), e["newText"]))
        else:
            m = _fuzzy_find(content, old)
            if m is None:
                return None, f"error: oldText 不存在（精确与模糊匹配均失败）: {old[:80]}"
            segments.append((m[0], m[1], e["newText"]))
    segments.sort(key=lambda s: s[0])
    out = []
    pos = 0
    for s, en, new in segments:
        out.append(content[pos:s])
        out.append(new)
        pos = en
    return "".join(out) + content[pos:], None


# ---------------------------------------------------------------------------
# 编辑 diff 预览：写盘前用 difflib 生成 unified diff（零依赖），
# 按 confirm_edits 逐次/逐文件确认。默认关闭，其他 skill 零影响。
# ---------------------------------------------------------------------------
_DIFF_MAX_LINES = 200  # diff 超过该长度截断展示（全文重写类的大 diff）
_DIFF_MAX_CHARS = 500  # diff 总字符上限（行数限制之外的双保险，防单行超长刷屏）
_DIFF_NEW_FILE_PREVIEW_LINES = 40  # 新建文件时仅预览前 N 行


def _render_diff(path: str, old: str, new: str) -> str:
    """生成供展示/确认的 unified diff；过长时截断并附提示。"""
    import difflib
    # 新建文件：整份 diff 都是新增行，预览前几行 + 行数摘要即可，避免 709 行刷屏
    if not old:
        lines = new.splitlines()
        total = len(lines)
        preview = lines[:_DIFF_NEW_FILE_PREVIEW_LINES]
        text = (f"[将新建文件 {path}，共 {total} 行，预览前 {len(preview)} 行]\n+"
                + "\n+".join(preview))
        if len(preview) < total:
            text += f"\n... (其余 {total - len(preview)} 行省略)"
        if len(text) > _DIFF_MAX_CHARS:
            text = (text[:_DIFF_MAX_CHARS]
                    + f"\n...(diff 共 {len(text)} 字符，仅显示前 {_DIFF_MAX_CHARS} 字符)")
        return text
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{path} (before)", tofile=f"{path} (after)", lineterm=""))
    total = len(lines)
    if total > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES] + [f"... (diff 共 {total} 行，仅显示前 {_DIFF_MAX_LINES} 行)"]
    text = "\n".join(lines) if lines else "(内容无变化)"
    if len(text) > _DIFF_MAX_CHARS:
        text = (text[:_DIFF_MAX_CHARS]
                + f"\n...(diff 共 {len(text)} 字符，仅显示前 {_DIFF_MAX_CHARS} 字符)")
    return text
