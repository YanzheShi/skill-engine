"""ToolHandler 协议：把 run() 的 if/elif 链换成分发表的接口约定。

每个内建工具一个 handler（住 ``handlers/`` 下自己的模块）；
``handler.execute(tc, ctx) -> ToolResult``，五步仪式由 dispatch 层
``_apply_result`` 统一完成，handler 只产数据。
"""

from typing import Optional, Protocol, runtime_checkable

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.result import ToolResult


@runtime_checkable
class ToolHandler(Protocol):
    name: str

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        """执行一次工具调用，返回结构化结果（不直接动 messages）。"""
        ...

    @property
    def batchable(self) -> bool:
        """True=纯磁盘读类工具（read/search），可入 IO 并行批。"""
        ...


class BaseHandler:
    """handler 基类：默认串行；子类覆盖 name / batchable / execute。"""

    name: str = ""
    batchable: bool = False

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        raise NotImplementedError

    def schema(self):
        """返回供 bind_tools 的 @tool schema（可选；默认由 tool_defs 提供）。"""
        return None


class BatchableHandler(BaseHandler):
    """可并行批工具（read_file / search_files）的三段式协议。

    IoScheduler 负责编排：主线程 prepare（安全门/缓存等共享态操作）→
    线程池 run_io（纯磁盘读/搜索）→ 主线程 finish（登记/格式化/装配结果）。
    结果严格按 tool_calls 顺序回灌，保住 OpenAI 的 tool-message 顺序约束。
    """

    batchable = True

    def prepare(self, tc: dict, ctx: ToolContext, step_results: list) -> Optional[ToolResult]:
        """主线程预处理。返回 ToolResult 表示短路（拒绝/缓存命中）；返回 None 进入 IO。"""
        raise NotImplementedError

    def run_io(self, tc: dict, ctx: ToolContext):
        """工作线程纯 IO。返回 ("ok", content) 或 ("err", message)。"""
        raise NotImplementedError

    def finish(self, tc: dict, ctx: ToolContext, io_result: tuple) -> ToolResult:
        """主线程后处理：登记/格式化，装配 ToolResult。"""
        raise NotImplementedError

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        """串行兜底（正常路径都经 IoScheduler，不会走到这里）。"""
        short = self.prepare(tc, ctx, [])
        if short is not None:
            return short
        return self.finish(tc, ctx, self.run_io(tc, ctx))
