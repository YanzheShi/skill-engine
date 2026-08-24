#!/usr/bin/env python3
"""
generate-report.py
将面试数据填入报告模板，生成最终的 Markdown 报告

用法:
  python generate-report.py --round 1 --data interview-data.json
"""

import json
import re
import sys
import os
import argparse
from datetime import datetime

SKILL_DIR = os.path.dirname(os.path.dirname(__file__))
TEMPLATE_PATH = os.path.join(SKILL_DIR, "assets", "report-template.md")
REPORTS_DIR = os.path.join(SKILL_DIR, "reports")


def load_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def fill_questions(template, questions):
    """将题目列表渲染为 Markdown 并替换模板中的占位题目区域"""
    if not questions:
        return template.replace(
            "### 题目 1：{{QUESTION_1}}",
            "### 题目记录\n\n（无题目记录）"
        )

    q_sections = []
    for i, q in enumerate(questions, 1):
        followups = q.get("followups", [])
        fup_text = "\n".join(
            f"{j+1}. {fup}" for j, fup in enumerate(followups)
        ) if followups else "（无追问）"

        section = f"""
### 题目 {i}：{q.get('question', '')}

**候选人回答摘要**：
{q.get('answer_summary', '')}

**追问路径**：
{fup_text}

**评估**：{q.get('assessment', '')}
**得分**：{q.get('score', 0)} / 5

---
"""
        q_sections.append(section)

    return template.replace(
        "### 题目 1：{{QUESTION_1}}",
        "### 题目记录\n" + "".join(q_sections)
    )


def fill_golden_answers(template, golden_answers):
    """填充黄金话术纠偏章节"""
    if not golden_answers:
        # 移除整个话术纠偏章节
        start = template.find("## 🎤 黄金话术纠偏")
        if start == -1:
            start = template.find("## 🎤 话术纠偏")
        if start != -1:
            end = template.find("## ", start + 1)
            if end == -1:
                end = len(template)
            section = template[start:end]
            # 找下一章节标题来截断
            next_section = template.find("\n## ", start + 1)
            if next_section != -1:
                return template[:start] + "\n（本轮无话术纠偏数据）\n" + template[next_section:]
            return template[:start] + "\n（本轮无话术纠偏数据）\n"
        return template

    result = template

    # 填充纠偏 1
    if len(golden_answers) >= 1:
        ga = golden_answers[0]
        result = fill_single_golden(result, 1, ga)

    # 填充纠偏 2
    if len(golden_answers) >= 2:
        ga = golden_answers[1]
        result = fill_single_golden(result, 2, ga)

    # 填充纠偏 3
    if len(golden_answers) >= 3:
        ga = golden_answers[2]
        result = fill_single_golden(result, 3, ga)

    return result


def fill_single_golden(template, idx, ga):
    """填充单个黄金话术对比"""
    result = template
    result = result.replace("{{GOLDEN_Q" + str(idx) + "}}", ga.get("question", ""))
    result = result.replace("{{GOLDEN_L" + str(idx) + "}}", str(ga.get("level", "")))
    result = result.replace("{{GOLDEN_A" + str(idx) + "}}", ga.get("candidate_answer", ""))
    result = result.replace("{{GOLDEN_IDEAL_" + str(idx) + "}}", ga.get("golden_answer", ""))

    diags = ga.get("diagnoses", [])
    for d_idx, diag in enumerate(diags, 1):
        result = result.replace("{{GOLDEN_DIAGNOSIS_" + str(d_idx) + "}}", diag)

    tips = ga.get("tips", [])
    for t_idx, tip in enumerate(tips, 1):
        letter = chr(ord("A") + t_idx - 1)  # A, B, C...
        result = result.replace("{{GOLDEN_TIP_" + str(idx) + letter + "}}", tip)

    return result


def fill_jd_gap(template, jd_gap):
    """填充 JD Gap 分析章节"""
    if not jd_gap:
        return template

    result = template

    skills = jd_gap.get("skills", [])
    for i, skill in enumerate(skills, 1):
        result = result.replace("{{JD_SKILL_" + str(i) + "}}", skill.get("name", ""))
        result = result.replace("{{JD_SELF_" + str(i) + "}}", str(skill.get("self_score", "")))
        result = result.replace("{{JD_ACTUAL_" + str(i) + "}}", str(skill.get("actual_score", "")))
        result = result.replace("{{JD_REQ_" + str(i) + "}}", str(skill.get("required", "")))
        result = result.replace("{{JD_GAP_" + str(i) + "}}", str(skill.get("gap", "")))
        result = result.replace("{{JD_STATUS_" + str(i) + "}}", skill.get("status", ""))

    # 高危缺口
    critical = jd_gap.get("critical_gaps", [])
    for i, cg in enumerate(critical, 1):
        result = result.replace("{{CRITICAL_GAP_" + str(i) + "}}", cg.get("name", ""))
        result = result.replace("{{CRITICAL_GAP_" + str(i) + "_DETAIL}}", cg.get("detail", ""))
        result = result.replace("{{CRITICAL_GAP_" + str(i) + "_FIX}}", cg.get("fix", ""))

    # 意外亮点
    surprises = jd_gap.get("surprises", [])
    for i, s in enumerate(surprises, 1):
        result = result.replace("{{SURPRISE_" + str(i) + "}}", s.get("name", ""))
        result = result.replace("{{SURPRISE_" + str(i) + "_SELF}}", str(s.get("self_score", "")))
        result = result.replace("{{SURPRISE_" + str(i) + "_ACTUAL}}", str(s.get("actual_score", "")))

    return result


def fill_emotional_stability(template, emo):
    """填充情绪稳定性评估"""
    if not emo:
        return template

    result = template
    for i in range(1, 5):
        result = result.replace("{{EMO_SCORE_" + str(i) + "}}", str(emo.get(f"score_{i}", "")))
        result = result.replace("{{EMO_NOTE_" + str(i) + "}}", emo.get(f"note_{i}", ""))
    result = result.replace("{{EMO_TOTAL}}", str(emo.get("total", "")))
    result = result.replace("{{EMO_SUMMARY}}", emo.get("summary", ""))
    result = result.replace("{{STRESS_BEHAVIOR}}", emo.get("stress_behavior", ""))
    return result


def fill_improvement_plan(template, plan):
    """填充备考计划"""
    if not plan:
        return template

    result = template
    result = result.replace("{{DAYS_UNTIL_NEXT}}", str(plan.get("days_until_next", "7")))

    for day_key in ["DAY1_2", "DAY3_5", "DAY6_7"]:
        result = result.replace("{{" + day_key + "_TASK}}", plan.get(day_key.lower() + "_task", ""))
        for i in range(1, 5):
            result = result.replace("{{" + day_key + "_ITEM_" + str(i) + "}}", plan.get(day_key.lower() + "_item_" + str(i), ""))

    return result


def fill_strengths_weaknesses(template, strengths, weaknesses):
    """填充强项和薄弱项"""
    result = template

    # 强项
    for i, s in enumerate(strengths or [], 1):
        result = result.replace("{{STRENGTH_" + str(i) + "}}", s.get("name", ""))
        result = result.replace("{{STRENGTH_" + str(i) + "_EVIDENCE}}", s.get("evidence", ""))

    # 薄弱项
    critical = weaknesses.get("critical", []) if weaknesses else []
    medium = weaknesses.get("medium", []) if weaknesses else []
    minor = weaknesses.get("minor", []) if weaknesses else []

    if critical:
        w = critical[0]
        result = result.replace("{{WEAKNESS_CRITICAL_1}}", w.get("name", ""))
        result = result.replace("{{WEAKNESS_CRITICAL_1_SYMPTOM}}", w.get("symptom", ""))
        result = result.replace("{{RESOURCE_1}}", w.get("resource", ""))
        result = result.replace("{{PRACTICE_1}}", w.get("practice", ""))
        result = result.replace("{{TIME_1}}", w.get("time", ""))
        result = result.replace("{{IMPACT_1}}", w.get("impact", ""))

    if medium:
        for i, w in enumerate(medium[:2], 1):
            idx = 1 if i == 1 else 2
            if i == 1:
                result = result.replace("{{WEAKNESS_MEDIUM_" + str(idx) + "}}", w.get("name", ""))
                result = result.replace("{{WEAKNESS_MEDIUM_" + str(idx) + "_SYMPTOM}}", w.get("symptom", ""))
                result = result.replace("{{RESOURCE_" + str(idx + 1) + "}}", w.get("resource", ""))
                result = result.replace("{{PRACTICE_" + str(idx + 1) + "}}", w.get("practice", ""))
                result = result.replace("{{TIME_" + str(idx + 1) + "}}", w.get("time", ""))
            else:
                result = result.replace("{{WEAKNESS_MEDIUM_" + str(idx) + "}}", w.get("name", ""))
                result = result.replace("{{WEAKNESS_MEDIUM_" + str(idx) + "_SYMPTOM}}", w.get("symptom", ""))
                result = result.replace("{{RESOURCE_" + str(idx + 1) + "}}", w.get("resource", ""))

    if minor:
        w = minor[0]
        result = result.replace("{{WEAKNESS_MINOR_1}}", w.get("name", ""))
        result = result.replace("{{WEAKNESS_MINOR_1_SYMPTOM}}", w.get("symptom", ""))
        result = result.replace("{{MINOR_TIP_1}}", w.get("tip", ""))

    return result


def fill_defect_summary(template, defects):
    """填充回答缺陷模式总结"""
    if not defects:
        return template

    result = template
    for defect in defects:
        dtype = defect.get("type", "")
        count = defect.get("count", 0)
        question = defect.get("question", "")
        fix = defect.get("fix", "")

        if "没有数字" in dtype:
            result = result.replace("{{DEFECT_NO_NUM}}", str(count))
            result = result.replace("{{DEFECT_NO_NUM_Q}}", question)
        elif "只说工具" in dtype:
            result = result.replace("{{DEFECT_TOOL_ONLY}}", str(count))
            result = result.replace("{{DEFECT_TOOL_ONLY_Q}}", question)
        elif "背八股" in dtype:
            result = result.replace("{{DEFECT_PARROT}}", str(count))
            result = result.replace("{{DEFECT_PARROT_Q}}", question)
        elif "回避模糊" in dtype:
            result = result.replace("{{DEFECT_VAGUE}}", str(count))
            result = result.replace("{{DEFECT_VAGUE_Q}}", question)

    return result


def fill_template(template, data):
    """将数据填充到模板中"""
    result = template

    # 基本替换
    replacements = {
        "{{ROUND}}": str(data.get("round", "")),
        "{{ROUND_NAME}}": data.get("round_name", ""),
        "{{CANDIDATE_NAME}}": data.get("candidate_name", "候选人"),
        "{{LEVEL}}": data.get("level", "中级"),
        "{{DATE}}": data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "{{DURATION}}": str(data.get("duration", "45")),
        "{{TOTAL_SCORE}}": str(data.get("total_score", "0")),
        "{{DECISION}}": data.get("decision", "待定"),
        "{{DECISION_ICON}}": data.get("decision_icon", "⚠️"),
        "{{DECISION_REASON}}": data.get("decision_reason", ""),
        "{{NEXT_ROUND_DECISION}}": data.get("next_round_decision", "待定"),
        "{{NEXT_ROUND_REASON}}": data.get("next_round_reason", ""),
        "{{GENERATED_AT}}": datetime.now().isoformat(),
        "{{TOTAL_QUESTIONS}}": str(data.get("total_questions", "0")),
        "{{COMPLETENESS}}": str(data.get("completeness", "100")),
        "{{EARLY_END}}": data.get("early_end", "否"),
        "{{EARLY_END_REASON}}": data.get("early_end_reason", "N/A"),
        "{{PREV_WEAKNESS_CHECK}}": data.get("prev_weakness_check", "N/A"),
        "{{RADAR_SCORES}}": json.dumps(data.get("radar_scores", []), ensure_ascii=False),
    }

    for key, val in replacements.items():
        result = result.replace(key, val)

    # 维度评分（动态数量）
    dims = data.get("dimensions", [])
    for i, dim in enumerate(dims, 1):
        result = result.replace("{{DIM" + str(i) + "_NAME}}", dim.get("name", ""))
        result = result.replace("{{DIM" + str(i) + "_WEIGHT}}", dim.get("weight", ""))
        result = result.replace("{{DIM" + str(i) + "_SCORE}}", str(dim.get("score", "0")))
        result = result.replace("{{DIM" + str(i) + "_WEIGHTED}}", str(dim.get("weighted", "0")))

    # 分段填充
    result = fill_questions(result, data.get("questions", []))
    result = fill_golden_answers(result, data.get("golden_answers", []))
    result = fill_jd_gap(result, data.get("jd_gap", {}))
    result = fill_emotional_stability(result, data.get("emotional_stability", {}))
    result = fill_improvement_plan(result, data.get("improvement_plan", {}))
    result = fill_strengths_weaknesses(result, data.get("strengths", []), data.get("weaknesses", {}))
    result = fill_defect_summary(result, data.get("defects", []))

    # 清理残留占位符
    result = re.sub(r"\{\{[A-Z_0-9]+\}\}", "—", result)

    return result


def main():
    parser = argparse.ArgumentParser(description="生成面试报告")
    parser.add_argument("--round", type=int, required=True, help="面试轮次")
    parser.add_argument("--data", type=str, required=True, help="面试数据 JSON 文件路径")
    args = parser.parse_args()

    # 加载数据
    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["round"] = args.round

    # 加载并填充模板
    template = load_template()
    report = fill_template(template, data)

    # 生成文件名
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"interview-round{args.round}-{timestamp}.md"
    filepath = os.path.join(REPORTS_DIR, filename)

    # 确保目录存在
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # 写入
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"✅ 报告已生成: {filepath}")
    print(f"   轮次: 第{args.round}轮")
    print(f"   总分: {data.get('total_score', 'N/A')}")
    print(f"   决策: {data.get('decision', 'N/A')}")


if __name__ == "__main__":
    main()
