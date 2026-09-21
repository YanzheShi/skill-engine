"""Windows srt 沙箱接入：会话级 ACL 授权 + 逐命令 ``srt-win exec``。

设计依据见 ``docs/sandbox-integration-design.md`` §3 / §4.7；三处与文档初版
假设不符、已按实测修正的硬约束（改动前务必先读 §4.7.12）：

1. **只有 DENY ACE 能挡，``acl grant`` 是加性 ALLOW、不是边界**。
   本机 ``D:\\`` 根有 ``Authenticated Users:(OI)(CI)(IO)(M)``，沙箱用户在
   ``BUILTIN\\Users`` 内 → **D 盘默认全盘可写**（含兄弟项目）。写边界只能靠
   DENY，而 DENY 见第 2 条 → **per-session 建不出 D 盘写边界**，该缺口由路由层
   （越界路径走 ASK）与快照回滚承担，不由本模块承担。``capability_report()``
   会把这条如实报出去，不做粉饰。

2. **``acl stamp`` / ``acl grant`` 的成本随目标父目录子树大小线性上升**。
   ACE 带 ``(OI)(CI)``，Windows 同步向下传播：
   小目录里的单文件 0.52s；``config.yml``（父目录 17 320 文件）3.8s；
   项目根目录本身 158s；全盘级不可行。
   因此本模块**只 stamp 单个机密文件**，绝不 stamp 目录。

3. **子进程不继承宿主 env**（``exec`` 的 env 完全来自 ``--env`` 覆盖 + 沙箱用户
   的 profile 默认值），**cwd 则继承 broker 进程的 cwd**。
   所以：命令的 env 必须显式全量透传，cwd 靠 ``Popen(cwd=...)`` 传进来。

调用路径上刻意**不使用 node CLI**（``dist/cli.js``）：实测它每个命令 8–20s，
而原生 ``vendor/srt-win/x64/srt-win.exe`` 每命令稳定 0.29–0.71s。这也意味着
设计文档 §4.5 的「常驻守护进程」方案（route B）不再需要。

会话生命周期：``holder_pid`` = 引擎自身 PID。``acl`` 的 ACE 按 holder 引用计数，
持有者退出或显式 release 才摘除；进程崩溃留下的孤儿 ACE 会被下一次 ``acl``
操作自动回收（``recovery pruned dead broker(s)``），因此是自愈的。
"""

import atexit
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from skill_engine.execution.paths import runtime_dir

logger = logging.getLogger("skill_engine.sandbox")

# ---------------------------------------------------------------- 常量

SRT_BIN_ENV = "SKILL_ENGINE_SRT_WIN"
"""显式指定 ``srt-win.exe`` 路径（未设置时按 npx 缓存通配搜索）。"""

_NPX_CACHE_GLOBS = (
    # npx 缓存（@anthropic-ai/sandbox-runtime）
    r"%LOCALAPPDATA%\npm-cache\_npx\*\node_modules\@anthropic-ai\sandbox-runtime\vendor\srt-win",
    r"%APPDATA%\npm-cache\_npx\*\node_modules\@anthropic-ai\sandbox-runtime\vendor\srt-win",
    r"%USERPROFILE%\AppData\Local\npm-cache\_npx\*\node_modules\@anthropic-ai\sandbox-runtime\vendor\srt-win",
)

# 引擎自身在工作目录内的机密文件 → 默认 denyRead（沙箱内不可读）
_DEFAULT_DENY_READ = ("config.yml", "config.yaml", "mcp.json", ".env")

# 需要透传进沙箱的敏感度豁免（默认拦下所有疑似密钥的变量）
_SECRET_NAME_RE = re.compile(
    r"(API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)",
    re.IGNORECASE,
)

# 沙箱内无网络（WFP 按沙箱用户 SID 全拦）→ 这些命令必须走审批后裸跑
_NETWORK_VERBS = frozenset({
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "ping", "tracert",
    "nc", "ncat", "telnet", "ftp", "iwr",
    # PowerShell 小写存放：cmdlet 本身大小写不敏感，而这里的比较是「小写 in 集合」
    "invoke-webrequest", "invoke-restmethod",
})

# 需要宿主缓存 / 凭据目录（C:\Users\<user>\AppData 沙箱用户不可读）→ 走审批后裸跑
_HOST_VERBS = frozenset({
    "uv", "uvx", "pip", "pip3", "poetry", "pipenv", "conda",
    "npm", "npx", "pnpm", "yarn", "corepack",
    "winget", "choco", "scoop", "gh", "docker", "kubectl", "az", "aws", "gcloud",
    "code", "start", "explorer",
})

# 纯只读动词：无副作用，直接裸跑省掉 ~0.35s/步
_READONLY_VERBS = frozenset({
    "ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "rg", "find",
    "findstr", "file", "stat", "du", "df", "pwd", "cd", "echo", "which",
    "where", "typeperf", "tree", "diff", "cmp", "sort", "uniq", "cut", "awk",
    "sed", "jq", "hostname", "whoami", "date", "ver", "set",
})

# git 只读子命令（其余 git 子命令可能写 ref / 联网）
_GIT_READONLY_SUB = frozenset({
    "status", "diff", "log", "show", "branch", "rev-parse", "ls-files",
    "remote", "describe", "shortlog", "blame", "config", "rev-list",
    "cat-file", "symbolic-ref", "name-rev", "whatchanged",
})

# 删除类动词：沙箱内 rm 会绕过宿主 safe-delete（实测硬删），
# 所以删除命令**不进沙箱**，一律走审批后裸跑，保留回收站/FAIL_CLOSED 守卫。
_DELETE_VERBS = frozenset({
    "rm", "rmdir", "rd", "del", "erase", "unlink", "shred",
    "remove-item", "ri",
})

# 解释器动词：只有首词是它们时，才做「脚本内删除等价物」扫描。
# **必须按首词门控** —— 否则 `grep "os.remove" src/` 这种「只是提到删除字样」
# 的只读命令会被误判成删除操作（这是本清单从「只认命令词」扩到「认脚本内容」
# 时最容易踩的坑）。
_PY_VERBS = frozenset({"python", "python3", "py", "pythonw"})

# 解释器命令行 / 代码里的删除 API。两个刻意取舍：
# - 用 `\b(rmtree|rmdir|unlink|removedirs)\s*\(` 而非只认带点前缀的写法，
#   是为了覆盖 `from shutil import rmtree; rmtree(...)` 这种裸名导入；
# - **不收**裸 `remove(`：`a.remove(x)`（list.remove）太常见，误伤成本高于收益，
#   所以 `remove` 只认带命名空间的 `os.remove(`。
_SCRIPT_DELETE_RE = re.compile(
    r"(?i)(shutil\.rmtree|os\.remove|\b(rmtree|rmdir|unlink|removedirs)\s*\()"
)

# 解释器把删除「外包给子进程」的形态：os.system('rm -rf x') /
# subprocess.run(['rm','-rf','x'])。上面那条正则抓不到（`rm` 被引号/列表语法包住），
# 所以这里用「有外包调用 + 有删除词」双条件，避免 subprocess.run(['ls']) 也触发审批。
_SHELLOUT_RE = re.compile(r"(?i)(os\.system|os\.popen|os\.exec\w*|subprocess\.\w+)")
_DELETE_WORD_RE = re.compile(
    r"(?i)\b(rm|rmdir|rd|del|erase|unlink|shred|remove-item|ri)\b"
)

ROUTE_DIRECT = "direct"
ROUTE_SANDBOX = "sandbox"
ROUTE_ASK = "ask"

# 破坏「只读」语义的 shell 控制符（重定向 / 串联 / 命令替换）。
# 刻意不含单个 `|`：`echo x | grep y` 这类管道两端都是只读动词，值不值一次
# 沙箱包装由「首词是否只读」决定，加 `|` 只会让常见只读管道全部变慢。
_CONTROL_OPS_RE = re.compile(r"[<>]|&&|\|\||;|`|\$\(")

# 白名单里的「只读动词」其实带**就地写**开关，设计文档 §4.7.9 明确要求这类要进沙箱
# （`sed -i` 就是写文件）。刻意按动词分派而非统一匹配 `-i`：`grep -i`（忽略大小写）
# 极常见，统一匹配会把它误判成写操作。
_INPLACE_WRITE_FLAGS = {
    "sed": re.compile(r"(?i)(?:^|\s)-i(?:\s|\.|=|$)"),
    "sort": re.compile(r"(?i)(?:^|\s)-o(?:\s|$)"),
    "awk": re.compile(r"(?i)(?:^|\s)-i\s+inplace\b"),
}

# 只读白名单动词上「本身就是删除」的开关：`find . -delete` 明明是删除，
# 却因为 `find` 在只读白名单里而裸跑（宿主守卫仍生效，但**没有审批**，
# 且白名单名不副实）。命中 → ASK，而不是降级进沙箱：沙箱内删得更彻底。
_DESTRUCTIVE_FLAGS = {
    "find": re.compile(r"(?i)(?:^|\s)-delete(?:\s|$)"),
}

# 取值不落「子命令」位置的 git 选项（`git -C <dir> status` 里的 <dir> 不是子命令）。
# 小写存放：-C（chdir）与 -c（config）都取值，无需区分大小写。
_GIT_OPT_WITH_VALUE = frozenset({
    "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
})


# ---------------------------------------------------------------- 路由


_EXE_SUFFIXES = frozenset({".exe", ".cmd", ".bat", ".com", ".ps1"})


def _verb_name(token: str) -> str:
    """把 token 归一化成「动词名」：去目录、去引号、小写、去可执行后缀。

    ``D:\\tools\\rm.exe`` → ``rm``（否则白名单/删除清单永远匹配不上带后缀的调用）。
    只剥已知可执行后缀，避免把 ``a.py`` 之类误伤成 ``a``。
    """
    name = Path(token.replace("\\", "/")).name.lower()
    stem, dot, ext = name.rpartition(".")
    if dot and f".{ext}" in _EXE_SUFFIXES:
        return stem
    return name


def _first_token(command: str) -> str:
    """取命令首个词（去路径、去引号、小写、去可执行后缀）。"""
    stripped = command.strip().lstrip("@").strip()
    if not stripped:
        return ""
    return _verb_name(stripped.split()[0].strip("'\""))


def _tokens(command: str) -> list[str]:
    """粗切所有词（用于动词全命令扫描，不追求精确分词）。"""
    return [t.strip("'\"") for t in re.split(r"[\s|&;()<>]+", command) if t.strip()]


def _has_verb(command: str, verbs: frozenset) -> bool:
    for t in _tokens(command.lower()):
        if _verb_name(t) in verbs:
            return True
    return False


def _has_inplace_write(command: str) -> bool:
    """首词是只读动词、但带了就地写开关（sed -i / sort -o / awk -i inplace）。"""
    pat = _INPLACE_WRITE_FLAGS.get(_first_token(command))
    return bool(pat and pat.search(command))


def detect_script_level_delete(code: str) -> Optional[str]:
    """在**已确定是解释器代码**的文本里找删除等价物；命中返回模式串，否则 None。

    与 ``decide_route`` 里的用法不同：本函数**不做首词门控**。调用方已明确知道
    手里是 Python 代码时用它 —— 典型是 ``run_python`` 的 ``code`` 参数，其首词
    常是 top-level ``import`` / 赋值 / ``with``，用 ``_PY_VERBS`` 门控会把整类
    代码漏掉。shell 命令行请走 ``decide_route``。

    覆盖两种形态：
    1. 直调删除 API：``import shutil; shutil.rmtree('build')``
    2. 外包给子进程：``import os; os.system('rm -rf build')``
    """
    m = _SCRIPT_DELETE_RE.search(code)
    if m:
        return m.group(0)
    if _SHELLOUT_RE.search(code) and _DELETE_WORD_RE.search(code):
        return "子进程删除"
    return None


def _has_script_level_delete(command: str, first: str) -> bool:
    """shell 命令行里的「脚本内删除等价物」——按首词门控到解释器。

    门控是为了不误伤：``grep "os.remove" src/`` 只是**提到**删除 API，
    不是执行删除。``first`` 由调用方传入（``decide_route`` 已算过），避免重复解析。
    """
    return first in _PY_VERBS and detect_script_level_delete(command) is not None


def _has_destructive_flag(command: str) -> str:
    """首词带「本身就是删除」的开关时返回开关名（如 ``-delete``），否则空串。"""
    pat = _DESTRUCTIVE_FLAGS.get(_first_token(command))
    if not pat:
        return ""
    m = pat.search(command)
    return m.group(0).strip() if m else ""


def _git_subcommand(command: str) -> str:
    """取 git 的子命令，跳过取值选项及其值（`git -C <dir> status` → ``status``）。"""
    toks = _tokens(command)[1:]
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("--") and "=" in t:      # --git-dir=... 自带值
            i += 1
        elif t.lower() in _GIT_OPT_WITH_VALUE:   # 选项与值占两个 token
            i += 2
        elif t.startswith("-"):                  # 无值开关，或 --version 这类
            i += 1
        else:
            return t.lower()
    return ""


def decide_route(command: str) -> tuple[str, str]:
    """决定命令的路由：``(route, reason)``，route ∈ {direct, sandbox, ask}。

    顺序即优先级，与设计文档 §4.7.9 的「白名单豁免 + 默认进沙箱」一致，
    另加三条实测驱动的例外：

    - **删除类命令 → ask**：沙箱内 ``rm`` 绕过宿主 safe-delete 守卫（实测
      工作区内直接硬删，不再进回收站），比现状更危险 → 不进沙箱。
    - **解释器里的脚本级删除 → ask**：``python -c "shutil.rmtree(...)"`` 这类
      命令，首个词不是删除动词，但执行的是删除。只消硬删，同样绕过宿主守卫
      → 按首词门控到 ``_PY_VERBS`` 后扫内容（见 ``_has_script_level_delete``）。
    - **联网/宿主工具 → ask**：沙箱内网络被 WFP 全拦、``C:\\Users\\<user>\\AppData``
      不可读（uv/npm/pip 缓存都在那儿）→ 进沙箱必然失败，走审批后裸跑。

    Returns:
        (route, reason)；reason 面向日志/trace，用于解释为什么这么路由。
    """
    cmd = (command or "").strip()
    if not cmd:
        return ROUTE_DIRECT, "空命令"

    first = _first_token(cmd)

    # 更具体的检查放前面：解释器里的删除（`python -c "...rmtree(...)"`）与
    # 裸删除动词都路由到 ASK，但前者的 reason 能直接指出「是代码里的删除」。
    # 顺序在这里只影响 reason 的可读性，不影响 route。
    # 注：`_has_verb` 是**全 token** 扫描，`os.system('rm -rf x')` 里被引号包住的
    # `'rm` 它也能命中（strip 引号后等于 `rm`）—— 所以那条子进程形态在本次改动
    # 之前其实就已是 ASK，只是 reason 说的是「删除类命令」而非「脚本内删除」。
    if _has_script_level_delete(cmd, first):
        return ROUTE_ASK, (
            f"解释器命令含脚本内删除等价物（{first}）："
            "沙箱内删除绕过宿主守卫，需审批后裸跑"
        )

    if _has_verb(cmd, _DELETE_VERBS):
        return ROUTE_ASK, "删除类命令：保留宿主回收站守卫（沙箱内 rm 会硬删）"

    flag = _has_destructive_flag(cmd)
    if flag:
        return ROUTE_ASK, f"{first} {flag}：本身就是删除，只读白名单不适用"

    if first in _NETWORK_VERBS:
        return ROUTE_ASK, f"联网命令（{first}）：沙箱内网络被 WFP 全拦"
    if first in _HOST_VERBS:
        return ROUTE_ASK, f"宿主工具（{first}）：需要 AppData 缓存/凭据，沙箱内不可读"

    if first == "git":
        sub = _git_subcommand(cmd)
        if sub and sub not in _GIT_READONLY_SUB:
            return ROUTE_ASK, f"git {sub}：可能写 ref / 联网"
        # 只读子命令（或 `git --version` 这类无子命令形态）→ 无副作用，裸跑
        if not _CONTROL_OPS_RE.search(cmd):
            return ROUTE_DIRECT, f"git {sub or '只读形态'}：只读子命令"
        # 只读子命令 + 重定向/串联 → 落到下面的默认分支进沙箱

    if first in _READONLY_VERBS:
        if _has_inplace_write(cmd):
            return ROUTE_SANDBOX, f"{first} 带就地写开关（-i/-o）：白名单不适用"
        if not _CONTROL_OPS_RE.search(cmd):
            return ROUTE_DIRECT, f"只读白名单（{first}）且无重定向/串联"
        return ROUTE_SANDBOX, f"含重定向/串联，降级进沙箱（{first}）"

    return ROUTE_SANDBOX, "默认进沙箱"


# ---------------------------------------------------------------- 能力矩阵


class SandboxUnavailable(RuntimeError):
    """沙箱后端不可用（未安装 / 平台不支持 / 探测失败）。"""


CAPABILITY_MATRIX = (
    ("网络（含回环）", "全拦",
     "WFP 按沙箱用户 SID 过滤：外联 curl 返回 000；回环 127.0.0.1 亦被拦（WinError 10013）"),
    ("进程与桌面", "隔离", "受限令牌 + kill-on-close job + 非交互桌面"),
    ("C 盘写入", "默认拒绝", "home / Program Files / Windows 实测 DENIED"),
    ("机密文件读取", "拦住（需会话内 stamp）", "config.yml / mcp.json 实测 DENIED"),
    ("D 盘写入", "挡不住（已知缺口）", "Authenticated Users 全盘 M，DENY 传播成本不可接受"),
    ("删除保护", "沙箱内失效",
     "沙箱 rm 绕过宿主回收站守卫 → 删除类命令（含解释器里的 rmtree/remove/unlink）均改走 ASK"),
)
"""该机器上沙箱实际挡得住什么（实测结论，随平台/ACL 变化的项目需重新实测）。"""


class SandboxManager:
    """一个会话内的 srt 沙箱管理器（进程级单例语义，按工作目录复用）。

    用法::

        mgr = SandboxManager(workspace_root=cwd)
        argv = mgr.wrap("pytest -q", cwd=cwd, env=env, shell="cmd")
        if argv is None:            # 沙箱不可用 → 由调用方 fail-closed 或按策略降级
            ...
        subprocess.Popen(argv, cwd=str(cwd), env=env)

    ``ensure_ready()`` 是惰性的：只有真的要进沙箱时才付出会话级一次性成本
    （机密文件 stamp ~3–7s + 一次能力探测 ~0.35s），只跑只读白名单的会话
    完全不付这个成本。
    """

    _registry: dict = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        workspace_root: Path,
        deny_read: Optional[list] = None,
        grant_read: Optional[list] = None,
        srt_bin: Optional[str] = None,
        env_passthrough: Optional[list] = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.holder_pid = os.getpid()
        self.deny_read = [Path(p) for p in (deny_read or [])]
        self.grant_read = [Path(p) for p in (grant_read or [])]
        self.env_passthrough = set(env_passthrough or [])
        self._srt_bin = srt_bin or None
        self._sid: Optional[str] = None
        self._state = "new"          # new | ready | unavailable
        self._lock = threading.RLock()
        self._stamped: list = []
        self._granted: list = []
        self._tmp_dir: Optional[Path] = None
        self.last_error = ""
        self.workspace_writable = True
        self.sandboxed_calls = 0
        self.setup_ms = 0

    # ------------------------------------------------------------ 工厂

    @classmethod
    def get(cls, workspace_root: Path, **kw) -> "SandboxManager":
        """按工作目录复用实例（同一进程内重复构造会重复付 stamp 成本）。"""
        key = str(Path(workspace_root).resolve())
        with cls._registry_lock:
            mgr = cls._registry.get(key)
            if mgr is None:
                mgr = cls(Path(workspace_root), **kw)
                cls._registry[key] = mgr
                atexit.register(mgr.close)
            return mgr

    @classmethod
    def forget(cls, workspace_root: Path) -> None:
        with cls._registry_lock:
            cls._registry.pop(str(Path(workspace_root).resolve()), None)

    # ------------------------------------------------------------ 二进制发现

    def _resolve_srt_bin(self) -> Optional[str]:
        if self._srt_bin and Path(self._srt_bin).is_file():
            return self._srt_bin
        env_bin = os.environ.get(SRT_BIN_ENV, "").strip()
        if env_bin and Path(env_bin).is_file():
            self._srt_bin = env_bin
            return env_bin

        arch = "arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() == "arm64" else "x64"
        candidates: list[str] = []
        for pat in _NPX_CACHE_GLOBS:
            expanded = os.path.expandvars(pat)
            candidates.extend(glob.glob(os.path.join(expanded, arch, "srt-win.exe")))
            candidates.extend(glob.glob(os.path.join(expanded, "*", "srt-win.exe")))
        for c in candidates:
            if Path(c).is_file():
                self._srt_bin = c
                return c
        self.last_error = (
            "未找到 srt-win.exe。请先 `npx @anthropic-ai/sandbox-runtime` 安装运行时，"
            f"或设置环境变量 {SRT_BIN_ENV} 指向 srt-win.exe。"
        )
        return None

    # ------------------------------------------------------------ srt 调用

    def _run_srt(self, args: list, stdin: Optional[bytes] = None, timeout: int = 300):
        """调 srt-win，返回 (rc, stdout, stderr)。srt-win 的中文提示是系统编码。"""
        assert self._srt_bin
        try:
            p = subprocess.run(
                [self._srt_bin] + args,
                input=stdin,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, "", f"srt-win {' '.join(args[:2])} 超时（{timeout}s）"
        except OSError as e:
            return 1, "", f"srt-win 启动失败：{e}"
        out = (p.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
        return p.returncode, out, err

    # ------------------------------------------------------------ 会话准备

    def ensure_ready(self) -> bool:
        """惰性完成会话级准备：探测后端 → stamp 机密文件 → 验证可执行/可写。

        只有真要进沙箱的调用才走这里，成本一次性（见模块 docstring 第 2 条）。
        """
        with self._lock:
            if self._state == "ready":
                return True
            if self._state == "unavailable":
                return False

            if os.name != "nt":
                return self._mark_unavailable("srt 沙箱当前仅支持 Windows（srt-win 是 Windows 专用实现）")
            if not self._resolve_srt_bin():
                return self._mark_unavailable(self.last_error)

            import time as _time
            t0 = _time.perf_counter()

            rc, out, err = self._run_srt(["user", "status"], timeout=60)
            if rc != 0:
                return self._mark_unavailable(f"`srt-win user status` 失败：{err or out}")
            try:
                status = json.loads(out.splitlines()[0])
            except Exception:
                return self._mark_unavailable(f"`srt-win user status` 输出无法解析：{out[:200]}")
            if not status.get("cred_present"):
                return self._mark_unavailable(
                    "沙箱账户未安装（cred_present=false）。请先以管理员身份执行 "
                    "`srt-win install` 完成一次性装机。"
                )
            self._sid = status.get("marker_user_sid") or ""
            if not self._sid:
                return self._mark_unavailable("无法取得沙箱用户 SID")

            self._tmp_dir = runtime_dir(self.workspace_root) / "tmp"
            try:
                self._tmp_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return self._mark_unavailable(f"无法创建工作目录临时目录 {self._tmp_dir}：{e}")

            if not self._stamp_denies():
                return self._mark_unavailable(self.last_error)
            if not self._verify_exec():
                return self._mark_unavailable(self.last_error)

            self._verify_workspace_writable()
            self.setup_ms = int((_time.perf_counter() - t0) * 1000)
            self._state = "ready"
            logger.info(
                "sandbox ready: 会话准备 %.1fs（stamp %d 项），工作目录%s",
                self.setup_ms / 1000, len(self._stamped),
                "可写" if self.workspace_writable else "只读（已尝试 grant，仍不可写）",
            )
            return True

    def _mark_unavailable(self, reason: str) -> bool:
        self._state = "unavailable"
        self.last_error = reason
        logger.warning("sandbox 不可用：%s", reason)
        return False

    def _stamp_denies(self) -> bool:
        """一次性 stamp 机密文件读拒绝（同父目录的多个文件合并成一次调用）。

        只 stamp **单个文件**：ACE 带 ``(OI)(CI)`` 会同步向子树传播，
        stamp 目录等于把整个子树重写一遍 ACL（项目根 158s）。
        """
        targets: list[str] = []
        for name in _DEFAULT_DENY_READ:
            p = (self.workspace_root / name)
            if p.is_file():
                targets.append(str(p))
        for p in self.deny_read:
            rp = Path(p)
            if not rp.is_absolute():
                rp = self.workspace_root / rp
            if rp.is_file() and str(rp) not in targets:
                targets.append(str(rp))
        if not targets:
            return True
        rc, out, err = self._run_srt(
            ["acl", "stamp", "--holder-pid", str(self.holder_pid), "--sandbox-user-sid", self._sid],
            stdin=json.dumps({"denyRead": targets, "denyWrite": []}).encode(),
            timeout=600,
        )
        if rc != 0:
            self.last_error = f"`acl stamp` 失败：{err or out}"
            return False
        self._stamped = targets
        return True

    def _grant(self, read: list, write: list) -> bool:
        rc, out, err = self._run_srt(
            ["acl", "grant", "--holder-pid", str(self.holder_pid), "--sandbox-user-sid", self._sid],
            stdin=json.dumps({"read": read, "write": write}).encode(),
            timeout=600,
        )
        if rc != 0:
            logger.warning("`acl grant` 失败：%s", err or out)
            return False
        self._granted.extend(read)
        return True

    def _sandbox_env(self, env: dict) -> dict:
        """构造透传给 ``exec --env`` 的环境：过滤密钥 + 重定向 HOME/TMP + git 兼容。"""
        out = {}
        dropped = 0
        for k, v in env.items():
            if v is None:
                continue
            if _SECRET_NAME_RE.search(k) and k not in self.env_passthrough:
                dropped += 1
                continue
            out[str(k)] = str(v)
        if dropped:
            logger.debug("sandbox env：已过滤 %d 个疑似密钥变量", dropped)

        tmp = str(self._tmp_dir or (runtime_dir(self.workspace_root) / "tmp"))
        # 沙箱用户对 C:\Users\<真实用户> 无写权 → HOME/TMP 必须指到工作目录内
        for k in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR"):
            out[k] = tmp

        # 沙箱用户不是仓库属主 → git 会报 dubious ownership。
        # 用 GIT_CONFIG_* 环境注入 safe.directory，避免改宿主/用户的 gitconfig。
        try:
            n = int(out.get("GIT_CONFIG_COUNT", "0") or 0)
        except ValueError:
            n = 0
        out["GIT_CONFIG_COUNT"] = str(n + 1)
        out[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
        out[f"GIT_CONFIG_VALUE_{n}"] = "*"
        return out

    def _probe_argv(self) -> Optional[list]:
        return self._exec_argv("echo srt-probe-ok", self.workspace_root, {}, shell="cmd", raw=True)

    def _verify_exec(self) -> bool:
        """跑一次最小命令，确认两跳启动真的能起来（失败重试一次，防偶发）。"""
        argv = self._probe_argv()
        if argv is None:
            self.last_error = "无法构造沙箱命令（shell 不受支持）"
            return False
        last = ""
        for attempt in (1, 2):
            try:
                p = subprocess.run(argv, capture_output=True, timeout=90,
                                   cwd=str(self.workspace_root))
            except (OSError, subprocess.TimeoutExpired) as e:
                last = f"沙箱探测执行失败：{e}"
                continue
            if p.returncode == 0:
                return True
            err = (p.stderr or b"").decode("utf-8", errors="replace").strip()
            last = f"沙箱探测执行退出码 {p.returncode}：{err[:300]}"
        self.last_error = self._explain_probe_failure(last)
        return False

    def _explain_probe_failure(self, raw: str) -> str:
        """把 srt-win 的原始报错翻译成可执行的修复指引。

        实测：``mapped_drive_cwd`` 的文案（"``D:\\`` is DRIVE_REMOTE"）是**误导**的——
        真正的条件是「工作目录（或其祖先）的 ACL 没有向沙箱用户开放」，srt-win 会
        把它归类成「映射/网络驱动器」。最常见的触发源是 ``tempfile.mkdtemp`` /
        pytest ``tmp_path`` 建的目录：它们只授权 SYSTEM / Administrators /
        OWNER RIGHTS，没有 ``Authenticated Users``，因此沙箱用户无法穿越。
        对同一目录执行 ``icacls <dir> /reset /T`` 恢复继承后立刻可用（已实测）。
        """
        if "mapped_drive_cwd" in raw:
            return (
                f"沙箱无法以 {self.workspace_root} 作为工作目录：该目录（或其祖先）的 ACL "
                "没有向沙箱用户开放，srt-win 把它误报成「映射/网络驱动器」。"
                "常见于 tempfile.mkdtemp / pytest tmp_path 创建的目录。"
                f'修复：icacls "{self.workspace_root}" /reset /T'
            )
        if "exit code 15" in raw or "requires srt-win install" in raw.lower():
            return "沙箱账户未正确安装：请以管理员身份执行 `srt-win install`。"
        return raw

    def _verify_workspace_writable(self) -> None:
        """探测沙箱用户对工作目录的写权；不可写才付 grant 成本（本机默认无需）。"""
        probe = self._tmp_dir / "srt_write_probe.txt"
        argv = self._exec_argv(f'echo x > "{probe}"', self.workspace_root, {}, shell="cmd", raw=True)
        ok = False
        if argv is not None:
            try:
                p = subprocess.run(argv, capture_output=True, timeout=90, cwd=str(self.workspace_root))
                ok = p.returncode == 0 and probe.exists()
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        if ok:
            return
        logger.info("sandbox：工作目录对沙箱用户不可写，尝试一次性 acl grant …")
        roots = [str(self.workspace_root)] + [str(p) for p in self.grant_read]
        if self._grant(read=roots, write=[str(self.workspace_root)]):
            argv = self._exec_argv(f'echo x > "{probe}"', self.workspace_root, {}, shell="cmd", raw=True)
            if argv is not None:
                try:
                    p = subprocess.run(argv, capture_output=True, timeout=90, cwd=str(self.workspace_root))
                    ok = p.returncode == 0 and probe.exists()
                except (OSError, subprocess.TimeoutExpired):
                    ok = False
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        self.workspace_writable = bool(ok)
        if not ok:
            logger.warning(
                "sandbox：工作目录对沙箱用户仍不可写，沙箱内命令将只能读不能写"
            )

    # ------------------------------------------------------------ 命令包装

    def _shell_argv(self, shell: str) -> Optional[list]:
        """把引擎的 shell 名映射成沙箱内可执行的绝对可执行文件 + 参数前缀。"""
        if shell == "cmd":
            comspec = os.environ.get("COMSPEC", "").strip()
            exe = comspec if comspec and Path(comspec).is_file() else r"C:\Windows\System32\cmd.exe"
            # 裸 "cmd.exe" 在 srt-win 下会 path-not-found（不走宿主 PATH 解析）
            return [exe, "/c"]
        if shell == "bash":
            exe = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
            if not Path(exe).is_file():
                return None
            return [exe, "-c"]
        return None   # wsl / 其他形态不支持沙箱

    def _exec_argv(
        self,
        command: str,
        cwd: Path,
        env: dict,
        shell: str,
        raw: bool = False,
    ) -> Optional[list]:
        shell_argv = self._shell_argv(shell)
        if shell_argv is None:
            self.last_error = f"shell={shell!r} 不受沙箱支持（WSL 形态请改用 cmd/bash）"
            return None
        argv = [self._srt_bin, "exec", "--quiet"]
        for k, v in (env if raw else self._sandbox_env(env)).items():
            argv += ["--env", f"{k}={v}"]
        argv += shell_argv + [command]
        return argv

    def wrap(self, command: str, cwd: Path, env: dict, shell: str) -> Optional[list]:
        """把命令包装成沙箱 argv；不可用时返回 None 并置 ``last_error``。

        调用方必须把返回的 argv 用 ``Popen(..., cwd=cwd)`` 启动：``exec``
        不接受 cwd 参数，子进程继承 broker 进程的 cwd（实测）。
        """
        if not self.ensure_ready():
            return None
        argv = self._exec_argv(command, Path(cwd), env, shell)
        if argv is not None:
            self.sandboxed_calls += 1
        return argv

    # ------------------------------------------------------------ 收尾 / 报告

    def close(self) -> None:
        """release 本 holder 的 ACE（幂等；进程退出时由 atexit 兜底调用）。"""
        with self._lock:
            if self._state != "ready" or not self._srt_bin or not self._sid:
                return
            if self._stamped:
                rc, _, err = self._run_srt(
                    ["acl", "restore", "--holder-pid", str(self.holder_pid),
                     "--sandbox-user-sid", self._sid],
                    timeout=900,
                )
                if rc != 0:
                    logger.warning("`acl restore` 失败（ACE 会随本进程退出被回收）：%s", err)
                self._stamped = []
            if self._granted:
                rc, _, err = self._run_srt(
                    ["acl", "revoke", "--holder-pid", str(self.holder_pid),
                     "--sandbox-user-sid", self._sid],
                    timeout=900,
                )
                if rc != 0:
                    logger.warning("`acl revoke` 失败：%s", err)
                self._granted = []
            self._state = "closed"

    def capability_report(self) -> str:
        """人类可读的能力矩阵 + 本次会话实际配置（供 doctor / 会话开头展示）。"""
        lines = ["沙箱能力（本机实测，非推断）："]
        for name, verdict, note in CAPABILITY_MATRIX:
            lines.append(f"  - {name}：{verdict} —— {note}")
        if self._state == "ready":
            lines.append(
                f"本次会话：stamp {len(self._stamped)} 个机密文件，"
                f"准备耗时 {self.setup_ms}ms，已沙箱执行 {self.sandboxed_calls} 次"
            )
            if not self.workspace_writable:
                lines.append("  ! 工作目录对沙箱用户不可写：沙箱内命令只能读")
        elif self._state == "unavailable":
            lines.append(f"沙箱不可用：{self.last_error}")
        else:
            lines.append("沙箱尚未启用（本次会话还没有需要进沙箱的命令）")
        return "\n".join(lines)
