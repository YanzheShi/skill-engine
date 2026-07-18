"""
Skills Engine 数据模型

定义 Skill 相关的核心数据结构，使用 Pydantic 做数据验证和序列化。
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Literal
from enum import Enum
from dataclasses import dataclass


class SkillContext(str, Enum):
    """Skill 执行上下文"""
    INLINE = "inline"
    FORK = "fork"


@dataclass
class SkillMeta:
    """轻量级 skill 元数据（来自 discovery，不含 body）

    用于索引和快速查找，不包含完整的 body 内容。
    """
    name: str
    description: str
    directory: str
    priority: int = 0  # 越高越优先
    state: str = "on"  # 受 skillOverrides 影响
    trust_tag: Optional[str] = None  # trusted / untrusted / None（来源标注）


class SkillMetadata(BaseModel):
    """Skill 的前元数据（来自 YAML frontmatter）

    对应 Claude Code Agent Skills 规范中的 frontmatter 字段。
    """
    name: str = Field(description="技能名称，默认使用目录名")
    description: str = Field(description="描述，用于匹配用户输入")
    when_to_use: str = Field(default="", description="额外触发条件")
    argument_hint: str = Field(default="", description="参数提示")
    arguments: list[str] = Field(default_factory=list, description="命名参数定义")
    disable_model_invocation: bool = Field(default=False, description="禁止自动触发")
    user_invocable: bool = Field(default=True, description="对用户可见")
    allowed_tools: list[str] = Field(default_factory=list, description="预审批工具")
    disallowed_tools: list[str] = Field(default_factory=list, description="禁用工具")
    context: SkillContext = Field(default=SkillContext.INLINE, description="执行上下文")
    agent: str = Field(default="general-purpose", description="子代理类型")
    paths: list[str] = Field(default_factory=list, description="文件路径限制")
    shell: str = Field(default="bash", description="bash 或 powershell")
    effort: str = Field(default="inherit", description="努力级别")
    model: str = Field(default="inherit", description="模型覆盖")
    groups: list[str] = Field(default_factory=list, description="技能分组标签")

    # ===== 引擎扩展字段（SKILL.md 里可写，也可由 .skill-local.yaml 覆写）=====
    alias: Optional[list[str]] = Field(default=None, description="语义别名")
    shortcuts: Optional[list[str]] = Field(default=None, description="命令行缩写")
    intent_verbs: Optional[list[str]] = Field(default=None, description="作者手写意图动词")


class MergedMeta(BaseModel):
    """三层合并后的 Skill 元数据（SKILL.md + .skill-meta.yaml + .skill-local.yaml）

    包含 SkillMetadata 所有字段 + _meta_cache 挂载预处理抽取结果。
    """

    name: str = Field(description="技能名称")
    description: str = Field(default="", description="描述")
    when_to_use: str = Field(default="", description="额外触发条件")
    argument_hint: str = Field(default="", description="参数提示")
    arguments: list[str] = Field(default_factory=list, description="命名参数定义")
    disable_model_invocation: bool = Field(default=False)
    user_invocable: bool = Field(default=True)
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    context: SkillContext = Field(default=SkillContext.INLINE)
    agent: str = Field(default="general-purpose")
    paths: list[str] = Field(default_factory=list)
    shell: str = Field(default="bash")
    effort: str = Field(default="inherit")
    model: str = Field(default="inherit")
    groups: list[str] = Field(default_factory=list)
    alias: Optional[list[str]] = Field(default=None)
    shortcuts: Optional[list[str]] = Field(default=None)
    intent_verbs: Optional[list[str]] = Field(default=None)

    # 预处理缓存（.skill-meta.yaml 原始内容，score_keyword 用）
    meta_cache: dict = Field(default_factory=dict, description="预处理缓存")

    model_config = {"extra": "allow"}  # 允许 .skill-local.yaml 追加未知字段不炸


class SelectedSkill(BaseModel):
    """LLM 选定的一个 skill（多 skill 协同场景）"""
    name: str = Field(description="skill 名称")
    role: Optional[str] = Field(default=None, description="在该协同中的角色")
    args_override: Optional[dict] = Field(default=None, description="参数覆盖")


class MatchPlan(BaseModel):
    """Router 最终输出"""
    mode: Literal["single", "multi"] = "single"
    primary: Optional[SelectedSkill] = Field(default=None, description="single 时直接取")
    selections: list[SelectedSkill] = Field(default_factory=list, description="multi 时 ≥2")
    method: str = Field(description="匹配方法: exact / keyword / llm")
    score: Optional[float] = Field(default=None, description="single 时的置信度")
    reason: Optional[str] = Field(default=None, description="LLM 给的协同理由")
    uncertain: bool = Field(default=False, description="LLM 也没把握")


class Skill(BaseModel):
    """完整的 Skill 对象（含 body）

    包含 metadata（frontmatter）、body（正文）、directory（目录路径）、
    supporting_files（支持文件列表）。
    """
    metadata: SkillMetadata
    body: str = Field(description="Markdown 正文（body）")
    directory: str = Field(description="Skill 所在目录路径")
    supporting_files: list[str] = Field(default_factory=list, description="支持文件列表")


class MatchResult(BaseModel):
    """匹配结果（已弃用 — 请使用 MatchPlan + runner.run_plan() 替代）

    保留以兼容旧测试和 runner.run() 内部包装。
    新代码请使用 MatchPlan / SelectedSkill 方案。
    """
    skill: Skill
    score: float = Field(description="匹配分数 0-1")
    method: str = Field(description="匹配方法: name / keyword / embedding")
    arguments: dict = Field(default_factory=dict, description="解析后的参数")


class Step(BaseModel):
    """Steps DSL 中的一步

    engine 原生 DSL，用于确定性执行 skill 的步骤。
    Phase 3 引入。
    """
    name: str = Field(description="步骤名称")
    type: str = Field(description="step 类型: fetch / llm / write / exec / read")
    command: Optional[str] = Field(default=None, description="exec 类型的命令")
    url: Optional[str] = Field(default=None, description="fetch 类型的 URL")
    model: Optional[str] = Field(default=None, description="llm 类型的模型")
    template: Optional[str] = Field(default=None, description="llm 类型的 prompt 模板")
    output_file: Optional[str] = Field(default=None, description="write 类型的输出文件")
    input_ref: Optional[str] = Field(default=None, description="引用前一步的输出")
    timeout: Optional[int] = Field(default=30, description="超时秒数")


class SkillOverride(BaseModel):
    """skillOverrides 配置

    控制 skill 的可见性和行为。
    """
    skill_name: str
    state: str = Field(description="on | name-only | user-invocable-only | off")
