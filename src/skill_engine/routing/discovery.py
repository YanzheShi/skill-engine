"""
Discovery — 扫描多根目录，建立 skill 索引

职责：
- 扫描自定义根目录（roots 参数）
- 可选：扫描 ~/.agents/skills/、~/.claude/skills/、~/.skill-engine/skills/ 等外部路径
- 建立 {name: SkillMeta} 索引
- 应用 priority 覆盖（高 priority 覆盖低 priority）
- 应用 skillOverrides 状态

默认只加载 roots 指定的项目级技能。
通过 extend_skills=True 额外加载外部技能。

扫描顺序（优先级从低到高，同优先级后者覆盖前者）：
0. 用户级: ~/.agents/skills/          (priority=10)
1. 用户级: ~/.claude/skills/          (priority=10)
2. 用户级: ~/.skill-engine/skills/    (priority=10)
3. 项目级: .claude/skills/            (priority=20)
4. 项目级: .skill-engine/skills/      (priority=20)
5. 自定义根: roots 参数传入            (priority=30)
"""

from pathlib import Path
from typing import Optional
import yaml
import re
import importlib.resources as _resources
from skill_engine.models import SkillMeta

# ----- 来源标注（安全设计 v2，第 0 层）-----

_SOURCES_CONFIG_PATH = Path.home() / ".skill-engine" / "sources.toml"
_ALLOWED_ORIGINS: Optional[set[str]] = None


def _load_allowed_origins() -> set[str]:
    """加载 sources.toml 中的 allowed_origins"""
    global _ALLOWED_ORIGINS
    if _ALLOWED_ORIGINS is not None:
        return _ALLOWED_ORIGINS
    _ALLOWED_ORIGINS = set()
    try:
        if _SOURCES_CONFIG_PATH.exists():
            import tomllib
            data = tomllib.loads(_SOURCES_CONFIG_PATH.read_text(encoding="utf-8"))
            origins = data.get("allowed_origins", {}).get("origins", [])
            _ALLOWED_ORIGINS = set(origins)
    except Exception:
        pass
    return _ALLOWED_ORIGINS


def _get_git_remote(skill_dir: Path) -> Optional[str]:
    """从 skill 目录的 .git/config 中读取 remote origin URL"""
    git_config = skill_dir / ".git" / "config"
    if not git_config.exists():
        return None
    try:
        text = git_config.read_text(encoding="utf-8")
        match = re.search(r'\[remote\s+"origin"\]\s*\n(?:\s*url\s*=\s*(\S+))', text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _origin_matches(remote: str, allowed: str) -> bool:
    """检查 remote URL 是否匹配 allowed origin 模式"""
    if allowed.startswith("github:"):
        owner_repo = allowed[7:]
        return f"github.com/{owner_repo}" in remote
    return remote == allowed


def _tag_trust(skill_dir: Path) -> Optional[str]:
    """根据来源标注 trust_tag：trusted / untrusted"""
    remote = _get_git_remote(skill_dir)
    if remote is None:
        return "trusted"  # 非 git 目录（本地创建），视为可信
    allowed = _load_allowed_origins()
    if not allowed:
        return "untrusted"  # 未配 allowed_origins → 全标 untrusted
    for a in allowed:
        if _origin_matches(remote, a):
            return "trusted"  # 远程 URL 在白名单内
    print(f"  [untrusted] remote={remote} 不在 sources.toml allowed_origins 中")
    return "untrusted"


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
            trust_tag = _tag_trust(item) if priority == 10 else "trusted"
            index[name] = SkillMeta(
                name=name,
                description=description,
                directory=str(item),
                priority=priority,
                trust_tag=trust_tag,
            )
    return index


def discover(
    roots: Optional[list[Path]] = None,
    overrides: Optional[dict[str, str]] = None,
    skip_defaults: bool = False,
    extend_skills: bool = False,
    include_builtin: bool = False,
) -> dict[str, SkillMeta]:
    """扫描多根目录，建立 skill 索引。

    同名 skill 的处理：
    - 高 priority 覆盖低 priority
    - overrides 可以强制指定 state

    Args:
        roots: 额外的扫描根目录
        overrides: {skill_name: state} 覆盖配置
        skip_defaults: 兼容参数（已弃用），等价于 extend_skills=False
        extend_skills: 如果为 True，额外加载 ~/.agents/skills/、~/.claude/skills/、
                       ~/.skill-engine/skills/ 等外部技能路径。
                       默认 False，只加载 roots 指定的项目级技能。

    Returns:
        {skill_name: SkillMeta} 索引
    """
    overrides = overrides or {}
    all_index: dict[str, SkillMeta] = {}

    # 0. 内置 skills（引擎自带，优先级最低=5，用户/项目级可覆盖）
    if include_builtin:
        builtin_dir = _builtin_skills_dir()
        if builtin_dir:
            builtin_index = _discover_skill_dir(builtin_dir, priority=5)
            for name, meta in builtin_index.items():
                meta.state = overrides.get(name, "on")
                if name not in all_index:       # 最低优先级，不覆盖已有
                    all_index[name] = meta

    # 是否加载外部 skill（兼容旧参数 skip_defaults）
    load_external = extend_skills and not skip_defaults

    if load_external:
        # 0. 用户级 — ~/.agents/skills/ 标准路径 (priority=10)
        agent_user_dir = Path.home() / ".agents" / "skills"
        agent_user_index = _discover_skill_dir(agent_user_dir, priority=10)
        for name, meta in agent_user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

        # 1. 用户级 — Claude Code 标准路径 (priority=10)
        cc_user_dir = Path.home() / ".claude" / "skills"
        cc_user_index = _discover_skill_dir(cc_user_dir, priority=10)
        for name, meta in cc_user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

        # 2. 用户级 — skill-engine 路径 (priority=10)
        user_dir = Path.home() / ".skill-engine" / "skills"
        user_index = _discover_skill_dir(user_dir, priority=10)
        for name, meta in user_index.items():
            meta.state = overrides.get(name, "on")
            if name not in all_index:
                all_index[name] = meta

        # 3. 项目级 — Claude Code 标准路径 (priority=20)
        cc_project_dir = Path.cwd() / ".claude" / "skills"
        cc_project_index = _discover_skill_dir(cc_project_dir, priority=20)
        for name, meta in cc_project_index.items():
            meta.state = overrides.get(name, "on")
            if name in all_index and meta.priority > all_index[name].priority:
                all_index[name] = meta
            elif name not in all_index:
                all_index[name] = meta

        # 4. 项目级 — skill-engine 路径 (priority=20)
        project_dir = Path.cwd() / ".skill-engine" / "skills"
        project_index = _discover_skill_dir(project_dir, priority=20)
        for name, meta in project_index.items():
            meta.state = overrides.get(name, "on")
            if name in all_index and meta.priority > all_index[name].priority:
                all_index[name] = meta
            elif name not in all_index:
                all_index[name] = meta

    # 5. 自定义根 (priority=30)
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

    # 应用 overrides 的 state 覆盖
    for name, state in overrides.items():
        if name in all_index:
            all_index[name].state = state

    return all_index


def _builtin_skills_dir() -> Optional[Path]:
    """引擎自带 skills 目录（统一来源，开发态与安装态通用）。

    定位优先级：
      1. 安装态：skills 经 pyproject 的 force-include 打进
         ``site-packages/skill_engine/skills``，用 importlib.resources 解析；
      2. 开发态：包内无 skills 子目录时，回退到仓库根 ``skills/``
         （基于本文件位置向上回溯：src/skill_engine/routing/discovery.py
          → src/skill_engine → src → 仓库根）。
    返回 None 表示均不可用。
    """
    # 1. 安装态：包内 skills（importlib.resources 解析真实路径）
    try:
        ref = _resources.files("skill_engine").joinpath("skills")
        with _resources.as_file(ref) as p:
            if p.is_dir():
                return p
    except Exception:
        pass
    # 2. 开发态：仓库根 skills/（包内无 skills 子目录时回退）
    #    discovery.py 位于 <repo>/src/skill_engine/routing/，
    #    向上 3 层到达仓库根：routing → skill_engine → src → <repo>
    try:
        routing_dir = Path(__file__).resolve().parent      # .../src/skill_engine/routing
        repo_skills = routing_dir.parent.parent.parent / "skills"  # 仓库根/skills
        if repo_skills.is_dir():
            return repo_skills
    except Exception:
        pass
    return None


def discover_skills(
    working_root: Optional[str] = None,
    extra_roots: Optional[list] = None,
    extend_skills: bool = True,
    include_builtin: bool = True,
) -> dict[str, SkillMeta]:
    """统一 skill 索引入口（替代各处重复的 CWD/skills + discover 调用）。

    优先级（高 → 低）：
      - 命令行 roots（CWD/skills、working_root/skills、extra_roots）  priority=30
      - 项目级（.skill-engine/skills、.claude/skills）                priority=20
      - 用户级（~/.skill-engine/skills、~/.claude/skills、~/.agents/skills） priority=10
      - 内置 skills（引擎自带）                                      priority=5（最低）

    即：用户/项目级 skills 可覆盖同名内置 skill；命令行显式目录优先级最高。

    Args:
        working_root: -w 指定的目标项目目录（其 skills/ 子目录会被扫描）。
        extra_roots: 额外扫描根（如 scan 命令的 --root）。
        extend_skills: 是否扫描用户级/项目级外部路径（默认 True）。
        include_builtin: 是否包含引擎内置 skills（默认 True）。
    """
    roots: list = []
    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.is_dir():
        roots.append(cwd_skills)
    if working_root:
        wr_skills = Path(working_root) / "skills"
        if wr_skills.is_dir():
            roots.append(wr_skills)
    if extra_roots:
        for r in extra_roots:
            rp = Path(r)
            if rp.is_dir():
                roots.append(rp)
    return discover(
        roots=roots or None,
        extend_skills=extend_skills,
        include_builtin=include_builtin,
    )