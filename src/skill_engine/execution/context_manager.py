"""上下文管理器：token 预算 + 历史摘要压缩

解决大项目下档位 B 循环 messages 只增不减、撑爆模型窗口的问题。

设计取舍：
- 轻量 token 估算（字符数 // 4），零依赖；预留精确 tiktoken 接口
- 超过 budget*threshold 时，保留首条 user prompt 与最近 keep_recent 个完整轮次，
  其余历史交给 llm 生成 <condensed_history> 摘要替换
- 轮次感知：以 "assistant 且带 tool_calls" 为轮次起点，避免破坏工具调用配对
- 全引擎档位 B skill 通用受益，不绑定任何领域语义（保持通用引擎定位）

该模块不引入新依赖，可单独单测。
"""

import logging
from typing import Optional


class ContextManager:
    """管理档位 B 循环的 messages，提供 token 预算与自动压缩。"""

    def __init__(self, budget: int = 8192, keep_recent: int = 4, threshold: float = 0.8):
        """
        Args:
            budget: token 预算上限（默认 8192）
            keep_recent: 压缩时保留最近多少个完整轮次不压缩（默认 4）
            threshold: 触发压缩的预算占比（默认 0.8，即达到 80% 预算即压缩）
        """
        self.messages: list[dict] = []
        self.budget = budget
        self.keep_recent = keep_recent
        self.threshold = threshold

    # ---- token 估算 ----
    def estimate_tokens(self) -> int:
        """轻量估算当前 messages 的 token 数（字符数 // 4）。"""
        total = 0
        for m in self.messages:
            content = m.get("content", "")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):  # 多模态 / 结构化 content
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
            if m.get("tool_calls"):
                total += sum(len(str(tc)) for tc in m["tool_calls"])
        return total // 4

    # ---- 压缩 ----
    def _round_starts(self) -> list[int]:
        """返回所有 'assistant 且带 tool_calls' 的消息索引（即每个工具轮次的起点）。"""
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

    def maybe_compress(self, llm) -> bool:
        """若接近 token 预算，压缩旧历史。返回是否发生了压缩。

        压缩范围：messages[1 : starts[-(keep_recent+1)]]，
        保留首条 user prompt 与最近 keep_recent 个完整轮次。
        不足 keep_recent+1 个轮次时不压缩（避免破坏最近上下文）。
        """
        if not hasattr(llm, "invoke"):
            return False
        if self.estimate_tokens() <= self.budget * self.threshold:
            return False

        starts = self._round_starts()
        if len(starts) < self.keep_recent + 1:
            return False

        cut = starts[-self.keep_recent]  # 保留最近 keep_recent 个完整轮次
        head = self.messages[:1]               # 首条 user prompt（final_prompt）
        to_summarize = self.messages[1:cut]
        tail = self.messages[cut:]
        if not to_summarize:
            return False

        summary = self._summarize(to_summarize, llm)
        condensed = {
            "role": "user",
            "content": f"<condensed_history>\n{summary}\n</condensed_history>",
        }
        # 原地替换，保持外部对 messages 的引用有效
        self.messages[:] = head + [condensed] + tail
        return True

    def _summarize(self, segment: list[dict], llm) -> str:
        """把一段历史交给 llm 压成中文操作摘要。失败返回提示串（不压缩）。"""
        try:
            text = "\n\n".join(
                f"[{m.get('role')}]\n{self._msg_text(m)}" for m in segment
            )
            prompt = (
                "你是上下文压缩器。下面是一段 agent 执行历史，请压缩为简洁的操作摘要，"
                "保留：已读/改/写的文件路径与关键内容、已执行的命令与结果、当前任务进度与未决问题。"
                "不要编造新信息，输出中文，≤400 字。\n\n" + text
            )
            resp = llm.invoke(prompt)
            if isinstance(resp, str):
                return resp
            return getattr(resp, "content", str(resp)) or str(resp)
        except Exception as e:
            logging.getLogger("skill_engine.context_manager").warning(
                "上下文压缩失败，跳过: %s", e
            )
            return "(上下文压缩失败，历史未压缩)"

    @staticmethod
    def _msg_text(m: dict) -> str:
        c = m.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        return str(c)
