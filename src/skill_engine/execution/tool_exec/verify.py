"""verify_command 自动验证钩子。

轮内写盘完成后跑一次，失败时把结构化信号回灌 LLM，驱动"改→验→修"闭环
（不依赖 prompt 自觉）。命令来自 frontmatter（作者声明、与 Steps DSL 命令
同级可信），不走运行时审批。
"""

from pathlib import Path


def _extract_test_failures(output: str) -> list:
    """从 pytest 风格输出中提取 FAILED/ERROR 清单行（上限 20 条）。"""
    fails = []
    for ln in (output or "").splitlines():
        s = ln.strip()
        if s.startswith(("FAILED", "ERROR")):
            fails.append(s[:200])
            if len(fails) >= 20:
                break
    return fails


def _run_verification(executor, base_dir: Path, verify_command: str, timeout: int):
    """运行 verify_command。成功返回 None；失败返回回灌给 LLM 的反馈文本。"""
    try:
        r = executor.run_step(verify_command, cwd=base_dir, timeout=timeout)
    except Exception as e:
        return f"[自动验证执行异常] {verify_command}\n{e}"
    if r.get("exit_code", -1) == 0 and not r.get("timed_out"):
        return None
    fails = _extract_test_failures((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
    lines = [
        f"[自动验证失败] 命令: {verify_command}",
        f"exit_code: {r.get('exit_code', -1)}" + (" (timed_out)" if r.get("timed_out") else ""),
        "请根据以下失败信息诊断并修复，然后再次验证。",
    ]
    if fails:
        lines.append("失败清单:")
        lines.extend(f"  {x}" for x in fails)
    err = (r.get("stderr") or "").strip()
    if err:
        # 验证失败 stderr 不静默截断，超长时标注截断量
        if len(err) > 8000:
            lines.append("stderr:\n" + err[:8000] + f"\n... (stderr 已截断，原长 {len(err)} 字符)")
        else:
            lines.append("stderr:\n" + err)
    out = (r.get("stdout") or "").strip()
    if out:
        lines.append("stdout(尾部):\n" + out[-1500:])
    return "\n".join(lines)[:4000]
