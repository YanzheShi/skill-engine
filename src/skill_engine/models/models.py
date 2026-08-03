"""
Skills Engine 数据模型

定义 Skill 相关的核心数据结构，使用 Pydantic 做数据验证和序列化。
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Literal
from enum import Enum
from dataclasses import dataclass, field


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


@dataclass
class TurnPolicy:
    """多轮对话的终止策略

    max_turns: 最大轮数（防无限循环）
    user_exit: 用户输入这些关键词时结束
    stop_when: LLM 输出包含这些字符串时自动结束（不追问用户）
    """
    max_turns: int = 20
    user_exit: list[str] = field(default_factory=lambda: ["/done", "/exit", "结束", "再见", "拜拜", "退出"])
    stop_when: Optional[list[str]] = None  # 接受 str 或 list[str]，__post_init__ 统一

    def __post_init__(self):
        """统一 stop_when 为 list[str]"""
        if self.stop_when is not None and isinstance(self.stop_when, str):
            self.stop_when = [self.stop_when]

    def should_stop(self, text: str) -> bool:
        """LLM 输出是否包含结束信号？
        
        如果 stop_when 中的任意字符串出现在 text 中，返回 True。
        """
        if not self.stop_when:
            return False
        return any(kw in text for kw in self.stop_when)


@dataclass
class RunResult:
    """tool_dispatch 的返回值，替代裸 dict

    output:  最终文本（LLM 无 tool_calls 时返回的文本 / 超时/错误信息）
    ctx:     执行上下文（steps 结果、files_created 等）
    history: 本次完整 messages，含 system/user/assistant/tool

    支持 dict 式访问（result["output"]）和属性访问（result.output），
    保持向后兼容。
    """
    output: Optional[str]
    ctx: dict
    history: list[dict]

    def __getitem__(self, key: str):
        """兼容 dict 式访问：result["output"] → result.output"""
        if key in ("output", "history"):
            return getattr(self, key)
        # ctx 中的 key 直接透传（如 skill_name, iterations, steps 等）
        if key in self.ctx:
            return self.ctx[key]
        raise KeyError(key)

    def get(self, key: str, default=None):
        """兼容 dict.get()"""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str) -> bool:
        """兼容 key in result"""
        try:
            self[key]
            return True
        except KeyError:
            return False


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
    human_in_loop: bool = Field(default=False, description="启用多轮对话模式（CC 静默忽略）")
    turn_policy: dict | None = Field(default=None, description="轮次策略配置（CC 静默忽略）")

    # ===== 大型代码能力扩展字段（P1）=====
    extra_tools: list[str] = Field(
        default_factory=list,
        description="额外工具模块文件名（相对 skill 目录，如 ['tools.py']），引擎自动加载其中 @tool 并合并进 bind_tools",
    )
    context_budget: int = Field(
        default=0,
        description="档位 B 上下文 token 预算；0=引擎默认(32768，可被 SKILLS_ENGINE_CONTEXT_BUDGET 覆盖)",
    )
    strict_file_tracking: bool = Field(
        default=False,
        description="编辑前一致性硬约束：未读/外部已变的文件拒绝 edit（默认软约束仅提示）",
    )
    verify_command: str = Field(
        default="",
        description="自动验证命令：每轮写/改完成后执行一次，失败输出回灌驱动修复（如 'pytest -x -q'）；空=关闭",
    )
    verify_timeout: int = Field(
        default=120,
        description="verify_command 超时（秒）",
    )
    compact_tool_output: bool = Field(
        default=True,
        description="允许上下文 L1 微压缩：折叠旧轮次的大块工具输出",
    )
    compress_template: str = Field(
        default="",
        description="L2 历史压缩的自定义 prompt 模板（空=引擎默认的任务中立模板）",
    )
    confirm_edits: str = Field(
        default="",
        description="编辑 diff 预览：''=关闭（默认）；'true'=每次编辑确认；'batch'=逐文件确认（首次批准后该文件自动放行）",
    )

    # ===== MCP 外部工具（方案 A：全局 mcp.json + 字段引用 server 名）=====
    mcp_servers: list[str] = Field(
        default_factory=list,
        description="要接入的 MCP server 名称列表（对应全局 mcp.json 的 mcpServers 键）；引擎自动连接并合并其工具",
    )


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

    human_in_loop: bool = Field(default=False, description="启用多轮对话模式")
    turn_policy: dict | None = Field(default=None, description="轮次策略配置")

    # ===== 大型代码能力扩展字段（P1，与 SkillMetadata 对齐）=====
    extra_tools: list[str] = Field(
        default_factory=list,
        description="额外工具模块文件名（相对 skill 目录），引擎自动加载其中 @tool 并合并进 bind_tools",
    )
    context_budget: int = Field(
        default=0,
        description="档位 B 上下文 token 预算；0=引擎默认(32768，可被 SKILLS_ENGINE_CONTEXT_BUDGET 覆盖)",
    )
    strict_file_tracking: bool = Field(
        default=False,
        description="编辑前一致性硬约束：未读/外部已变的文件拒绝 edit（默认软约束仅提示）",
    )
    verify_command: str = Field(
        default="",
        description="自动验证命令：每轮写/改完成后执行一次，失败输出回灌驱动修复（如 'pytest -x -q'）；空=关闭",
    )
    verify_timeout: int = Field(
        default=120,
        description="verify_command 超时（秒）",
    )
    compact_tool_output: bool = Field(
        default=True,
        description="允许上下文 L1 微压缩：折叠旧轮次的大块工具输出",
    )
    compress_template: str = Field(
        default="",
        description="L2 历史压缩的自定义 prompt 模板（空=引擎默认的任务中立模板）",
    )
    confirm_edits: str = Field(
        default="",
        description="编辑 diff 预览：''=关闭（默认）；'true'=每次编辑确认；'batch'=逐文件确认（首次批准后该文件自动放行）",
    )

    # ===== MCP 外部工具（方案 A：全局 mcp.json + 字段引用 server 名）=====
    mcp_servers: list[str] = Field(
        default_factory=list,
        description="要接入的 MCP server 名称列表（对应全局 mcp.json 的 mcpServers 键）",
    )

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
    human_in_loop: bool = Field(default=False, description="V0.2: Steps DSL 路径用")
    turn_policy: dict | None = Field(default=None, description="V0.2: Steps DSL 路径用")


class SkillOverride(BaseModel):
    """skillOverrides 配置

    控制 skill 的可见性和行为。
    """
    skill_name: str
    state: str = Field(description="on | name-only | user-invocable-only | off")
