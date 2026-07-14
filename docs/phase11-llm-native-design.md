# Phase 11: LLM-Native Skill Generation 设计方案

> 更新时间: 2026-07-27
> 相关分支: `create-skills`
> 参与改动: `designer.py`(新), `builtins.py`(新), `creator.py`, `runner.py`, `cli.py`, `tests/test_phase11_12.py`

---

## 一、核心原则（钉死）

### ❻ 铁律：LLM 驱动，非脚手架工具

```
❌ CC 的错误假设（已废弃）：
   skill-engine create my-skill --desc "xxx" --steps "read:exec,analyze:llm"
   → 人写参数 → 程序拼文件

✅ Phase 11 的正确语义：
   skill-engine create "帮我写一个分析 Python 代码质量的 skill"
   → 自然语言意图 → LLM 设计 → Creator 落地 → Validator 验证
```

### ❷ 架构分离：三层各司其职

```
Designer (LLM 层)    → 构造 prompt，调 LLM 生成 JSON design
Creator (渲染器)     → 纯机械写入文件，不碰 LLM
Validator (验证器)   → 编译验证 + 脚本引用检查
```

- **Creator 不需要知道 data 是 LLM 生成的还是手动填的**
- 所有"智能"都在 Designer 的 prompt 里
- 保持纯机械的可测试性

### ❸ CLI 不暴露内部结构

`create` 命令只接受：
- `intent` — 自然语言意图（必填）
- `--name` — 可选，覆盖 LLM 生成的名称
- `--dry-run` — 可选，只输出 JSON 不写文件

**不暴露**：`--steps`, `--scripts`, `--args-def`, `--groups`, `--desc` 等

---

## 二、完整数据流

```
CLI:
  skill-engine create "帮我写一个分析代码的skill" [--name code-analyzer] [--dry-run]
       │
       ▼
runner.py :: create_skill(intent, llm, name, dry_run)
       │
       ├── designer.py :: SkillDesigner.design(intent, llm)
       │     ├── 构造 CREATE_SKILL_PROMPT（含 Few-Shot 示例 + 内置脚本参考）
       │     ├── llm.invoke(prompt) → raw_output
       │     ├── extract_json(raw_output) → design_dict  （3层容错）
       │     └── validate_design(design_dict)            （必要字段校验）
       │
       ├── if dry_run: return design_dict（不写文件）
       │
       ├── 覆盖 name（如果用户指定了 --name）
       │
       ├── 过滤 design 参数（只保留 Creator 支持的字段）
       │
       ├── creator.create(**filtered_design)  ← 纯机械写入
       │     ├── 写 SKILL.md（含 Steps DSL 注入）
       │     ├── 写 scripts/
       │     └── 写 assets/
       │
       ├── validator.full_validate(skill)
       │     ├── validate_compile() — Assembler 编译
       │     └── validate_scripts() — 脚本引用检查
       │
       ├── if invalid: 返回错误（零重试，MVP）
       │
       └── return {name, path, status, valid, errors, design}
```

---

## 三、Prompt 设计（灵魂）

### 核心原则：少说教，多举例

LLM 学样比学规则快。Prompt 包含：
1. 输出格式说明（JSON schema）
2. 一个完整的 Few-Shot 示例
3. 内置脚本的源码参考（让 LLM 直接复用逻辑）
4. 路径变量约定（`${SKILL_DIR}` 等）

### 不包含的内容：
- ❌ 模型名（`sensenova-deepseek` 等）
- ❌ 硬编码的 `python scripts/xxx.py`
- ❌ 内置模板引用机制（`@builtin:write_to_file`）
- ✅ LLM 直接生成完整脚本内容，自包含

### 输出 JSON 格式

```json
{
  "name": "slug-style-name",
  "description": "一句话描述",
  "when_to_use": "详细适用场景",
  "arguments": ["param1", "param2"],
  "groups": ["group1", "group2"],
  "steps": [
    {
      "name": "step_name",
      "type": "exec | llm | read | write",
      "command": "仅在 exec 时填写",
      "template": "仅在 llm/write 时填写",
      "output_file": "仅在 write 时填写",
      "timeout": 30
    }
  ],
  "scripts": {
    "step1.py": "#!/usr/bin/env python3\n...",
    "step2.py": "#!/usr/bin/env python3\n..."
  },
  "assets": {
    "template.md": "# Template..."
  }
}
```

**注意**：`scripts` 的 key 是相对路径，不加 `scripts/` 前缀。`assets` 同理。

---

## 四、容错策略

### JSON 提取（3 层）

1. 直接 `json.loads(text)` — 尝试直接解析
2. 匹配 ` ```json ... ``` ` 代码块 — LLM 爱加的引用
3. 贪婪匹配第一个 `{` 到最后一个 `}` — 最暴力兜底

### Design 校验（MVP 保命级）

- 必填字段：`name`, `description`, `steps`
- `steps` 必须是非空列表
- 每个 step 必须包含 `name` 和 `type`
- `type` 必须是 `exec | llm | read | write` 之一

### 重试策略

- MVP：**零重试**
- 如果 JSON 解析失败或校验失败，返回错误 + 打印 LLM 原始输出
- 未来可扩展：`max_retries` 参数，把错误喂回 LLM 修复

---

## 五、CC 改动的保留/废弃清单

| CC 的改动 | 处理方式 |
|---------|--------|
| Steps DSL 序列化（`_serialize_steps_to_body`） | ✅ 保留 |
| Assets 目录支持（`_write_assets`） | ✅ 保留 |
| 结构化 body 生成（`_generate_structured_body`） | ✅ 保留 |
| validate 增强（assets/ 引用检查） | ✅ 保留 |
| `_parse_steps_from_body`（自动检测） | ✅ 保留 |
| 两个 `run()` 方法 | ⚠️ 留到另一个 PR |
| Windows UTF-8 补丁 | ⚠️ 留到另一个 PR |
| `--steps`/`--scripts`/`--args-def` CLI 参数 | ❌ 删除 |
| `sensenova-deepseek` 硬编码 | ❌ 删除 |
| `command: python scripts/xxx.py` 硬编码 | ❌ 删除 |
| creator.py 内联模板工厂（`_tmpl_*`） | ❌ 移到 builtins.py |
| `_resolve_script_templates` | ❌ 删除（MVP 不用） |
| `script_templates` 参数 | ❌ 删除（MVP 不用） |

---

## 六、文件清单

### 新建文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `src/skill_engine/builtins.py` | ~80 | 三个内置脚本的源码字符串 |
| `src/skill_engine/designer.py` | ~120 | Prompt + JSON 提取 + 校验 |

### 修改文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/skill_engine/creator.py` | 删除 | 删除 `_tmpl_*` 工厂函数、`_BUILTIN_SCRIPT_TEMPLATES`、`_resolve_script_templates`、`script_templates` 参数 |
| `src/skill_engine/runner.py` | 重写 | `create_skill()` 增加 `intent`+`llm` 模式，保留旧参数模式的向后兼容 |
| `src/skill_engine/cli.py` | 重写 | `create` 命令改为 `intent` + `--name` + `--dry-run` |

### 不动文件

| 文件 | 原因 |
|------|------|
| `models.py` | 数据模型不需要改 |
| `executor.py` | 执行器不需要改 |
| `assembler.py` | 编译器不需要改 |
| `config.py` | 模型配置不需要改 |
| `router.py` | 匹配器不需要改 |

---

## 七、测试策略

### 新测试（designer.py）

- `test_extract_json_direct` — 直接合法 JSON
- `test_extract_json_codeblock` — ```json 代码块包裹
- `test_extract_json_greedy` — 有前后废话
- `test_extract_json_invalid` — 返回 None
- `test_validate_design_ok` — 合法 design
- `test_validate_design_missing_fields` — 缺少必填字段
- `test_validate_design_bad_step_type` — 非法 step type

### 旧测试适配（test_phase11_12.py）

- `TestRegistrySkill.test_hot_register` — 改为直接调 `creator.create()` 而非 `runner.create_skill()`
- `TestSkillCreatorEnhanced` — 测试不受影响，`creator.create()` 接口不变
- `TestRunnerStepAutoDetection` — 测试不受影响

---

## 八、已知问题和限制

1. **零重试** — 如果 LLM 生成的设计验证失败，不自动重试，只报错
2. **`_llm_step` 不归 Phase 11 管** — 那属于 Phase 3/4 的执行层维护
3. **两个 `run()` 方法** — 不在此 PR 修复，留到后续
4. **Windows UTF-8 补丁** — 不在此 PR 修复，留到后续
5. **`--steps` 运行时 flag** — 保留在 `run` 命令中，仅删除 `create` 命令的对应参数

---

## 九、当前进度（2026-07-27）

### 已完成

- [x] 新建 `builtins.py`、`designer.py`
- [x] `creator.py` 清理：删除模板工厂、`script_templates` 参数
- [x] `runner.py`：`create_skill()` 双模式（LLM + 直接模式向后兼容）
- [x] `cli.py`：`create` 命令重写为 `intent` + `--name` + `--dry-run`
- [x] 测试：43 个测试全部通过
- [x] 文档：`docs/phase11-llm-native-design.md`

### 待验证

- [ ] LLM 模式端到端测试：`skill-engine create "..."` 实际调用 LLM 时，`llm.invoke()` 返回后无输出、skill 未创建
  - 可能原因：extract_json 失败或 validate_design 失败导致 ValueError 被抛出，但异常信息未显示
  - 下一步：先用 `--dry-run` 查看 LLM 返回的原始内容，定位问题