"""POC：大 HTML 文件写入，路线一（少发射/分块/补丁）三候选横向对比。

目的：复现"长时间会话里一次性 write_file 一个大 HTML 被 max_tokens 截断、
且模型不自知、反复重发整份"的问题，并用**真实 handler**验证三种策略谁能
稳定产出完整、字节正确的文件。

设计：
- 目标文件 = 一个确定性的 ~60KB 自包含中文 HTML（卡片+标签页+搜索+进度+内联 SVG）。
- "每轮输出预算"由 max_tokens 折算成字符上限（CJK 密集文本 token 占比高，
  用更保守的 chars/token，并预留 headroom 给前言文本与 JSON 包裹）。
- 截断是**唯一合成**的环节，用 emission[:budget] 模拟生成中途被砍；其余全部走
  src/skill_engine 里的生产代码：WriteFileHandler / EditFileHandler / _apply_edits。
  （真实截断发生在 LLM 服务端，本地无法调用网关复现；此处模拟只影响"何时被砍"，
   不影响"分块后能否拼回正确文件"这一被考察的核心性质。）

三种策略：
  S1 一次性 overwrite            —— 现状；整份 content 装进一次调用
  S2 骨架 + 分块 append          —— 每轮只发一小段，write_file(mode=append) 续写
  S3 骨架占位 + edit_file 逐段填 —— 先写带唯一标记的小骨架，再用 edit 把每段填进标记

外加两个"修复代价"实验：
  R-naive  S1 截断后重发整份      —— 复现"再截断→死循环"（config.py 注释里的 5 次/545s）
  R-append / R-slot：一次续写再次被砍时，两种策略各自的恢复成本与是否会写坏。

跑法（仓库根）：
  set PYTHONIOENCODING=utf-8 && python -m poc.large_write_poc
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# 允许从仓库根直接运行（未安装为包时）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from skill_engine.execution.tool_exec.edit_patch import _apply_edits  # noqa: E402
from skill_engine.execution.tool_exec.handlers.edit_file import EditFileHandler  # noqa: E402
from skill_engine.execution.tool_exec.handlers.write_file import WriteFileHandler  # noqa: E402

# ---------------------------------------------------------------------------
# 预算模型：把 max_tokens 折成"每轮可发射的 content 字符数"
# ---------------------------------------------------------------------------
BUDGET_TOKENS = 8192          # = config.DEFAULT_LLM_MAX_TOKENS
HEADROOM = 0.6                # 预留给思考/前言文本 + JSON 包裹，只把 60% 预算给 content

# chars/token 场景：中文密集取保守值，ASCII 取常见经验值（非 tokenizer 精确，标注为推算）
CJK_TOKENS = {"ascii_heavy": 4.0, "cjk_heavy": 1.5}


def content_budget(chars_per_token: float) -> int:
    return int(BUDGET_TOKENS * chars_per_token * HEADROOM)


# ---------------------------------------------------------------------------
# 构造一个确定性的 ~60KB 自包含中文 HTML（卡片+标签页+搜索+进度+内联 SVG）
# ---------------------------------------------------------------------------
def _card_html(i: int, topic: str) -> str:
    q = f"Q{i}. {topic}？"
    a = (f"要点：先讲清楚{topic}的动机与约束，再给出取舍；用一句话点出最容易"
         f"被追问的坑，并准备一个可量化的指标来佐证效果。这里补充一段实现细节，"
         f"确保卡片正文足够长以模拟真实复习笔记的信息密度，避免用极短占位低估体量。"
         f"（示例第 {i} 条，重复两遍加长：{topic}。{topic}。）")
    return (
        f'  <article class="card" id="card-{i}" data-topic="{topic}">\n'
        f'    <div class="card-head" onclick="toggleCard(this)">'
        f'<span class="q">{q}</span><span class="chev">▾</span></div>\n'
        f'    <div class="card-body"><p>{a}</p>\n'
        f'      <svg viewBox="0 0 120 40" width="120" height="40" aria-hidden="true">'
        f'<rect x="2" y="8" width="30" height="20" fill="#888"/>'
        f'<line x1="34" y1="18" x2="52" y2="18" stroke="#888"/>'
        f'<circle cx="62" cy="18" r="9" fill="none" stroke="#888"/>'
        f'<text x="80" y="22" font-size="10">step{i}</text></svg>\n'
        f'      <label class="chk"><input type="checkbox" '
        f'data-key="k{i}"> 已掌握</label>\n'
        f'    </div>\n'
        f'  </article>\n'
    )


def build_target_html(n_cards: int = 40) -> str:
    topics = ["限流熔断", "幂等设计", "缓存一致性", "连接池", "分布式锁", "事务隔离",
              "消息削峰", "服务降级", "灰度发布", "熔断恢复", "背压", "分库分表",
              "读写分离", "热点 key", "长尾延迟", "超时重试", "链路追踪", "容量评估",
              "优雅停机", "热更新", "配置中心", "注册发现", "负载均衡", "反序列化",
              "上下文传播", "取消传播", "资源泄漏", "锁等待", "死锁检测", "快照读",
              "当前读", "间隙锁", "MVCC", "回滚段", "两阶段提交", "三阶段提交",
              "Paxos", "Raft 选主", "日志压缩", "租约机制"][:n_cards]
    head = (
        "<!DOCTYPE html>\n<html lang=\"zh\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<title>面试复习笔记（POC 目标）</title>\n"
        "<style>\n"
        "body{font-family:sans-serif;max-width:900px;margin:24px auto}\n"
        ".card{border:1px solid #ddd;border-radius:8px;margin:8px 0;padding:0}\n"
        ".card-head{padding:10px 14px;cursor:pointer;font-weight:600}\n"
        ".card-body{padding:0 14px;display:none}\n"
        ".card.open .card-body{display:block}\n"
        ".tabs button{margin-right:6px}\n"
        "#prog{position:sticky;top:0;background:#111;color:#fff;padding:6px 14px}\n"
        "</style>\n</head>\n<body>\n"
        "<div id=\"prog\">进度：<span id=\"pct\">0%</span></div>\n"
        "<div class=\"tabs\">\n"
        "  <button onclick=\"showTab('all')\">全部</button>\n"
        "  <button onclick=\"showTab('weak')\">薄弱</button>\n"
        "  <button onclick=\"showTab('done')\">已掌握</button>\n"
        "</div>\n"
        "<input id=\"search\" placeholder=\"搜索专题…\" oninput=\"doSearch(this.value)\">\n"
        "<main id=\"list\">\n"
    )
    cards = "".join(_card_html(i + 1, t) for i, t in enumerate(topics))
    js = (
        "</main>\n<script>\n"
        "function toggleCard(h){h.parentElement.classList.toggle('open')}\n"
        "function showTab(t){/* 过滤标签页 */console.log(t)}\n"
        "function doSearch(v){/* 关键词过滤卡片 */console.log(v)}\n"
        "function update(){var c=document.querySelectorAll('input[data-key]:checked');"
        "var n=document.querySelectorAll('input[data-key]').length;"
        "document.getElementById('pct').textContent=Math.round(100*c.length/n)+'%'}\n"
        "document.addEventListener('change',function(e){if(e.target.matches('input[data-key]'))"
        "{e.target.closest('.card').classList.toggle('done',e.target.checked);update()}});\n"
        "update();\n</script>\n</body>\n</html>\n"
    )
    return head + cards + js


# ---------------------------------------------------------------------------
# 测试脚手架：duck-type 出一个满足 write/edit handler 的最小 ctx（复用生产 handler）
# ---------------------------------------------------------------------------
def make_ctx(base_dir):
    return SimpleNamespace(
        base_dir=base_dir,
        skill=None,
        confirm_edits_mode="",                       # 关闭 diff 确认门，免交互
        snapshot=SimpleNamespace(record=lambda *a, **k: None),
        file_tracker=SimpleNamespace(
            on_write=lambda *a, **k: None,
            check_editable=lambda *a, **k: (True, ""),
        ),
        check_file_safety=lambda *a, **k: (True, ""),
        confirm_edit=lambda *a, **k: True,
    )


def simulate_truncated(content: str, budget: int) -> tuple[str, bool]:
    """合成截断：若一次发射的 content 超出预算，则在预算处被砍。"""
    if len(content) <= budget:
        return content, False
    return content[:budget], True


def is_complete_html(text: str) -> bool:
    return "</html>" in text[-400:]


def read_result(base_dir, name: str = "out.html") -> str:
    """优先读正式文件；未转正时读 .partial（暂存语义）。"""
    target = base_dir / name
    if target.exists():
        return target.read_text(encoding="utf-8")
    pp = base_dir / (name + ".partial")
    return pp.read_text(encoding="utf-8") if pp.exists() else ""


def reset_outputs(base_dir, name: str = "out.html") -> None:
    """清掉上一策略留下的成品/暂存，避免跨策略误判。"""
    for p in (base_dir / name, base_dir / (name + ".partial")):
        if p.exists():
            p.unlink()


def split_by_lines(content: str, max_bytes: int) -> list[str]:
    """按行边界切块，且保证每块以 \\n 结尾——避免 append 自动补换行改变字节。"""
    chunks, cur, cur_len = [], [], 0
    for line in content.splitlines(keepends=True):
        if cur and cur_len + len(line) > max_bytes:
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


# ---------------------------------------------------------------------------
# S1：一次性 overwrite
# ---------------------------------------------------------------------------
def run_s1(base_dir, target, budget):
    reset_outputs(base_dir)
    emitted, truncated = simulate_truncated(target, budget)
    res = WriteFileHandler().execute(
        {"id": "s1", "type": "write_file",
         "input": {"path": "out.html", "content": emitted}},
        make_ctx(base_dir))
    final = read_result(base_dir)
    return {
        "calls": 1,
        "max_emit": len(emitted),
        "any_truncated": truncated,
        "complete": is_complete_html(final),
        "byte_exact": final == target,
        "finalized": (base_dir / "out.html").exists(),
        "verdict_flag": ("complete: NO" in res.content),
    }


# ---------------------------------------------------------------------------
# S2：分块 append
# ---------------------------------------------------------------------------
def run_s2(base_dir, target, budget):
    reset_outputs(base_dir)
    chunks = split_by_lines(target, budget)
    max_emit = 0
    truncated_any = False
    for idx, chunk in enumerate(chunks):
        emitted, trunc = simulate_truncated(chunk, budget)
        max_emit = max(max_emit, len(emitted))
        truncated_any = truncated_any or trunc
        WriteFileHandler().execute(
            {"id": f"s2-{idx}", "type": "write_file",
             "input": {"path": "out.html", "content": emitted,
                       "mode": "overwrite" if idx == 0 else "append"}},
            make_ctx(base_dir))
    final = read_result(base_dir)
    return {
        "calls": len(chunks),
        "max_emit": max_emit,
        "any_truncated": truncated_any,
        "complete": is_complete_html(final),
        "byte_exact": final == target,
        "finalized": (base_dir / "out.html").exists(),
    }


# ---------------------------------------------------------------------------
# S3：骨架占位 + edit_file 逐段填
# ---------------------------------------------------------------------------
def run_s3(base_dir, target, budget):
    reset_outputs(base_dir)
    import re
    # 精确切分，保证 head + "".join(cards) + tail == target（逐字节可复原）
    first = target.index("  <article")
    head, rest = target[:first], target[first:]
    cards = re.findall(r"(  <article class=\"card\".*?</article>\n)", rest, re.S)
    cards_region = "".join(cards)
    tail = rest[len(cards_region):]
    # 每段一个唯一标记；骨架 = head + 标记串 + tail，体积很小（<预算）
    slots = [f"<!--@C{i}@-->".ljust(0) for i in range(len(cards))]
    skeleton = head + "".join(slots) + tail
    # 1) 写骨架
    WriteFileHandler().execute(
        {"id": "s3-sk", "type": "write_file",
         "input": {"path": "out.html", "content": skeleton}},
        make_ctx(base_dir))
    # 2) 若干 slot 填进"一次 edit 调用"；累计 newText ≤ 预算*0.95（留 JSON 余量）
    ctx = make_ctx(base_dir)
    limit = int(budget * 0.95)
    calls = 1
    max_emit = len(skeleton)
    truncated_any = False
    batch, batch_bytes = [], 0

    def flush():
        nonlocal calls, max_emit, truncated_any, batch, batch_bytes
        if not batch:
            return
        payload = sum(len(e["newText"]) + len(e["oldText"]) for e in batch)
        max_emit = max(max_emit, payload)
        EditFileHandler().execute(
            {"id": f"s3-e{calls}", "type": "edit_file",
             "input": {"path": "out.html", "edits": batch}},
            ctx)
        calls += 1
        batch, batch_bytes = [], 0

    for slot, block in zip(slots, cards):
        # block 已含结尾 \n；newText 原样，oldText=标记，替换后逐字节复原卡片
        item = {"oldText": slot, "newText": block}
        if batch and batch_bytes + len(block) > limit:
            flush()
        batch.append(item)
        batch_bytes += len(block)
    flush()
    final = read_result(base_dir)
    return {
        "calls": calls,
        "max_emit": max_emit,
        "any_truncated": truncated_any,
        "complete": is_complete_html(final),
        "byte_exact": final == target,
        "finalized": (base_dir / "out.html").exists(),
    }


# ---------------------------------------------------------------------------
# 修复代价实验
# ---------------------------------------------------------------------------
def run_r_naive(base_dir, target, budget):
    """S1 截断后重发整份：模拟每次都超预算 → 反复截断（死循环）。"""
    loops = 0
    ok = False
    for _ in range(5):                    # 上限 5，复现"5 次仍不成"
        loops += 1
        emitted, trunc = simulate_truncated(target, budget)
        WriteFileHandler().execute(
            {"id": f"r{loops}", "type": "write_file",
             "input": {"path": "loop.html", "content": emitted}},
            make_ctx(base_dir))
        if not trunc:
            ok = True
            break
    return {"calls": loops, "recovered": ok}


def run_r_silent_corrupt_append(base_dir, target, budget):
    """S2 中途某块被砍：append 是盲拼接，缺的是中间一段，结尾仍 </html>。
    考察 _completeness_note 能否发现——它只看结尾，中间空洞是静默损坏。
    """
    chunks = split_by_lines(target, budget)
    mid = len(chunks) // 2
    reset_outputs(base_dir, "sc.html")
    res = None
    for idx, chunk in enumerate(chunks):
        if idx == mid:
            chunk = chunk[: len(chunk) // 2]      # 该块中途被砍一半
        if idx == mid + 1:
            chunk = "\n" + chunk                   # 模拟下一块照常发出
        res = WriteFileHandler().execute(
            {"id": f"sc{idx}", "type": "write_file",
             "input": {"path": "sc.html", "content": chunk,
                       "mode": "overwrite" if idx == 0 else "append"}},
            make_ctx(base_dir))
    final = read_result(base_dir, "sc.html")
    return {
        "ends_with_html": is_complete_html(final),   # 仍 True → 判不出
        "byte_exact": final == target,               # False → 实际已损坏
        "receipt_says": "complete: yes" if "complete: yes" in res.content else "NO",
    }


def run_r_atomic_fail_edit(base_dir, target, budget):
    """S3 某次 edit 调用的 JSON 被砍：edits 残缺 → _apply_edits 报错 → 原子不落盘，
    标记仍在，下一轮原样重发该批即可，且 oldText 唯一可锚定，无重复风险。
    """
    import re
    reset_outputs(base_dir)
    first = target.index("  <article")
    head, rest = target[:first], target[first:]
    cards = re.findall(r"(  <article class=\"card\".*?</article>\n)", rest, re.S)
    tail = rest[len("".join(cards)):]
    slots = [f"<!--@C{i}@-->" for i in range(len(cards))]
    skeleton = head + "".join(slots) + tail
    ctx = make_ctx(base_dir)
    WriteFileHandler().execute(
        {"id": "af-sk", "type": "write_file",
         "input": {"path": "out.html", "content": skeleton}}, ctx)

    def fill(edits):
        r = EditFileHandler().execute(
            {"id": "af-e", "type": "edit_file",
             "input": {"path": "out.html", "edits": edits}}, ctx)
        return r.content

    # 正常填前一半
    half = len(cards) // 2
    fill([{"oldText": slots[i], "newText": cards[i]} for i in range(half)])
    # 模拟"JSON 被砍"：第 half 批的 oldText 残缺（标记写坏，匹配不到）
    broken = [{"oldText": "<!--@C9@--><!--@C", "newText": cards[half] if half < len(cards) else "x"}]
    err = fill(broken)
    file_after_fail = read_result(base_dir)
    marker_still_there = slots[half] in file_after_fail
    # 失败后原样重发这一槽（锚定唯一标记），无重复
    if half < len(cards):
        fill([{"oldText": slots[half], "newText": cards[half]}])
        fill([{"oldText": slots[i], "newText": cards[i]}
              for i in range(half + 1, len(cards))])
    final = read_result(base_dir)
    return {
        "failed_call_written": (err.startswith("applied")),  # 期望 False：原子未落盘
        "marker_survived": marker_still_there,
        "byte_exact_after_retry": (final == target),
        "error_type": err[:24],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    tmp = Path(".poc_out")
    tmp.mkdir(exist_ok=True)
    target = build_target_html()
    n_cards = target.count('<article class="card"')
    print(f"目标 HTML：{len(target):,} 字节，含 {n_cards} 张卡片\n")

    header = (f"{'策略':<22}{'轮次':>5}{'峰值发射':>10}"
              f"{'截断?':>7}{'完整?':>7}{'字节一致?':>10}{'转正?':>7}{'回执NO?':>8}")
    for cpta, cta in CJK_TOKENS.items():
        budget = content_budget(cta)
        print("=" * 70)
        print(f"场景 chars/token={cta}（{cta}）→ 每轮 content 预算 ≈ {budget:,} 字符")
        print(f"（max_tokens={BUDGET_TOKENS}, headroom={HEADROOM}）")
        print(header)
        print("-" * 70)
        rows = {
            "S1 一次性 overwrite": run_s1(tmp, target, budget),
            "S2 分块 append": run_s2(tmp, target, budget),
            "S3 骨架+edit 逐段填": run_s3(tmp, target, budget),
        }
        for name, m in rows.items():
            print(f"{name:<22}{m['calls']:>5}{m['max_emit']:>10,}"
                  f"{str(m['any_truncated']):>7}{str(m['complete']):>7}"
                  f"{str(m['byte_exact']):>10}{str(m['finalized']):>7}"
                  f"{str(m.get('verdict_flag','-')):>8}")
        # 修复代价
        rn = run_r_naive(tmp, target, budget)
        sa = run_r_silent_corrupt_append(tmp, target, budget)
        ea = run_r_atomic_fail_edit(tmp, target, budget)
        print(f"\n  R-naive（S1 截断后重发整份）：{rn['calls']} 次仍"
              f"{'成功' if rn['recovered'] else '未恢复 → 死循环'}")
        corrupt = sa["ends_with_html"] and not sa["byte_exact"]
        print(f"  静默损坏·append（中间块被砍）：结尾仍</html>={sa['ends_with_html']}，"
              f"回执={sa['receipt_says']}，字节一致={sa['byte_exact']}"
              f"{' → 判不出损坏' if corrupt else ''}")
        print(f"  原子失败·edit（JSON 被砍）：残缺调用落盘={ea['failed_call_written']}，"
              f"标记存活={ea['marker_survived']}，重发该槽后字节一致={ea['byte_exact_after_retry']}"
              f"（错误:{ea['error_type']}…）\n")


if __name__ == "__main__":
    main()
