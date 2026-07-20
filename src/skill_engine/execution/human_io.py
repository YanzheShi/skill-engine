"""人机交互抽象层

HumanIO ABC：抽象人的输入输出，让 Runner 不依赖 CLI/Web 具体实现。

CliHumanIO：CLI 实现，用于 skill-engine 命令行模式。
WebHumanIO：预留，V0.2 给 code-tutor React 用。
"""

from abc import ABC, abstractmethod


class HumanIO(ABC):
    """人机交互抽象层

    emit: 输出 LLM 的文本给用户（CLI→print, Web→SSE push）
    read: 读取用户的输入（CLI→input, Web→await SSE resume）
    """

    @abstractmethod
    def emit(self, text: str) -> None:
        ...

    @abstractmethod
    def read(self) -> str:
        ...


class CliHumanIO(HumanIO):
    """CLI 实现：print + input"""

    def emit(self, text: str):
        print(f"\n[AI] {text}")

    def read(self) -> str:
        try:
            return input("[你] ").strip()
        except (EOFError, KeyboardInterrupt):
            return "/exit"