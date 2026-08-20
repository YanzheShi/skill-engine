"""LLM configuration: purpose → model registry.

统一配置源：项目根目录的 ``config.yml``（单一文件，已 git 忽略），取代原先
分散的 ``.env``（模型/设置）与 ``models.yaml``（多模型 profile）。结构与密钥
分离——settings / 密钥支持 ``${ENV}`` 引用，YAML 本体可入库（见 config.yml.example）。

config.yml 结构：
    models:                       # 模型 profile 列表（含内置 default / secondary + 自定义）
      - name: default
        model: gpt-4o
        base_url: https://api.openai.com/v1
        api_key: ${OPENAI_API_KEY}
        provider: openai
      - name: deepseek
        ...
    settings:                     # 全局设置（安全模式 / 自动审批 / MCP / 第三方 key 等）
      security_mode: permissive
      auto_approve: all
      mcp_config: ./mcp.json
      tavily_api_key: ${TAVILY_API_KEY}

加载时机与桥接：config.py 在 import 时读取 config.yml，并**回填 os.environ**
（仅当对应环境变量未设置时才填，故真实环境变量 / CI 注入始终优先）。这样其它
模块里既有的 ``os.getenv(...)`` 调用（security / mcp / auto_approve 等）零改动
即可从 config.yml 取值——这是"桥接兼容"策略，单文件统一但低侵入。

业务代码只通过 get_llm(purpose="xxx") 获取模型实例，不关心具体用哪个模型。
模型选择由下方的 PURPOSE_CONFIGS 统一控制，改模型只需改这一个文件。
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# YAML 是项目已声明依赖(pyyaml>=6.0)。若运行环境异常缺失，降级为"不支持
# models.yaml"，不影响内置 default/secondary 与 env 声明机制。
try:
    import yaml
except Exception:  # pragma: no cover - 依赖缺失时优雅降级
    yaml = None

# 在模块加载时加载 .env，使 LLM_CONFIGS 能读取环境变量
# 先找项目根目录下的 .env，再找 CWD
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()

# 项目根目录（config.yml / mcp.json 等 sidecar 配置与此并列）
_project_root = _env_path.parent


# ================================================================
# 统一配置源：config.yml
# ================================================================
# 在读取任何配置常量之前加载并回填 os.environ，使后续 LLM_CONFIGS /
# SECURITY_MODE / MCP_CONFIG_PATH / TAVILY_API_KEY，以及其它模块里的
# os.getenv 调用，都能从 config.yml 取到值。回填用 setdefault：仅当对应
# 环境变量未设置时才写，故真实环境变量 / CI 注入始终优先于 config.yml。
def _load_config_yml() -> dict:
    """读取统一配置 config.yml。

    路径：环境变量 SKILL_ENGINE_CONFIG_YAML 优先，否则 <项目根>/config.yml。
    返回 {"models": [...], "settings": {...}}；文件缺失 / yaml 不可用 /
    解析失败 / 非 dict：均返回 {}（优雅降级，不抛异常）。
    """
    if yaml is None:
        return {}
    # 主覆盖：SKILL_ENGINE_CONFIG_YAML；兼容旧名 SKILL_ENGINE_MODELS_YAML
    # （曾用于独立 models.yaml）；两者皆空则默认 <项目根>/config.yml。
    path = os.getenv("SKILL_ENGINE_CONFIG_YAML") or os.getenv("SKILL_ENGINE_MODELS_YAML")
    cfg_path = Path(path) if path else (_project_root / "config.yml")
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# settings 段 → 对应环境变量名（回填用，真实 env 优先）
_SETTINGS_ENV_MAP = {
    "security_mode": "SKILLS_ENGINE_SECURITY_MODE",
    "auto_approve": "SKILLS_ENGINE_AUTO_APPROVE",
    "allowlist": "SKILLS_ENGINE_ALLOWLIST",
    "context_budget": "SKILLS_ENGINE_CONTEXT_BUDGET",
    "llm_call_interval": "SKILLS_ENGINE_LLM_CALL_INTERVAL",
    "mcp_config": "SKILL_ENGINE_MCP_CONFIG",
    "tavily_api_key": "TAVILY_API_KEY",
    "vault_path": "VAULT_PATH",
    "ollama_host": "OLLAMA_HOST",
    "r2_token": "CF_R2_TOKEN",
    "r2_account_id": "CF_R2_ACCOUNT_ID",
    "r2_bucket": "CF_R2_BUCKET",
    "r2_public_base": "CF_R2_PUBLIC_BASE",
}


def _apply_config_backfill(cfg: dict) -> None:
    """把 config.yml 的 settings 与 default/secondary 模型回填进 os.environ。

    使用 os.environ.setdefault：仅当环境变量尚未设置时才填入，保证真实
    环境变量（含 CI 注入）优先。回填后，所有既有的 os.getenv 调用与
    LLM_CONFIGS 的旧路径无需改动即可从 config.yml 取值。

    注意：此处直接用 os.path.expandvars，避免在 import 早期对下文
    _expand_env 的前向引用。
    """
    if not isinstance(cfg, dict):
        return

    settings = cfg.get("settings")
    if isinstance(settings, dict):
        for key, env_name in _SETTINGS_ENV_MAP.items():
            val = settings.get(key)
            if val is None or val == "":
                continue
            os.environ.setdefault(env_name, os.path.expandvars(str(val)))

    # default / secondary 作为 LLM_CONFIGS 的内置别名，也通过 env 回填，
    # 使 get_llm(purpose) 与 MOA 的 default/secondary 沿用既有路径。
    models = cfg.get("models")
    if isinstance(models, list):
        by_name = {m.get("name"): m for m in models if isinstance(m, dict)}
        alias_env = {
            "default": ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"),
            "secondary": ("LLM_MODEL_ALT", "LLM_BASE_URL_ALT", "LLM_API_KEY_ALT"),
        }
        for alias, (m_env, b_env, k_env) in alias_env.items():
            m = by_name.get(alias)
            if not isinstance(m, dict):
                continue
            model = os.path.expandvars(str(m.get("model", ""))).strip()
            base = os.path.expandvars(str(m.get("base_url", ""))).strip()
            key = os.path.expandvars(str(m.get("api_key", ""))).strip()
            if model:
                os.environ.setdefault(m_env, model)
            if base:
                os.environ.setdefault(b_env, base)
            if key:
                os.environ.setdefault(k_env, key)


# import 时即加载并回填（此后再定义 LLM_CONFIGS / SECURITY_MODE 等常量）
_CONFIG_YML = _load_config_yml()
_apply_config_backfill(_CONFIG_YML)


def _get_llm_env(primary: str, fallback: str, default: str = "") -> str:
    """读取 LLM 环境变量，优先用带 SKILL_ENGINE_ 前缀的新名，兼容无前缀旧名。

    Args:
        primary: 新名（如 SKILL_ENGINE_LLM_MODEL）
        fallback: 旧名（如 LLM_MODEL），过渡期兼容
        default: 取不到时的默认值
    """
    return os.getenv(primary) or os.getenv(fallback) or default


# ── 模型注册表（alias → provider 配置） ──
# 这里只定义"有哪些模型可用"，不决定业务用哪个。
# PURPOSE_CONFIGS 引用这里的 alias。
LLM_CONFIGS = {
    "default": {
        "model": _get_llm_env("SKILL_ENGINE_LLM_MODEL", "LLM_MODEL"),
        "model_provider": "openai",
        "base_url": _get_llm_env("SKILL_ENGINE_LLM_BASE_URL", "LLM_BASE_URL"),
        "api_key": _get_llm_env("SKILL_ENGINE_LLM_API_KEY", "LLM_API_KEY"),
    },
    "secondary": {
        "model": _get_llm_env("SKILL_ENGINE_LLM_MODEL_ALT", "LLM_MODEL_ALT"),
        "model_provider": "openai",
        "base_url": _get_llm_env("SKILL_ENGINE_LLM_BASE_URL_ALT", "LLM_BASE_URL_ALT"),
        "api_key": _get_llm_env("SKILL_ENGINE_LLM_API_KEY_ALT", "LLM_API_KEY_ALT"),
    },
}


# ── 多模型 Profile（MOA 多模型协作用） ──
# 除了内置的 default / secondary，用户可在 .env 声明任意多个模型 profile，
# 供 `moa` 命令"选择配置的模型"时使用。声明方式：
#
#   SKILL_ENGINE_MODELS=gpt4o,claude,deepseek-vl,qwen
#   SKILL_ENGINE_MODEL_GPT4O_MODEL=gpt-4o
#   SKILL_ENGINE_MODEL_GPT4O_BASE_URL=https://api.openai.com/v1
#   SKILL_ENGINE_MODEL_GPT4O_API_KEY=sk-xxx
#   SKILL_ENGINE_MODEL_CLAUDE_MODEL=claude-3-5-sonnet-20241022
#   SKILL_ENGINE_MODEL_CLAUDE_BASE_URL=https://api.anthropic.com/v1
#   SKILL_ENGINE_MODEL_CLAUDE_API_KEY=sk-ant-xxx
#   SKILL_ENGINE_MODEL_CLAUDE_PROVIDER=anthropic
#   ...
#
# 每个 profile 的 _PROVIDER 默认 openai（兼容所有 OpenAI 兼容网关，如
# DeepSeek / 通义 / 本地 vLLM）。anthropic / gemini 等按 langchain
# init_chat_model 的 model_provider 取值。
def _expand_env(value: str) -> str:
    """对字符串中的 ${ENV} / $ENV 做环境变量展开。

    - 已设置的环境变量：替换为值（支持明文与 ${ENV} 引用混用）。
    - 未设置的环境变量：${VAR} / $VAR 保留原样，调用方据此判定"未解析"。
    注意：api_key 不应包含字面 '$'（会被当作变量引用展开）。
    """
    return os.path.expandvars(value) if isinstance(value, str) else value


def _parse_model_entries(raw) -> dict:
    """解析 ``models:`` 列表为 {profile_name: {model, model_provider, base_url, api_key}}。

    - 结构：每项含 name/model/base_url/api_key/provider
    - api_key 支持明文与 ${ENV} 引用混用；未解析的 ${VAR}（展开后仍以 '$' 开头）
      视为缺失 → 该 profile 被过滤（与"model 与 api_key 必须非空"一致）。
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        model = _expand_env(str(item.get("model", ""))).strip()
        api_key = _expand_env(str(item.get("api_key", ""))).strip()
        # 未解析的 ${VAR} / $VAR（展开后仍以 '$' 开头）视为缺失 → 过滤
        if api_key.startswith("$") or model.startswith("$"):
            continue
        if not model or not api_key:
            continue
        out[name] = {
            "model": model,
            "model_provider": (item.get("provider") or "openai"),
            "base_url": _expand_env(str(item.get("base_url", ""))).strip(),
            "api_key": api_key,
            "vision": bool(item.get("vision", False)),
        }
    return out


def _load_models_yaml() -> dict:
    """从统一配置加载模型 profile（默认读 config.yml 的 ``models:`` 段）。

    路径解析同 ``_load_config_yml``：SKILL_ENGINE_CONFIG_YAML 优先，
    兼容旧名 SKILL_ENGINE_MODELS_YAML，否则 <项目根>/config.yml。

    Returns:
        {profile_name: {model, model_provider, base_url, api_key}}
    """
    cfg = _load_config_yml()
    return _parse_model_entries(cfg.get("models"))


def _build_model_profiles() -> dict:
    """聚合所有可用模型 profile：default + secondary + 用户自定义。

    来源与合并优先级（后者覆盖前者同名）：
      1. 内置 default / secondary（来自 config.yml 的 models: 段，经 env 回填）
      2. env 声明（SKILL_ENGINE_MODELS=... 各 SKILL_ENGINE_MODEL_{NAME}_*）
      3. config.yml 的 models: 段（结构化声明，优先级最高，可覆盖同名）

    返回 {profile_name: {"model","model_provider","base_url","api_key"}}，
    仅包含 model 与 api_key 都非空、可实例化的 profile（MOA 选择列表用）。
    """
    profiles: dict[str, dict] = {}

    # 1. 内置 default / secondary
    for alias, cfg in LLM_CONFIGS.items():
        if cfg.get("model") and cfg.get("api_key"):
            profiles[alias] = {
                "model": cfg["model"],
                "model_provider": cfg.get("model_provider", "openai"),
                "base_url": cfg.get("base_url", ""),
                "api_key": cfg["api_key"],
                "vision": False,
            }

    # 2. 用户自定义 profile（SKILL_ENGINE_MODELS=name1,name2,...）
    declared = [s.strip() for s in os.getenv("SKILL_ENGINE_MODELS", "").split(",") if s.strip()]
    for name in declared:
        key = name.upper().replace("-", "_")
        model = os.getenv(f"SKILL_ENGINE_MODEL_{key}_MODEL", "")
        api_key = os.getenv(f"SKILL_ENGINE_MODEL_{key}_API_KEY", "")
        base_url = os.getenv(f"SKILL_ENGINE_MODEL_{key}_BASE_URL", "")
        provider = os.getenv(f"SKILL_ENGINE_MODEL_{key}_PROVIDER", "openai")
        if model and api_key:
            # 自定义优先级高于内置同名（少见，但允许覆盖）
            profiles[name] = {
                "model": model,
                "model_provider": provider or "openai",
                "base_url": base_url,
                "api_key": api_key,
                "vision": os.getenv(f"SKILL_ENGINE_MODEL_{key}_VISION", "").lower() in ("1", "true", "yes"),
            }

    # 3. 统一配置 config.yml 的 models: 段（优先级最高，可覆盖同名）
    #    重新加载以遵循 SKILL_ENGINE_CONFIG_YAML / SKILL_ENGINE_MODELS_YAML 覆盖
    for name, cfg in _parse_model_entries(_load_config_yml().get("models")).items():
        profiles[name] = cfg

    return profiles


# 模块加载时一次性聚合（环境变量在 import 时已由上方 load_dotenv 注入）
MODEL_PROFILES = _build_model_profiles()


def list_model_profiles() -> dict:
    """返回当前所有可实例化的模型 profile。

    Returns:
        {profile_name: {"model", "model_provider", "base_url", "api_key"}}
        api_key 已被脱敏为 "***" 长度提示，避免泄露；真实 key 仅在
        get_llm_by_profile 实例化时使用。
    """
    safe = {}
    for name, cfg in MODEL_PROFILES.items():
        safe[name] = {
            "model": cfg["model"],
            "model_provider": cfg["model_provider"],
            "base_url": cfg["base_url"],
            "api_key": ("***" if cfg["api_key"] else ""),
            "vision": bool(cfg.get("vision", False)),
        }
    return safe


_MODEL_VISION: dict[int, bool] = {}
"""vision 标记注册表：id(model) → bool。

原因：langchain 新版模型（ChatOpenAI 等）是 pydantic v2 且 model_config
extra=forbid，运行期 ``model.vision = True`` 会抛 ValueError
（"object has no field 'vision'"）——上一版因此直接导致 MOA 全部
model_config_error。此处 try/setattr 失败时改走注册表。
"""

LLM_CALL_TIMEOUT = 120
"""单次 LLM 调用的网络超时（秒）。

openai 客户端默认 600s（10 分钟）——MOA 曾出现「bash 返回后下一轮调用无响应，
屏幕静默 10 分钟」的现象。这里显式钳制到 120s：超时以异常形式上抛，由
MOA 的异常隔离（_run_agent_safe / commander_error）接手，报错进黑板继续，
而不是无提示地干等。
"""


def _apply_call_timeout(cfg: dict) -> None:
    """按 provider 注入 LLM 调用超时参数（openai 兼容用 request_timeout）。"""
    if not isinstance(cfg, dict):
        return
    provider = str(cfg.get("model_provider", "")).lower()
    if provider in ("openai", "google", "groq", "mistralai", "azure_openai"):
        cfg.setdefault("request_timeout", LLM_CALL_TIMEOUT)
    elif provider == "anthropic":
        cfg.setdefault("timeout", LLM_CALL_TIMEOUT)


def _mark_model_vision(model, vision: bool) -> None:
    """把 vision 标记挂到模型实例上；实例不容许 setattr 时落注册表。"""
    if not vision:
        return
    try:
        model.vision = True
        return
    except Exception:  # noqa: BLE001 — pydantic v2 extra=forbid 等
        pass
    _MODEL_VISION[id(model)] = True


def model_supports_vision(model) -> bool:
    """查询模型是否支持视觉：属性 → 注册表 → 沿 CountingLLM 包装链下钻。"""
    seen: set[int] = set()
    m = model
    while m is not None and id(m) not in seen:
        seen.add(id(m))
        if _MODEL_VISION.get(id(m)):
            return True
        try:
            if getattr(m, "vision", False):
                return True
        except Exception:  # noqa: BLE001 — __getattr__ 透传失败等
            pass
        m = getattr(m, "_llm", None)  # CountingLLM 包装链
    return False


def get_llm_by_profile(profile_name: str, **kwargs):
    """按 profile 名直接获取模型实例（MOA 多模型协作核心入口）。

    与 get_llm(purpose) 不同：这里绕过 PURPOSE_CONFIGS，直接以用户声明的
    profile 名取模型，从而支持"同一个会话里用多个不同模型"。

    Args:
        profile_name: MODEL_PROFILES 中的 key（如 "default" / "gpt4o"）。
        **kwargs: 覆盖参数（temperature 等）。

    Raises:
        ValueError: profile 不存在或配置不完整。
    """
    if profile_name not in MODEL_PROFILES:
        available = ", ".join(sorted(MODEL_PROFILES.keys())) or "（无，请在 .env 配置 SKILL_ENGINE_MODELS）"
        raise ValueError(
            f"未知的模型 profile: '{profile_name}'，可选: {available}"
        )
    cfg = MODEL_PROFILES[profile_name].copy()
    vision = bool(cfg.pop("vision", False))  # 视觉标记不进 init_chat_model，挂到实例上
    cfg.update(kwargs)
    if not cfg.get("model") or not cfg.get("api_key"):
        raise ValueError(f"模型 profile '{profile_name}' 配置不完整（缺 model 或 api_key）")
    _apply_call_timeout(cfg)   # 显式网络超时，避免 openai 默认 600s 静默等待
    model = init_chat_model(**cfg)
    _mark_model_vision(model, vision)
    return model

# ── 业务用途 → 模型配置映射 ──
# 业务代码只表达"用途"，不感知具体模型。
# 改模型只需改这里，业务代码一行不动。
PURPOSE_CONFIGS = {
    # === CLI ===
    "cli-chat":        {"alias": "default"},
    "cli-tool":        {"alias": "default"},
    "cli-index":       {"alias": "default"},
    "cli-create":      {"alias": "default"},
    "cli-security":    {"alias": "default"},

    # === 路由 ===
    "router":          {"alias": "secondary"},

    # === Steps DSL ===
    "steps-llm":       {"alias": "default", "temperature": 0.7},

    # === 编排 ===
    "orchestrator":    {"alias": "default"},

    # === UI ===
    "ui-engine":       {"alias": "default"},
    "ui-chat":         {"alias": "default"},
}


def get_llm(purpose: str, **kwargs):
    """根据业务用途获取大模型实例。

    业务代码只表达"用途"（如 ``purpose="router"``），
    具体用哪个模型由 ``PURPOSE_CONFIGS`` 统一控制。

    Args:
        purpose: 业务用途，对应 PURPOSE_CONFIGS 中的 key。
        **kwargs: 额外的模型参数，会覆盖用途配置中的默认值。

    Returns:
        LangChain chat model 实例。

    Raises:
        ValueError: 用途名不存在，或模型配置不完整。
    """
    if purpose not in PURPOSE_CONFIGS:
        raise ValueError(
            f"未知的用途: '{purpose}'，可选: {list(PURPOSE_CONFIGS.keys())}"
        )

    purpose_cfg = PURPOSE_CONFIGS[purpose].copy()
    alias = purpose_cfg.pop("alias")

    if alias not in LLM_CONFIGS:
        raise ValueError(
            f"用途 '{purpose}' 引用了未知的模型别名: '{alias}'，"
            f"可选: {list(LLM_CONFIGS.keys())}"
        )

    # 合并：模型注册表配置 + 用途默认参数 + 调用方覆盖参数
    config = LLM_CONFIGS[alias].copy()
    config.update(purpose_cfg)   # 用途默认参数（temperature, streaming 等）
    config.update(kwargs)        # 调用方显式覆盖

    # 确保必填项不为空
    if not config.get("model") or not config.get("api_key"):
        raise ValueError(
            f"模型 '{alias}'（用途 '{purpose}'）的配置不完整，"
            f"请检查 .env 文件中的环境变量"
        )

    return init_chat_model(**config)


# ================================================================
# 安全配置
# ================================================================

SECURITY_MODE = os.getenv("SKILLS_ENGINE_SECURITY_MODE", "strict").strip().lower()
"""安全模式
- strict（默认）: tool_dispatch/ctx_relay 直接 BLOCK
- permissive: tool_dispatch/ctx_relay 降级为 ATTENTION（弹窗确认）
- off: 所有命令放行
"""


# ================================================================
# MCP 配置
# ================================================================

MCP_CONFIG_PATH = os.getenv("SKILL_ENGINE_MCP_CONFIG", "")
"""MCP 服务器配置文件路径（mcp.json）。"""


# ================================================================
# 第三方服务 API Key
# ================================================================

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
"""Tavily AI Search API key。免费注册：https://app.tavily.com"""


def get_security_mode() -> str:
    """获取当前安全模式（每次读环境变量，不缓存）"""
    return os.getenv("SKILLS_ENGINE_SECURITY_MODE", "strict").strip().lower()


def llm_call_interval() -> float:
    """每次 LLM 调用之间的人为节流间隔（秒），默认 0 = 关闭。

    旧版无条件 sleep(3)（性能诊断 P0-1）。429 限流已有独立指数退避兜底，
    固定节流不再默认叠加；需要保守节流时通过
    SKILLS_ENGINE_LLM_CALL_INTERVAL（或 config.yml settings.llm_call_interval）设置。
    """
    try:
        v = float(os.getenv("SKILLS_ENGINE_LLM_CALL_INTERVAL", "") or 0)
        return max(0.0, v)
    except ValueError:
        return 0.0