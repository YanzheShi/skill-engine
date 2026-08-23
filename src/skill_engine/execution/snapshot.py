"""通用文件快照（检查点）能力 —— 引擎核心，不绑定 git。

在每次修改文件前自动记录其"进入本次运行前的原始内容"到
<base_dir>/.skill-engine/snapshots/，供回滚使用（统一收口到 .skill-engine/ 子目录，避免污染项目目录）。

设计纪律（见 docs/large-code-capability-design.md §1）：
- 这是**通用**能力，沉引擎核心，不依赖 git，任何 skill 写文件都能受益。
- 与 code-builder 的 cb_git_checkpoint（skill 层、依赖 git）互补，而非重复。
"""

import hashlib
import json
from pathlib import Path

from skill_engine.execution.paths import runtime_dir


class FileSnapshot:
    """记录文件进入本次运行前的原始内容，支持按需回滚。

    特性：
    - record() 对同一文件只记录第一次（即"进入前状态"），后续编辑不覆盖检查点。
    - 快照落盘到 .skill-engine/snapshots/ 并维护 manifest.json，跨进程/续跑可见。
    - 任何异常都被吞掉，快照失败绝不影响主执行流程。
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.dir = runtime_dir(self.base_dir) / "snapshots"
        self.manifest: dict[str, str] = {}   # 绝对路径 -> .bak 文件名
        self._recorded: set[str] = set()     # 已记录路径（仅首次）
        self._load_manifest()

    def _bak_name(self, path: str) -> str:
        key = Path(path).resolve().as_posix()
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return h + ".bak"

    def record(self, path, original_content: str):
        """记录文件进入本次运行前的原始内容（同一文件仅首次有效）。"""
        resolved = str(Path(path).resolve())
        if resolved in self._recorded:
            return
        self._recorded.add(resolved)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / self._bak_name(resolved)).write_text(
                original_content, encoding="utf-8"
            )
            self.manifest[resolved] = self._bak_name(resolved)
            self._save_manifest()
        except Exception:
            # 快照失败不应阻断主流程
            pass

    def restore(self, path) -> tuple[bool, str]:
        """将文件恢复到记录前的快照状态。返回 (成功?, 提示信息)。"""
        resolved = str(Path(path).resolve())
        bak_name = self.manifest.get(resolved)
        if not bak_name:
            # 尝试用传入路径直接找（兼容路径写法差异）
            alt = str(Path(path).resolve())
            bak_name = self.manifest.get(alt)
        if not bak_name:
            return False, f"无快照: {path}（可能该文件本次运行未被修改，或为新文件）"
        bak = self.dir / bak_name
        if not bak.exists():
            return False, f"快照文件缺失: {bak}"
        content = bak.read_text(encoding="utf-8")
        Path(path).write_text(content, encoding="utf-8")
        return True, f"已回滚: {path}"

    def _load_manifest(self):
        m = self.dir / "manifest.json"
        if m.exists():
            try:
                self.manifest = json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                self.manifest = {}

    def _save_manifest(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "manifest.json").write_text(
                json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
