"""Scanner — 安全扫描 + 运行时审批

两层架构：
1. 离线扫描（scan_skill / scan_skill_deep）：正则 + LLM 分析 skill 安全性，只提醒不阻止
2. 运行时审批（should_approve）：判定操作是否需要用户确认

设计原则（v2）：
- 信任 skill 作者（steps、!cmd 不审批）
- 不信任 LLM 输出（tool_dispatch、ctx_relay 直接拒）
- 扫描只提醒，不阻止
"""

import re
from pathlib import Path
from typing import Literal, Optional
from skill_engine.models import Skill, SkillMeta

# ================================================================
# 常量表
# ================================================================

# 危险命令名单（运行时弹审批）
RISKY_BINARIES: set[str] = {"rm", "cp", "mv", "chmod", "chown", "dd", "mkfs", "python"}

# 敏感路径前缀（读写到此路径外弹审批）
RISKY_PREFIXES: list[str] = ["/etc/", "~/.ssh/", "~/.aws/", "~/.kube/"]

# 敏感文件名（无论路径，匹配到就弹审批）
RISKY_FILENAMES: set[str] = {".env", ".npmrc", ".pypirc", ".netrc"}

# 允许写入的安全路径
ALLOWED_WRITE: list[str] = ["/output/", "<skill_dir>/"]

# 危险语义操作（不搞分类体系，单行例外）
RISKY_SEMANTIC: set[tuple[str, str]] = {("git", "push")}

# ================================================================
# 辅助函数
# ================================================================

_APPROVALS_PATH = Path.home() / ".skill-engine" / "approvals.yaml"
_BLOCKLIST_PATH = Path.home() / ".skill-engine" / "blocklist.yaml"


def _classify(op_str: str) -> tuple[str, Optional[str]]:
    """从命令字符串中拆出 binary 和子命令

    >>> _classify("git push origin main")
    ("git", "push")
    >>> _classify("python scripts/fetch.py")
    ("python", None)
    """
    parts = op_str.strip().split()
    if not parts:
        return ("", None)
    binary = parts[0]
    subcmd = parts[1] if len(parts) > 1 else None
    return (binary, subcmd)


def _path_escapes(cmd_str: str, skill_dir: Path) -> bool:
    """检查命令字符串中是否涉及 skill 目录外的路径

    Args:
        cmd_str: 命令字符串（如 "rm /etc/hosts"）
        skill_dir: skill 所在目录

    Returns:
        True 如果路径出界
    """
    # 提取所有路径 token（/ 开头、./ 开头、~/ 开头）
    tokens = re.findall(r"(?:/[\w./-]+|\.\.?/\S+|~/\S+)", cmd_str)
    for token in tokens:
        # 检查是否在 RISKY_PREFIXES 中（直接字符串比对，跨平台兼容）
        for prefix in RISKY_PREFIXES:
            if token.startswith(prefix):
                return True
        # 检查文件名是否在 RISKY_FILENAMES 中
        token_name = Path(token).name
        if token_name in RISKY_FILENAMES:
            return True
        # 跨平台路径出界检查
        try:
            resolved = Path(token).resolve()
            resolved.relative_to(Path(skill_dir).resolve())
        except (ValueError, OSError):
            pass
    return False


# ================================================================
# 运行时审批
# ================================================================

def should_approve(
    op_str: str,
    skill_dir: str,
    risk_hint: str = "step_exec",
) -> tuple[Literal["SAFE", "ATTENTION", "BLOCK"], str]:
    """判定操作是否需要用户确认

    Args:
        op_str: 操作字符串（如 "rm /etc/hosts"）
        skill_meta: 发起操作的 skill 元数据
        risk_hint: 操作来源
            - step_exec: Steps DSL 硬编码命令（skill 可信）
            - assembler_bang: !cmd 预处理（skill 可信，只查路径出界）
            - ctx_relay: 上一步 LLM 输出作为参数（不信任）
            - tool_dispatch: LLM 吐的 bash tool_call（不信任）

    Returns:
        ("SAFE", "") 或 ("ATTENTION", "原因") 或 ("BLOCK", "原因")
    """
    # BLOCK: LLM 侧命令，不弹窗直接拒
    if risk_hint in ("ctx_relay", "tool_dispatch"):
        return ("BLOCK", f"{risk_hint}: LLM 侧命令，不自动执行")

    binary, subcmd = _classify(op_str)

    # assembler_bang: 只查路径出界，不查 binary
    if risk_hint == "assembler_bang":
        if _path_escapes(op_str, Path(skill_dir)):
            return ("ATTENTION", "!cmd 目标路径在 skill 目录外")
        return ("SAFE", "")

    # step_exec: 检查 binary、路径、语义
    if binary in RISKY_BINARIES:
        return ("ATTENTION", f"危险命令: {binary}")
    if _path_escapes(op_str, Path(skill_dir)):
        return ("ATTENTION", "目标路径在 skill 目录外")
    if (binary, subcmd) in RISKY_SEMANTIC:
        return ("ATTENTION", f"语义风险: {binary} {subcmd}")

    return ("SAFE", "")


# ================================================================
# 审批记录持久化
# ================================================================

def _load_approvals() -> dict:
    """加载 ~/.skill-engine/approvals.yaml"""
    try:
        if _APPROVALS_PATH.exists():
            import yaml
            return yaml.safe_load(_APPROVALS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_approval(skill_name: str, binary: str) -> None:
    """将 (skill, binary) 审批写入 approvals.yaml"""
    import yaml
    data = _load_approvals()
    if skill_name not in data:
        data[skill_name] = {"approvals": []}
    approvals = data[skill_name].setdefault("approvals", [])
    if binary not in approvals:
        approvals.append(binary)
    _APPROVALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _APPROVALS_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _is_approved(skill_name: str, binary: str) -> bool:
    """检查 (skill, binary) 是否已被审批"""
    data = _load_approvals()
    skill_data = data.get(skill_name, {})
    return binary in skill_data.get("approvals", [])


def _is_blocked(skill_name: str) -> bool:
    """检查 skill 是否在阻止列表中"""
    try:
        if _BLOCKLIST_PATH.exists():
            import yaml
            data = yaml.safe_load(_BLOCKLIST_PATH.read_text(encoding="utf-8")) or {}
            return skill_name in data.get("blocklist", {})
    except Exception:
        pass
    return False


def _save_blocklist(skill_name: str) -> None:
    """将 skill 加入阻止列表"""
    import yaml
    data = {}
    if _BLOCKLIST_PATH.exists():
        data = yaml.safe_load(_BLOCKLIST_PATH.read_text(encoding="utf-8")) or {}
    if "blocklist" not in data:
        data["blocklist"] = {}
    data["blocklist"][skill_name] = {
        "blocked_at": __import__("datetime").datetime.now().isoformat(),
        "reason": "用户拒绝",
    }
    _BLOCKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BLOCKLIST_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


# ================================================================
# 离线扫描
# ================================================================

class ScanFinding:
    """单条扫描结果"""

    def __init__(self, severity: str, message: str):
        self.severity = severity  # HIGH / MEDIUM / INFO
        self.message = message

    def __repr__(self):
        return f"[{self.severity}] {self.message}"

    def to_dict(self):
        return {"severity": self.severity, "message": self.message}


def scan_skill(skill: Skill) -> list[ScanFinding]:
    """正则扫描 skill 安全性

    Args:
        skill: Skill 对象

    Returns:
        ScanFinding 列表
    """
    findings: list[ScanFinding] = []

    # 1. 扫 steps 定义（正则匹配 body 中的 exec 命令）
    for m in re.finditer(r'type:\s*exec\s*\n.*?command:\s*([^\n]+)', skill.body, re.DOTALL):
        step_cmd = m.group(1).strip()
        binary, subcmd = _classify(step_cmd)
        if binary in RISKY_BINARIES:
            findings.append(ScanFinding("MEDIUM",
                f"step 使用危险命令: {binary} ({step_cmd[:50]})"))
        if _path_escapes(step_cmd, Path(skill.directory)):
            findings.append(ScanFinding("MEDIUM",
                f"step 目标路径出界: {step_cmd[:50]}"))
        if (binary, subcmd) in RISKY_SEMANTIC:
            findings.append(ScanFinding("MEDIUM",
                f"step 语义风险: {step_cmd[:50]}"))

    # 2. 扫 !cmd 预处理（仅查路径出界，不查 binary）
    bang_cmds = re.findall(r"!`([^`]+)`", skill.body)
    for cmd in bang_cmds:
        if _path_escapes(cmd, Path(skill.directory)):
            findings.append(ScanFinding("MEDIUM",
                f"!cmd 目标路径出界: {cmd[:60]}"
                f"  (↑ !cmd 为作者确定性命令，仅查路径出界)"))

    # 3. 扫 scripts/ 目录（运行时 Gate 看不见的盲区）
    from pathlib import Path as PPath
    scripts_dir = PPath(skill.directory) / "scripts"
    if scripts_dir.exists():
        for script_path in scripts_dir.iterdir():
            if script_path.is_file() and script_path.suffix in (".py", ".sh", ".bat"):
                try:
                    text = script_path.read_text(encoding="utf-8")
                    if re.search(r"os\.system|subprocess\.", text):
                        findings.append(ScanFinding("HIGH",
                            f"脚本 {script_path.name} 含子进程调用"))
                    if re.search(r"requests\.|urllib|httpx\.", text):
                        findings.append(ScanFinding("INFO",
                            f"脚本 {script_path.name} 含网络调用"))
                except Exception:
                    pass

    # 4. 扫 body 中的网络请求
    if "curl" in skill.body or "wget" in skill.body:
        findings.append(ScanFinding("INFO", "skill 发起网络请求"))

    return findings


def scan_all(registry) -> dict[str, list[ScanFinding]]:
    """批量扫描所有 active skill"""
    results: dict[str, list[ScanFinding]] = {}
    for name in registry.list_active():
        skill = registry.load_skill(name)
        if skill:
            results[name] = scan_skill(skill)
    return results


def scan_skill_deep(skill: Skill, llm) -> str:
    """LLM 深度分析 skill 安全性

    Args:
        skill: Skill 对象
        llm: LLM 客户端

    Returns:
        LLM 的自然语言点评
    """
    script_names = []
    from pathlib import Path as PPath
    scripts_dir = PPath(skill.directory) / "scripts"
    if scripts_dir.exists():
        script_names = [p.name for p in scripts_dir.iterdir() if p.is_file()]

    prompt = f"""分析以下 skill 的安全性，关注：
1. 是否有文件删除/修改操作
2. 是否读写敏感路径
3. 是否发起网络请求
4. 是否有可疑脚本

SKILL.md:
{skill.body[:3000]}

脚本列表: {script_names}
"""
    resp = llm.invoke(prompt)
    if hasattr(resp, "content"):
        return resp.content if isinstance(resp.content, str) else str(resp.content)
    return str(resp)