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

from .models import Skill


# ================================================================
# LLM 抽取 Prompt
# ================================================================

PROMPT_EXTRACT = """你是一个 Skill 元数据提取器。根据 SKILL.md 内容提取结构化字段，返回 JSON。

要求：
1. intention：这个 skill 的「核心动作/目的」，中文动词短语，2-4 个
2. synonyms：intention 中每个词的中文近义词 + 英文对应词
3. purpose：一句话说明这个 skill 干什么，30 字以内
4. keywords.动词：描述里出现的动作词（中英文原形，如 solve/generate）
5. keywords.名词：描述里出现的领域名词（中英文原形，如 leetcode/dfs/binary tree）

SKILL.md:
---
{skill_markdown}

返回 JSON（不要包含任何解释文字）：
{{"intention":[],"synonyms":{{}},"purpose":"","keywords":{{"动词":[],"名词":[]}}}}"""


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
        cache_dir: 可选，默认 skill 目录下 .skill-meta.yaml
    """

    def __init__(
        self,
        llm,
        cache_dir: Optional[Path] = None,
    ):
        self.llm = llm
        self.cache_dir = cache_dir

    def ensure_meta(self, skill: Skill) -> dict:
        """增量保证 .skill-meta.yaml 存在且最新

        如果缓存命中且 SKILL.md 未变（source_hash 一致），
        直接返回缓存内容，不调 LLM。

        Args:
            skill: Skill 对象

        Returns:
            .skill-meta.yaml 的 dict 内容
        """
        skill_dir = Path(skill.directory)
        meta_path = skill_dir / ".skill-meta.yaml"

        # 计算 SKILL.md 的 hash
        src_hash = self._hash_skill(skill)

        # 缓存命中且 SKILL.md 未变
        if meta_path.exists():
            try:
                old = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                if old.get("source_hash") == src_hash:
                    return old
            except (yaml.YAMLError, OSError):
                pass  # 文件损坏或不可读，重抽

        # 需重抽
        data = self._extract_with_llm(skill)
        data["meta_version"] = 2
        data["source_hash"] = src_hash
        data["computed_at"] = datetime.now(timezone.utc).isoformat()
        data["provider"] = "llm"

        # 写回磁盘
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