"""IoScheduler：read_file / search_files 的并行批调度器。

纯磁盘读、互不依赖、无副作用的工具入批并行执行（工作线程只做磁盘读/
ripgrep，共享状态操作留在主线程）；遇到任何串行工具先 flush 批，保证
工具消息严格按 tool_calls 顺序回灌（OpenAI 协议要求）。
用 ``batchable`` 标志 + flush 钩子建模，替代原 if/elif 特判。
"""

from concurrent.futures import ThreadPoolExecutor

# IO 工具（read_file / search_files）并行执行的工作线程上限。
_IO_MAX_WORKERS = 4


class IoScheduler:
    """按序积攒可并行工具调用，串行工具触发前/轮末统一 flush。"""

    def __init__(self, handlers: dict, max_workers: int = _IO_MAX_WORKERS):
        self._handlers = handlers        # 仅含 batchable handler
        self._max_workers = max_workers
        self._batch = []

    def feed(self, tc: dict) -> bool:
        """tc 属于可并行工具则入批并返回 True，否则返回 False（调用方走串行执行）。"""
        h = self._handlers.get(tc["type"])
        if h is None or not h.batchable:
            return False
        self._batch.append(tc)
        return True

    def flush(self, ctx, messages, step_results, files_created, apply_fn) -> None:
        """并行执行批内工具，结果严格按入批顺序回灌。

        apply_fn: dispatch 层的 _apply_result（五步仪式只此一处）。
        """
        if not self._batch:
            return
        batch, self._batch = self._batch, []
        prepared = []   # (handler, tc, short_result | None)
        for tc in batch:
            h = self._handlers[tc["type"]]
            prepared.append((h, tc, h.prepare(tc, ctx, step_results)))

        # 工作线程只做磁盘读/搜索（纯函数，无共享状态）
        def _worker(item):
            h, tc, short = item
            if short is not None:
                return None
            try:
                return h.run_io(tc, ctx)
            except FileNotFoundError:
                return ("err", f"[文件不存在: {tc['input'].get('path', '')}]")
            except Exception as e:
                return ("err", f"[读取失败: {e}]")

        to_run = [it for it in prepared if it[2] is None]
        outcomes = []
        if to_run:
            with ThreadPoolExecutor(max_workers=min(self._max_workers, len(to_run))) as pool:
                outcomes = list(pool.map(_worker, to_run))
        run_iter = iter(outcomes)

        for h, tc, short in prepared:
            if short is not None:
                apply_fn(short, ctx, messages, step_results, files_created)
                continue
            io_result = next(run_iter)
            if io_result is None:  # 理论上不可达（short 已分流）
                continue
            apply_fn(h.finish(tc, ctx, io_result), ctx, messages, step_results, files_created)
