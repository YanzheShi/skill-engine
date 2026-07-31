"""人机交互抽象层

HumanIO ABC：抽象人的输入输出，让 Runner 不依赖 CLI/Web 具体实现。

CliHumanIO：CLI 实现，用于 skill-engine 命令行模式。
WebHumanIO：预留，V0.2 给 code-tutor React 用。
"""

from abc import ABC, abstractmethod
import sys

try:
    from prompt_toolkit import PromptSession
    _HAS_PROMPT_TOOLKIT = True
except Exception:  # pragma: no cover - 无 prompt_toolkit 环境回退 input()
    _HAS_PROMPT_TOOLKIT = False

from skill_engine.execution.paste_buffer import save_paste


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
        print(f"\n[AI] {text}")

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
