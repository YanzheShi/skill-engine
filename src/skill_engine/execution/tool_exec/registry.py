"""BUILTIN_HANDLERS：内建工具注册表（switch-on-type → 分发表）。

加第 15 个工具 = 新建 ``handlers/xxx.py`` + 这里加一行，不再动 run()。
"""

from skill_engine.execution.tool_exec.handlers.ask_user import AskUserHandler
from skill_engine.execution.tool_exec.handlers.bash import BashHandler
from skill_engine.execution.tool_exec.handlers.edit_file import EditFileHandler
from skill_engine.execution.tool_exec.handlers.get_current_time import GetCurrentTimeHandler
from skill_engine.execution.tool_exec.handlers.query_db import QueryDbHandler
from skill_engine.execution.tool_exec.handlers.read_file import ReadFileHandler
from skill_engine.execution.tool_exec.handlers.restore_file import RestoreFileHandler
from skill_engine.execution.tool_exec.handlers.run_python import RunPythonHandler
from skill_engine.execution.tool_exec.handlers.search_files import SearchFilesHandler
from skill_engine.execution.tool_exec.handlers.shot_web import ShotWebHandler
from skill_engine.execution.tool_exec.handlers.stop import StopHandler
from skill_engine.execution.tool_exec.handlers.update_plan import UpdatePlanHandler
from skill_engine.execution.tool_exec.handlers.view_image import ViewImageHandler
from skill_engine.execution.tool_exec.handlers.web_search import WebSearchHandler
from skill_engine.execution.tool_exec.handlers.write_file import WriteFileHandler


def build_builtin_handlers(session_mode: bool = False) -> dict:
    """装配内建工具分发表：{工具名: handler 实例}。

    Args:
        session_mode: True 时额外注册 ask_user（session 模式专属）。
    """
    handlers = {
        h.name: h for h in (
            BashHandler(),
            ReadFileHandler(),
            ViewImageHandler(),
            WriteFileHandler(),
            EditFileHandler(),
            SearchFilesHandler(),
            UpdatePlanHandler(),
            QueryDbHandler(),
            RunPythonHandler(),
            StopHandler(),
            WebSearchHandler(),
            GetCurrentTimeHandler(),
            ShotWebHandler(),
            RestoreFileHandler(),
        )
    }
    if session_mode:
        handlers[AskUserHandler.name] = AskUserHandler()
    return handlers
