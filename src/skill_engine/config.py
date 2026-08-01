"""LLM configuration: purpose → model registry.

Reads from .env:
- AGNES_API_KEY, AGNES_BASE_URL
- SENSENOVA_API_KEY, SENSENOVA_BASE_URL
- SENSENOVA_MODEL, SENSENOVA_MODEL1
- etc.

业务代码只通过 get_llm(purpose="xxx") 获取模型实例，不关心具体用哪个模型。
模型选择由下方的 PURPOSE_CONFIGS 统一控制，改模型只需改这一个文件。
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# 在模块加载时加载 .env，使 LLM_CONFIGS 能读取环境变量
# 先找项目根目录下的 .env，再找 CWD
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()

# ── 模型注册表（alias → provider 配置） ──
# 这里只定义"有哪些模型可用"，不决定业务用哪个。
# PURPOSE_CONFIGS 引用这里的 alias。
LLM_CONFIGS = {
    "sensenova-deepseek": {
        "model": os.getenv("SENSENOVA_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "sensenova": {
        "model": os.getenv("SENSENOVA_MODEL1"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
}

# ── 业务用途 → 模型配置映射 ──
# 业务代码只表达"用途"，不感知具体模型。
# 改模型只需改这里，业务代码一行不动。
PURPOSE_CONFIGS = {
    # === CLI ===
    "cli-chat":        {"alias": "sensenova-deepseek"},
    "cli-tool":        {"alias": "sensenova-deepseek"},
    "cli-index":       {"alias": "sensenova-deepseek"},
    "cli-create":      {"alias": "sensenova-deepseek"},
    "cli-security":    {"alias": "sensenova-deepseek"},

    # === 路由 ===
    "router":          {"alias": "sensenova-deepseek"},

    # === Steps DSL ===
    "steps-llm":       {"alias": "sensenova-deepseek", "temperature": 0.7},

    # === 编排 ===
    "orchestrator":    {"alias": "sensenova-deepseek"},

    # === UI ===
    "ui-engine":       {"alias": "sensenova-deepseek"},
    "ui-chat":         {"alias": "sensenova-deepseek"},
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
# 第三方服务 API Key
# ================================================================

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
"""Tavily AI Search API key。免费注册：https://app.tavily.com"""


def get_security_mode() -> str:
    """获取当前安全模式（每次读环境变量，不缓存）"""
    return os.getenv("SKILLS_ENGINE_SECURITY_MODE", "strict").strip().lower()