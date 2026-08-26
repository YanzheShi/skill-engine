"""ToolResult：工具执行结果 → message 的统一协议。

各分支原来各自重造的「step_results.append + messages.append(tool)」
五步仪式，收进 dispatch 层唯一的 ``_apply_result``，本类只承载数据。
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional


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
