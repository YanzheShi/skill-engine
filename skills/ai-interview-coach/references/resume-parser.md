# 简历解析规则（Resume Parser Guide）

> 本文件指导 Skill 如何从用户简历中提取结构化信息，用于个性化面试。

---

## 一、支持的文件格式

| 格式 | 处理方式 |
|---|---|
| `.md` / `.txt` | 直接 Read 解析 |
| `.pdf` | 用 `pdf` skill 提取文字 |
| `.docx` | 用 `docx` skill 提取文字 |

---

## 二、简历存放位置

```
resume/
  ├── resume.md       ← 优先读取
  ├── resume.pdf       ← 备选
  └── resume.docx      ← 备选
```

如果目录不存在或为空，向用户提示上传简历。

---

## 三、需提取的结构化字段

解析简历后，在内部构建如下 JSON（不写入文件，仅用于面试个性化）：

```json
{
  "basic_info": {
    "name": "",
    "years_of_experience": 0,
    "education": "",
    "current_location": ""
  },
  "target_role": {
    "title": "AI大模型应用开发",
    "level": "中级|高级|初级",
    "preferred_companies": []
  },
  "skill_stack": {
    "languages": ["Python", "Go"],
    "llm_frameworks": ["LangChain", "LlamaIndex"],
    "vector_dbs": ["Milvus", "Chroma"],
    "model_experience": ["GPT-4", "Qwen", "DeepSeek"],
    "deployment": ["vLLM", "Docker", "K8s"],
    "other": ["RAG", "Agent", "微调"]
  },
  "projects": [
    {
      "name": "",
      "role": "",
      "duration": "",
      "description": "",
      "tech_stack": [],
      "key_metrics": "",
      "my_contribution": ""
    }
  ],
  "experience_timeline": [
    {
      "company": "",
      "role": "",
      "start_date": "",
      "end_date": "",
      "highlights": ""
    }
  ],
  "red_flags": [],
  "green_flags": []
}
```

---

## 四、解析后的面试个性化策略

### 4.1 根据技能栈调整题目

| 简历关键词 | 面试侧重调整 |
|---|---|
| 有 RAG 项目 | 深挖 RAG 全链路（分块/检索/重排/评测） |
| 有 Agent 项目 | 深挖 Agent Loop / 工具调用 / 多 Agent 编排 |
| 有微调经验 | 加考 LoRA / DPO / 训练资源配置 |
| 有向量数据库经验 | 加考 ANN 算法 / 索引优化 / 大规模检索 |
| 只有 API 调用经验 | 重点考 Prompt 工程 + 系统设计补足深度 |
| 无 AI 项目但有传统后端 | 从基础八股+编码入手，项目题改用场景设计题 |

### 4.2 根据工作年限调整深度

| 年限 | 八股权重 | 项目权重 | 系统设计权重 | 编码权重 |
|---|---|---|---|---|
| 0-1年（校招/转行） | 30% | 25% | 15% | 30% |
| 1-3年 | 25% | 35% | 20% | 20% |
| 3-5年 | 15% | 40% | 30% | 15% |
| 5年+ | 10% | 35% | 40% | 15% |

> 注意：以上为调整系数，需在 SKILL.md 默认权重基础上浮动 ±10%

### 4.3 Red Flag 检测

以下情况在解析时标记为 `red_flags`，面试中重点验证：

- ⚠️ 项目描述全是"我们"没有"我" → 可能非核心贡献者
- ⚠️ 技能栈列了 20+ 项但项目只有 1 个 → 可能夸大
- ⚠️ 项目时间重叠严重 → 需要确认真实投入
- ⚠️ 没有量化指标（全篇"提升了效果"） → 追问必问数字
- ⚠️ GitHub/作品集链接缺失或为空 → 编码能力需重点验证

### 4.4 Green Flag 检测

以下情况标记为 `green_flags`，面试中可作为加分锚点：

- ✅ 项目有精确数字（"延迟从 3s→800ms"、"准确率 89%"）
- ✅ 有开源贡献 / 技术博客 / 社区活跃
- ✅ 有从 0 到 1 的完整项目经历
- ✅ 有跨团队协作 / 带人经验
- ✅ 有 AI 产品的真实用户数据和迭代记录

---

## 五、简历缺失时的降级策略

如果候选人没有简历：

1. 用 `AskUserQuestion` 收集关键信息：
   - 工作年限
   - 最熟悉的技术栈
   - 做过的最有挑战的项目（一句话描述）
   - 目标公司/岗位级别

2. 根据回答构建一个"虚拟候选人画像"

3. 在面试开始时告知："我没有你的简历，会根据你刚才说的来个性化提问"

4. 面试结束后，建议候选人补一份简历以便更精准的评估
