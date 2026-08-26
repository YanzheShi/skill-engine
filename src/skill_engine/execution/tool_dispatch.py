"""档位 B：Tool Dispatch 循环（Agent Loop）

职责（重构后只剩循环控制）：
1. 编译 final prompt（Assembler）
2. 绑定工具（bind_tools，schema 来自 tool_defs / handler.schema()）
3. LLM 循环调用 → 解析 tool_calls → 分发表执行（tool_exec handlers）→
   追加 tool message → 回到 3
4. 无 tool_calls → 返回最终答案

工具执行逻辑在 ``skill_engine.execution.tool_exec``：
- 每工具一个 handler（handlers/），``execute(tc, ctx) -> ToolResult``
- 五步仪式（step_results / messages / files_created / print）收进唯一
  ``_apply_result``；跨切面能力（安全门 / diff 确认 / 截断 / 展示）经
  ``ToolContext`` 注入，替代原 run() 内闭包捕获。

与 Runner 的交互：
- 通过 approval_fn 回调（Runner._check_approval）处理安全审批
- 直接使用 executor 和 assembler 实例
"""

import os
import time
import json
import logging
from pathlib import Path
from typing import Optional, Callable

from skill_engine.models import Skill, MatchResult, TurnPolicy, RunResult
from skill_engine.execution.assembler import Assembler
from skill_engine.execution.executor import Executor
from skill_engine.execution.tool_defs import TOOL_REGISTRY, load_skill_tools, load_mcp_tools
from skill_engine.execution.context_manager import ContextManager, default_context_budget
from skill_engine.execution.human_io import HumanIO
from skill_engine.execution.tracer import DebugTracer, truncate
from skill_engine.execution.snapshot import FileSnapshot
from skill_engine.execution.file_tracker import FileStateTracker
from skill_engine.execution.paths import to_native_path, _resolve_path  # noqa: F401（旧名兼容）
from skill_engine.security.scanner import should_approve
from skill_engine.config import llm_call_interval

from skill_engine.execution.tool_exec.budget import _progress_hint, _build_handoff, _pop_progress_hint
from skill_engine.execution.tool_exec.parse import parse_tool_calls
from skill_engine.execution.tool_exec.bash_util import (  # noqa: F401（向后兼容 re-export）
    _PATH_TOKEN_RE, _extract_cmd_paths, format_observation,
    _diagnose_shell_error, _SHELL_ERROR_HINTS, build_env_header,
)
from skill_engine.execution.tool_exec.edit_patch import (  # noqa: F401
    _norm_ws, _fuzzy_find, _apply_edits, _render_diff,
    _DIFF_MAX_LINES, _DIFF_MAX_CHARS, _DIFF_NEW_FILE_PREVIEW_LINES,
)
from skill_engine.execution.tool_exec.read_util import _read_file_with_lines  # noqa: F401
from skill_engine.execution.tool_exec.search import (  # noqa: F401
    _format_match, _run_ripgrep, _python_search, _search_files,
    _RG_TIMEOUT, _SEARCH_DEFAULT_MAX, _SEARCH_MAX_CAP,
)
from skill_engine.execution.tool_exec.verify import _extract_test_failures, _run_verification
from skill_engine.execution.tool_exec.context import ToolContext, LoopState
from skill_engine.execution.tool_exec.result import ToolResult
from skill_engine.execution.tool_exec.io_sched import IoScheduler, _IO_MAX_WORKERS  # noqa: F401
from skill_engine.execution.tool_exec.registry import build_builtin_handlers
from skill_engine.execution.tool_exec.handlers.bash import BASH_MAX_TIMEOUT  # noqa: F401
from skill_engine.execution.tool_exec.handlers.skill_tool import SkillToolHandler

logger = logging.getLogger(__name__)


class ToolDispatchRunner:
    """档位 B：tool_dispatch 循环（CC 原生 skill 兼容）

    工作流程：
    1. 编译 final prompt 作为 system message
    2. 调用 LLM(llm.invoke(messages))  返回 {content, tool_calls}
    3. 有 tool_calls  handler 分发表执行  追加 tool message  回到 2
    4. 无 tool_calls  判断 human_in_loop  问用户 / 结束
    """

    def __init__(
        self,
        executor: Executor,
        assembler: Assembler,
        approval_fn: Optional[Callable] = None,
        human_io: Optional[HumanIO] = None,
        turn_policy: Optional[TurnPolicy] = None,
        working_root: Optional[str] = None,
        plain_text: bool = False,
        verbose: bool = False,
        trusted_root: Optional[str] = None,
        tracer: Optional["DebugTracer"] = None,
    ):
        self.executor = executor
        self.assembler = assembler
        self.approval_fn = approval_fn  # Runner._check_approval 回调
        self.human_io = human_io
        self.turn_policy = turn_policy
        # 归一化：Windows 下允许用户传 Git Bash / WSL 风格路径（/d/x、/mnt/d/x）
        self.working_root = to_native_path(working_root)
        self.plain_text = plain_text  # CLI 纯文本终端：禁用 Markdown 输出
        self.verbose = verbose  # 是否显示引擎内部调试态（迭代/历史条数/LLM 响应）
        self.tracer = tracer  # 可选 DebugTracer；None / 未开启时全程 no-op
        # trusted_root：用户显式指定的受信任工作目录（如 MOA -w）。
        # 目录内的文件读写自动放行（免审批/免 diff 确认）；目录外维持原审批。
        self.trusted_root = to_native_path(trusted_root)
        # batch 模式下会话内已批准的文件（run_repl 复用同一 runner 实例，跨轮存活）
        self._file_edit_approvals: set = set()
        self._confirm_edits_mode = ""
        # view_image 的 R2 上传缓存：(path, size, mtime_ns) → 公网 URL，同文件只传一次
        self._view_image_urls: dict = {}

    def _trace_finish(self, res) -> "RunResult":
        """包装一次 RunResult 返回：顺带记一条 stop 事件（debug 模式）。

        同时把 stopped_by 存到实例上：run() 的 finally 收尾 dump_context 需要它，
        但 finally 里的局部变量 ``result`` 可能被主循环里的工具输出字符串覆盖
        （Python 无块级作用域），不能直接引用——这里是最可靠的采集点，
        因为所有 RunResult 退出路径（正常完成/提前 stop/error/max_iterations）
        都经过 _trace_finish。
        """
        if self.tracer and self.tracer.enabled():
            stopped_by = res.ctx.get("stopped_by")
            self.tracer.event(
                "stop",
                stopped_by=stopped_by,
                iterations=res.ctx.get("iterations"),
            )
            self._dbg_stopped_by = stopped_by
        return res

    def _is_trusted_path(self, full_path: Path) -> bool:
        """路径是否位于受信任工作目录（trusted_root）内。规范化后前缀匹配，防 .. 逃逸。"""
        if not self.trusted_root:
            return False
        try:
            root = os.path.normcase(str(Path(self.trusted_root).resolve()))
            target = os.path.normcase(str(Path(full_path).resolve()))
        except OSError:
            return False
        return target == root or target.startswith(root + os.sep)

    def _truncate_msg(self, content: str, max_chars: int = 30000) -> str:
        """Truncate tool result message content to prevent context overflow.

        Full content is preserved in step_results for logging.
        """
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n...(truncated, {len(content)} chars total, showing first {max_chars})"

    # ---- 用户态执行轨迹：语义化输出 ----
    # 统一经 human_io 的 emit_* 语义通道；human_io 为 None（非交互/Web）或
    # 未实现新语义方法（如旧版 Fake/测试替身）时，回退为纯文本 print，
    # 保持与原有无条件 print 行为一致，不破坏 Web 端/测试。
    def _emit_tool(self, label: str, detail: str = "") -> None:
        m = getattr(self.human_io, "emit_tool", None) if self.human_io is not None else None
        if callable(m):
            m(label, detail)
        else:
            print(f"  [tool] {label}  {detail}" if detail else f"  [tool] {label}")

    def _emit_result(self, out: str) -> None:
        m = getattr(self.human_io, "emit_result", None) if self.human_io is not None else None
        if callable(m):
            m(out)
        elif out:
            # 回退路径（human_io 无 emit_result）：按 2 行截断，与 CliHumanIO 行为一致
            lines = out.splitlines()
            for line in lines[:2]:
                print(f"  {line}")
            if len(lines) > 2:
                print(f"  ...(还有 {len(lines) - 2} 行未显示，共 {len(lines)} 行)")

    def _emit_thinking(self, text: str) -> None:
        """模型本轮的「思考文字」（content）实时展示给用户。"""
        m = getattr(self.human_io, "emit_thinking", None) if self.human_io is not None else None
        if callable(m):
            m(text)
        elif text:
            print(f"\n{text}")

    def _check_file_safety(self, op_type: str, filepath: str, skill: Skill) -> tuple[bool, str]:
        """检查文件操作的安全性

        Args:
            op_type: 操作类型（read/write/edit）
            filepath: 目标文件路径
            skill: 当前 skill

        Returns:
            (approved, error_message)
        """
        # 防御：目标路径若为已存在目录，不能当文件做 read/write/edit。
        # 空 path 会被解析成 base_dir 本身（目录）；Windows 上对目录
        # read_text/write_text 抛 PermissionError，会让整个 worker 崩溃。
        # 这里在信任目录判断之前就拦下，优先级最高。
        base_dir = self.working_root or Path(skill.directory)
        if _resolve_path(filepath, base_dir).is_dir():
            return False, f"[拒绝] 目标路径是目录，不能进行{op_type}操作：{filepath}"

        from skill_engine.security.scanner import RISKY_FILENAMES
        # 直接检查文件名（不依赖 _path_escapes 的正则提取）
        if Path(filepath).name in RISKY_FILENAMES:
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 敏感文件操作已取消"

        # 受信任工作目录内的文件操作自动放行（用户显式指定 trusted_root 时）
        if self.trusted_root:
            base_dir = self.working_root or Path(skill.directory)
            if self._is_trusted_path(_resolve_path(filepath, base_dir)):
                return True, ""

        decision, reason = should_approve(
            f"{op_type}:{filepath}", skill.directory, risk_hint="tool_file"
        )
        if decision == "BLOCK":
            return False, f"[安全拦截] {reason}"
        elif decision == "ATTENTION":
            if self.approval_fn:
                approved = self.approval_fn(skill.metadata.name, op_type, filepath)
            else:
                approved = False
            if not approved:
                return False, "[用户跳过] 操作已取消"
        return True, ""

    # ---------------- 编辑 diff 预览与确认（confirm_edits） ----------------
    def _confirm_edit(self, op: str, filepath: str, diff_text: str) -> bool:
        """diff 预览门。返回 True=放行落盘，False=用户拒绝。

        模式（frontmatter confirm_edits）：
        - "true" ：每次编辑都确认
        - "batch"：逐文件确认——某文件首次编辑询问，批准后本会话内该文件自动放行
        无 human_io（非交互场景）降级为仅展示、不阻断。
        """
        mode = self._confirm_edits_mode
        # 受信任工作目录内的文件操作自动放行（不展示 diff、不询问）
        if self._is_trusted_path(Path(filepath)):
            print(f"     [diff 预览] {op} → {filepath}（工作目录内，自动放行）")
            return True
        if mode == "batch" and filepath in self._file_edit_approvals:
            print(f"     [diff 预览] {op} → {filepath}（本会话已批准该文件，自动放行）")
            print(diff_text)
            return True
        if self.human_io is None:
            print(f"     [diff 预览] {op} → {filepath}（非交互模式：仅展示，直接应用）")
            print(diff_text)
            return True
        ask = ("允许吗？(y 允许 / n 拒绝)" if mode != "batch"
               else "允许吗？(y 允许并记住该文件 / n 拒绝)")
        pencil = "📝" if getattr(self.human_io, "_emoji", True) else "[diff]"
        self.human_io.emit(f"{pencil} 编辑预览 [{op} → {filepath}]\n{diff_text}\n{ask}")
        answer = (self.human_io.read() or "").strip().lower()
        approved = answer in ("y", "yes", "是", "好", "ok")
        if approved and mode == "batch":
            self._file_edit_approvals.add(filepath)
        return approved

    # ---------------- 工具结果 → message 的统一协议（五步仪式只此一处） ----------------
    def _apply_result(self, result: ToolResult, tctx, messages: list,
                      step_results: list, files_created: list) -> None:
        """把 handler 的 ToolResult 落进对话与执行轨迹。

        handler 只在 ToolResult 上声明 ``round_had_write``，由这里唯一写回
        ``tctx.loop``，驱动轮末 verify 钩子——避免 handler 直接改循环态。
        """
        if result.step is not None:
            step_results.append(result.step)
        if result.round_had_write:
            tctx.loop.round_had_write = True
        msg = {"role": "tool", "tool_call_id": result.tool_call_id}
        if result.name:  # 未知工具消息原样不带 name（与旧行为一致）
            msg["name"] = result.name
        msg["content"] = result.content
        messages.append(msg)
        if result.extra_messages:
            messages.extend(result.extra_messages)
        if result.files_created:
            files_created.extend(result.files_created)
        if result.print_line:
            print(result.print_line)

    def _finish_result(self, output: str, stopped_by: str, step_results: list,
                       files_created: list, skill_name: str, iterations: int,
                       messages: list, extra_ctx: Optional[dict] = None) -> "RunResult":
        """装配经 _trace_finish 的 RunResult（统一所有退出路径的 ctx 结构）。"""
        ctx = {"steps": step_results, "files_created": files_created,
               "skill_name": skill_name, "iterations": iterations,
               "stopped_by": stopped_by}
        if extra_ctx:
            ctx.update(extra_ctx)
        return self._trace_finish(RunResult(output=output, ctx=ctx, history=messages))

    def run(
        self,
        match_result: MatchResult,
        llm,
        max_iterations: int = 10,
        state_path: Optional[str] = None,
        resume_from: Optional[str] = None,
        initial_messages: Optional[list] = None,
        session_mode: bool = False,
        snapshot: Optional[FileSnapshot] = None,
        file_tracker: Optional[FileStateTracker] = None,
        append_final_prompt: bool = False,
    ) -> dict:
        """执行 tool_dispatch 循环

        Args:
            match_result: 匹配结果
            llm: LLM 客户端（需支持 bind_tools）
            max_iterations: 最大迭代次数
            state_path: 可选，运行状态落盘路径（JSON）。每轮结束后写入，
                供后续 resume_from 续跑。不传则不持久化。
            resume_from: 可选，从指定状态文件续跑（载入 messages / 进度），
                继续对话而非重头来过。与 state_path 同源时即为"中断后续跑"。
            initial_messages: 可选，直接以该历史起轮（session 续轮用）。
            session_mode: 会话模式。注入 ask_user 工具，并在无 tool_calls 时
                立即以 stopped_by="session_turn_end" 返回，把控制权交还编排层。
            snapshot: 可选，外部注入的 FileSnapshot 实例。不传则每次 run() 新建
                （检查点=本次运行起点）；session 模式由 run_repl 传入同一实例，
                使 restore_file 能回滚到整个会话的起点而非本轮起点。
            file_tracker: 可选，外部注入的 FileStateTracker 实例。不传则每次 run()
                新建（软/硬约束由 skill 的 strict_file_tracking 决定）；session 模式
                由 run_repl 传入同一实例，使"已读"登记跨轮有效。

        Returns:
            执行结果 dict
        """
        skill = match_result.skill
        final_prompt = self.assembler.assemble(
            skill, match_result.arguments, plain_text=self.plain_text)

        base_dir = self.working_root or Path(skill.directory)

        # 环境头：显式告诉模型 OS / shell / 工作目录 / 路径风格。
        # 缺了这段，模型在 Windows 上会照发 ls/pwd 与 /d/... 路径，空转到迭代上限。
        final_prompt = build_env_header(base_dir, getattr(self.executor, "shell", "")) + final_prompt
        # 复用外部快照时，_recorded 集合得以跨轮保留，第 2 轮的首次写入不会
        # 覆盖第 1 轮记录的 .bak（否则只能回滚到本轮起点）。
        self._snapshot = snapshot if snapshot is not None else FileSnapshot(base_dir)
        # 文件状态跟踪（read-before-write）。外部注入时跨轮复用（session）；
        # 否则本次运行新建。软约束为默认，skill 可声明 strict_file_tracking 升级硬约束。
        if file_tracker is not None:
            self._file_tracker = file_tracker
        else:
            self._file_tracker = FileStateTracker(
                strict=bool(getattr(skill.metadata, "strict_file_tracking", False)))
        # skill 声明的自动验证命令（frontmatter 作者声明，可信）——轮后钩子，留在循环层
        verify_command = (getattr(skill.metadata, "verify_command", "") or "").strip()
        # 编辑 diff 预览模式（''/off 关闭；'true' 逐次确认；'batch' 逐文件确认）
        self._confirm_edits_mode = str(getattr(skill.metadata, "confirm_edits", "") or "").strip().lower()
        try:
            verify_timeout = int(getattr(skill.metadata, "verify_timeout", 120) or 120)
        except (TypeError, ValueError):
            verify_timeout = 120

        # ---- 分发表装配：内建 handler + skill/MCP 注入工具 ----
        handlers = build_builtin_handlers(session_mode=session_mode)
        skill_handlers: dict[str, object] = {}
        if hasattr(llm, "bind_tools"):
            skill_extra = load_skill_tools(skill)
            skill_mcp = load_mcp_tools(skill)  # 接入 mcp.json 声明的远程 MCP 工具
            restore_schema = handlers["restore_file"].schema()
            session_schemas = [handlers["ask_user"].schema()] if session_mode else []
            # MCP 远程工具并入。同名时优先保留内建工具与 restore_file，
            # 避免远程工具意外覆盖核心文件操作（bash/read_file/edit_file/...）。
            builtin_names = set(TOOL_REGISTRY.keys()) | {"restore_file"}
            skill_mcp_safe = [t for t in skill_mcp if t.name not in builtin_names]
            if len(skill_mcp_safe) != len(skill_mcp):
                logger.warning(
                    "MCP 工具存在与内建同名的项，已跳过被覆盖的 %d 个",
                    len(skill_mcp) - len(skill_mcp_safe),
                )
            skill_handlers = {t.name: SkillToolHandler(t)
                              for t in skill_extra + skill_mcp_safe}
            tools = (list(TOOL_REGISTRY.values()) + skill_extra + [restore_schema]
                     + session_schemas + skill_mcp_safe)
            disallowed = getattr(skill.metadata, "disallowed_tools", None) or []
            allowed = getattr(skill.metadata, "allowed_tools", None) or []
            if disallowed:
                tools = [t for t in tools if t.name not in disallowed]
            if allowed:
                tools = [t for t in tools if t.name in allowed]
            # 按模型能力过滤：文本模型不暴露视觉工具（view_image / shot_web），
            # 从根源杜绝「截图 → 看图 → 被告知无视觉」的无效步骤。
            from skill_engine.config import model_supports_vision
            self._model_has_vision = model_supports_vision(llm)
            if not self._model_has_vision:
                tools = [t for t in tools if t.name not in ("view_image", "shot_web")]
            llm_with_tools = llm.bind_tools(tools)
        else:
            llm_with_tools = llm

        # 上下文管理：三级渐进压缩。预算默认贴长会话需求
        # （SKILLS_ENGINE_CONTEXT_BUDGET 可覆盖）；L1 折叠与 L2 压缩模板可按 skill 配置。
        cm = ContextManager(
            budget=getattr(skill.metadata, "context_budget", 0) or default_context_budget(),
            compact_tool_output=bool(getattr(skill.metadata, "compact_tool_output", True)),
            summary_prompt=(getattr(skill.metadata, "compress_template", "") or ""),
        )
        messages = cm.messages

        iterations = 0
        step_results = []
        files_created = []

        # 落盘续跑：若给定 resume_from，载入上次运行状态继续对话
        save_path = state_path or resume_from
        if initial_messages is not None:
            # session 模式续轮：直接用历史起轮，final_prompt 已含在 initial_messages 中
            cm.messages[:] = list(initial_messages)
            messages = cm.messages
            if append_final_prompt:
                # MOA 续跑：在已有私有历史上追加本轮 final_prompt（含 env 头）作为新 user 轮
                messages.append({"role": "user", "content": final_prompt})
        elif resume_from:
            loaded = self._load_state(resume_from)
            if loaded is not None:
                if loaded.get("session_mode") and not session_mode:
                    logger.warning(
                        "状态文件 %s 由 session 模式产生（含多轮用户指令/ask_user 交互），"
                        "正以普通 run 载入：ask_user 工具不可用且不会在轮末交还控制权。"
                        "如需续接会话请改用 `session --resume-from`。",
                        resume_from,
                    )
                cm.messages[:] = loaded.get("messages", [])
                messages = cm.messages
                iterations = loaded.get("iterations", 0)
                step_results = loaded.get("step_results", [])
                files_created = loaded.get("files_created", [])
                # final_prompt 已含在 messages 中，不再重复追加
            else:
                messages.append({"role": "user", "content": final_prompt})
        else:
            _fp = final_prompt
            if not getattr(self, "_model_has_vision", True):
                _fp = (
                    "【能力声明】当前模型是**无视觉的文本模型**：view_image / shot_web "
                    "工具不可用。需要验证 UI 渲染等视觉任务时，请改用读取 HTML/CSS/JS "
                    "源码、或运行自动化测试（node / pytest 等）等方式完成，不要尝试截图看图。\n\n"
                    + _fp
                )
            messages.append({"role": "user", "content": _fp})

        # ---- ToolContext：handler 的共享态装配（替代闭包捕获） ----
        tctx = ToolContext(
            executor=self.executor,
            base_dir=base_dir,
            skill=skill,
            file_tracker=self._file_tracker,
            snapshot=self._snapshot,
            llm=llm,
            human_io=self.human_io,
            approval_fn=self.approval_fn,
            tracer=self.tracer,
            confirm_edits_mode=self._confirm_edits_mode,
            model_has_vision=getattr(self, "_model_has_vision", True),
            image_url_cache=self._view_image_urls,
            check_file_safety=self._check_file_safety,
            confirm_edit=self._confirm_edit,
            truncate_msg=self._truncate_msg,
            emit_tool=self._emit_tool,
            emit_result=self._emit_result,
        )
        scheduler = IoScheduler({n: h for n, h in handlers.items() if h.batchable})

        result = None
        # 执行开始标题头（语义通道；无 human_io 时静默——Web 端/测试回退默认实现）
        if self.human_io is not None:
            hdr = getattr(self.human_io, "emit_header", None)
            if callable(hdr):
                hdr(f"Running in {skill.metadata.name} @ {base_dir}")

        # debug 轨迹：run 开始（含 skill / 模式 / 工作目录）
        if self.tracer and self.tracer.enabled():
            self.tracer.event(
                "run_start",
                skill=skill.metadata.name,
                session_mode=session_mode,
                working_root=str(base_dir),
            )
        try:
            for i in range(max_iterations):
                iterations += 1
                # debug 轨迹：每轮迭代起点
                if self.tracer and self.tracer.enabled():
                    self.tracer.event("iteration", n=iterations, max=max_iterations)

                # 隐藏迭代轮次计数（正常输出不显示）；每轮之间用空行分割，方便观察。
                # 仅在 --verbose 调试模式保留「迭代 N/max」与历史条数。
                if self.verbose:
                    print(f"  > 迭代 {iterations}/{max_iterations}")
                    print(f"  Messages in history: {len(messages)} items")
                elif iterations > 1:
                    print()

                # 上下文压缩在轮末执行：本轮工具结果全部追加完毕、下轮 invoke 之前
                # 压缩——轮首压缩的旧实现压缩对象滞后一轮，且压缩抢在 LLM 调用前占热路径。
                # 每轮 LLM 调用之间的人为节流：可配置（SKILLS_ENGINE_LLM_CALL_INTERVAL /
                # config.yml settings.llm_call_interval），默认 0 = 关闭，
                # 429 限流由下方指数退避兜底。测试环境下跳过（PYTEST_CURRENT_TEST）。
                interval = llm_call_interval()
                if interval > 0 and not os.environ.get("PYTEST_CURRENT_TEST"):
                    time.sleep(interval)

                # 预算可见注入：临时把进度提示作为 user 消息追加到 messages
                # （用 user 角色而非 system，避免部分 LLM 拒绝 mid-conversation
                # system 消息导致 invoke 抛异常、循环直接挂掉），让本轮模型能看到
                # 剩余预算并自我调节收敛；invoke 后立即 pop 掉，不残留进 history。
                messages.append({"role": "user", "content": _progress_hint(iterations, max_iterations, step_results)})
                resp = None
                max_retries = 5
                for attempt in range(max_retries):
                    try:
                        resp = llm_with_tools.invoke(messages)
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "rate" in err_str.lower() or "quota" in err_str.lower() or "exhaust" in err_str.lower():
                            wait_time = 20 * (attempt + 1)
                            # 测试环境下跳过退避等待（PYTEST_CURRENT_TEST 由 pytest 自动设置），加速用例
                            if not os.environ.get("PYTEST_CURRENT_TEST"):
                                time.sleep(wait_time)
                            if attempt == max_retries - 1:
                                _pop_progress_hint(messages)  # 清理临时进度提示
                                return self._finish_result(
                                    f"[LLM 调用被限流（已重试 {max_retries} 次）: {err_str}]",
                                    "rate_limited", step_results, files_created,
                                    skill.metadata.name, iterations, messages[:])
                        else:
                            _pop_progress_hint(messages)  # 清理临时进度提示
                            return self._finish_result(
                                f"[LLM 调用失败: {err_str}]",
                                "error", step_results, files_created,
                                skill.metadata.name, iterations, messages[:])

                # 移除本轮临时预算进度提示，避免残留进 history / 压缩 / 续跑。
                _pop_progress_hint(messages)

                # 标准化 LLM 响应为 dict（兼容 LangChain AIMessage）。
                # 关键：保留推理字段 reasoning_content —— 推理模型（DeepSeek-R1 /
                # V4-thinking / Qwen-thinking 等）把"思考过程"吐在的独立字段，
                # LangChain ChatOpenAI 将其放在 additional_kwargs 里；flash 等非
                # 推理模型通常不返回，此时 reasoning 为空，无思考可展示（正常现象）。
                reasoning = ""
                if isinstance(resp, dict):
                    reasoning = resp.get("reasoning_content") or resp.get("reasoning") or ""
                elif hasattr(resp, "additional_kwargs"):
                    ak = resp.additional_kwargs or {}
                    reasoning = ak.get("reasoning_content") or ak.get("reasoning") or ""
                if hasattr(resp, "tool_calls"):
                    resp = {
                        "content": resp.content if hasattr(resp, "content") else str(resp),
                        "tool_calls": list(resp.tool_calls) if resp.tool_calls else [],
                        "reasoning": reasoning,
                    }
                elif not isinstance(resp, dict):
                    resp = {"content": str(resp), "tool_calls": [], "reasoning": ""}
                else:
                    resp.setdefault("reasoning", "")

                # 解析 tool_calls
                tool_calls = parse_tool_calls(resp)

                # 模型本轮的「思考文字」：实时展示。
                # 优先级：reasoning_content（推理模型思考）→ content（模型写在工具调用前的说明）。
                # 两者皆空 → 该模型在调工具时不输出思考（如 DeepSeek-v4-flash），属正常现象，
                # 无内容可展示。仅在有 tool_calls 时 emit，避免与最终回答（无 tool_calls 的
                # content）重复打印。
                thinking = resp.get("reasoning") or resp.get("content", "")
                if thinking and tool_calls:
                    self._emit_thinking(thinking)

                # 内部调试态仅在 --verbose 显示；工具调用改走语义通道 emit_tool
                if self.verbose:
                    print(f"  LLM response: content={len(resp.get('content', ''))} chars, tool_calls={len(tool_calls)}")
                # debug 轨迹：LLM 响应概览（content/推理长度 + 本轮工具调用名）
                if self.tracer and self.tracer.enabled():
                    self.tracer.event(
                        "llm_response",
                        content_len=len(resp.get("content", "") or ""),
                        reasoning_len=len(resp.get("reasoning", "") or ""),
                        tool_calls=[tc.get("type") for tc in tool_calls],
                    )
                for tc in tool_calls:
                    self._emit_tool(tc['type'], str(tc['input']))
                    # debug 轨迹：引擎层工具调用（raw input，human_io 为 None 时也能捕获）
                    if self.tracer and self.tracer.enabled():
                        self.tracer.event(
                            "tool_call",
                            type=tc.get("type"),
                            input=truncate(str(tc.get("input", "")), 1000),
                        )

                if not tool_calls:
                    text = resp.get("content", "")
                    messages.append({"role": "assistant", "content": text})

                    if session_mode:
                        # session 模式：子任务完成文本，由外层 REPL 处理后等待下条指令。
                        # 禁用内部 human_in_loop 追问循环，避免双重提问。
                        step_results.append({"name": "llm_response", "type": "llm", "output": text})
                        return self._finish_result(
                            text, "session_turn_end", step_results, files_created,
                            skill.metadata.name, iterations, messages)

                    if self.human_io and self.turn_policy:
                        # 多轮对话模式
                        if self.turn_policy.should_stop(text):
                            # LLM 说完了  直接结束，不追问用户
                            step_results.append({"name": "llm_response", "type": "llm", "output": text})
                            return self._finish_result(
                                text, "stop", step_results, files_created,
                                skill.metadata.name, iterations, messages)
                        else:
                            # LLM 在问用户  emit + read + 追 history + 继续
                            self.human_io.emit(text)
                            user_input = self.human_io.read()

                            # 用户退出
                            if user_input in (self.turn_policy.user_exit or []):
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return self._finish_result(
                                    text, "user_exit", step_results, files_created,
                                    skill.metadata.name, iterations, messages)

                            # 达到最大轮数
                            if iterations >= self.turn_policy.max_turns:
                                step_results.append({"name": "llm_response", "type": "llm", "output": text})
                                return self._finish_result(
                                    text, "max_turns", step_results, files_created,
                                    skill.metadata.name, iterations, messages)

                            # 追加用户回答，继续循环
                            messages.append({"role": "user", "content": user_input})
                            continue

                    # 非多轮模式：原行为
                    step_results.append({"name": "llm_response", "type": "llm", "output": text})
                    return self._finish_result(
                        text, "stop", step_results, files_created,
                        skill.metadata.name, iterations, messages)

                # 有 tool_calls：先落 assistant 消息，再经分发表执行
                lc_tool_calls = []
                for tc in tool_calls:
                    lc_tool_calls.append({
                        "id": tc["id"],
                        "name": tc["type"],
                        "args": tc["input"],
                    })
                messages.append({
                    "role": "assistant",
                    "content": resp.get("content", ""),
                    "tool_calls": lc_tool_calls,
                })

                tctx.loop = LoopState()  # 每轮重置循环态
                for tc in tool_calls:
                    # 可并行批工具（read/search）入批；遇到串行工具先 flush，
                    # 保证工具消息严格按 tool_calls 顺序回灌（OpenAI 协议要求）。
                    if scheduler.feed(tc):
                        continue
                    scheduler.flush(tctx, messages, step_results, files_created,
                                    self._apply_result)
                    handler = handlers.get(tc["type"]) or skill_handlers.get(tc["type"])
                    if handler is not None:
                        t_result = handler.execute(tc, tctx)
                    else:
                        t_result = ToolResult.unknown(tc)
                    if t_result.stop:
                        # stop 工具：原行为不追加任何 tool message，直接终止
                        return self._finish_result(
                            t_result.stop_reason, "tool_stop", step_results,
                            files_created, skill.metadata.name, iterations, messages)
                    self._apply_result(t_result, tctx, messages, step_results, files_created)
                    if t_result.hard_stop:
                        # bash BLOCK 等：已回灌错误消息，终止本轮并告知原因
                        return self._finish_result(
                            t_result.content, t_result.stopped_by, step_results,
                            files_created, skill.metadata.name, iterations, messages)

                # 轮内写/改完成后跑一次声明的验证命令（轮后钩子，留在循环层），
                # 只在失败时回灌结构化信号，驱动"改→验→修"闭环。
                if verify_command and tctx.loop.round_had_write:
                    feedback = _run_verification(
                        self.executor, base_dir, verify_command, verify_timeout)
                    if feedback:
                        messages.append({"role": "user", "content": feedback})
                        print("     VERIFY FAILED → 失败信号已回灌")
                    else:
                        print("     verify passed")

                # 收尾：flush 批内剩余的 IO 结果（工具消息顺序与 tool_calls 对齐）
                scheduler.flush(tctx, messages, step_results, files_created,
                                self._apply_result)

                # 上下文压缩在轮末：本轮全部工具结果已追加完毕、即将进入下一轮
                # invoke——轮首压缩的旧实现压缩对象滞后一轮，且压缩抢在 LLM
                # 调用前占热路径。
                cm.maybe_compress(llm)

        # 达到最大迭代次数
            result = self._finish_result(
                "[达到最大迭代次数]", "max_iterations", step_results, files_created,
                skill.metadata.name, iterations, messages,
                # 硬中断结构化移交，供 resume / 人工续跑，避免静默丢失成果
                extra_ctx={"handoff_summary": _build_handoff(messages, step_results, skill.metadata.name)})
            return result
        finally:
            # 任一退出路径（含提前 stop / error / max_iterations）都落盘，支撑续跑
            if save_path:
                self._save_state(save_path, messages, iterations, step_results, files_created,
                                 final_prompt, session_mode)
            # debug 轨迹：run 收尾，把完整上下文（含 messages）单独 dump 到 .ctx.json
            if self.tracer and self.tracer.enabled():
                self.tracer.dump_context(
                    messages=messages,
                    step_results=step_results,
                    files_created=files_created,
                    skill_name=skill.metadata.name,
                    iterations=iterations,
                    # stopped_by 从 _trace_finish 采集（所有 RunResult 退出路径都经过它）。
                    # 不能引用局部变量 result：主循环里的工具输出会把 result 覆盖，
                    # 正常完成路径（不经过 max_iterations 赋值）下会 AttributeError。
                    stopped_by=getattr(self, "_dbg_stopped_by", None) or "unknown",
                )
        return result

    def _save_state(self, path, messages, iterations, step_results, files_created,
                    final_prompt, session_mode: bool = False):
        """将运行状态落盘为 append-only JSONL。

        每次调用追加一行完整快照（type=snapshot）。JSONL 天然防半写损坏：
        崩溃/中断只损失最后一行，之前的历史快照仍可续跑；加载侧取最后一行。
        旧版单 JSON 整文件覆写（一次崩溃即全损）自动迁移兼容。

        session_mode 一并落盘：session 产生的历史含 ask_user 交互与多轮用户指令，
        若被普通 run --resume-from 载入，行为语义不同（无 ask_user 工具、
        无轮边界交还），载入侧据此给出提示。
        """
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "type": "snapshot",
                "final_prompt": final_prompt,
                "messages": messages,
                "iterations": iterations,
                "step_results": step_results,
                "files_created": files_created,
                "session_mode": session_mode,
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False) + "\n")
        except Exception:
            # 状态持久化失败绝不影响主执行流程
            pass

    def _load_state(self, path):
        """读取状态文件；不存在或损坏返回 None。

        JSONL（append-only）：取最后一行的完整快照；兼容旧版单 JSON 对象。
        """
        try:
            p = Path(path)
            if not p.exists():
                return None
            text = p.read_text(encoding="utf-8")
            if "\n" in text.strip():
                last_line = [ln for ln in text.splitlines() if ln.strip()][-1]
                return json.loads(last_line)
            return json.loads(text)
        except Exception:
            return None
