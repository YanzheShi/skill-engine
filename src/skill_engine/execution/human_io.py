"""人机交互抽象层

HumanIO ABC：抽象人的输入输出，让 Runner 不依赖 CLI/Web 具体实现。

CliHumanIO：CLI 实现，用于 skill-engine 命令行模式。
WebHumanIO：预留，V0.2 给 code-tutor React 用。
"""

from abc import ABC, abstractmethod
import re
import sys

try:
    from prompt_toolkit import PromptSession
    _HAS_PROMPT_TOOLKIT = True
except Exception:  # pragma: no cover - 无 prompt_toolkit 环境回退 input()
    _HAS_PROMPT_TOOLKIT = False

from skill_engine.execution.paste_buffer import save_paste


def strip_markdown(text: str) -> str:
    """把 Markdown 转换为纯文本（CLI 纯文本终端输出兜底）。

    处理：标题、粗体、行内代码、引用、表格、列表标记；围栏代码块内容保留
    （仅去掉 ``` 围栏），避免块内 ``#`` 注释等被误伤。无论模型吐不吐 Markdown，
    终端一律拿到可读纯文本。
    """
    if not text:
        return text
    # 1. 先抽离围栏代码块（保护块内 # 注释 / 特殊符号）
    blocks: list[str] = []

    def _extract(m):
        blocks.append(m.group(1))
        return f"\x00CB{len(blocks) - 1}\x00"

    text = re.sub(r"```[^\n]*\n(.*?)```", _extract, text, flags=re.DOTALL)
    # 2. 行内代码 `x` → x
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # 3. 标题 # → 去
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    # 4. 粗体 **x** / __x__
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # 5. 引用 >
    text = re.sub(r"(?m)^>\s?", "", text)
    # 6. 表格分隔行 |---|
    text = re.sub(r"(?m)^\s*\|?[\s:\-]+\|?\s*$", "", text)
    # 7. 表格竖线 → 空格
    text = re.sub(r"(?m)\|", " ", text)
    # 8. 列表 - / * / + 前缀去掉（保留缩进文字）
    text = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1", text)
    # 9. 还原代码块：去围栏，每行前置两空格
    def _restore(m):
        idx = int(m.group(1))
        return "\n".join("  " + ln for ln in blocks[idx].splitlines())

    text = re.sub(r"\x00CB(\d+)\x00", _restore, text)
    # 10. 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class HumanIO(ABC):
    """人机交互抽象层

    emit: 输出 LLM 的文本给用户（CLI→print, Web→SSE push）
    read: 读取用户的输入（CLI→input/prompt_toolkit, Web→await SSE resume）
    """

    @abstractmethod
    def emit(self, text: str) -> None:
        ...

    @abstractmethod
    def read(self, prompt: str = None) -> str:
        ...

    # ---- 用户态执行轨迹（语义化输出，CLI/Web 共用）----
    # 默认实现为纯文本 print；CliHumanIO 重写为带 ANSI 着色版本。
    # 预留的 WebHumanIO 可重写为 SSE push，不影响现有逻辑。

    def set_verbose(self, verbose: bool) -> None:
        """设置是否显示引擎内部调试态（迭代/历史条数/LLM 响应）。"""
        pass

    def set_plain_text(self, plain_text: bool) -> None:
        """CLI 纯文本终端：输出时剥离 Markdown 语法（默认不剥离，Web 端不受影响）。"""
        pass

    def emit_header(self, title: str) -> None:
        """执行开始的总标题，如 '# Running in <skill> @ <root>'。"""
        print(f"\n{title}")

    def emit_thinking(self, text: str, max_lines: int = 40) -> None:
        """模型的推理/思考文本（每轮 content）——独立视觉通道。

        与工具执行/命令/结果区分：每行带 💭 标记，前后留空行，
        避免和工具行的 🔧 输出混淆（CliHumanIO 另加品红斜体着色）。
        超长思考按行截断（默认 40 行），避免刷屏；完整内容仍留在模型上下文用于决策。
        """
        if not text:
            return
        if getattr(self, "plain_text", False):
            text = strip_markdown(text)
        lines = text.splitlines()
        shown = []
        for i, line in enumerate(lines[:max_lines]):
            shown.append(f"💭 {line}")
        if len(lines) > max_lines:
            shown.append(
                f"💭 ...(思考过程还有 {len(lines) - max_lines} 行未显示，"
                f"完整内容已用于决策)"
            )
        print("\n" + "\n".join(shown) + "\n")

    def emit_command(self, cmd: str) -> None:
        """执行的一条 shell 命令，渲染为 $ cmd。"""
        print(f"\n$ {cmd}")

    def emit_result(self, out: str, max_lines: int = 2) -> None:
        """工具/shell 的真实输出（替代原先只打 'N chars' 的噪声）。

        CLI 用户态展示：超长输出按行截断（默认仅保留 2 行），避免刷屏
        （模型侧 messages 仍保留全文）。
        """
        if not out:
            return
        lines = out.splitlines()
        if len(lines) > max_lines:
            for line in lines[:max_lines]:
                print(f"  {line}")
            print(f"  ...(还有 {len(lines) - max_lines} 行未显示，共 {len(lines)} 行)")
        else:
            for line in lines:
                print(f"  {line}")

    def emit_tool(self, label: str, detail: str = "") -> None:
        """一次工具调用的摘要（类型 + 参数），人类可读（替代 '- type: input'）。"""
        if detail:
            print(f"  🔧 {label}  {detail}")
        else:
            print(f"  🔧 {label}")


class CliHumanIO(HumanIO):
    """CLI 实现：print + 带 bracketed paste 的 prompt_toolkit 输入。

    - 交互 TTY：用 ``PromptSession``（prompt_toolkit 3.x 默认启用 bracketed
      paste），整段粘贴被识别为一条输入（不被终端按换行拆成多条命令）；
      大段内容再经 ``save_paste`` 外置成引用 token，避免撑爆 agent 的 prompt。
    - 粘贴落盘前不显示原文：在 ``on_text_changed`` 回调中检测大段内容，立即
      落盘并将 buffer 替换为紧凑占位符，用户终端不会滚屏。
    - 非 TTY（测试 / 管道）：回退内置 ``input()``，保证可运行。
    """

    def __init__(self, paste_dir=None):
        self._session = None
        self._paste_dir = paste_dir
        self._suppress_change = False
        # 非 TTY（管道/测试/CI）不输出 ANSI 转义，避免乱码；Web 端同理
        self._color = sys.stdout.isatty()
        self.verbose = False
        self.plain_text = False  # CLI 纯文本终端：输出时剥离 Markdown 语法

    def _get_session(self):
        if self._session is None and _HAS_PROMPT_TOOLKIT and sys.stdin.isatty():
            try:
                # prompt_toolkit 3.x 默认启用 bracketed paste（终端支持时），
                # 整段粘贴会被识别为一条输入，无需额外参数。
                self._session = PromptSession()
            except Exception:
                self._session = None
        return self._session

    def _on_buffer_changed(self, buf):
        """检测 buffer 中出现大段粘贴，立即落盘并替换为占位符。

        当 prompt_toolkit 的 buffer 文本突变（超过阈值），说明用户粘贴了
        大段内容。此时将原文写入临时文件，把 buffer 内容替换成紧凑的
        [Pasted text #N: X lines → path] 占位符，避免终端滚屏。
        """
        if self._suppress_change:
            return
        text = buf.text
        if not text:
            return
        # 不拦截元命令（/exit, :load, :paste 等）
        stripped = text.strip()
        if stripped.startswith('/') or stripped.startswith(':'):
            return

        token = save_paste(text, base=self._paste_dir)
        if token:
            print(f"[粘贴已暂存] {token}")
            self._suppress_change = True
            buf.text = token
            buf.cursor_position = len(token)
            self._suppress_change = False

    def emit(self, text: str):
        if self.plain_text:
            text = strip_markdown(text)
        print(f"\n[AI] {text}")

    # ---- 用户态执行轨迹：CLI 带 ANSI 着色实现 ----
    def _c(self, code: str) -> str:
        return code if self._color else ""

    def set_verbose(self, verbose: bool) -> None:
        self.verbose = bool(verbose)

    def set_plain_text(self, plain_text: bool) -> None:
        self.plain_text = bool(plain_text)

    def emit_header(self, title: str) -> None:
        R, C = self._c("\033[0m"), self._c("\033[36m")  # 青色标题
        print(f"\n{C}# {title}{R}")

    def emit_thinking(self, text: str, max_lines: int = 40) -> None:
        if not text:
            return
        if self.plain_text:
            text = strip_markdown(text)
        R, T = self._c("\033[0m"), self._c("\033[35m\033[3m")  # 品红斜体：思考专属
        lines = text.splitlines()
        shown = []
        for i, line in enumerate(lines[:max_lines]):
            shown.append(f"💭 {line}")
        if len(lines) > max_lines:
            shown.append(
                f"💭 ...(思考过程还有 {len(lines) - max_lines} 行未显示，"
                f"完整内容已用于决策)"
            )
        print("\n" + "\n".join(f"{T}{ln}{R}" for ln in shown) + "\n")

    def emit_command(self, cmd: str) -> None:
        R, G = self._c("\033[0m"), self._c("\033[32m")  # 绿色命令
        print(f"\n{G}$ {cmd}{R}")

    def emit_result(self, out: str, max_lines: int = 2) -> None:
        if not out:
            return
        lines = out.splitlines()
        R, D = self._c("\033[0m"), self._c("\033[2m")  # 暗色输出
        if len(lines) > max_lines:
            for line in lines[:max_lines]:
                print(f"  {D}{line}{R}")
            print(f"  {D}...(还有 {len(lines) - max_lines} 行未显示，共 {len(lines)} 行){R}")
        else:
            for line in lines:
                print(f"  {D}{line}{R}")

    def emit_tool(self, label: str, detail: str = "") -> None:
        R, Y = self._c("\033[0m"), self._c("\033[33m")  # 黄色工具
        if detail:
            print(f"  {Y}🔧 {label}{R}  {detail}")
        else:
            print(f"  {Y}🔧 {label}{R}")

    def input_mode(self) -> str:
        """返回当前输入模式，供启动诊断显示（精准区分回退原因）。"""
        if not _HAS_PROMPT_TOOLKIT:
            return "标准 input()（未安装 prompt_toolkit；请 pip install prompt_toolkit，或改用 :paste / :load）"
        if self._get_session() is not None:
            return "prompt_toolkit（bracketed paste 已启用，粘贴多行自动成一条）"
        if not sys.stdin.isatty():
            return "标准 input()（stdin 非真实终端，粘贴多行会被按行拆分；请用 :paste 或 :load）"
        return "标准 input()（PromptSession 构造失败；请用 :paste 或 :load）"

    def read(self, prompt: str = None) -> str:
        p = prompt if prompt else "[你] "
        session = self._get_session()
        try:
            if session is not None:
                # 在 prompt() 创建 buffer 后挂载 on_text_changed 回调
                # （prompt_toolkit 3.0.53 的 PromptSession.__init__ 不支持
                #  on_text_changed 参数，改用 pre_run 在应用启动时注入）
                if not hasattr(self, '_handler_attached'):
                    self._handler_attached = False

                def _pre_run():
                    if not self._handler_attached:
                        session.default_buffer.on_text_changed += self._on_buffer_changed
                        self._handler_attached = True

                raw = session.prompt(p, pre_run=_pre_run)
            else:
                raw = input(p)
        except (EOFError, KeyboardInterrupt):
            return "/exit"
        raw = (raw or "").strip()
        if not raw:
            return raw
        token = save_paste(raw, base=self._paste_dir)
        if token:
            # 给用户可见反馈（对齐 Hermes：粘贴落盘后展示引用 token）
            print(f"[粘贴已暂存] {token}")
            return token
        return raw
