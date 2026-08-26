"""循环预算 helper：进度提示注入 / 移除 / 硬中断交接摘要。

模型在循环中看不到自己用了多少步 / 还剩多少步，导致「根因已定位仍无脑探索」
的迭代爆炸。每轮把剩余预算作为一条临时消息注入，invoke 后立即 pop，
不进 messages 历史（不污染压缩 / 重放 / 续跑）。
"""


def _progress_hint(iterations: int, max_iterations: int, step_results: "list|None" = None) -> str:
    """生成本轮迭代的预算进度提示（供模型自我调节收敛用）。

    三段式紧迫度（对应 trace 痛点：根因常在第 14 轮找到后仍探索 16 轮）：
    - 剩余 <= 3：紧急，立即停止新工具调用。
    - 已用 >= 一半：尽快收敛（覆盖 15~27 轮，根因定位后立刻给强信号）。
    - 否则：温和提示在剩余步内收敛。
    分阶段约束：探索预算（前 1/3）用尽但 step_results 仍无 write/edit
    → 追加「立即停止探索、转入执行」强信号，防探索阶段无限膨胀。
    """
    remaining = max(max_iterations - iterations, 0)
    pct = int(iterations * 100 / max_iterations) if max_iterations else 0
    extra = ""
    if step_results is not None:
        has_write = any(s.get("type") in ("write_file", "edit_file") for s in step_results)
        if not has_write and iterations >= max_iterations // 3:
            extra = " 你已用尽探索预算却尚未开始编辑任何文件——请立即总结已发现信息、输出改动计划并转入执行阶段；若信息不足，用一句话说明缺什么后做一次定向搜索补全。"
    if remaining <= 3:
        urgency = "⚠️ 预算即将耗尽：请立即停止发起新工具调用，用当前已有信息输出最终总结或调用 stop。"
    elif iterations >= max_iterations // 2:
        urgency = "请尽快收敛：完成判定满足即调用 stop 并给总结，禁止在已完成时继续探索。"
    else:
        urgency = "请在剩余步数内收敛：完成判定满足即调用 stop 并给总结，避免无关探索。"
    return (
        f"[进度] 已用 {iterations} / 上限 {max_iterations} 步（剩余 {remaining}，已用 {pct}%）。"
        f"{urgency}{extra}"
    )


def _build_handoff(messages: "list[dict]", step_results: "list[dict]", skill_name: str) -> str:
    """硬中断（max_iterations）时的结构化交接摘要。

    从当前上下文抽取三段（已确认结论 / 进行到哪 / 建议下一步），供 MOA commander
    或人工 resume 时直接续上，避免无人值守任务静默丢失成果。
    不依赖 LLM（纯规则抽取），保证中断路径零额外调用。
    """
    # 1. 已确认结论：优先取压缩历史 <condensed_history>（role 可能是 user 或 system，
    #    取决于 ContextManager.maybe_compress 的存储；此处两者都查，避免找不到）。
    condensed = ""
    for m in messages:
        if m.get("role") in ("system", "user") and "<condensed_history>" in str(m.get("content", "")):
            condensed = str(m["content"])
            break
    # 2. 进行到哪：最近若干轮 assistant 文本（去重截断）
    recent = []
    for m in messages[-8:]:
        if m.get("role") == "assistant" and m.get("content"):
            snippet = str(m["content"]).strip()[:200]
            if snippet and snippet not in recent:
                recent.append(snippet)
    recent_txt = "；".join(recent[-3:]) if recent else "（无）"
    # 3. 已执行的工具步骤（动作轨迹）
    actions = [s.get("type") for s in step_results if s.get("type")]
    actions_txt = "、".join(actions[-8:]) if actions else "（无）"
    summary = (
        f"【已确认结论/压缩历史】{condensed[:300] or '（无压缩历史）'}\n"
        f"【进行到哪】最近动作：{actions_txt}\n最近 assistant 文本：{recent_txt}\n"
        f"【建议下一步】基于已执行步骤续做未完成的编辑/验证；如需恢复根因上下文请阅读上方压缩历史。"
    )
    return summary[:800]


def _pop_progress_hint(messages: list) -> None:
    """移除本轮临时注入的预算进度提示（invoke 后调用，保证不残留进 history）。

    仅当末尾确为进度提示时才 pop，避免误删真实消息。进度提示可能是 system 或
    user 角色（历史实现用 system，现改用 user 以兼容拒绝 mid-conversation system
    消息的 LLM），两者都识别。
    """
    if messages and messages[-1].get("role") in ("system", "user") and \
            str(messages[-1].get("content", "")).startswith("[进度]"):
        messages.pop()
