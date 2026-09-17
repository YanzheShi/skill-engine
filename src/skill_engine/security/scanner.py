"""Scanner — 安全扫描 + 运行时审批

两层架构：
1. 离线扫描（scan_skill / scan_skill_deep）：正则 + LLM 分析 skill 安全性，只提醒不阻止
2. 运行时审批（should_approve）：判定操作是否需要用户确认

设计原则（v2）：
- 信任 skill 作者（steps、!cmd 不审批）
- 不信任 LLM 输出（tool_dispatch、ctx_relay 直接拒）
- 扫描只提醒，不阻止
"""

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Optional
from skill_engine.models import Skill, SkillMeta

# ================================================================
# 常量表
# ================================================================

# 危险命令名单（运行时弹审批）
# 含进程终止类：taskkill / kill / pkill —— 这些命令可能误杀 skill-engine 自身
# 进程（MOA 进程内运行时尤其危险），permissive 模式下强制弹审批。
RISKY_BINARIES: set[str] = {"rm", "cp", "mv", "chmod", "chown", "dd", "mkfs",
                            "python", "taskkill", "kill", "pkill"}

# 敏感路径前缀（读写到此路径外弹审批）
# 同时登记 POSIX 形态与「家目录展开」形态：Windows 下 ~ 展开为 C:\Users\<name>，
# 只写字面量 ``~/.ssh/`` 永远匹配不到真实路径（历史 bug，见 _normalize_prefixes）。
RISKY_PREFIXES: list[str] = [
    "/etc/", "~/.ssh/", "~/.aws/", "~/.kube/", "~/.gnupg/", "~/.config/gcloud/",
]

# 敏感文件名（无论路径，匹配到就弹审批）
# 注：config.yml / mcp.json 是本生态里明文密钥最常落地的两个文件
# （mcp_hub_token / tavily_api_key / r2_token 等），必须进名单。
RISKY_FILENAMES: set[str] = {
    ".env", ".npmrc", ".pypirc", ".netrc",
    "config.yml", "config.yaml", "mcp.json",
    ".git-credentials", ".htpasswd", "id_rsa", "id_ed25519",
}

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
    # 二进制名统一转小写，避免模型写 TASKKILL / Kill 之类大小写变体漏判
    binary = parts[0].lower()
    subcmd = parts[1] if len(parts) > 1 else None
    return (binary, subcmd)


# ----------------------------------------------------------------
# 路径出界判定
# ----------------------------------------------------------------
# 历史 bug（P0 修复）：原实现里 `resolved.relative_to(root)` 失败（= 出界）
# 时走了 `except: pass`，于是「路径出界」这条分支永远不返回 True —— 整个函数
# 实际只在前缀/文件名黑名单命中时才生效。后果：`write_file` / `read_file`
# 可以触碰工作目录外的任意绝对路径，且 `config.yml` 不在名单里，明文密钥
# （mcp_hub_token / tavily_api_key / r2_token）可被直接读出。
# 另一个失效点：RISKY_PREFIXES 只写了字面量 `~/.ssh/`，Windows 上真实路径是
# `C:\Users\<name>\.ssh\...`，永远匹配不到。

# 路径 token 提取。覆盖 5 类形态：
#   win     C:\x / C:/x        Windows 盘符绝对路径
#   home    ~/x / ~\x          家目录
#   rel     ./x / ../x         显式相对路径
#   posix   /x                 POSIX 绝对路径（负向后视排除 URL 中的 //host）
#   bare    config.yml        裸文件名（带扩展名，无目录分隔符）
#   dotfile .env               裸文件名（点开头，无第二个点）
# bare / dotfile 只用于「敏感文件名」判定，不参与出界计算（无路径语义）。
# win 分支带负向后视 `(?<!\w)`：前一个字符是字母数字时不算盘符（挡掉 `https` 的 `s:`）。
# 注意不能用 `(?<![:\w])` —— 工具侧的 op_str 形如 `read:D:/x`，路径紧跟在冒号后。
_PATH_TOKEN_RE = re.compile(
    r"""
      (?P<win>     (?<!\w)[A-Za-z]:[\\/][^\s"'|&;<>()]*   )
    | (?P<home>    ~[\\/][^\s"'|&;<>()]*                    )
    | (?P<rel>     \.\.?[\\/][^\s"'|&;<>()]*                )
    | (?P<posix>   (?<![:\w/])/[^\s"'|&;<>()]*              )
    | (?P<bare>    \.?[\w][\w.-]*\.[A-Za-z0-9]{1,8}         )
    | (?P<dotfile> \.[A-Za-z0-9_\-]{1,32}                   )
    """,
    re.VERBOSE,
)

# URL 剥离：`scheme://...` 整体不是文件系统路径。必须先剥掉再抽 token，
# 否则 `api.example.com/v1` 里的 `.com/` 会被 rel 分支当成相对路径 `.com/v1`
# （配上 cwd 就成了假出界），`s://` 也会被 win 分支当成盘符。
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S*")



def _normalize_prefixes() -> tuple[str, ...]:
    """把 RISKY_PREFIXES 展开为「归一化后可直接前缀比对」的元组。

    归一化：反斜杠→斜杠、小写、补尾部斜杠。``~/`` 额外展开为 ``<home>/``，
    使 Windows 绝对路径（``C:/Users/x/.ssh/...``）也能命中。
    """
    home = Path.home()
    out: set[str] = set()
    for p in RISKY_PREFIXES:
        candidates = [p]
        if p.startswith("~/"):
            candidates.append(str(home / p[2:]))
        for c in candidates:
            c = c.replace("\\", "/").lower()
            if not c.endswith("/"):
                c += "/"
            out.add(c)
    return tuple(sorted(out))


_NORMALIZED_PREFIXES = _normalize_prefixes()


def _is_absolute_token(token: str) -> bool:
    """token 是否为绝对路径。跨平台判定：Windows 看盘符/UNC，POSIX 看前导 /。"""
    try:
        return PureWindowsPath(token).is_absolute() or PurePosixPath(token).is_absolute()
    except (ValueError, TypeError):
        return False


def _within_root(target, root: Path) -> bool:
    """target 解析后是否落在 root 内（root 自身也算在内）。

    解析失败一律判「不在 root 内」—— 故意 fail-closed。
    """
    try:
        p = Path(target)
    except (ValueError, TypeError):
        return False
    if not p.is_absolute():
        return False
    try:
        r = os.path.normcase(str(Path(root).resolve()))
        t = os.path.normcase(str(p.resolve()))
    except (OSError, ValueError, RuntimeError):
        return False
    return t == r or t.startswith(r + os.sep)


def _path_escapes(cmd_str: str, skill_dir: Path, cwd: Optional[Path] = None) -> bool:
    """检查命令字符串是否涉及 root 之外的路径，或引用了敏感文件名。

    Args:
        cmd_str: 命令字符串（如 ``"rm /etc/hosts"``、
            ``r"type C:\\Users\\me\\.ssh\\id_rsa"``）
        skill_dir: 允许访问的**根目录**。工具侧应传 ``working_root``；
            命令侧应传命令实际的工作目录（``Executor`` 用的是 base_dir），
            否则「在工作区内但不在 skill 目录内」的合法路径会被误判。
        cwd: 相对路径（``./x``、``../x``）的解析基准；None 时仅做保守判定
            （token 含 ``..`` 即视为出界）。

    Returns:
        True 表示涉及越界路径或敏感文件名，调用方应升级为 ATTENTION。
    """
    root = Path(skill_dir).expanduser()
    base = Path(cwd).expanduser() if cwd else None
    # 先剥离 URL：see _URL_RE 注释（不剥会产生一类固定误报）
    scan_str = _URL_RE.sub(" ", cmd_str)

    for m in _PATH_TOKEN_RE.finditer(scan_str):
        token = m.group(0)
        kind = m.lastgroup
        name = Path(token.replace("\\", "/")).name

        # 1) 敏感文件名：任意形态（含无分隔符的裸文件名）都拦
        if name in RISKY_FILENAMES:
            return True
        # 裸文件名没有路径语义，不参与出界计算
        if kind in ("bare", "dotfile"):
            continue

        norm = token.replace("\\", "/").lower()
        # 2) URL 里的 `//host/path` 不是文件系统路径
        if norm.startswith("//"):
            continue
        # 3) 敏感前缀（已归一化：反斜杠 / 大小写 / 家目录展开）
        if norm.startswith(_NORMALIZED_PREFIXES):
            return True
        # 4) 家目录 token 一律出界（工作区不会落在 ~ 下）
        if token.startswith("~"):
            return True
        # 5) 绝对路径：必须落在 root 内
        if _is_absolute_token(token):
            if not _within_root(token, root):
                return True
            continue
        # 6) 相对路径
        if base is not None:
            if not _within_root(base / token, root):
                return True
        elif ".." in Path(token).parts:
            return True
    return False


def _load_allowlist() -> set[str]:
    """从环境变量 SKILLS_ENGINE_ALLOWLIST 加载允许的 binary 列表"""
    raw = os.environ.get("SKILLS_ENGINE_ALLOWLIST", "").strip()
    if not raw:
        return set()
    return set(b.strip().lower() for b in raw.split(",") if b.strip())


# ================================================================
# 运行时审批
# ================================================================

def should_approve(
    op_str: str,
    skill_dir: str,
    risk_hint: str = "step_exec",
    cwd: Optional[str] = None,
) -> tuple[Literal["SAFE", "ATTENTION", "BLOCK"], str]:
    """判定操作是否需要用户确认

    Args:
        op_str: 操作字符串（如 "rm /etc/hosts"）
        skill_dir: 允许访问的**根目录**。工具侧传 working_root，
            命令侧传命令实际的工作目录（Executor 用 base_dir）
        risk_hint: 操作来源
            - step_exec: Steps DSL 硬编码命令（skill 可信）
            - assembler_bang: !cmd 预处理（skill 可信，只查路径出界）
            - ctx_relay: 上一步 LLM 输出作为参数（不信任）
            - tool_dispatch: LLM 吐的 bash tool_call（不信任）
            - tool_file: LLM 吐的文件操作 tool_call（只查路径，strict 不 BLOCK）
        cwd: 相对路径的解析基准（一般等于 skill_dir）。None 时相对路径走保守判定。

    Returns:
        ("SAFE", "") 或 ("ATTENTION", "原因") 或 ("BLOCK", "原因")
    """
    # 安全模式检查
    from skill_engine.config import get_security_mode
    sec_mode = get_security_mode()

    # 安全模式 off：全放行
    if sec_mode == "off":
        return ("SAFE", "")

    # strict 模式：LLM 侧命令直接 BLOCK
    if sec_mode == "strict" and risk_hint in ("ctx_relay", "tool_dispatch"):
        return ("BLOCK", f"{risk_hint}: LLM 侧命令，不自动执行")

    root = Path(skill_dir)
    base = Path(cwd) if cwd else None

    # tool_file: 文件操作，只查路径出界，不查 binary
    # strict 模式下也不 BLOCK，因为文件操作受路径解析约束
    if risk_hint == "tool_file":
        if _path_escapes(op_str, root, cwd=base):
            return ("ATTENTION", "目标路径在工作目录之外")
        return ("SAFE", "")

    # permissive：LLM 侧命令也走命令级检查
    binary, subcmd = _classify(op_str)

    # allowlist 检查：命中 allowlist 的 binary 自动放行（只查路径）
    allowlist = _load_allowlist()
    if binary in allowlist:
        if _path_escapes(op_str, root, cwd=base):
            return ("ATTENTION", "目标路径在工作目录之外")
        if (binary, subcmd) in RISKY_SEMANTIC:
            return ("ATTENTION", f"语义风险: {binary} {subcmd}")
        return ("SAFE", "")

    # assembler_bang: 只查路径出界，不查 binary
    if risk_hint == "assembler_bang":
        if _path_escapes(op_str, root, cwd=base):
            return ("ATTENTION", "!cmd 目标路径在工作目录之外")
        return ("SAFE", "")

    # 检查 binary、路径、语义
    if binary in RISKY_BINARIES:
        return ("ATTENTION", f"危险命令: {binary}")
    if _path_escapes(op_str, root, cwd=base):
        return ("ATTENTION", "目标路径在工作目录之外")
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