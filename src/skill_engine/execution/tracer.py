"""Debug 轨迹落盘（可选，debug 开关关闭时全程 no-op）。

把「执行上下文」（messages）、「状态栏」（human_io 语义通道输出）、
「交互流程」（ask_user 问答、多轮输入/输出）统一收集到一个 JSONL 日志文件，
方便 CI / 排查问题时回放整条执行链路。

设计要点：
- debug 不开 → 构造 DebugTracer(None)，enabled() 返回 False，所有 event 调用
  直接 return，零开销（不打开文件、不序列化）。
- 主日志为 JSONL：每行一个 JSON（ts + kind + payload），行缓冲（buffering=1），
  崩溃最多丢最后一行，之前的历史都在。
- 完整上下文（messages 可能很大）在 run 结束的 finally 里单独 dump 到
  ``<log>.ctx.json``，排查时按需打开，不污染主日志可读性。

事件 kind 清单：
    header / thinking / command / result / tool / emit / user_input   （human_io 语义通道）
    run_start / iteration / llm_response / tool_call / command / stop  （ToolDispatchRunner 引擎层）
    route                                                          （cli 路由匹配结果）
    context                                                       （run 结束的完整快照落盘通知）
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


def truncate(value, max_chars: int = 2000):
    """调试日志用：把可能很大的字符串截断到头部，避免单条 event 撑爆日志。"""
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"…(截断，共 {len(value)} 字符)"


class DebugTracer:
    """可选的调试轨迹记录器。

    用法：
        tracer = DebugTracer(path)            # path=None → 全程 no-op
        tracer.event("iteration", n=3)
        tracer.dump_context(messages, step_results, files_created, skill_name=...)
        tracer.close()                         # 或配合 with 语句

    作为上下文管理器使用（推荐，自动 close）：
        with DebugTracer(path) as tracer:
            ...
    """

    def __init__(self, path: Optional[str] = None):
        self._fh = None
        self._path: Optional[str] = None
        if path:
            try:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                # 行缓冲：每条 event 即时落盘，崩溃最多丢最后一行
                self._fh = open(p, "w", encoding="utf-8", buffering=1)
                self._path = str(p)
            except Exception:
                # 落盘失败绝不应影响主执行流程
                self._fh = None
                self._path = None

    def __enter__(self) -> "DebugTracer":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def enabled(self) -> bool:
        return self._fh is not None

    def log_path(self) -> Optional[str]:
        return self._path

    def event(self, kind: str, **payload) -> None:
        if self._fh is None:
            return
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind}
        rec.update(payload)
        try:
            self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            # 单条事件写失败不影响主流程
            pass

    def dump_context(
        self,
        messages,
        step_results,
        files_created,
        skill_name: str = "",
        iterations: int = 0,
        stopped_by: str = "",
        context_path: Optional[str] = None,
    ) -> None:
        """把完整执行上下文（含 messages）落到一个独立的 ``.ctx.json`` 文件。

        messages 可能很大，单独存储避免撑爆主 JSONL；排查时按需打开。
        """
        if self._fh is None:
            return
        ctx_path = context_path or (self._path + ".ctx.json")
        try:
            msgs = list(messages) if hasattr(messages, "__iter__") else messages
            with open(ctx_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "skill_name": skill_name,
                        "iterations": iterations,
                        "stopped_by": stopped_by,
                        "files_created": files_created,
                        "step_results": step_results,
                        "messages": msgs,
                    },
                    f,
                    ensure_ascii=False,
                    default=str,
                    indent=2,
                )
            self.event(
                "context",
                path=ctx_path,
                messages=len(msgs) if hasattr(msgs, "__len__") else 0,
            )
        except Exception:
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None
