"""ToolResult：工具执行结果 → message 的统一协议。

各分支原来各自重造的「step_results.append + messages.append(tool)」
五步仪式，收进 dispatch 层唯一的 ``_apply_result``，本类只承载数据。
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional

CALL_TRUNCATED_HINT = (
    "⚠ 本次工具调用缺少必填参数 `{missing}`。\n"
    "这通常不是你把参数写错了，而是模型的输出在生成途中被 max_tokens 上限截断——"
    "正文已经生成、参数尾部丢失，上游 JSON 容错解析后仍交付成一个「合法但不完整」的调用。\n"
    "不要原样重发整份内容（会再次被截断）。请改为：\n"
    "  1) 用 write_file(path=..., content=..., mode=\"append\") 从断点继续追加；或\n"
    "  2) 用 edit_file(path=..., edits=[...]) 做局部修改。"
)


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    content: str = ""                 # 回灌给 LLM 的 tool message 内容
    step: Optional[dict] = None       # step_results.append 的条目
    files_created: List[str] = field(default_factory=list)
    round_had_write: bool = False     # 触发轮后 verify_command 钩子（由 _apply_result 唯一写回 ctx.loop）
    extra_messages: List[dict] = field(default_factory=list)  # 追加消息（如 view_image 的多模态注入）
    stop: bool = False                # stop 工具：终止循环
    stop_reason: str = ""
    hard_stop: bool = False           # bash BLOCK 等：携带 RunResult 的 ctx 级终止
    stopped_by: str = ""              # hard_stop=True 时 RunResult.ctx["stopped_by"]
    print_line: str = ""              # 原各分支裸 print 的等价输出行（可选）

    @classmethod
    def unknown(cls, tc: dict) -> "ToolResult":
        return cls(tool_call_id=tc["id"], name=tc["type"],
                   content=f"[未知工具类型: {tc['type']}]")
