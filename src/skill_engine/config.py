"""LLM configuration: model alias -> provider, temperature, base URL.

Reads from .env:
- AGNES_API_KEY, AGNES_BASE_URL
- LLM_MODEL_AGNES, LLM_MODEL_AGNES_STREAM
- OPENAI_API_KEY etc.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

# 在模块加载时加载 .env，使 LLM_CONFIGS 能读取环境变量
# 先找项目根目录下的 .env，再找 CWD
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()

# 模型别名 -> 具体配置的映射表
# 你可以在这里随时增加或修改你的模型
LLM_CONFIGS = {
    "sensenova": {
        "model": os.getenv("SENSENOVA_MODEL1"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "sensenova-deepseek": {
        "model": os.getenv("SENSENOVA_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("SENSENOVA_BASE_URL"),
        "api_key": os.getenv("SENSENOVA_API_KEY"),
    },
    "gpt-4o": {
        "model": "gpt-4o-mini",
        "model_provider": "openai",
        "api_key": os.getenv("OPENAI_API_KEY"),
    },
    "deepseek": {
        # 假设你装了 langchain-deepseek
        "model": "deepseek-coder",
        "model_provider": "deepseek",
        "api_key": os.getenv("DEEPSEEK_API_KEY"),
    },
    "qwen": {
        # 通义千问兼容 openai 接口
        "model": "qwen-plus",
        "model_provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": os.getenv("DASHSCOPE_API_KEY"),
    },
    "agnes": {
        "model": os.getenv("AGNES_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("AGNES_BASE_URL"),
        "api_key": os.getenv("AGNES_API_KEY"),
    },
    "agnes-stream": {
        "model": os.getenv("AGNES_MODEL"),
        "model_provider": "openai",
        "base_url": os.getenv("AGNES_BASE_URL"),
        "api_key": os.getenv("AGNES_API_KEY"),
        "streaming": True
    }
}


DEFAULT_LLM = "sensenova-deepseek"
ROUTER_LLM = "sensenova-deepseek"

def get_llm(alias: str = DEFAULT_LLM, **kwargs):
    """
    根据别名获取大模型实例

    参数:
        alias: 模型别名，如 "sensenova", "gpt-4o"
        **kwargs: 额外的模型参数，如 temperature=0.7, streaming=True
    """
    if alias not in LLM_CONFIGS:
        raise ValueError(f"未找到模型别名: '{alias}'，可选: {list(LLM_CONFIGS.keys())}")

    # 复制配置，避免修改原字典
    config = LLM_CONFIGS[alias].copy()

    # 允许覆盖参数（如 temperature, streaming 等）
    config.update(kwargs)

    # 确保必填项不为空
    if not config.get("model") or not config.get("api_key"):
        raise ValueError(f"模型 '{alias}' 的配置不完整，请检查 .env 文件中的环境变量")

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


def get_security_mode() -> str:
    """获取当前安全模式（每次读环境变量，不缓存）"""
    return os.getenv("SKILLS_ENGINE_SECURITY_MODE", "strict").strip().lower()
