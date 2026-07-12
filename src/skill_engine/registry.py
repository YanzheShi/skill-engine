"""
Registry — Skill 注册表

职责：
- 接 discovery.index，按需加载 Skill
- 缓存已加载的 Skill（避免重复读文件）
- 懒 body：只有需要时才读 body
- 提供 list_names / list_active / info / load_skill 接口
"""

from pathlib import Path
from typing import Optional
from .models import Skill, SkillMetadata, SkillMeta
from .discovery import _parse_frontmatter


class Registry:
    """Skill 注册表

    使用方式：
    >>> index = discover()
    >>> registry = Registry(index)
    >>> skill = registry.load_skill("leetcode-solution-writer")
    >>> print(skill.body)  # 懒加载
    """

    def __init__(self, index: dict[str, SkillMeta]):
        """初始化注册表

        Args:
            index: discovery 返回的索引 {name: SkillMeta}
        """
        self.index = index
        self._cache: dict[str, Skill] = {}

    def list_names(self) -> list[str]:
        """列出所有 skill 名称（不受 state 过滤）

        Returns:
            所有 skill 名称列表
        """
        return list(self.index.keys())

    def list_active(self) -> list[str]:
        """列出 state != 'off' 的 skill 名称

        Returns:
            活跃 skill 名称列表
        """
        return [
            name for name, meta in self.index.items()
            if meta.state != "off"
        ]

    def info(self, name: str) -> Optional[SkillMeta]:
        """获取 skill 的元数据（不加载 body）

        Args:
            name: skill 名称

        Returns:
            SkillMeta 对象，不存在返回 None
        """
        return self.index.get(name)

    def info_full(self, name: str) -> Optional[dict]:
        """获取 skill 的完整 metadata（仅解析 frontmatter，不读 body）

        比 load_skill 轻量，适合 catalog 构建等场景。

        Args:
            name: skill 名称

        Returns:
            dict 包含 name, description, when_to_use, argument_hint, effort 等
        """
        meta = self.index.get(name)
        if not meta:
            return None

        skill_path = Path(meta.directory) / "SKILL.md"
        if not skill_path.exists():
            return None

        content = skill_path.read_text(encoding="utf-8")
        fm_dict, _ = _parse_frontmatter(content)
        fm_dict["directory"] = meta.directory
        fm_dict["state"] = meta.state
        return fm_dict

    def get_groups(self) -> dict[str, list[str]]:
        """按 groups 字段分组 skill

        返回 {group_name: [skill_name, ...]}，未分组的 skill 归入 "__ungrouped__"。

        使用方式：
        >>> groups = registry.get_groups()
        >>> for name, skills in groups.items():
        ...     print(f"{name}: {skills}")

        Returns:
            分组映射表
        """
        groups: dict[str, list[str]] = {}
        for name in self.list_active():
            fm = self.info_full(name)
            if not fm:
                continue
            fm_groups = fm.get("groups", [])
            if fm_groups:
                for g in fm_groups:
                    groups.setdefault(g, []).append(name)
            else:
                groups.setdefault("__ungrouped__", []).append(name)
        return groups

    def load_skill(self, name: str) -> Optional[Skill]:
        """加载完整的 Skill 对象（含 body）

        如果已缓存，直接返回；否则从磁盘加载。

        Args:
            name: skill 名称

        Returns:
            Skill 对象，不存在或 state=off 返回 None
        """
        if name in self._cache:
            return self._cache[name]

        meta = self.index.get(name)
        if not meta:
            return None

        # 检查 state
        if meta.state == "off":
            return None

        skill_path = Path(meta.directory) / "SKILL.md"
        if not skill_path.exists():
            return None

        content = skill_path.read_text(encoding="utf-8")
        fm_dict, body = _parse_frontmatter(content)

        # 查找支持文件
        supporting_files = []
        for f in skill_path.parent.iterdir():
            if f.is_file() and f.name != "SKILL.md":
                supporting_files.append(str(f))

        skill = Skill(
            metadata=SkillMetadata(**fm_dict),
            body=body,
            directory=meta.directory,
            supporting_files=supporting_files,
        )

        self._cache[name] = skill
        return skill

    def invalidate(self, name: str) -> None:
        """使指定 skill 的缓存失效

        Args:
            name: skill 名称
        """
        self._cache.pop(name, None)

    def clear_cache(self) -> None:
        """清空所有缓存"""
        self._cache.clear()
