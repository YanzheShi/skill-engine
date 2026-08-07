#!/usr/bin/env python3
"""
extract-cards.py — 从多轮 Learning Log 中按标签汇总面试卡片

用法:
    python extract-cards.py --dir <logs目录> --tag <标签> [--output <输出文件>]
    python extract-cards.py --dir <logs目录> --list-tags
    python extract-cards.py --dir <logs目录> --stats
    python extract-cards.py --dir <logs目录> --module <模块名>

示例:
    python extract-cards.py --dir ./learning-logs --tag "分布式锁"
    python extract-cards.py --dir ./learning-logs --tag "SQL注入" --output security-cards.md
    python extract-cards.py --dir ./learning-logs --list-tags
    python extract-cards.py --dir ./learning-logs --stats
"""

import argparse
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path
from datetime import datetime

# ─── 颜色输出（终端支持时） ────────────────────────────────────────────────
class C:
    RED    = "\033[91m" if sys.stdout.isatty() else ""
    GREEN  = "\033[92m" if sys.stdout.isatty() else ""
    YELLOW = "\033[93m" if sys.stdout.isatty() else ""
    BLUE   = "\033[94m" if sys.stdout.isatty() else ""
    BOLD   = "\033[1m"  if sys.stdout.isatty() else ""
    DIM    = "\033[2m"  if sys.stdout.isatty() else ""
    RESET  = "\033[0m"  if sys.stdout.isatty() else ""

# ─── 解析 Learning Log ──────────────────────────────────────────────────────

def parse_frontmatter(text):
    """提取 YAML frontmatter 中的 tags 字段"""
    tags = defaultdict(list)
    fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not fm_match:
        return tags
    fm = fm_match.group(1)
    # 匹配 tags 块
    tag_block = re.search(r'tags:\s*\n(.*?)(?:\n\S|\Z)', fm, re.DOTALL)
    if not tag_block:
        return tags
    block = tag_block.group(1)
    current_cat = None
    for line in block.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'(\w+):\s*\[(.*)\]', line)
        if m:
            cat, vals = m.group(1), m.group(2)
            current_cat = cat
            tags[cat] = [v.strip() for v in vals.split(',') if v.strip()]
    return tags

def extract_basic_info(text):
    """提取基本信息字段"""
    info = {}
    fields = ['模块名称', '文件路径', '学习日期', '技术栈标签']
    for f in fields:
        m = re.search(r'\|\s*' + re.escape(f) + r'\s*\|\s*(.+?)\s*\|', text)
        if m:
            info[f] = m.group(1).strip()
    return info

def extract_qa_pairs(text):
    """提取面试追问 Q&A"""
    qas = []
    qa_section = re.search(r'##\s*4\.\s*面试高频追问.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if not qa_section:
        return qas
    content = qa_section.group(0)
    questions = re.split(r'###\s*Q\d+:', content)
    for q in questions[1:]:
        qa = {'question': '', 'answer': '', 'verdict': '', 'tags': []}
        lines = q.strip().split('\n')
        if lines:
            qa['question'] = lines[0].strip()
        for line in lines:
            if line.startswith('- **我的回答**：'):
                qa['answer'] = line.replace('- **我的回答**：', '').strip()
            elif line.startswith('- **评判**：'):
                qa['verdict'] = line.replace('- **评判**：', '').strip()
            elif line.startswith('- **Tags**：'):
                tag_str = line.replace('- **Tags**：', '').strip()
                qa['tags'] = [t.strip('`[] ') for t in tag_str.split(',')]
        qas.append(qa)
    return qas

def extract_gaps(text):
    """提取盲区"""
    gaps = []
    sec = re.search(r'##\s*10\.\s*我的盲区.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if line.strip().startswith('- ❌'):
                gaps.append(line.strip()[2:].strip())
    return gaps

def extract_highlights(text):
    """提取亮点"""
    highlights = []
    sec = re.search(r'##\s*9\.\s*我的亮点.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if line.strip().startswith('- ✅'):
                highlights.append(line.strip()[2:].strip())
    return highlights

def extract_todos(text):
    """提取 TODO"""
    todos = []
    sec = re.search(r'##\s*11\.\s*TODO.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if line.strip().startswith('- [ ]'):
                todos.append(line.strip()[5:].strip())
    return todos

def extract_security_findings(text):
    """提取安全漏洞"""
    findings = []
    sec = re.search(r'##\s*8\.\s*🛡️.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if line.strip().startswith('| V-'):
                findings.append(line.strip())
    return findings

def extract_sre_findings(text):
    """提取 SRE 缺口"""
    findings = []
    sec = re.search(r'##\s*7\.\s*🔧.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if '✅' in line or '⚠️' in line or '❌' in line:
                findings.append(line.strip())
    return findings

def extract_test_gaps(text):
    """提取测试缺口"""
    gaps = []
    sec = re.search(r'##\s*6\.\s*🧪.*?(?=\n##\s*\d|$)', text, re.DOTALL)
    if sec:
        for line in sec.group(0).split('\n'):
            if line.strip().startswith('|') and 'L0' in line or 'L1' in line:
                gaps.append(line.strip())
    return gaps

# ─── 加载所有日志 ───────────────────────────────────────────────────────────

def load_all_logs(directory):
    """加载目录下所有 .md 日志文件"""
    logs = []
    d = Path(directory)
    if not d.exists():
        print(f"{C.RED}❌ 目录不存在: {directory}{C.RESET}")
        sys.exit(1)
    files = sorted(d.glob('*.md'))
    if not files:
        print(f"{C.YELLOW}⚠️ 未找到 .md 文件: {directory}{C.RESET}")
        sys.exit(0)
    for f in files:
        text = f.read_text(encoding='utf-8')
        logs.append({
            'file': f.name,
            'text': text,
            'tags': parse_frontmatter(text),
            'info': extract_basic_info(text),
            'qas': extract_qa_pairs(text),
            'gaps': extract_gaps(text),
            'highlights': extract_highlights(text),
            'todos': extract_todos(text),
            'security': extract_security_findings(text),
            'sre': extract_sre_findings(text),
            'test_gaps': extract_test_gaps(text),
        })
    return logs

# ─── 命令：列出所有标签 ─────────────────────────────────────────────────────

def cmd_list_tags(logs):
    all_tags = defaultdict(Counter)
    for log in logs:
        for cat, tags in log['tags'].items():
            for t in tags:
                all_tags[cat][t] += 1
    print(f"\n{C.BOLD}🏷️  所有标签（按类别）{C.RESET}\n")
    for cat in sorted(all_tags.keys()):
        tags_sorted = all_tags[cat].most_common()
        print(f"  {C.BLUE}{cat}{C.RESET} ({len(tags_sorted)}):")
        for tag, count in tags_sorted:
            bar = '█' * count
            print(f"    {tag:<20} {C.GREEN}{bar}{C.RESET} ({count})")
    print()

# ─── 命令：统计概览 ─────────────────────────────────────────────────────────

def cmd_stats(logs):
    total = len(logs)
    all_gaps = sum(len(l['gaps']) for l in logs)
    all_highlights = sum(len(l['highlights']) for l in logs)
    all_todos = sum(len(l['todos']) for l in logs)
    all_qas = sum(len(l['qas']) for l in logs)
    all_security = sum(len(l['security']) for l in logs)
    all_sre = sum(len(l['sre']) for l in logs)
    all_test = sum(len(l['test_gaps']) for l in logs)

    print(f"\n{C.BOLD}📊 Learning Log 统计概览{C.RESET}\n")
    print(f"  总日志数:        {C.BOLD}{total}{C.RESET} 个模块")
    print(f"  面试问答:        {all_qas} 条")
    print(f"  我的亮点:        {C.GREEN}{all_highlights}{C.RESET} 条")
    print(f"  我的盲区:        {C.RED}{all_gaps}{C.RESET} 条")
    print(f"  待办 TODO:       {all_todos} 条")
    print(f"  安全漏洞:        {C.RED}{all_security}{C.RESET} 条")
    print(f"  SRE 风险:        {C.YELLOW}{all_sre}{C.RESET} 条")
    print(f"  测试缺口:        {C.YELLOW}{all_test}{C.RESET} 条")

    # 高频盲区 Top 5
    gap_counter = Counter()
    for l in logs:
        for g in l['gaps']:
            gap_counter[g] += 1
    if gap_counter:
        print(f"\n  {C.RED}高频盲区 Top 5{C.RESET}:")
        for g, c in gap_counter.most_common(5):
            print(f"    [{c}] {g[:80]}")

    # 高频亮点 Top 5
    hl_counter = Counter()
    for l in logs:
        for h in l['highlights']:
            hl_counter[h] += 1
    if hl_counter:
        print(f"\n  {C.GREEN}高频亮点 Top 5{C.RESET}:")
        for h, c in hl_counter.most_common(5):
            print(f"    [{c}] {h[:80]}")

    print()

# ─── 命令：按标签筛选 ───────────────────────────────────────────────────────

def cmd_filter_by_tag(logs, tag, output=None):
    tag_lower = tag.lower()
    matched = []
    for log in logs:
        # 搜索所有 tag 类别
        for cat, tags in log['tags'].items():
            for t in tags:
                if tag_lower in t.lower():
                    matched.append(log)
                    break
            else:
                continue
            break
        # 也搜索 Q&A 中的 inline tags
        if log not in matched:
            for qa in log['qas']:
                for t in qa.get('tags', []):
                    if tag_lower in t.lower():
                        matched.append(log)
                        break

    if not matched:
        print(f"{C.YELLOW}⚠️ 没有找到包含标签 '{tag}' 的日志{C.RESET}")
        return

    print(f"\n{C.BOLD}🔎 标签 '{tag}' 匹配 {len(matched)} 个模块{C.RESET}\n")

    output_lines = []
    output_lines.append(f"# 🔎 标签检索结果: `{tag}`\n")
    output_lines.append(f"> 匹配 {len(matched)} 个模块 | 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    for log in matched:
        info = log['info']
        name = info.get('模块名称', log['file'])
        path = info.get('文件路径', 'N/A')
        date = info.get('学习日期', 'N/A')

        print(f"  {C.BOLD}{name}{C.RESET} ({path}) — {date}")
        output_lines.append(f"\n## 📋 {name}\n")
        output_lines.append(f"- **文件**: `{path}`\n")
        output_lines.append(f"- **日期**: {date}\n")
        output_lines.append(f"- **来源**: `{log['file']}`\n")

        # 匹配的 Q&A
        matched_qas = [qa for qa in log['qas']
                       if any(tag_lower in t.lower() for t in qa.get('tags', []))
                       or any(tag_lower in qa['question'].lower() for _ in [0])]
        # 也匹配问题正文
        matched_qas = [qa for qa in log['qas']
                       if tag_lower in qa['question'].lower()
                       or tag_lower in qa['answer'].lower()
                       or any(tag_lower in t.lower() for t in qa.get('tags', []))]

        if matched_qas:
            print(f"    {C.BLUE}相关问答 ({len(matched_qas)}):{C.RESET}")
            output_lines.append(f"\n### 相关面试问答\n")
            for qa in matched_qas:
                verdict_icon = {'✅ 通过': '✅', '⚠️ 部分正确': '⚠️', '❌ 需补强': '❌'}.get(qa['verdict'], '•')
                print(f"      {verdict_icon} Q: {qa['question'][:70]}")
                output_lines.append(f"\n**Q: {qa['question']}**\n")
                output_lines.append(f"- **我的回答**: {qa['answer']}\n")
                output_lines.append(f"- **评判**: {qa['verdict']}\n")
                if qa['tags']:
                    output_lines.append(f"- **Tags**: {', '.join(['`'+t+'`' for t in qa['tags']])}\n")

        # 匹配的盲区
        matched_gaps = [g for g in log['gaps'] if tag_lower in g.lower()]
        if matched_gaps:
            print(f"    {C.RED}相关盲区:{C.RESET}")
            output_lines.append(f"\n### 相关盲区\n")
            for g in matched_gaps:
                print(f"      ❌ {g[:70]}")
                output_lines.append(f"- ❌ {g}\n")

        # 匹配的 TODO
        matched_todos = [t for t in log['todos'] if tag_lower in t.lower()]
        if matched_todos:
            output_lines.append(f"\n### 相关 TODO\n")
            for t in matched_todos:
                output_lines.append(f"- [ ] {t}\n")

        print()

    if output:
        Path(output).write_text('\n'.join(output_lines), encoding='utf-8')
        print(f"\n{C.GREEN}✅ 已保存到: {output}{C.RESET}")
    else:
        # 也输出到 stdout
        print('\n' + '─' * 60)
        print('\n'.join(output_lines))

# ─── 命令：按模块筛选 ───────────────────────────────────────────────────────

def cmd_filter_by_module(logs, module):
    mod_lower = module.lower()
    matched = [l for l in logs
               if mod_lower in l['info'].get('模块名称', '').lower()
               or mod_lower in l['file'].lower()]
    if not matched:
        print(f"{C.YELLOW}⚠️ 没有找到模块 '{module}'{C.RESET}")
        return
    for log in matched:
        info = log['info']
        print(f"\n{C.BOLD}📋 {info.get('模块名称', log['file'])}{C.RESET}")
        print(f"  文件: {info.get('文件路径', 'N/A')}")
        print(f"  日期: {info.get('学习日期', 'N/A')}")
        print(f"  问答: {len(log['qas'])} 条 | 亮点: {len(log['highlights'])} | 盲区: {len(log['gaps'])} | TODO: {len(log['todos'])}")
        if log['qas']:
            print(f"\n  {C.BLUE}面试问答:{C.RESET}")
            for qa in log['qas']:
                print(f"    • Q: {qa['question'][:80]}")
                print(f"      A: {qa['answer'][:80]}")
                print(f"      评判: {qa['verdict']}")
        if log['gaps']:
            print(f"\n  {C.RED}盲区:{C.RESET}")
            for g in log['gaps']:
                print(f"    ❌ {g[:80]}")
        if log['todos']:
            print(f"\n  {C.YELLOW}TODO:{C.RESET}")
            for t in log['todos'][:5]:
                print(f"    [ ] {t}")
        print()

# ─── 命令：导出完整汇总 ─────────────────────────────────────────────────────

def cmd_export_all(logs, output):
    """导出所有日志的完整汇总为一个 Markdown 文件"""
    lines = [f"# 📚 Learning Log 完整汇总\n",
             f"> 共 {len(logs)} 个模块 | 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    
    # 目录
    lines.append("## 📑 目录\n")
    for i, log in enumerate(logs, 1):
        name = log['info'].get('模块名称', log['file'])
        lines.append(f"{i}. [{name}](#{i})")
    lines.append("")

    for i, log in enumerate(logs, 1):
        info = log['info']
        name = info.get('模块名称', log['file'])
        lines.append(f"\n---")
        lines.append(f"\n## <a id='{i}'></a>📋 {name}\n")
        lines.append(f"- 文件: `{info.get('文件路径', 'N/A')}`")
        lines.append(f"- 日期: {info.get('学习日期', 'N/A')}")
        lines.append(f"- 来源: `{log['file']}`\n")

        if log['qas']:
            lines.append("### 面试问答\n")
            for qa in log['qas']:
                lines.append(f"**Q: {qa['question']}**\n")
                lines.append(f"- 回答: {qa['answer']}")
                lines.append(f"- 评判: {qa['verdict']}\n")

        if log['highlights']:
            lines.append("### ✅ 亮点\n")
            for h in log['highlights']:
                lines.append(f"- ✅ {h}")
            lines.append("")

        if log['gaps']:
            lines.append("### ❌ 盲区\n")
            for g in log['gaps']:
                lines.append(f"- ❌ {g}")
            lines.append("")

        if log['todos']:
            lines.append("### 📝 TODO\n")
            for t in log['todos']:
                lines.append(f"- [ ] {t}")
            lines.append("")

    Path(output).write_text('\n'.join(lines), encoding='utf-8')
    print(f"{C.GREEN}✅ 完整汇总已保存到: {output}{C.RESET}")

# ─── 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='🔎 从 Learning Log 中按标签/模块汇总面试卡片',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('--dir', default='./learning-logs',
                        help='Learning Log 文件目录（默认: ./learning-logs）')
    parser.add_argument('--tag', help='按标签筛选（模糊匹配）')
    parser.add_argument('--module', help='按模块名筛选（模糊匹配）')
    parser.add_argument('--list-tags', action='store_true', help='列出所有标签及出现次数')
    parser.add_argument('--stats', action='store_true', help='显示统计概览')
    parser.add_argument('--export', help='导出所有日志完整汇总到指定文件')
    parser.add_argument('--output', '-o', help='输出文件路径（与 --tag 或 --export 配合使用）')

    args = parser.parse_args()
    logs = load_all_logs(args.dir)

    # 默认行为：显示统计
    if not any([args.tag, args.module, args.list_tags, args.stats, args.export]):
        cmd_stats(logs)
        print(f"{C.DIM}💡 使用 --help 查看所有命令{C.RESET}\n")
        return

    if args.list_tags:
        cmd_list_tags(logs)
    elif args.stats:
        cmd_stats(logs)
    elif args.tag:
        cmd_filter_by_tag(logs, args.tag, args.output)
    elif args.module:
        cmd_filter_by_module(logs, args.module)
    elif args.export:
        cmd_export_all(logs, args.export)

if __name__ == '__main__':
    main()
