"""上下文管理器：token 预算 + 三级渐进压缩（P0 S0-3）

三级降级策略（详见设计文档 §3.3）：
- L1 micro-compaction：折叠旧轮次的大块工具输出（规则化，零 LLM 开销）；
- L2 结构化压缩：LLM 产出中立 schema 的 <task_state> 摘要
  （默认模板任务中立，skill 可用 compress_template 覆盖）；
- L3 截断兜底：L2 失败时保留首条 prompt 与最近轮次，丢弃中间并显式告知 LLM。

设计纪律（通用性，见设计文档 §4）：
- 默认压缩模板任务中立，不含 coding 语义——写诗、面试类 skill 同样适用；
- L1 可由 frontmatter compact_tool_output: false 整体关闭；
- 一切异常吞掉，压缩失败回退下一级而不是崩；
- token 估算对中文修正：ASCII 按 1/4 token，非 ASCII 按 1 token 计
  （旧 chars//4 对中文低估 4-8 倍，长会话会悄悄打爆窗口）。
"""

import os
import time
import logging
from typing import Optional


# L1：超过该字符数的旧工具输出才会被折叠（太小的折叠收益不抵信息损失）
FOLD_THRESHOLD = 1500
# L1：折叠时保留的头部预览长度
FOLD_KEEP_PREVIEW = 200
# 引擎级默认预算（context_budget=0 时使用），可被环境变量覆盖
DEFAULT_CONTEXT_BUDGET = 32768

# L2 压缩遇到 429 限流（rpm exhausted）时的退避重试间隔（秒）。
# 可配置：SKILLS_ENGINE_COMPRESS_RETRY_DELAYS（逗号分隔秒数）。
# 旧版固定 (40, 60) 会让压缩在热路径上阻塞最长 100s（性能诊断 P1-3：压缩加剧限流）。
# 默认仅快速重试 1 次（5s），仍失败即走 L3 截断兜底，不再长时间挂起主循环。
RPM_RETRY_DELAYS = (5,)


def compress_retry_delays() -> tuple:
    """压缩 429 重试间隔（可配置，默认 (5,) = 快速重试一次后走 L3 兜底）。"""
    raw = os.environ.get("SKILLS_ENGINE_COMPRESS_RETRY_DELAYS", "").strip()
    if not raw:
        return RPM_RETRY_DELAYS
    out = []
    for part in raw.split(","):
        try:
            v = float(part.strip())
            if v >= 0:
                out.append(v)
        except ValueError:
            continue
    return tuple(out) if out else RPM_RETRY_DELAYS


def default_context_budget() -> int:
    """引擎级默认上下文预算。

    优先级：SKILLS_ENGINE_CONTEXT_BUDGET 环境变量 > DEFAULT_CONTEXT_BUDGET。
    session 长会话的编码任务工作集动辄 30k+ token，旧默认 8192 只够短 run。
    """
    try:
        v = int(os.environ.get("SKILLS_ENGINE_CONTEXT_BUDGET", "") or 0)
        if v > 0:
            return v
    except ValueError:
        pass
    return DEFAULT_CONTEXT_BUDGET


# L2 默认压缩模板：**任务中立**（原始请求/已完成动作/进展/待办/关键对象引用）。
# coding 场景如需"已改文件/验证状态"等专用字段，由 skill 用 compress_template 覆盖。
_NEUTRAL_SUMMARY_PROMPT = (
    "你是上下文压缩器。下面是一段 agent 执行历史，请压缩为结构化任务状态，"
    "严格按以下字段输出中文（总长 ≤400 字，不要编造新信息）：\n"
    "- 原始请求：(逐字保留用户最初的指令)\n"
    "- 已完成动作：[{对象, 做了什么}]\n"
    "- 当前进展：...\n"
    "- 待办事项：[...]\n"
    "- 关键对象引用：(只留文件路径/命令名等引用，不保留原文；需要细节时用工具重新获取)\n\n"
)

# L3 截断时插入的显式告知（让 LLM 知道细节已丢失、该重新取证）
_TRUNCATION_NOTICE = (
    "[系统提示] 由于上下文预算超限且摘要压缩失败，中间执行历史已被截断。"
    "早前步骤的细节已丢失，若涉及早前内容，请先用 read_file / search_files 等工具重新获取核实再继续。"
)

# L1 折叠扩展：user 消息中的 MOA 黑板/composed 大块（性能诊断建议 4/5）。
# 这些 user 消息是「轮末注入的黑板摘要 / 指挥官任务派发」，跨轮留在历史里会二次方
# 膨胀；命中标记且超阈值 → 规则化折叠（零 LLM 开销），普通用户指令不受影响。
_BOARD_USER_MARKERS = (
    "## 需要你知晓的协作上下文",
    "## 当前协作状态",
    "## 原始总任务",
    "## 指挥官在第",
    "## 本轮任务",
)


def _estimate_text_tokens(text: str) -> int:
    """轻量 token 估算：ASCII 字符按 1/4 token，非 ASCII（中文等）按 1 token。

    中文 1 字 ≈ 1-2 token，旧实现 chars//4 会把中文内容低估 4-8 倍，
    导致预算形同虚设。纯 ASCII 文本的估算结果与旧实现一致（不破坏存量行为）。
    """
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return ascii_n // 4 + (len(text) - ascii_n)


def _is_rate_limited(e: Exception) -> bool:
    """判断异常是否为 429 限流（rpm exhausted / quota_exceeded）。"""
    text = f"{e}"
    return ("429" in text
            or "rate limit" in text.lower()
            or "rpm" in text.lower()
            or "quota_exceeded" in text.lower())


class ContextManager:
    """管理档位 B 循环的 messages，提供 token 预算与三级渐进压缩。"""

    def __init__(self, budget: int = 8192, keep_recent: int = 4, threshold: float = 0.8,
                 compact_tool_output: bool = True, summary_prompt: str = ""):
        """
        Args:
            budget: token 预算上限（调用方未指定时此处保留旧默认；
                tool_dispatch 会以 default_context_budget() 传入更大值）
            keep_recent: 压缩时保留最近多少个完整轮次不压缩（默认 4）
            threshold: 触发压缩的预算占比（默认 0.8，即达到 80% 预算即压缩）
            compact_tool_output: 是否启用 L1 旧工具输出折叠（frontmatter 可关）
            summary_prompt: L2 压缩模板覆盖（空=引擎中立模板）
        """
        self.messages: list[dict] = []
        self.budget = budget
        self.keep_recent = keep_recent
        self.threshold = threshold
        self.compact_tool_output = compact_tool_output
        self.summary_prompt = summary_prompt
        self._last_summary_error: "Exception | None" = None

    # ---- token 估算 ----
    def estimate_tokens(self, messages: "list[dict] | None" = None) -> int:
        """估算消息列表的 token 数（ASCII/非 ASCII 分别计权，见 _estimate_text_tokens）。

        messages 为 None 时估算 self.messages。
        """
        total = 0
        for m in (messages if messages is not None else self.messages):
            content = m.get("content", "")
            if isinstance(content, str):
                total += _estimate_text_tokens(content)
            elif isinstance(content, list):  # 多模态 / 结构化 content
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += _estimate_text_tokens(part["text"])
            if m.get("tool_calls"):
                total += sum(_estimate_text_tokens(str(tc)) for tc in m["tool_calls"])
        return total

    # ---- 轮次边界 ----
    def _round_starts(self) -> list[int]:
        """返回轮次边界索引（每个轮次的起点）。

        有工具历史（assistant 且带 tool_calls）：每个工具轮为边界（旧行为）；
        无工具历史（commander 纯决策 / 纯对话）：回退以 user 消息为边界，保证
        「提示词 + 回复」成对保留在 keep_recent 窗口内，使 L2/L3 对纯对话上下文
        同样生效——否则压缩永不触发、上下文无界增长。
        """
        starts = [
            i for i, m in enumerate(self.messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        if starts:
            # 若末尾以 tool 消息收尾（轮次尚未闭合），补一个虚拟边界
            last_assistant = max(
                (i for i, m in enumerate(self.messages) if m.get("role") == "assistant"),
                default=-1,
            )
            if last_assistant < starts[-1]:
                starts.append(len(self.messages))
            return starts
        # 无工具历史（commander 纯决策 / 纯对话）：以 user 消息为轮次边界
        return [
            i for i, m in enumerate(self.messages)
            if m.get("role") == "user"
        ]

    # ---- L1：micro-compaction（规则化折叠旧的大块工具输出）----
    def _micro_compact(self) -> bool:
        """折叠 keep_recent 窗口之外的旧轮内容，降低上下文体积。

        - tool 消息：超过 FOLD_THRESHOLD 的大块输出（大段 pytest 日志、整文件
          读取）折成带头部预览的占位符——几轮之后只剩诊断价值；
        - assistant 且带 tool_calls 的消息：剥离旧工具轮的思考/前言 content，
          仅保留 tool_calls（模型跨轮不再需要旧轮思考，零成本零有损）；
        - user 消息中的 MOA 黑板/composed 大块：命中 _BOARD_USER_MARKERS 且超
          阈值时折叠（黑板块已由轮末增量注入治理，旧轮黑板块只剩历史价值）。

        这些旧内容几轮之后只剩诊断价值，先降级它们往往就不必动用有损的 L2
        摘要。已折叠的幂等跳过。
        """
        if not self.compact_tool_output:
            return False
        starts = self._round_starts()
        if len(starts) < self.keep_recent + 1:
            return False
        cut = starts[-self.keep_recent]
        changed = False
        for m in self.messages[1:cut]:
            if m.get("role") == "tool":
                content = m.get("content", "")
                if not isinstance(content, str) or len(content) <= FOLD_THRESHOLD:
                    continue
                if content.startswith("[已折叠"):
                    continue
                name = m.get("name") or "tool"
                m["content"] = (
                    f"[已折叠: {name} 输出 {len(content)} 字，细节已省略；如仍需要请用工具重新获取]\n"
                    f"[头部预览] {content[:FOLD_KEEP_PREVIEW]}"
                )
                changed = True
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                # 扩展：剥离旧工具轮的思考/前言 content，仅保留 tool_calls
                content = m.get("content")
                if content and not (isinstance(content, str) and content.startswith("[已折叠")):
                    m["content"] = "[已折叠: 旧轮思考过程]"
                    changed = True
            elif m.get("role") == "user":
                # 扩展：MOA 黑板/composed 大块（黑板块已由增量注入治理，旧轮折叠）
                content = m.get("content")
                if (isinstance(content, str) and len(content) > FOLD_THRESHOLD
                        and any(mk in content for mk in _BOARD_USER_MARKERS)
                        and not content.startswith("[已折叠")):
                    m["content"] = (
                        f"[已折叠: 旧轮黑板/任务块 {len(content)} 字，细节已省略；"
                        f"最新协作状态请以最近轮次的注入为准]\n"
                        f"[头部预览] {content[:FOLD_KEEP_PREVIEW]}"
                    )
                    changed = True
        return changed

    # ---- 三级主入口 ----
    def maybe_compress(self, llm) -> bool:
        """按需压缩。返回是否发生了任何形式的压缩（含 L1 折叠）。

        流程：L1 折叠 → 仍超预算则 L2 结构化摘要 → L2 失败则 L3 截断兜底。
        """
        changed = self._micro_compact()
        if self.estimate_tokens() <= self.budget * self.threshold:
            return changed

        starts = self._round_starts()
        if len(starts) < self.keep_recent + 1:
            return changed
        cut = starts[-self.keep_recent]  # 保留最近 keep_recent 个完整轮次
        head = self.messages[:1]               # 首条 user prompt（final_prompt）
        to_summarize = self.messages[1:cut]
        tail = self.messages[cut:]
        if not to_summarize:
            return changed
        if not hasattr(llm, "invoke"):
            return changed

        summary = self._summarize(to_summarize, llm)
        if summary:
            condensed = {
                "role": "user",
                "content": f"<condensed_history>\n{summary}\n</condensed_history>",
            }
            # 原地替换，保持外部对 messages 的引用有效
            self.messages[:] = head + [condensed] + tail
            return True

        # L3 兜底：摘要失败（异常/空输出）→ 直接截断并显式告知
        self.messages[:] = head + [{"role": "user", "content": _TRUNCATION_NOTICE}] + tail
        if self._last_summary_error:
            print(f"     [上下文压缩失败: {self._last_summary_error}] 已走截断兜底")
        return True

    def _clip_segment_for_summary(self, segment: list[dict]) -> list[dict]:
        """把待摘要历史裁到预算内：单条摘要 prompt 超上下文是 L2 失败主因。

        从尾部保留消息，累计 token 不超过 budget×threshold×50%（下限 2000），
        保证摘要调用本身一定能被模型接受；被裁掉的说明以一条占位消息补充。
        """
        cap = max(int(self.budget * self.threshold * 0.5), 2000)
        kept: list[dict] = []
        total = 0
        for m in reversed(segment):
            t = self.estimate_tokens([m])
            if kept and total + t > cap:
                break
            kept.append(m)
            total += t
        kept.reverse()
        if len(kept) < len(segment):
            kept.insert(0, {
                "role": "user",
                "content": f"[历史过长：仅保留最近 {len(kept)}/{len(segment)} 条消息用于摘要，"
                            f"更早内容已省略]",
            })
        return kept

    def _summarize(self, segment: list[dict], llm) -> str:
        """把一段历史交给 llm 压成结构化摘要。失败返回空串（触发 L3）。

        429 限流（rpm exhausted）按 RPM_RETRY_DELAYS 退避重试，其余错误不重试。
        """
        try:
            segment = self._clip_segment_for_summary(segment)
            text = "\n\n".join(
                f"[{m.get('role')}]\n{self._msg_text(m)}" for m in segment
            )
            prompt = (self.summary_prompt or _NEUTRAL_SUMMARY_PROMPT) + text
            resp = llm.invoke(prompt)
            if isinstance(resp, str):
                summary = resp
            else:
                summary = getattr(resp, "content", str(resp)) or str(resp)
            return (summary or "").strip()
        except Exception as e:
            if _is_rate_limited(e):
                # 限流：等 RPM 窗口过去再试，别急着丢上下文
                for delay in RPM_RETRY_DELAYS:
                    logging.getLogger(__name__).warning(
                        "上下文压缩遇 429 限流（%s），%ds 后重试", e, delay)
                    print(f"     [上下文压缩 429 限流，{delay}s 后重试]")
                    time.sleep(delay)
                    try:
                        resp = llm.invoke(prompt)
                        if isinstance(resp, str):
                            summary = resp
                        else:
                            summary = getattr(resp, "content", str(resp)) or str(resp)
                        if (summary or "").strip():
                            return (summary or "").strip()
                    except Exception as e2:  # noqa: BLE001
                        e = e2
                self._last_summary_error = e
                logging.getLogger(__name__).warning("上下文压缩 429 重试耗尽，走截断兜底: %s", e)
                return ""
            self._last_summary_error = e
            logging.getLogger(__name__).warning("上下文压缩失败，将走截断兜底: %s", e)
            return ""

    @staticmethod
    def _msg_text(m: dict) -> str:
        c = m.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        return str(c)
