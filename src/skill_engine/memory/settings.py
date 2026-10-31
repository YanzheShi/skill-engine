"""长期记忆配置。

配置来源：``config.yml`` 的 ``settings.memory`` 段；环境变量优先。
测试/CI 环境默认关闭，避免把临时记录写进真实家目录；显式设置
``SKILLS_ENGINE_MEMORY_ENABLED=1`` 可覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "enable", "enabled"):
        return True
    if text in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _expand_path(value: Any, default: Path) -> Path:
    if value in (None, ""):
        return default
    try:
        return Path(os.path.expandvars(str(value))).expanduser()
    except (OSError, ValueError, TypeError):
        return default


@dataclass
class MemoryConfig:
    enabled: bool = True
    user_store: Path = field(
        default_factory=lambda: Path.home() / ".skill-engine" / "memory"
    )
    project_store_rel: str = ".skill-engine/memory"

    profile_budget: int = 700
    now_budget: int = 250
    project_budget: int = 250
    search_budget: int = 300
    max_total: int = 1500

    status_ttl_days: int = 7
    resource_verify_days: int = 30
    episode_retention_days: int = 90

    auto_distill: bool = True
    distill_model: str = "secondary"
    auto_commit_project: str = "high"
    auto_commit_user: str = "review"

    default_sensitivity: str = "L2"
    l3_injection: str = "on_demand"  # always | on_demand | allowlist
    export_exclude_private: bool = True
    fts_enabled: bool = True
    max_body_chars: int = 2000

    def project_store(self, working_root: Path | str) -> Path:
        root = Path(working_root).expanduser()
        p = Path(os.path.expandvars(self.project_store_rel)).expanduser()
        return p if p.is_absolute() else root / p

    def sensitivity_allowed(self, level: str, *, include_private: bool = False) -> bool:
        if str(level).upper() == "L3":
            return bool(include_private) or self.l3_injection == "always"
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "user_store": str(self.user_store),
            "project_store_rel": self.project_store_rel,
            "profile_budget": self.profile_budget,
            "now_budget": self.now_budget,
            "project_budget": self.project_budget,
            "search_budget": self.search_budget,
            "max_total": self.max_total,
            "status_ttl_days": self.status_ttl_days,
            "resource_verify_days": self.resource_verify_days,
            "episode_retention_days": self.episode_retention_days,
            "auto_distill": self.auto_distill,
            "distill_model": self.distill_model,
            "default_sensitivity": self.default_sensitivity,
            "l3_injection": self.l3_injection,
            "fts_enabled": self.fts_enabled,
        }


def _section() -> dict[str, Any]:
    try:
        from skill_engine.config import _load_config_yml

        cfg = _load_config_yml()
    except Exception:  # noqa: BLE001 — 配置读取失败必须优雅降级
        cfg = {}
    settings = cfg.get("settings") if isinstance(cfg, dict) else None
    section = settings.get("memory") if isinstance(settings, dict) else None
    return section if isinstance(section, dict) else {}


def load_memory_config() -> MemoryConfig:
    section = _section()
    cfg = MemoryConfig()

    explicit_enabled = os.getenv("SKILLS_ENGINE_MEMORY_ENABLED")
    if explicit_enabled is not None:
        cfg.enabled = _as_bool(explicit_enabled, cfg.enabled)
    elif os.getenv("PYTEST_CURRENT_TEST") or os.getenv("CI"):
        # 测试/CI 默认关闭：真实记忆写入属于用户数据，不应被测试污染。
        cfg.enabled = False
    else:
        cfg.enabled = _as_bool(section.get("enabled"), cfg.enabled)

    cfg.user_store = _expand_path(
        os.getenv("SKILLS_ENGINE_MEMORY_DIR") or section.get("user_store"),
        cfg.user_store,
    )
    cfg.project_store_rel = str(
        section.get("project_store") or cfg.project_store_rel
    )

    inject = section.get("inject") if isinstance(section.get("inject"), dict) else {}
    cfg.profile_budget = _as_int(inject.get("profile_budget"), cfg.profile_budget, 50)
    cfg.now_budget = _as_int(inject.get("now_budget"), cfg.now_budget, 50)
    cfg.project_budget = _as_int(inject.get("project_budget"), cfg.project_budget, 50)
    cfg.search_budget = _as_int(inject.get("search_budget"), cfg.search_budget, 50)
    cfg.max_total = _as_int(inject.get("max_total"), cfg.max_total, 100)

    cfg.status_ttl_days = _as_int(section.get("status_ttl_days"), cfg.status_ttl_days, 1)
    cfg.resource_verify_days = _as_int(
        section.get("resource_verify_days"), cfg.resource_verify_days, 1
    )
    cfg.episode_retention_days = _as_int(
        section.get("episode_retention_days"), cfg.episode_retention_days, 1
    )

    if os.getenv("SKILLS_ENGINE_MEMORY_AUTO_DISTILL") is not None:
        cfg.auto_distill = _as_bool(os.getenv("SKILLS_ENGINE_MEMORY_AUTO_DISTILL"), cfg.auto_distill)
    else:
        cfg.auto_distill = _as_bool(section.get("auto_distill"), cfg.auto_distill)

    if os.getenv("SKILLS_ENGINE_MEMORY_L3_INJECTION"):
        cfg.l3_injection = os.environ["SKILLS_ENGINE_MEMORY_L3_INJECTION"].strip().lower()
    elif section.get("sensitivity") and isinstance(section["sensitivity"], dict):
        cfg.l3_injection = str(
            section["sensitivity"].get("l3_injection") or cfg.l3_injection
        ).strip().lower()
    if cfg.l3_injection not in ("always", "on_demand", "allowlist"):
        cfg.l3_injection = "on_demand"

    if os.getenv("SKILLS_ENGINE_MEMORY_FTS") is not None:
        cfg.fts_enabled = _as_bool(os.getenv("SKILLS_ENGINE_MEMORY_FTS"), cfg.fts_enabled)
    elif "fts_enabled" in section:
        cfg.fts_enabled = _as_bool(section.get("fts_enabled"), cfg.fts_enabled)

    if section.get("distill_model"):
        cfg.distill_model = str(section["distill_model"])
    auto_commit = section.get("auto_commit") if isinstance(section.get("auto_commit"), dict) else {}
    if auto_commit.get("project"):
        cfg.auto_commit_project = str(auto_commit["project"])
    if auto_commit.get("user"):
        cfg.auto_commit_user = str(auto_commit["user"])

    export = section.get("export") if isinstance(section.get("export"), dict) else {}
    if "exclude_private" in export:
        cfg.export_exclude_private = _as_bool(export.get("exclude_private"), cfg.export_exclude_private)

    return cfg
