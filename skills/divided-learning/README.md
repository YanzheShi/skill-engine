# Skill: Divided Learning — 多角色分工学习系统 v2.0

> 一套结构化、可沉淀的多 AI 角色协作学习框架，
> 帮你深度吃透项目代码、提升架构表达力、从容应对技术面试。
> **v2 新增**：测试视角、SRE 运维视角、安全审计三个扩展角色。

---

## 快速上手（3 步）

### Step 1：了解系统
阅读 `SKILL.md` — 系统总纲，包含角色列表、协作原则、标准流程。

### Step 2：准备上下文
复制 `templates/context-input.md` 的内容，填写你要学习的模块信息。
**新**：可以勾选启用哪些扩展角色（测试/SRE/安全）。

### Step 3：启动会话
将以下内容发送给 AI：

```
请加载 divided-learning v2，按流程启动新会话。

目标模块：<你的模块名>
启用扩展角色：测试视角, SRE视角, 安全视角
（粘贴 context-input 的内容）
```

---

## 文件导航

| 文件 | 用途 | 谁该读 |
|---|---|---|
| `SKILL.md` | 系统入口与总纲 | 所有人（先读这个） |
| `roles/lecturer.md` | 📖 代码讲解者 | AI 自动加载 |
| `roles/interviewer.md` | 🔍 面试官·质疑者 | AI 自动加载 |
| `roles/arch-reviewer.md` | 🏛️ 架构评审员 | AI 自动加载 |
| `roles/tester.md` | 🧪 测试视角官（**v2 新增**） | AI 自动加载 |
| `roles/sre.md` | 🔧 SRE 运维视角官（**v2 新增**） | AI 自动加载 |
| `roles/security.md` | 🛡️ 安全审计官（**v2 新增**） | AI 自动加载 |
| `roles/orchestrator.md` | 🎯 流程主持与阶段控制 | AI 自动加载 |
| `templates/learning-log.md` | 📋 面试卡片模板（含 Tags） | Human 查看/编辑 |
| `templates/context-input.md` | 📥 上下文投放模板（含扩展角色声明） | Human 填写 |
| `scripts/extract-cards.py` | 🔎 按标签汇总面试卡片（**v2 新增**） | Human 运行 |
| `examples/session-example.md` | 📝 完整会话示例 | Human 参考 |
| `learning-logs/` | 📂 已生成的面试卡片（示例数据） | Human 积累 |

---

## 七大角色一览

```
                    ┌──────────────────────┐
                    │   🧑 Human           │
                    │   (你)               │
                    │   开发者/架构者       │
                    └──────────┬───────────┘
                               │ 提供上下文
                               ▼
                    ┌──────────────────────┐
                    │  📖 Lecturer         │──→ 讲清 What & Why
                    │  代码讲解者           │
                    └──────────┬───────────┘
                               │ 讲解完毕
                               ▼
                    ┌──────────────────────┐
                    │  🗣️ Human 复述       │──→ 模拟面试回答
                    └──────────┬───────────┘
                               │ 回答完毕
                               ▼
                    ┌──────────────────────┐
                    │  🔍 Interviewer      │──→ 追问暴露盲区
                    │  面试官·质疑者        │
                    └──────────┬───────────┘
                               │ 追问结束
                               ▼
                    ┌──────────────────────┐
                    │  🏛️ ArchReviewer     │──→ 战略评审
                    │  架构评审员           │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │  🧪 Tester   │ │  🔧 SRE      │ │  🛡️ Security │
    │  测试视角官   │ │  运维视角官   │ │  安全审计官   │
    │  覆盖率/边界  │ │  监控/灾备    │ │  注入/越权    │
    └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
           └────────────────┼────────────────┘
                           ▼
                    ┌──────────────────────┐
                    │  📋 Learning Log     │──→ 带 Tags 的面试卡片
                    │  + extract-cards.py  │──→ 按标签检索汇总
                    └──────────────────────┘
```

---

## extract-cards.py 使用指南

### 前置条件
将每轮的 Learning Log 保存为 `learning-logs/<模块名>-<日期>.md`。

### 命令一览

```bash
# 统计概览：你一共学了多少模块、多少盲区、多少待办
python3 scripts/extract-cards.py --dir learning-logs --stats

# 列出所有标签：看看你覆盖了哪些知识点
python3 scripts/extract-cards.py --dir learning-logs --list-tags

# 按标签筛选：把所有"分布式锁"相关问答汇总
python3 scripts/extract-cards.py --dir learning-logs --tag "分布式锁"

# 按标签筛选并保存
python3 scripts/extract-cards.py --dir learning-logs --tag "SQL注入" -o security.md

# 按模块名筛选
python3 scripts/extract-cards.py --dir learning-logs --module "order_lock"

# 导出全部汇总为一个文件
python3 scripts/extract-cards.py --dir learning-logs --export all-cards.md
```

### 标签系统说明

Learning Log 中的 Tags 分 11 个类别：

| 类别 | 示例标签 |
|---|---|
| `architecture` | 微服务、单体、事件驱动、CQRS、DDD |
| `patterns` | 工厂、策略、观察者、装饰器 |
| `concurrency` | goroutine、channel、mutex、分布式锁、幂等 |
| `distributed` | CAP、一致性、消息队列、主从复制 |
| `data` | 索引、事务隔离级别、乐观锁、连接池 |
| `middleware` | Redis、Kafka、gRPC、Nginx |
| `security` | SQL注入、XSS、越权、认证、加密 |
| `testing` | 单元测试、集成测试、混沌工程 |
| `sre` | 监控、告警、熔断、限流、链路追踪 |
| `interview_topics` | 系统设计、算法、权衡决策 |
| `custom` | 你自定义的项目特定标签 |

---

## 设计原则

### 1. 角色隔离
讲解者只讲、质疑者只问、评审员只看大局、测试只看覆盖、SRE 只看运维、安全只看漏洞。

### 2. 阶段化流程
核心 5 阶段 + 可选 3 扩展阶段，不允许跳步（除非你明确要求结束）。

### 3. 对抗式学习
质疑者的存在不是为了帮你，是为了"为难你"。压力下的表达才是真理解。

### 4. 六维全景
架构美不美只是一维。测试能覆盖吗？半夜挂了能恢复吗？攻击者能突破吗？

### 5. 结构化沉淀
每轮产出带 Tags 的 Learning Log → 积累 → 用脚本检索 → 你的面试备战手册。

---

## 适用场景

- ✅ 接手新项目/新模块，需要快速吃透
- ✅ 准备技术面试，需要模拟追问
- ✅ 架构评审前，自我审视设计
- ✅ 代码 review 后，深度复盘
- ✅ **安全审计**：检查注入、越权、数据泄露风险
- ✅ **运维加固**：补齐监控、告警、灾备缺口
- ✅ **测试补强**：系统性发现未覆盖的关键路径

---

## Changelog

### v2.0.0 (2026-07-25)
- ➕ 新增 🧪 测试视角官（覆盖率审计、边界用例、可测试性）
- ➕ 新增 🔧 SRE 运维视角官（可观测性、告警、灾备、容量）
- ➕ 新增 🛡️ 安全审计官（注入、越权、数据泄露、供应链）
- 🔄 Learning Log 增加 Tags 系统（YAML 格式，11 个类别）
- ➕ 新增 `scripts/extract-cards.py`（统计/标签检索/导出）
- 🔄 工作流升级为 5+3 阶段
- 🔄 评分卡从 20 分升级为 24 分

### v1.0.0 (2026-07-25)
- 🎉 初始版本：4 角色 + 5 阶段 + 2 模板 + 1 示例

---

## License

MIT — 随意修改、复用、集成到你的工作流中。
