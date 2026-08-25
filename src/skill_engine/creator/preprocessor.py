"""Preprocessor — Skill 元数据预处理

职责：
- 接收 Skill 对象，调 LLM 抽 intention / synonyms / purpose / keywords
- 写 .skill-meta.yaml，带 source_hash 增量
- 第三方英文 SKILL.md 也能抽（不要求作者改）

使用方式：
>>> preprocessor = Preprocessor(llm=llm_client)
>>> meta = preprocessor.ensure_meta(skill)
>>> print(meta["intention"])  # ["解题", "写题解"]
"""

import hashlib
import json
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from skill_engine.models import Skill


# ================================================================
# Meta 缓存落点（out-of-tree，用户级）
# ================================================================
# 设计决策（详见 session 讨论）：
# - .skill-meta.yaml 是 LLM 从 SKILL.md 抽取的**派生缓存**，可随时重建，
#   不应留在 skill 源树（会与 git/可移植 skill 资产耦合，且读路径产生写副作用）。
# - 落点：用户级 ~/.skill-engine/cache/meta/（与 scanner 的 approvals/blocklist
#   同根，但放在 cache/ 子目录下，明确标"可重建"语义，区别于必须留存的 data）。
# - 寻址：文件名 = <source_hash>__v<EXTRACTOR_VERSION>.yaml（内容寻址 + 抽取器
#   版本）。同一份 SKILL.md 内容在全机器任何项目只抽一次（跨项目共享）。
#   改抽取 prompt 时只需 bump EXTRACTOR_VERSION，旧缓存自动失效重抽。
# - 旧 sidecar 文件 ./skills/<name>/.skill-meta.yaml 已删除；引擎下次自动在 cache 重建。

# 抽取器版本：PROMPT_EXTRACT 语义变化时 +1（旧缓存凭文件名自动失效）
EXTRACTOR_VERSION = 2

# 引擎派生缓存统一前缀（与 .skill-local.yaml 用户覆写区分：local 是 data，meta 是 cache）
_META_CACHE_PREFIX = ".skill-engine"

# 派生文件/目录黑名单：绝不作为 supporting_files 喂给 LLM 上下文
META_DERIVED_NAMES = {
    ".skill-meta.yaml",   # 旧 sidecar（迁移后不再生成，留作防御）
    ".skill-local.yaml",  # 用户覆写，非 skill 资产，避免进 LLM 上下文
}
META_DERIVED_DIRS = {".git", "__pycache__", ".skill-engine"}


def meta_cache_dir(root: Optional[Path] = None) -> Path:
    """返回 meta 缓存目录并确保存在。

    默认用户级 ~/.skill-engine/cache/meta；传入 root 时用于隔离（如测试）。

    Args:
        root: 自定义缓存根目录（None → 用户级 ~/.skill-engine/cache/meta）

    Returns:
        已 mkdir 的 Path
    """
    if root is not None:
        d = Path(root)
    else:
        d = Path.home() / _META_CACHE_PREFIX / "cache" / "meta"
    d.mkdir(parents=True, exist_ok=True)
    return d


def meta_cache_path(skill: Skill, root: Optional[Path] = None) -> Path:
    """计算某 skill 的 meta 缓存文件路径（内容寻址 + 抽取器版本）。

    Args:
        skill: Skill 对象
        root: 自定义缓存根目录（None → 用户级）

    Returns:
        <cache_dir>/<source_hash>__v<ver>.yaml
    """
    src_hash = Preprocessor._hash_skill(skill)
    return meta_cache_dir(root) / f"{src_hash}__v{EXTRACTOR_VERSION}.yaml"


# ================================================================
# LLM 抽取 Prompt
# ================================================================

PROMPT_EXTRACT = """你是一个 Skill 元数据抽取助手。根据 SKILL.md 内容提取结构化字段。

要求——所有值均为**扁平字符串**，不要嵌套对象，不要中文/英文对照：
1. intention: 核心意图动词列表（单字动词，如 ["写", "生成", "解题"]）
2. synonyms: 每个 intention 动词的近义词列表，值为扁平字符串列表
3. purpose: 一句话说明，30 字以内
4. keywords.动词: 描述中出现的动作词（纯中文动词，如 ["写", "生成", "解题"]）
5. keywords.名词: 描述中出现的领域名词（纯中文名词，如 ["诗", "LeetCode", "目录"]）

必须严格按以下 JSON 格式返回（不要 markdown 代码块，不要额外文字）：

{{
  "intention": ["写", "生成"],
  "synonyms": {{"写": ["作诗", "赋诗", "写诗"], "生成": ["产生", "创作"]}},
  "purpose": "写中国古诗并保存到指定目录",
  "keywords": {{
    "动词": ["写", "生成", "创建", "保存"],
    "名词": ["诗", "古诗", "目录", "文件"]
  }}
}}

SKILL.md:
---
{skill_markdown}

返回 JSON（不要包含任何解释文字）："""


# ================================================================
# JSON 提取（3 层容错，同 designer.py 逻辑）
# ================================================================


def extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中提取 JSON，3 层容错"""
    # Tier 1: 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Tier 2: 匹配 ```json ... ``` 代码块
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Tier 3: 贪婪匹配第一个 { 到最后一个 }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ================================================================
# Preprocessor
# ================================================================


class Preprocessor:
    """Skill 元数据预处理器

    Args:
        llm: LangChain LLM 客户端实例
        cache_dir: 可选自定义缓存根目录。None → 用户级 ~/.skill-engine/cache/meta
                   （内容寻址，跨项目共享）。测试可传入临时目录隔离。
    """

    def __init__(
        self,
        llm,
        cache_dir: Optional[Path] = None,
    ):
        self.llm = llm
        self.cache_root = cache_dir

    def ensure_meta(self, skill: Skill) -> dict:
        """增量保证 meta 缓存存在且最新（落点：用户级 ~/.skill-engine/cache/meta）

        如果缓存命中且 SKILL.md 未变（source_hash 一致），
        直接返回缓存内容，不调 LLM。

        注意：meta 是 LLM 从 SKILL.md 抽取的**派生缓存**，落点不在 skill 源树，
        因此本方法对 skill 目录零写副作用（读路径安全）。

        Args:
            skill: Skill 对象

        Returns:
            meta 缓存的 dict 内容
        """
        # 落点：cache（内容寻址 + 抽取器版本），不在 skill 源树
        # cache_root 为空 → 用户级 ~/.skill-engine/cache/meta；测试可注入临时根隔离
        meta_path = meta_cache_path(skill, self.cache_root)

        # 计算 SKILL.md 的 hash
        src_hash = self._hash_skill(skill)

        # 缓存命中且 SKILL.md 未变（文件名已含 source_hash，双保险再比字段）
        if meta_path.exists():
            try:
                old = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                if old.get("source_hash") == src_hash:
                    return old
            except (yaml.YAMLError, OSError):
                pass  # 文件损坏或不可读，重抽

        # 需重抽
        data = self._extract_with_llm(skill)
        data["meta_version"] = EXTRACTOR_VERSION
        data["source_hash"] = src_hash
        data["computed_at"] = datetime.now(timezone.utc).isoformat()
        data["provider"] = "llm"

        # 写回 cache 目录（不影响 skill 源树）
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(
                yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # 写失败不阻塞，后续调用会重试

        return data

    def batch_ensure(self, skills: list[Skill]) -> dict[str, dict]:
        """批量预处理（Discovery 阶段调用）

        Args:
            skills: Skill 对象列表

        Returns:
            {skill_name: meta_dict}
        """
        results: dict[str, dict] = {}
        for s in skills:
            try:
                results[s.metadata.name] = self.ensure_meta(s)
            except Exception:
                # 单条失败不影响其他
                pass
        return results

    def _extract_with_llm(self, skill: Skill) -> dict:
        """调 LLM 抽取结构化字段

        Args:
            skill: Skill 对象

        Returns:
            抽取结果 dict（含 intention / synonyms / purpose / keywords）

        Raises:
            ValueError: LLM 返回格式错误
        """
        raw_md = self._get_skill_raw_md(skill)
        prompt = PROMPT_EXTRACT.format(skill_markdown=raw_md)

        resp = self.llm.invoke(prompt)
        raw_text = ""
        if hasattr(resp, "content"):
            raw_text = resp.content if isinstance(resp.content, str) else str(resp.content)
        else:
            raw_text = str(resp)

        data = extract_json(raw_text)
        if data is None:
            raise ValueError(
                f"LLM 返回格式错误，无法提取有效 JSON。原始输出:\n{raw_text[:500]}"
            )

        # 确保字段存在
        data.setdefault("intention", [])
        data.setdefault("synonyms", {})
        data.setdefault("purpose", "")
        data.setdefault("keywords", {"动词": [], "名词": []})

        return data

    @staticmethod
    def _hash_skill(skill: Skill) -> str:
        """计算 SKILL.md 的 SHA256 摘要"""
        raw = skill.body or ""
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _get_skill_raw_md(skill: Skill) -> str:
        """获取 SKILL.md 的完整原始内容"""
        skill_path = Path(skill.directory) / "SKILL.md"
        try:
            return skill_path.read_text(encoding="utf-8")
        except OSError:
            return skill.body or ""