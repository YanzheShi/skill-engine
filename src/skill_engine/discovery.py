"""
Discovery — 扫描多根目录，建立 skill 索引

职责：
- 扫描 ~/.skill-engine/skills/（用户级）
- 扫描 .skill-engine/skills/（项目级）
- 扫描自定义根目录（roots 参数）
- 建立 {name: SkillMeta} 索引
- 应用 priority 覆盖（高 priority 覆盖低 priority）
- 应用 skillOverrides 状态

|扫描顺序（优先级从低到高，同优先级后者覆盖前者）：
|0. 用户级: ~/.agents/skills/          (priority=10)
|1. 用户级: ~/.claude/skills/          (priority=10)
|2. 用户级: ~/.skill-engine/skills/    (priority=10)
|3. 项目级: .claude/skills/            (priority=20)
|4. 项目级: .skill-engine/skills/      (priority=20)
|5. 自定义根: roots 参数传入            (priority=30)
"""

from pathlib import Path
from typing import Optional
import yaml
import re
from .models import SkillMeta


# 匹配 YAML frontmatter 的正则
# 格式: ---\nYAML内容\n---\nMarkdown正文
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """解析 SKILL.md，返回 (frontmatter_dict, body_markdown)

    Args:
        content: SKILL.md 的完整内容

    Returns:
        (frontmatter_dict, body_markdown)
    """
    match = _FM_RE.match(content)
    if not match:
        return {}, content
    try:
        data = yaml.safe_load(match.group(1)) or {}
        if not isinstance(data, dict):
            data = {}
    except yaml.YAMLError:
        data = {}
    return data, content[match.end():]


def _discover_skill_dir(dir_path: Path, priority: int = 0) -> dict[str, SkillMeta]:
    """从一个目录发现所有 skill，返回 {name: SkillMeta}

    每个子目录如果包含 SKILL.md，就是一个 skill。

    Args:
        dir_path: 包含 skills 的目录路径
        priority: 优先级

    Returns:
        {skill_name: SkillMeta} 字典
    """
    index: dict[str, SkillMeta] = {}
    if not dir_path.exists() or not dir_path.is_dir():
        return index

    for item in sorted(dir_path.iterdir()):
        if item.is_dir() and (item / "SKILL.md").exists():
            content = (item / "SKILL.md").read_text(encoding="utf-8")
            fm_dict, _ = _parse_frontmatter(content)
            name = fm_dict.get("name", item.name)
            description = fm_dict.get("description", "")
            index[name] = SkillMeta(
                name=name,
                description=description,
                directory=str(item),
                priority=priority,
            )
    return index


def discover(
    roots: Optional[list[Path]] = None,
    overrides: Optional[dict[str, str]] = None,
    skip_defaults: bool = False,
) -> dict[str, SkillMeta]:
    """扫描多根目录，建立 skill 索引。

    同名 skill 的处理：
    - 高 priority 覆盖低 priority
    - overrides 可以强制指定 state

    Args:
        roots: 额外的扫描根目录
        overrides: {skill_name: state} 覆盖配置
        skip_defaults: 如果为 True，跳过默认的 ~/.agents/skills/、~/.claude/skills/、
                      ~/.skill-engine/skills/ 等标准路径（测试隔离用）

    Returns:
        {skill_name: SkillMeta} 索引
    """
    overrides = overrides or {}
    all_index: dict[str, SkillMeta] = {}

    # 0. 用户级 — ~/.agents/skills/ 标准路径 (priority=10)
    if not skip_defaults:
        agent_user_dir = Path.home() / ".agents" / "skills"
        agent_user_index = _discover_skill_dir(agent_user_dir, priority=10)
        for name, meta in agent_user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

    # 1. 用户级 — Claude Code 标准路径 (priority=10)
    if not skip_defaults:
        cc_user_dir = Path.home() / ".claude" / "skills"
        cc_user_index = _discover_skill_dir(cc_user_dir, priority=10)
        for name, meta in cc_user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

    # 2. 用户级 — skill-engine 路径 (priority=10)
    if not skip_defaults:
        user_dir = Path.home() / ".skill-engine" / "skills"
        user_index = _discover_skill_dir(user_dir, priority=10)
        for name, meta in user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

    # 3. 项目级 — Claude Code 标准路径 (priority=20)
    if not skip_defaults:
        cc_project_dir = Path.cwd() / ".claude" / "skills"
        cc_project_index = _discover_skill_dir(cc_project_dir, priority=20)
        for name, meta in cc_project_index.items():
            meta.state = overrides.get(name, "on")
            if name in all_index and meta.priority > all_index[name].priority:
                all_index[name] = meta
            elif name not in all_index:
                all_index[name] = meta

    # 4. 项目级 — skill-engine 路径 (priority=20)
    if not skip_defaults:
        project_dir = Path.cwd() / ".skill-engine" / "skills"
        project_index = _discover_skill_dir(project_dir, priority=20)
        for name, meta in project_index.items():
            meta.state = overrides.get(name, "on")
            if name in all_index and meta.priority > all_index[name].priority:
                all_index[name] = meta
            elif name not in all_index:
                all_index[name] = meta

    # 3. 自定义根 (priority=30)
    if roots:
        for root in roots:
            root_path = Path(root) if isinstance(root, str) else root
            custom_index = _discover_skill_dir(root_path, priority=30)
            for name, meta in custom_index.items():
                meta.state = overrides.get(name, "on")
                if name in all_index and meta.priority > all_index[name].priority:
                    all_index[name] = meta
                elif name not in all_index:
                    all_index[name] = meta

    # 4. 应用 overrides 的 state 覆盖
    for name, state in overrides.items():
        if name in all_index:
            all_index[name].state = state

    return all_index
