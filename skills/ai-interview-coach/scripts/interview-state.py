#!/usr/bin/env python3
"""
interview-state.py
面试状态管理脚本 - 用于读写和更新面试历史记录

用法:
  python interview-state.py init <name> <resume_path>   # 初始化候选人
  python interview-state.py add-round <round_num> <score> <passed> <report_path> [strengths] [weaknesses]  # 添加轮次
  python interview-state.py summary                      # 打印总览
  python interview-state.py update-assessment            # 更新综合评估
  python interview-state.py add-weakness <type> <item>   # 添加薄弱项 (type: critical|moderate|minor)
  python interview-state.py add-strength <item>          # 添加强项
  python interview-state.py clear                        # 重置状态
"""

import json
import sys
import os
from datetime import datetime

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "interview-history.json")


def load_state():
    """加载状态文件，处理文件不存在或格式错误的情况"""
    if not os.path.exists(STATE_FILE):
        print("⚠️ 状态文件不存在，返回默认空状态")
        return get_default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return get_default_state()
            return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"❌ 状态文件格式错误: {e}")
        print("   已重置为默认状态")
        return get_default_state()


def get_default_state():
    return {
        "candidate": {
            "name": "",
            "target_role": "AI大模型应用开发",
            "resume_path": "",
            "years_of_experience": 0,
            "skill_stack": []
        },
        "rounds": [],
        "overall_assessment": {
            "current_level": "",
            "hire_recommendation": "",
            "total_interview_score": None,
            "last_updated": ""
        },
        "weakness_tracking": {
            "critical": [],
            "moderate": [],
            "minor": []
        },
        "strength_tracking": {
            "confirmed": [],
            "needs_verification": []
        },
        "interview_log": []
    }


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def cmd_init(name, resume_path):
    state = load_state()
    state["candidate"]["name"] = name
    state["candidate"]["resume_path"] = resume_path
    state["overall_assessment"]["last_updated"] = datetime.now().isoformat()
    save_state(state)
    print(f"✅ 已初始化候选人: {name}")
    print(f"   简历路径: {resume_path}")


def cmd_add_round(round_num, score, passed, report_path, strengths=None, weaknesses=None):
    state = load_state()
    round_data = {
        "round": int(round_num),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_score": float(score),
        "passed": str(passed).lower() in ("true", "1", "yes"),
        "report_path": report_path,
        "timestamp": datetime.now().isoformat()
    }
    # 更新或追加
    existing = [r for r in state["rounds"] if r["round"] == int(round_num)]
    if existing:
        idx = state["rounds"].index(existing[0])
        state["rounds"][idx] = round_data
        print(f"📝 已更新第 {round_num} 轮记录")
    else:
        state["rounds"].append(round_data)
        print(f"✅ 已添加第 {round_num} 轮记录 (得分: {score})")

    # 同步更新薄弱项和强项追踪
    if strengths:
        for s in strengths:
            if s not in state["strength_tracking"]["confirmed"]:
                state["strength_tracking"]["confirmed"].append(s)
                print(f"   💪 已记录强项: {s}")

    if weaknesses:
        for w in weaknesses:
            if w not in state["weakness_tracking"]["critical"]:
                state["weakness_tracking"]["critical"].append(w)
                print(f"   🔴 已记录薄弱项: {w}")

    # 记录日志
    state["interview_log"].append({
        "action": "add_round",
        "round": int(round_num),
        "score": float(score),
        "timestamp": datetime.now().isoformat()
    })

    state["overall_assessment"]["last_updated"] = datetime.now().isoformat()
    save_state(state)


def cmd_summary():
    state = load_state()
    cand = state["candidate"]
    print("=" * 50)
    print(f"候选人: {cand.get('name', '未设置')}")
    print(f"目标岗位: {cand.get('target_role', 'AI大模型应用开发')}")
    print(f"简历: {cand.get('resume_path', '未上传')}")
    print("-" * 50)
    print(f"已完成轮次: {len(state['rounds'])}")
    for r in state["rounds"]:
        status = "✅ 通过" if r["passed"] else "❌ 未通过"
        print(f"  第{r['round']}轮 | 得分 {r['total_score']} | {status} | {r['date']}")
    print("-" * 50)
    oa = state["overall_assessment"]
    print(f"综合评估: {oa.get('current_level', '待评估')}")
    print(f"录用建议: {oa.get('hire_recommendation', '待定')}")
    if oa.get("total_interview_score"):
        print(f"综合得分: {oa['total_interview_score']}")
    print("-" * 50)
    print("薄弱项追踪:")
    wt = state.get("weakness_tracking", {})
    for level in ["critical", "moderate", "minor"]:
        items = wt.get(level, [])
        if items:
            icon = {"critical": "🔴", "moderate": "🟡", "minor": "🟢"}[level]
            print(f"  {icon} {level}: {', '.join(items)}")
    st = state.get("strength_tracking", {})
    confirmed = st.get("confirmed", [])
    if confirmed:
        print(f"  💪 已确认强项: {', '.join(confirmed)}")
    print("=" * 50)


def cmd_update_assessment():
    state = load_state()
    rounds = state["rounds"]
    if not rounds:
        print("⚠️ 尚无面试记录，无法评估")
        return

    # 加权计算: 第1轮30% + 第2轮40% + 第3轮30%
    weights = {1: 0.3, 2: 0.4, 3: 0.3}
    total = 0
    weight_sum = 0
    for r in rounds:
        w = weights.get(r["round"], 0.33)
        total += r["total_score"] * w
        weight_sum += w

    final_score = total / weight_sum if weight_sum > 0 else 0

    # 映射决策
    if final_score >= 4.0:
        recommendation = "Strong Hire - 强烈推荐"
        level = "高级"
    elif final_score >= 3.5:
        recommendation = "Hire - 推荐录用"
        level = "中级偏上"
    elif final_score >= 3.0:
        recommendation = "Lean Hire - 基本达标，需关注短板"
        level = "中级"
    elif final_score >= 2.5:
        recommendation = "No Hire - 有明显短板"
        level = "中级偏下"
    else:
        recommendation = "Strong No Hire - 远未达标"
        level = "待提升"

    state["overall_assessment"]["total_interview_score"] = round(final_score, 2)
    state["overall_assessment"]["current_level"] = level
    state["overall_assessment"]["hire_recommendation"] = recommendation
    state["overall_assessment"]["last_updated"] = datetime.now().isoformat()

    # 记录日志
    state["interview_log"].append({
        "action": "update_assessment",
        "score": round(final_score, 2),
        "recommendation": recommendation,
        "timestamp": datetime.now().isoformat()
    })

    save_state(state)

    print(f"📊 综合得分: {final_score:.2f}")
    print(f"📋 级别建议: {level}")
    print(f"🎯 录用建议: {recommendation}")


def cmd_add_weakness(weakness_type, item):
    state = load_state()
    if weakness_type not in ("critical", "moderate", "minor"):
        print(f"❌ 无效的薄弱项类型: {weakness_type}（可选: critical, moderate, minor）")
        return

    if item not in state["weakness_tracking"][weakness_type]:
        state["weakness_tracking"][weakness_type].append(item)
        state["interview_log"].append({
            "action": "add_weakness",
            "type": weakness_type,
            "item": item,
            "timestamp": datetime.now().isoformat()
        })
        save_state(state)
        print(f"✅ 已添加 {weakness_type} 薄弱项: {item}")
    else:
        print(f"⚠️ 薄弱项已存在: {item}")


def cmd_add_strength(item):
    state = load_state()
    if item not in state["strength_tracking"]["confirmed"]:
        state["strength_tracking"]["confirmed"].append(item)
        state["interview_log"].append({
            "action": "add_strength",
            "item": item,
            "timestamp": datetime.now().isoformat()
        })
        save_state(state)
        print(f"✅ 已添加强项: {item}")
    else:
        print(f"⚠️ 强项已存在: {item}")


def cmd_clear():
    confirm = input("❓ 确定要重置所有状态？(y/N): ")
    if confirm.lower() == "y":
        save_state(get_default_state())
        print("✅ 状态已重置")
    else:
        print("已取消")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "init" and len(sys.argv) >= 4:
            cmd_init(sys.argv[2], sys.argv[3])
        elif cmd == "add-round" and len(sys.argv) >= 6:
            strengths = sys.argv[6].split(",") if len(sys.argv) >= 7 and sys.argv[6] else None
            weaknesses = sys.argv[7].split(",") if len(sys.argv) >= 8 and sys.argv[7] else None
            cmd_add_round(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], strengths, weaknesses)
        elif cmd == "summary":
            cmd_summary()
        elif cmd == "update-assessment":
            cmd_update_assessment()
        elif cmd == "add-weakness" and len(sys.argv) >= 4:
            cmd_add_weakness(sys.argv[2], sys.argv[3])
        elif cmd == "add-strength" and len(sys.argv) >= 3:
            cmd_add_strength(sys.argv[2])
        elif cmd == "clear":
            cmd_clear()
        else:
            print(__doc__)
            sys.exit(1)
    except Exception as e:
        print(f"❌ 执行出错: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
