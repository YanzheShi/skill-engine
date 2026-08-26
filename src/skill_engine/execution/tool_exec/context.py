"""ToolContext：工具执行所需的共享态，替代 run() 内闭包捕获。

handler 不再从词法作用域抓 base_dir / snapshot / human_io，而是从
``ctx`` 显式取依赖——循环态（round_had_write / stop_requested）也挂在
``ctx.loop`` 上，由 loop 每轮重置、收尾读取。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from skill_engine.execution.executor import Executor
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.human_io import HumanIO
from skill_engine.execution.snapshot import FileSnapshot


@dataclass
class LoopState:
    """每轮循环态：dispatch 层重置，handler 写入，loop 收尾读取。"""
    round_had_write: bool = False
    stop_requested: bool = False
    stop_reason: str = ""


@dataclass
class ToolContext:
    """一次工具调用的执行上下文（由 ToolDispatchRunner 每轮装配）。"""
    executor: Executor
    base_dir: Path
    skill: Any                       # models.Skill
    file_tracker: FileStateTracker
    snapshot: FileSnapshot
    llm: Any = None                  # 当前绑定了工具的 llm（view_image 判视觉能力用）
    human_io: Optional[HumanIO] = None
    approval_fn: Optional[Callable] = None
    tracer: Any = None               # 可选 DebugTracer；None / 未开启时 no-op
    confirm_edits_mode: str = ""
    model_has_vision: bool = True
    image_url_cache: dict = field(default_factory=dict)  # view_image R2 上传缓存（跨轮存活）
    # 安全/确认/截断/展示等跨切面能力由 dispatch 层提供（handler 不重复实现五步仪式）
    check_file_safety: Optional[Callable] = None   # (op, filepath, skill) -> (bool, err)
    confirm_edit: Optional[Callable] = None        # (op, filepath, diff_text) -> bool
    truncate_msg: Optional[Callable] = None        # (content, max_chars=...) -> str
    emit_tool: Optional[Callable] = None           # (label, detail="") -> None
    emit_result: Optional[Callable] = None         # (out) -> None
    loop: LoopState = field(default_factory=LoopState)
