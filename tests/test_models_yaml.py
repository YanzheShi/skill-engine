"""models.yaml 多模型 profile 加载测试。

覆盖：
- 明文 api_key 与 ${ENV} 引用混用
- 文件缺失 / yaml 依赖缺失 / 解析失败 均优雅降级为 {}
- 未解析的 ${VAR} 与不完整 profile 被过滤
- models.yaml 优先级高于 env 声明（SKILL_ENGINE_MODELS）
- 接入 _build_model_profiles / list_model_profiles（密钥脱敏）
"""

import os
import textwrap
from pathlib import Path

import pytest

from skill_engine import config


def _write(p: Path, text: str) -> None:
    p.write_text(textwrap.dedent(text), encoding="utf-8")


# ── 单元：_load_models_yaml ──

def test_yaml_plaintext_and_envref(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_ENGINE_DEEPSEEK_API_KEY", "sk-from-env")
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: deepseek
        model: deepseek-chat
        base_url: https://api.deepseek.com
        api_key: sk-plain
        provider: openai
      - name: claude-vl
        model: claude-3-5-sonnet-20241022
        base_url: https://api.anthropic.com/v1
        api_key: ${SKILL_ENGINE_DEEPSEEK_API_KEY}
        provider: anthropic
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    prof = config._load_models_yaml()
    assert prof["deepseek"]["api_key"] == "sk-plain"
    assert prof["claude-vl"]["api_key"] == "sk-from-env"      # ${ENV} 已展开
    assert prof["claude-vl"]["model_provider"] == "anthropic"
    assert prof["deepseek"]["base_url"] == "https://api.deepseek.com"


def test_yaml_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(tmp_path / "nope.yaml"))
    assert config._load_models_yaml() == {}


def test_yaml_dep_missing_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "yaml", None)
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: x
        model: m
        api_key: k
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    assert config._load_models_yaml() == {}


def test_yaml_malformed_degrades(monkeypatch, tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("models: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    assert config._load_models_yaml() == {}


def test_yaml_unresolved_env_filtered(monkeypatch, tmp_path):
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: bad
        model: x
        api_key: ${THIS_VAR_IS_NOT_SET}
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    assert config._load_models_yaml() == {}


def test_yaml_incomplete_filtered(monkeypatch, tmp_path):
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: noname
        model: x
      - name: nokey
        model: x
        api_key: ""
      - name: ok
        model: x
        api_key: sk-x
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    prof = config._load_models_yaml()
    assert "ok" in prof
    assert "noname" not in prof     # 缺 api_key
    assert "nokey" not in prof      # api_key 为空


# ── 集成：合并优先级 + 脱敏 ──

def test_yaml_priority_over_env_declared(monkeypatch, tmp_path):
    # env 声明 claude-vl = model-A；yaml 声明 claude-vl = model-B → yaml 胜
    monkeypatch.setenv("SKILL_ENGINE_MODELS", "claude-vl")
    monkeypatch.setenv("SKILL_ENGINE_MODEL_CLAUDE_VL_MODEL", "model-A")
    monkeypatch.setenv("SKILL_ENGINE_MODEL_CLAUDE_VL_API_KEY", "sk-A")
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: claude-vl
        model: model-B
        api_key: sk-B
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    prof = config._build_model_profiles()
    assert prof["claude-vl"]["model"] == "model-B"
    assert prof["claude-vl"]["api_key"] == "sk-B"


def test_yaml_integration_and_masking(monkeypatch, tmp_path):
    p = tmp_path / "m.yaml"
    _write(p, """
    models:
      - name: yamlonly
        model: yaml-model
        api_key: sk-yaml
    """)
    monkeypatch.setenv("SKILL_ENGINE_MODELS_YAML", str(p))
    prof = config._build_model_profiles()
    assert "yamlonly" in prof
    # list_model_profiles 读取模块级 MODEL_PROFILES 缓存，注入新鲜构建后验脱敏
    monkeypatch.setattr(config, "MODEL_PROFILES", prof)
    safe = config.list_model_profiles()
    assert safe["yamlonly"]["api_key"] == "***"     # 脱敏
    assert safe["yamlonly"]["model"] == "yaml-model"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
