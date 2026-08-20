"""文件状态跟踪器（FileStateTracker）—— read-before-write 一致性机制。

解决长会话下的"凭记忆编辑"翻车链：上下文压缩把文件原文摘要掉后，
LLM 凭印象发 edit_file，oldText 对不上或模糊匹配错 → 空转甚至错改。
本模块在编辑前校验"该文件是否读过、读后是否被改过"，把 SKILL.md 里
"edit_file 前先 read_file"的软约定变成引擎级机制。

设计纪律（见 docs/code-builder-vs-工业级coding-agent差距与提升设计.md §3.1/§4.3）：
- 通用能力，沉引擎核心，不绑定任何领域；任何写文件的 skill 都受益。
- **默认软约束**：校验不通过只注入提示、不阻断——"不读直接改"的小 skill 不受影响；
  code-builder 等可声明 frontmatter `strict_file_tracking: true` 升级为硬约束（拒绝执行）。
- **bash 执行后保守全失效**（invalidate_all）：命令可能改过任何文件而 tracker 无从得知，
  宁可提示重读，不误信陈旧内容。
- 一切异常吞掉：tracker 失败绝不影响主执行流程（与 FileSnapshot 同一纪律）。

判定基于 mtime_ns + size（比整文件哈希便宜，代码文件足够可靠）：
外部改动哪怕内容相同，最多换来一次无害的重读提示。
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class FileStateTracker:
    """跟踪 session 内每个文件的"已知版本"，供 edit 前置校验使用。

    生命周期：
    - 非 session 运行：每次 ToolDispatchRunner.run() 新建（检查点=本次运行起点）；
    - session 运行：由 SkillSession 持有同一实例跨轮复用（与 FileSnapshot 同级），
      使"读过"的登记跨轮有效。状态不落盘：resume 后从空白起步，
      第一次 edit 会触发提示/拒绝重读——保守且安全。
    """

    def __init__(self, strict: bool = False):
        """
        Args:
            strict: True=硬约束（校验失败拒绝编辑）；False=软约束（仅注入提示）
        """
        self.strict = strict
        # resolved posix 路径 -> {"mtime_ns": int, "size": int}
        self._known: dict[str, dict] = {}
        # read_file 去重缓存：resolved posix 路径 ->
        #   {"mtime_ns": int, "size": int, "total_lines": int, "full_read": bool,
        #    "intervals": [(start, end, content), ...]}
        # 同一会话（MOA 跨轮共享同一实例）内重复读同一区间直接命中，
        # 打断「读-压缩-遗忘-重读」循环；mtime/size 校验保证不读脏数据。
        self._read_cache: dict[str, dict] = {}

    # ---- 内部工具 ----

    @staticmethod
    def _key(path) -> Optional[str]:
        try:
            return Path(path).resolve().as_posix()
        except Exception:
            return None

    @staticmethod
    def _stat(path: Path) -> Optional[dict]:
        try:
            st = path.stat()
            return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
        except OSError:
            return None

    # ---- 登记 ----

    def on_read(self, path) -> None:
        """read_file 成功后登记该文件的当前版本。"""
        try:
            p = Path(path).resolve()
            st = self._stat(p)
            if st is not None:
                self._known[self._key(p)] = st
        except Exception:
            pass

    def on_write(self, path) -> None:
        """write_file / edit_file 写盘后更新登记（自己写的也算已知版本）。"""
        self.on_read(path)
        self.invalidate_read_cache(path)

    def invalidate_all(self) -> None:
        """bash 执行后保守失效全部登记：命令可能改过任何文件，tracker 无从得知。

        代价只是后续的 edit 会收到一次"建议重读"提示（软）或被要求重读（硬），
        换来的是不会基于 bash 之前的陈旧认知做编辑。read 缓存同纪律一并清空。
        """
        self._known.clear()
        self._read_cache.clear()

    def invalidate_paths(self, paths) -> None:
        """bash 执行后按命令中实际出现的路径选择性失效（性能诊断建议 6）。

        只失效命令明确涉及的文件/目录（目录=该目录下所有登记一并失效），
        未涉及的登记保留——消除「每次 bash 后全部文件回到未读」的迭代放大。
        paths 解析失败时调用方应回退 invalidate_all()（保守全失效）。
        """
        try:
            keys: set[str] = set()
            for p in paths:
                rp = Path(p).resolve()
                key = rp.as_posix()
                if rp.is_dir():
                    prefix = key + "/"
                    keys.update(k for k in self._known if k.startswith(prefix))
                    keys.update(k for k in self._read_cache if k.startswith(prefix))
                else:
                    keys.add(key)
            for k in keys:
                self._known.pop(k, None)
                self._read_cache.pop(k, None)
        except Exception:
            pass

    # ---- read_file 去重缓存 ----

    def cache_read(self, path, offset: int, limit: int,
                   total_lines: int, content: str) -> None:
        """登记一次 read_file 的读取区间与内容（带行号版本）。

        同文件重复读取不同区间会合并到同一 entry；文件已变则丢弃旧缓存重开。
        """
        try:
            key = self._key(path)
            st = self._stat(Path(path).resolve())
            if key is None or st is None or total_lines <= 0:
                return
            entry = self._read_cache.get(key)
            if (entry is None
                    or entry["mtime_ns"] != st["mtime_ns"]
                    or entry["size"] != st["size"]):
                entry = {"mtime_ns": st["mtime_ns"], "size": st["size"],
                         "total_lines": total_lines, "full_read": False,
                         "intervals": []}
                self._read_cache[key] = entry
            start = offset
            end = offset + limit if limit else total_lines
            if limit == 0:
                entry["full_read"] = True
            if content:
                entry["intervals"].append((start, end, content))
        except Exception:
            pass

    def cache_lookup(self, path, offset: int, limit: int) -> Optional[dict]:
        """按 (path, offset, limit) 查找已读区间。

        Returns:
            命中: {"content": str, "start": int, "end": int, "full": bool}
            未命中 / 文件已变: None
        """
        try:
            key = self._key(path)
            if key is None:
                return None
            st = self._stat(Path(path).resolve())
            if st is None:
                return None
            entry = self._read_cache.get(key)
            if entry is None:
                return None
            if (entry["mtime_ns"] != st["mtime_ns"]
                    or entry["size"] != st["size"]):
                return None  # 文件已变 → 缓存不可信
            total = entry.get("total_lines") or 0
            start, end = offset, (offset + limit if limit else total)
            if limit == 0:
                if entry["full_read"]:
                    for cs, ce, content in entry["intervals"]:
                        if cs == 0 and (ce >= total or ce == 0):
                            return {"content": content, "start": 0,
                                    "end": total, "full": True}
                return None
            for cs, ce, content in entry["intervals"]:
                if cs <= start and ce >= end:
                    return {"content": content, "start": start,
                            "end": end, "full": False}
            return None
        except Exception:
            return None

    def invalidate_read_cache(self, path) -> None:
        """文件被写入后失效其 read 缓存（下个读会重新读盘）。"""
        try:
            key = self._key(path)
            if key is not None:
                self._read_cache.pop(key, None)
        except Exception:
            pass

    # ---- 校验 ----

    def check_editable(self, path) -> Tuple[bool, str]:
        """编辑前一致性校验。

        Returns:
            (True, "")       已读且未见变化
            (True, 提示)     软约束：未读/疑似已变，注入提示但不阻断
            (False, 错误)    硬约束：拒绝本次编辑，引导 LLM 重读后重试
        """
        key = self._key(path)
        if key is None:
            return True, ""
        try:
            p = Path(path).resolve()
            name = p.name
            recorded = self._known.get(key)

            if recorded is None:
                msg = (f"提示: {name} 未在本次会话中 read_file 读取过"
                       f"（或读取登记已被其后的 bash 执行失效），"
                       f"建议先 read_file 确认最新内容再编辑。")
                if self.strict:
                    return False, (f"[一致性校验未通过] {name} 未在本次会话中读取，"
                                   f"请先用 read_file 读取该文件，再重新提交编辑。")
                return True, msg

            st = self._stat(p)
            if st is None:
                # 文件已不存在（可能被外部删除）——交给 edit_file 自己的
                # 存在性检查报错，不在这里误伤
                return True, ""
            if st["mtime_ns"] != recorded["mtime_ns"] or st["size"] != recorded["size"]:
                msg = (f"提示: {name} 在你上次读取后发生了变化"
                       f"（可能被外部进程或 bash 命令修改），"
                       f"建议先 read_file 重新读取再编辑。")
                if self.strict:
                    return False, (f"[一致性校验未通过] {name} 在上次读取后已变化，"
                                   f"请先用 read_file 重新读取该文件，再重新提交编辑。")
                return True, msg
            return True, ""
        except Exception:
            # tracker 失败绝不阻断主流程
            return True, ""
