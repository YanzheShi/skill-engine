"""包级入口，支持 `python -m skill_engine ...` 免安装调试。

与 console_scripts 入口 `skill-engine`（pyproject [project.scripts] 指向
skill_engine.cli:app）等价，仅调用路径不同。
"""

from skill_engine.cli import main

if __name__ == "__main__":
    main()
