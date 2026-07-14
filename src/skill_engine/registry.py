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
import yaml
from .models import Skill, SkillMetadata, SkillMeta, MergedMeta
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
        self._meta_cache: dict[str, MergedMeta] = {}
        self._meta_mtime: dict[str, float] = {}

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
        self._meta_cache.clear()
        self._meta_mtime.clear()

    def load_meta(self, name: str) -> Optional[MergedMeta]:
        """三层合并加载 skill 元数据（SKILL.md + .skill-meta.yaml + .skill-local.yaml）

        缓存 key=name，失效盯 .skill-local.yaml 的 mtime。
        .skill-meta.yaml 的 source_hash 由 Preprocessor 保证。

        Args:
            name: skill 名称

        Returns:
            MergedMeta 对象，不存在返回 None
        """
        skill = self.load_skill(name)
        if not skill:
            return None

        skill_dir = Path(skill.directory)
        meta_path = skill_dir / ".skill-meta.yaml"
        local_path = skill_dir / ".skill-local.yaml"

        # 失效检测
        local_mtime = local_path.stat().st_mtime if local_path.exists() else 0.0
        if name in self._meta_cache:
            if self._meta_mtime.get(name, -1.0) == local_mtime:
                return self._meta_cache[name]  # 命中，连文件都不读

        # 没命中或失效 → 合并
        merged = self._merge_meta(skill, meta_path, local_path)
        self._meta_cache[name] = merged
        self._meta_mtime[name] = local_mtime
        return merged

    def _merge_meta(
        self,
        skill: Skill,
        meta_path: Path,
        local_path: Path,
    ) -> MergedMeta:
        """三层合并：SKILL.md < .skill-meta.yaml < .skill-local.yaml"""
        # 第一层：SKILL.md frontmatter
        base = dict(skill.metadata)

        # 第二层：.skill-meta.yaml（LLM 抽取）
        meta_cache = {}
        if meta_path.exists():
            try:
                meta_data = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                meta_cache = meta_data
                # 非冲突字段 merge
                for k in ("intention", "synonyms", "purpose", "keywords"):
                    if k in meta_data:
                        base[k] = meta_data[k]
            except (yaml.YAMLError, OSError):
                pass

        # 第三层：.skill-local.yaml（用户覆写，优先级最高）
        if local_path.exists():
            try:
                local_data = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
                # 覆写 alias / shortcuts / intent_verbs 等
                for k in ("alias", "shortcuts", "intent_verbs",
                          "intention", "synonyms", "purpose"):
                    if k in local_data:
                        base[k] = local_data[k]
                # router_proper_en_append 特殊处理（追加到 meta_cache）
                if "router_proper_en_append" in local_data:
                    meta_cache.setdefault("router_proper_en_append", [])
                    meta_cache["router_proper_en_append"].extend(
                        local_data["router_proper_en_append"]
                    )
            except (yaml.YAMLError, OSError):
                pass

        return MergedMeta(**base, meta_cache=meta_cache)
