# 🎯 AI Interview Coach — Claude Code Skill v2.0

> **AI 大模型应用开发专属深度面试教练**
> 数字孪生驱动 · 多风格模拟 · 剥洋葱追问 · 靶向压力测试 · 黄金话术纠偏 · JD Gap 分析

---

## ✨ 核心能力一览

### 🧬 输入层：数字孪生驱动

| 能力 | 说明 |
|---|---|
| 结构化简历解析 | 自动读取并解析简历原文 |
| **项目防拷打档案** | 每个项目独立文件，含 STAR 扩展、技术选型对比、踩坑记录、失败反思 |
| **能力自评矩阵** | JSON 格式自评 10 项核心技能（1-10 分），驱动靶向压力测试 |
| **目标 JD 分析** | 解析 JD 要求，与自评做 Gap 分析，生成定向 Mock 题 |
| **Prompt 调优日志** | 读取你的 Prompt 迭代记录，评估工程深度 |
| **线上 Bad Case** | 读取真实踩坑经历，丰富追问素材 |

### 🎭 机制层：智能面试策略

| 能力 | 说明 |
|---|---|
| **剥洋葱连环追问** | 根据回答动态生成下一层追问，直到触及底层原理 |
| **靶向短板压力测试** | 对自评 ≤ 3 分的技能突然发难，逼出真实水平 |
| **4 种面试官风格** | 温和型 / 压力型 / 深挖型 / 业务型，支持动态切换 |
| **防作弊验证** | 读取防拷打档案后，用反向问题验证真实性 |
| **情绪稳定性追踪** | 记录压力下的反应模式，评估抗压能力 |
| **风格动态切换** | 根据表现实时切换（滔滔不绝→打压，卡壳→给机会） |

### 📊 输出层：增量反馈

| 能力 | 说明 |
|---|---|
| **详细面试报告** | 逐题记录 + 得分 + 追问路径 |
| **能力雷达图** | 8 维度 JSON 数据，可可视化 |
| **黄金话术纠偏** | 你的回答 vs 满分回答对比 + 升级路径 |
| **JD Gap 分析** | 自评 vs 实测 vs JD 要求三方对比 |
| **缺陷模式诊断** | 识别"没数字/背八股/只说工具"等 8 种缺陷 |
| **备考计划** | 3-7 天可执行方案 |
| **跨轮次追踪** | 薄弱项后续验证，强项深度挖掘 |

---

## 📁 目录结构

```
ai-interview-coach/
├── SKILL.md                          # 🔑 主入口（Claude Code 自动发现）
├── config.json                       # ⚙️ 完整配置（v2.0）
├── README.md                         # 📖 本文件
│
├── references/                       # 📚 知识库（渐进式披露）
│   ├── question-bank-core.md         #   核心题库
│   ├── round1-fundamentals.md       #   第1轮题库
│   ├── round2-system-design.md       #   第2轮题库
│   ├── round3-behavioral.md         #   第3轮题库
│   ├── scoring-rubric.md            #   评分标准
│   ├── digital-twin-input.md        #   🆕 数字孪生输入规范
│   ├── interviewer-styles.md        #   🆕 面试官风格引擎
│   ├── golden-answer.md             #   🆕 黄金话术纠偏机制
│   ├── jd-gap-analysis.md           #   🆕 JD Gap 分析
│   └── resume-parser.md             #   简历解析规则
│
├── assets/
│   └── report-template.md           # 📝 报告模板（含话术纠偏+Gap章节）
│
├── scripts/
│   ├── interview-state.py           #   状态管理
│   ├── generate-report.py          #   报告生成器
│   └── sample-interview-data.json  #   示例数据
│
├── state/
│   └── interview-history.json      # 💾 跨轮次状态
│
└── resume/                          # 👈 你的输入材料放这里
    ├── resume.md                    #   简历原文
    ├── self-assessment.json         #   ⭐ 能力自评矩阵
    ├── project-deep-dive/           #   ⭐ 项目防拷打档案
    │   └── template.md             #     模板
    ├── target-jd/                   #   目标岗位 JD
    ├── prompt-logs/                 #   Prompt 调优日志
    └── online-cases/                #   线上 Bad Case
```

---

## 🚀 快速开始

### 1. 安装

```bash
# 个人级（所有项目可用）
cp -r ai-interview-coach ~/.claude/skills/

# 或项目级（仅当前项目）
cp -r ai-interview-coach .claude/skills/
```

### 2. 准备输入材料（关键！）

**最低配置**（10 分钟）：
1. 把简历放进 `resume/resume.md`
2. 填写 `resume/self-assessment.json`（给每项技能打 1-10 分）

**推荐配置**（2 小时，效果提升 10 倍）：
3. 复制 `project-deep-dive/template.md`，为每个项目写一份防拷打档案
4. 把目标公司 JD 放进 `target-jd/` 目录

**进阶配置**：
5. 记录你的 Prompt 迭代日志到 `prompt-logs/`
6. 整理线上踩坑经历到 `online-cases/`

### 3. 启动面试

```
/ai-interview-coach
```

或直接说：
> "开始模拟面试" / "压力测试我" / "对比我的 JD" / "帮我纠偏话术"

---

## 💬 全部指令

| 你说 | 做什么 |
|---|---|
| "开始面试" / "模拟面试" | 完整面试流程 |
| "第1轮面试" | 只做基础技术面 |
| "第2轮面试" | 只做项目深挖 + 系统设计 |
| "第3轮面试" | 只做 Behavioral + 业务思维 |
| "压力测试我" | 🆕 针对薄弱项的纯压力模式 |
| "对比我的 JD" | 🆕 JD Gap 分析 + 定向 Mock |
| "帮我纠偏话术" | 🆕 上传回答 → 给黄金话术 |
| "查看面试历史" | 所有轮次记录 + 综合评估 |
| "生成面试报告" | 基于记录生成详细报告 |
| "我的薄弱项是什么" | 分析所有轮次薄弱点 |
| "帮我准备下一轮" | 生成备考计划 |

---

## 📊 评分体系

### 各轮权重

**第1轮**（基础面）：八股 25% + 项目 30% + 编码 25% + AI使用 10% + 沟通 10%
**第2轮**（深度面）：项目深度 35% + 架构 30% + 选型 20% + 工程化 15%
**第3轮**（Behavioral）：业务 25% + STAR 25% + 协作 20% + 规划 15% + 提问 15%

### 综合公式

```
总分 = R1×30% + R2×40% + R3×30%
```

### 决策映射

| 总分 | 建议 |
|---|---|
| 4.5-5.0 | ✅ Strong Hire |
| 4.0-4.4 | ✅ Hire |
| 3.5-3.9 | ⚠️ Lean Hire |
| 3.0-3.4 | ⚠️ 待定 |
| < 3.0 | ❌ No Hire |

---

## 🆕 v2.0 新增功能详解

### 1. 数字孪生输入

不只是简历，而是你的完整技术画像：
- 自评矩阵驱动靶向压力测试
- 项目防拷打档案驱动精准追问
- JD 文件驱动 Gap 分析和定向 Mock

### 2. 面试官风格引擎

4 种风格 + 动态切换：
- 🟢 温和型：适合校招/转行
- 🔴 压力型（默认）：适合大厂/高阶
- 🔵 深挖型：适合专家岗
- 🟡 业务型：适合创业公司

### 3. 黄金话术纠偏

不只打分，还给你满分样板：
- Level 1-4 分级
- 原回答 vs 黄金话术对比
- 缺陷模式诊断（8 种）
- 三步升级路径

### 4. JD Gap 分析

- 解析 JD 提取技能要求
- 与自评矩阵做 Gap 对比
- 生成定向 Mock 题
- 面试前预警高危缺口
- 面试后更新实测分数

### 5. 靶向压力测试

独立模式 + 嵌入正常面试：
- 对自评 ≤ 3 分技能突然发难
- 对吹牛行为连环追问到穿帮
- 沉默施压 + 打断式追问
- 抗压能力评估报告

---

## 🔧 脚本使用

```bash
# 状态管理
python scripts/interview-state.py init "张三" "resume/resume.md"
python scripts/interview-state.py add-round 1 3.6 true "reports/..." "RAG,Python" "系统设计,推理优化"
python scripts/interview-state.py summary
python scripts/interview-state.py update-assessment
python scripts/interview-state.py add-weakness critical "系统设计深度不足"
python scripts/interview-state.py add-strength "RAG全链路经验"
python scripts/interview-state.py clear

# 报告生成
python scripts/generate-report.py --round 1 --data scripts/sample-interview-data.json
```

---

## 📄 License

MIT License
