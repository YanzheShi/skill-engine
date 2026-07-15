# skill-engine Router 重构实施文档

**版本**：V0.2 → V0.3（路由管线重设计）
**日期**：2026-07-28
**背景**：≤80 skill、中文用户为主、含第三方英文 SKILL.md、需支持多 skill 协同；
砍掉 embedding，保留 name/alias 精确匹配 + intention 关键词 + LLM 兜底（三触发）+ 全量/增量预处理。

---

## 一、当前 Router V0.2 的问题

| 问题 | 影响 |
|------|------|
| embedding 分支重型（sentence-transformers + numpy） | 80 skill 场景下 ROI 为负，启动慢、依赖重 |
| keyword 评分是伪精度（desc +0.5 / when +0.3） | 出题/解题 类歧义分不清，分数不可比 |
| `_extract_keywords` 中文按字符拆 | 完全没用，中文动词/名词全丢 |
| `_match_by_llm` 返回 list[MatchResult] | 撑不住多 skill 协同，DEBUG print 没清 |
| 无预处理层 | SKILL.md 描述质量不可控（尤其第三方英文） |
| 无增量机制 | SKILL.md 改了不会重算 |

重构目标：砍 embedding → 留 name/alias → keyword 换 intention 权重 → LLM 兜底三触发 → 多 skill 协同 → 独立预处理模块（全量+增量）。

---

## 二、重构后整体架构

```
CLI (skills-engine list / match / run / index)
  │
  ▼
Discovery (扫多根 → index{name: SkillMeta})
  │
  ▼
Preprocessor  ←── LLM factory（抽 intention/synonyms/keywords，写 .skill-meta.yaml）
  │               source_hash 增量，第三方 skill 也能抽
  ▼
Registry (load_skill / load_meta / list_active)
  │
  ▼
Router.match(query) → MatchPlan
  1️⃣ name / alias / shortcut 精确（score=1.0）→ MatchPlan(mode=single)
  1.5️⃣ 纯英文 exact 没中 → 直接跳 LLM
  2️⃣ 结巴分词 + intention 打分 → 多候选?
  3️⃣ LLM 兜底（0 命中 / 多匹配）→ MatchPlan(single | multi)
  │
  ▼
Runner.run(plan)  ← 支持 mode=single / mode=multi（按 role 顺序或并行）
```

---

## 三、Model 层变更（models.py）

### 删除
- ~~MatchResult 作为 router 主返回~~（降级为 preprocessor/debug 用，可选保留）

### 新增

```python
class SelectedSkill(BaseModel):
    name: str
    role: str | None = None
    args_override: dict | None = None


class MatchPlan(BaseModel):
    mode: Literal["single", "multi"] = "single"
    primary: SelectedSkill | None = None
    selections: list[SelectedSkill] = []
    method: str
    score: float | None = None
    reason: str | None = None
    uncertain: bool = False
```

### SkillMetadata 扩展

```python
# 引擎扩展字段（SKILL.md 里可写，也可由 .skill-local.yaml 覆写）
alias: list[str] | None = None
shortcuts: list[str] | None = None
intent_verbs: list[str] | None = None
```

### MergedMeta

Registry.load_meta() 的三层合并结果：
- SKILL.md frontmatter（基础）
- .skill-meta.yaml（LLM 抽取，带 `_meta_cache`）
- .skill-local.yaml（用户覆写，优先级最高）

---

## 四、Preprocessor 独立模块（preprocessor.py）

### 职责

- 接收 Skill 对象（含 SKILL.md 原始文本）
- 调 LLM 抽 intention / synonyms / purpose / keywords（temp=0）
- 写 .skill-meta.yaml，带 source_hash 增量
- 第三方英文 SKILL.md 也能抽（不要求作者改）

### 接口

```python
class Preprocessor:
    def __init__(self, llm, cache_dir: Path | None = None)
    def ensure_meta(self, skill: Skill) -> dict
    def batch_ensure(self, skills: list[Skill]) -> dict[str, dict]
```

### LLM 抽取 Prompt

```python
PROMPT_EXTRACT = """你是一个 Skill 元数据提取器。根据 SKILL.md 内容提取结构化字段，返回 JSON。

要求：
1. intention：这个 skill 的「核心动作/目的」，中文动词短语，2-4 个
2. synonyms：intention 中每个词的中文近义词 + 英文对应词
3. purpose：一句话说明这个 skill 干什么，30 字以内
4. keywords.动词：描述里出现的动作词（中英文原形）
5. keywords.名词：描述里出现的领域名词（中英文原形）

返回 JSON：
{{"intention":[...],"synonyms":{{...}},"purpose":"...","keywords":{{"动词":[...],"名词":[...]}}}}"""
```

### .skill-meta.yaml schema

```yaml
meta_version: 2
source_hash: abc123
computed_at: "202X-..."
provider: "llm"
intention: [解题, 写题解, 分析算法]
synonyms:
  解题: [做题, 写题, solve, 答lc]
  题解: [solution, 解答]
purpose: "根据用户给的 LeetCode 题目，生成带复杂度分析的 Markdown 题解"
keywords:
  动词: [解题, 分析, 写]
  名词: [leetcode, 二叉树, dfs, bfs, 算法, 复杂度]
```

### .skill-local.yaml schema

```yaml
intention: [出题, 生成, 造题]
alias: [lc-gen, 出lc]
shortcuts: [lcg]
synonyms:
  出题: [出, generate]
router_proper_en_append:
  - mistral
  - vllm
```

加载优先级：SKILL.md < .skill-meta.yaml（LLM 抽） < .skill-local.yaml（用户覆写）

---

## 五、Router 重构（router.py）

### 接口

```python
THRESH_SINGLE = 0.7

class Router:
    def __init__(self, registry, preprocessor=None)
    def match(self, query, *, llm=None, top_k=3) -> MatchPlan
```

### 三步路由

1. **精确**：name / alias / shortcut 匹配（不挑语言）
2. **纯英文 exact 没中**：直接跳 LLM（skip keyword）
3. **中文/中英混**：结巴分词 + intention 权重打分 → 三触发 LLM
   - 0 命中 → LLM
   - 单候选高分（≥0.7）→ 直接返
   - 多候选 / 单候选低分 → LLM 决定

### `_match_exact` 修正

```python
for n in self.registry.list_active():
    if n.lower() == q:
        return n  # 返回注册表原始 name，不是 query.strip()
```

### score_keyword 权重公式

```python
# 动词：每个 query 动词取最高路径
#   intention 命中 → 0.6 (max, 不累加)
#   synonym 命中  → 0.55 (max, 不累加)
#   keyword.动词 → 0.25 (max, 不累加)
# 名词：累加封顶 0.3
# 整体封顶 1.0
```

### `_should_llm` 三触发

```python
def _should_llm(self, query, kws, qtokens) -> (bool, list[str], str):
    if not kws:                     → True, active, "0 命中"
    if single >= THRESH_SINGLE:     → False, [], ""  (直接返)
    else:                           → True, top10, "多候选/低分"
```

---

## 六、Registry 配套变更

```python
class Registry:
    _meta_cache: dict[str, MergedMeta] = {}
    _meta_mtime: dict[str, float] = {}

    def load_meta(self, name: str) -> MergedMeta
    def _merge_meta(self, skill, meta_path, local_path) -> MergedMeta
```

- 缓存 key=name，失效盯 .skill-local.yaml 的 mtime
- .skill-meta.yaml 的 source_hash 防 SKILL.md 变更（Preprocessor 层的事）

---

## 七、预处理触发矩阵

| 命令 | 触发 Preprocessor？ | 说明 |
|------|--------------------|------|
| list / scan / info | ❌ 不触发 | 只扫 index（SkillMeta 轻量） |
| `index` | ✅ 全量 batch_ensure | 显式命令，用户预期慢 |
| `index --build-meta` / `--rebuild-meta` | ✅ 强制全量 | 同上 |
| `match` / `run`（首次遇到无 meta） | ✅ lazy 单条 | 仅在该 skill 第一次 load_meta 时触发 |
| `match` / `run`（已有 meta + hash 命中） | ❌ | 直接读缓存 |

Preprocessor 不存在时，Registry 返回"裸 meta"（只有 SKILL.md 字段），score_keyword 给 0 分，自动进 LLM 兜底。

---

## Phase AB 完成记录（2026-07-28）

### 新增文件
- `src/skill_engine/tokenize.py` — tokenize_query（结巴）+ extract_proper_en（整 token）+ is_english
- `src/skill_engine/preprocessor.py` — Preprocessor.ensure_meta / batch_ensure / PROMPT_EXTRACT
- `src/skill_engine/scoring.py` — score_keyword 新权重公式
- `tests/test_tokenize.py` — 13 个测试
- `tests/test_scoring.py` — 16 个测试（含出题/解题歧义 case）
- `tests/test_models_phaseAB.py` — 12 个测试

### 修改文件
- `src/skill_engine/models.py` — 新增 MatchPlan / SelectedSkill / MergedMeta，扩展 SkillMetadata（alias/shortcuts/intent_verbs）

### 关键设计决策（代码落地修正）
- `meta_cache` 代替 `_meta_cache`（Pydantic V2 不允许下划线开头字段名）
- `model_config` 代替 `class Config`（Pydantic V2 语法）
- 中文单字动词不做 isascii 过滤（`写`、`出`等是合法动词）
- is_english 阈值定为 0.7（"solve leetcode problem 104" 20/26=0.769 通过）

### 测试结果
- 新测试 39/39 通过
- 老测试 135/135 通过
- 总计 174/174 通过

---

## Phase CDE 完成记录（2026-07-28）

### 已完成

| 模块 | 状态 |
|------|------|
| Router 三步路由（删 embedding / _match_exact / _should_llm / _llm_fallback） | ✅ |
| Registry load_meta 三层合并 + _meta_cache / _meta_mtime | ✅ |
| CLI match --explain | ✅ |
| CLI run 接 MatchPlan | ✅ |
| CLI index 命令（增量/全量/强制重抽） | ✅ |
| Discovery 默认只扫项目目录 | ✅ |
| pyproject.toml 清理（删 embedding 组，加 tokenize 组） | ✅ |
| 删旧 DEBUG print | ✅ |

### 剩余待做（非阻塞）

- ~~[x] numpy 从主依赖摘除（需 uv sync 后验证）~~
- ~~[x] `pip install skill-engine[tokenize]` 装 jieba~~
- ~~[x] Runner 直接接 MatchPlan（目前用 MatchResult 包装兼容）~~
- ~~[x] Preprocessor 传入 Router 实现 lazy 调用~~
- [ ] Preprocessor 单测 `test_preprocessor.py`（需要 mock LLM）

---

## 八、Phase 分步实施

### Phase AB：Models + Preprocessor + Tokenize + Score
- [ ] models.py：MatchPlan / SelectedSkill / MergedMeta，扩展 SkillMetadata
- [ ] tokenize.py：tokenize_query（结巴）+ extract_proper_en + is_english
- [ ] preprocessor.py：ensure_meta + batch_ensure + PROMPT_EXTRACT
- [ ] score_keyword 新权重公式
- [ ] 单测：出题/解题 歧义 case

### Phase C：Router 重构
- [ ] 删 embedding 整块
- [ ] _match_exact（name/alias/shortcut）
- [ ] match() 三步逻辑 + _should_llm 三触发
- [ ] _llm_fallback → MatchPlan(single|multi)

### Phase D：Registry + Discovery + CLI 配套
- [ ] Registry 加 _meta_cache + load_meta 三层合并
- [ ] Discovery 收窄（只扫 frontmatter）
- [ ] CLI：index / match --explain / run 接 MatchPlan

### Phase E：清理
- [ ] 删旧 DEBUG print
- [ ] pyproject.toml：摘 sentence-transformers / numpy
- [ ] jieba 进 optional-dependencies

---

## 九、风险点 & 决策记录

| 决策 | 理由 |
|------|------|
| 砍 embedding | 80 skill 下 ROI 负，keyword+intention 已覆盖 |
| LLM 抽 .skill-meta.yaml 而非手写 | 第三方 skill 质量不可控，LLM 抽一次永久受益 |
| 多候选一律进 LLM（不分 top1 高分自己吞） | "不论分高低多匹配都让 LLM 决定" |
| `_meta_cache` 挂 MergedMeta 扩展字段 | 不污染 agentskills.io 标准字段 |
| alias（语义）/ shortcut（缩写）分两种 | CLI 场景 shortcut 命中率比 alias 还高 |
| 纯英文 exact 没中 → 跳 keyword 直进 LLM | keyword 对纯英文准确率低 |
| intention 单动词 max + 名词累加封顶 0.3 | 防分数坍缩到 1.0 失去区分度 |
| primary + selections 双字段 | single 调用方更舒服，multi 信息也全 |