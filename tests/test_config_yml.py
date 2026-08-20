"""统一配置 config.yml 加载与桥接回填测试。

覆盖：
- config.yml 加载 models + settings 两段
- settings / 模型 api_key 支持 ${ENV} 引用展开
- 桥接回填：config.yml 的值经 setdefault 进入 os.environ（settings 映射 + default/secondary 模型 env）
- 真实环境变量优先于 config.yml（setdefault 语义，不覆盖已设置项）
- 文件缺失 / yaml 依赖缺失 / 解析失败 均优雅降级
- _load_models_yaml 默认读 config.yml（保留 SKILL_ENGINE_MODELS_YAML 旧覆盖）
- 接入 _build_model_profiles / list_model_profiles（密钥脱敏）

注：config.py 在 import 时已用真实 config.yml 回填一次；本测试通过
monkeypatch 隔离环境变量后，直接调用底层函数验证行为，不依赖 import 时状态。
"""

import os
import textwrap
from pathlib import Path

import pytest

from skill_engine import config


def _write(p: Path, text: str) -> None:
    p.write_text(textwrap.dedent(text), encoding="utf-8")


# ── 单元：_load_config_yml ──

def test_load_config_yml_models_and_settings(monkeypatch, tmp_path):
    p = tmp_path / "config.yml"
    _write(p, """
    models:
      - name: deepseek
        model: deepseek-chat
        api_key: sk-x
    settings:
      security_mode: permissive
      auto_approve: all
      tavily_api_key: tvly-abc
    """)
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    cfg = config._load_config_yml()
    assert [m["name"] for m in cfg["models"]] == ["deepseek"]
    assert cfg["settings"]["security_mode"] == "permissive"
    assert cfg["settings"]["tavily_api_key"] == "tvly-abc"


def test_load_config_yml_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(tmp_path / "nope.yml"))
    assert config._load_config_yml() == {}


def test_load_config_yml_dep_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "yaml", None)
    p = tmp_path / "config.yml"
    _write(p, "models:\n  - name: x\n    model: m\n    api_key: k\n")
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    assert config._load_config_yml() == {}


def test_load_config_yml_malformed(monkeypatch, tmp_path):
    p = tmp_path / "config.yml"
    p.write_text("models: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    assert config._load_config_yml() == {}


# ── 单元：_apply_config_backfill（settings → os.environ） ──

def test_backfill_settings_to_environ(monkeypatch, tmp_path):
    for k in ("SKILLS_ENGINE_SECURITY_MODE", "SKILLS_ENGINE_AUTO_APPROVE",
              "SKILL_ENGINE_MCP_CONFIG", "TAVILY_API_KEY",
              "SKILLS_ENGINE_CONTEXT_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    cfg = {"settings": {
        "security_mode": "permissive",
        "auto_approve": "all",
        "mcp_config": "./mcp.json",
        "tavily_api_key": "tvly-xyz",
        "context_budget": 12000,
    }}
    config._apply_config_backfill(cfg)
    assert os.environ["SKILLS_ENGINE_SECURITY_MODE"] == "permissive"
    assert os.environ["SKILLS_ENGINE_AUTO_APPROVE"] == "all"
    assert os.environ["SKILL_ENGINE_MCP_CONFIG"] == "./mcp.json"
    assert os.environ["TAVILY_API_KEY"] == "tvly-xyz"
    assert os.environ["SKILLS_ENGINE_CONTEXT_BUDGET"] == "12000"


def test_backfill_env_expands_refs(monkeypatch):
    monkeypatch.setenv("MY_TAVILY", "tvly-from-env")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = {"settings": {"tavily_api_key": "${MY_TAVILY}"}}
    config._apply_config_backfill(cfg)
    assert os.environ["TAVILY_API_KEY"] == "tvly-from-env"


def test_backfill_real_env_wins(monkeypatch):
    # 真实环境变量已设置 → 回填不得覆盖（setdefault 语义）
    monkeypatch.setenv("SKILLS_ENGINE_SECURITY_MODE", "strict")
    cfg = {"settings": {"security_mode": "permissive"}}
    config._apply_config_backfill(cfg)
    assert os.environ["SKILLS_ENGINE_SECURITY_MODE"] == "strict"


def test_backfill_skips_empty_settings(monkeypatch):
    monkeypatch.delenv("SKILLS_ENGINE_SECURITY_MODE", raising=False)
    cfg = {"settings": {"security_mode": "", "auto_approve": None}}
    config._apply_config_backfill(cfg)
    assert "SKILLS_ENGINE_SECURITY_MODE" not in os.environ


def test_backfill_default_secondary_models(monkeypatch):
    for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
              "LLM_MODEL_ALT", "LLM_BASE_URL_ALT", "LLM_API_KEY_ALT"):
        monkeypatch.delenv(k, raising=False)
    cfg = {"models": [
        {"name": "default", "model": "gpt-4o", "base_url": "https://x/v1", "api_key": "sk-d"},
        {"name": "secondary", "model": "gpt-4o-mini", "base_url": "https://x/v1", "api_key": "sk-s"},
    ]}
    config._apply_config_backfill(cfg)
    assert os.environ["LLM_MODEL"] == "gpt-4o"
    assert os.environ["LLM_API_KEY"] == "sk-d"
    assert os.environ["LLM_MODEL_ALT"] == "gpt-4o-mini"
    assert os.environ["LLM_API_KEY_ALT"] == "sk-s"


# ── 集成：_build_model_profiles 读 config.yml ──

def test_models_from_config_yml_priority(monkeypatch, tmp_path):
    # env 声明 claude-vl = model-A；config.yml 声明 claude-vl = model-B → config.yml 胜
    monkeypatch.setenv("SKILL_ENGINE_MODELS", "claude-vl")
    monkeypatch.setenv("SKILL_ENGINE_MODEL_CLAUDE_VL_MODEL", "model-A")
    monkeypatch.setenv("SKILL_ENGINE_MODEL_CLAUDE_VL_API_KEY", "sk-A")
    p = tmp_path / "config.yml"
    _write(p, """
    models:
      - name: claude-vl
        model: model-B
        api_key: sk-B
    settings:
      security_mode: permissive
    """)
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    prof = config._build_model_profiles()
    assert prof["claude-vl"]["model"] == "model-B"
    assert prof["claude-vl"]["api_key"] == "sk-B"


def test_load_models_yaml_default_reads_config_yml(monkeypatch, tmp_path):
    # 不设 SKILL_ENGINE_MODELS_YAML → 默认读 config.yml
    p = tmp_path / "config.yml"
    _write(p, """
    models:
      - name: fromcfg
        model: m
        api_key: sk-m
    """)
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    monkeypatch.delenv("SKILL_ENGINE_MODELS_YAML", raising=False)
    prof = config._load_models_yaml()
    assert prof["fromcfg"]["api_key"] == "sk-m"


def test_load_models_yaml_legacy_override(monkeypatch, tmp_path):
    # 旧 SKILL_ENGINE_MODELS_YAML 覆盖仍可用（兼容独立 models.yaml）
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: legacy
        model: lm
        api_key: sk-legacy
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    prof = config._load_models_yaml()
    assert prof["legacy"]["api_key"] == "sk-legacy"


def test_integration_and_masking(monkeypatch, tmp_path):
    p = tmp_path / "config.yml"
    _write(p, """
    models:
      - name: yamlonly
        model: yaml-model
        api_key: sk-yaml
    settings:
      security_mode: permissive
    """)
    monkeypatch.setenv("SKILL_ENGINE_CONFIG_YAML", str(p))
    prof = config._build_model_profiles()
    assert "yamlonly" in prof
    monkeypatch.setattr(config, "MODEL_PROFILES", prof)
    safe = config.list_model_profiles()
    assert safe["yamlonly"]["api_key"] == "***"
    assert safe["yamlonly"]["model"] == "yaml-model"


# ── llm_call_interval（性能诊断 P0-1：默认 0 = 关闭节流） ──

def test_llm_call_interval_default_zero(monkeypatch):
    monkeypatch.delenv("SKILLS_ENGINE_LLM_CALL_INTERVAL", raising=False)
    assert config.llm_call_interval() == 0.0


def test_llm_call_interval_from_env(monkeypatch):
    monkeypatch.setenv("SKILLS_ENGINE_LLM_CALL_INTERVAL", "0.5")
    assert config.llm_call_interval() == 0.5


def test_llm_call_interval_invalid_falls_back_zero(monkeypatch):
    monkeypatch.setenv("SKILLS_ENGINE_LLM_CALL_INTERVAL", "abc")
    assert config.llm_call_interval() == 0.0
    monkeypatch.setenv("SKILLS_ENGINE_LLM_CALL_INTERVAL", "-3")
    assert config.llm_call_interval() == 0.0   # 负数钳制为 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
