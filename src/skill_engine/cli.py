"""
CLI — 命令行接口

命令：
- list: 列出所有可用的 skills
- info <name>: 查看 skill 的详细信息
- match "<query>": 匹配 skills 到用户输入
- scan [--root DIR]: 扫描并显示发现的 skills
- clear-cache: 清空 skill 缓存
- run <query> [--llm]: 执行 skill
- install <url>: 安装 skill
- update <name>: 更新 skill
- uninstall <name>: 卸载 skill
- scan-security [name]: 安全扫描 skill
"""

import json
import logging
import os
import sys
from datetime import datetime
import typer
from typing import Optional

from .execution.paths import to_native_path, native_path_hint
from .execution.tracer import DebugTracer

# Fix Windows encoding for CLI output
if sys.platform == "win32":
    import locale
    locale.setlocale(locale.LC_ALL, "zh_CN.UTF-8")
    # 重写 stdout/stderr 为 UTF-8，解决中文乱码
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

app = typer.Typer(
    name="skill-engine",
    help="Skills Engine: 独立的 skills 解析和路由工具",
    add_completion=False,
)


def _resolve_debug_tracer(debug: bool, debug_log: Optional[str], working_root: Optional[str]):
    """解析 debug 落盘 tracer。优先级（高→低）：

    - --debug-log <path>                （显式命令行，最高优先）
    - SKILL_ENGINE_DEBUG_LOG 环境变量     （CI 注入，或 config.yml settings.debug_log 经 backfill 填入；setdefault 保证 CI 注入优先于配置文件）
    - --debug                           （命令行开关，用默认路径）
    - SKILL_ENGINE_DEBUG 环境变量        （config.yml settings.debug: true 经 backfill 填入，用默认路径）
    - 以上皆无 → 返回 enabled()=False 的 DebugTracer（全程 no-op，零开销）

    返回值始终是 DebugTracer 实例（支持 with 语句自动 close），调用方无需判空。
    """
    env_path = os.environ.get("SKILL_ENGINE_DEBUG_LOG")
    env_debug = os.environ.get("SKILL_ENGINE_DEBUG")
    want_debug = bool(debug) or str(env_debug).strip().lower() in ("1", "true", "yes", "on")
    path = debug_log or env_path
    if not path and want_debug:
        from pathlib import Path as _P
        wr = working_root or str(_P.cwd())
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(wr, f".{ts}.skill-engine-debug.log")
    else:
        # 相对路径的 debug_log 基于 working_root 解析，使其跟随 -w 指定的工作目录，
        # 而非引擎进程 cwd。绝对路径（如显式全路径或 CI 注入）保持不变。
        # 背景：config.yml 里常写 `debug_log: ./run.log`，若直接用 cwd 做 open() 基目录，
        # 日志会落在引擎启动目录而非 -w 目标目录，造成「指定了工作目录日志却不在那里」的困惑。
        if path and not os.path.isabs(path) and working_root:
            path = os.path.join(working_root, path)
    return DebugTracer(path)


@app.command()
def list(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
):
    """列出所有可用的 skills"""
    from .routing.discovery import discover_skills
    from .routing.registry import Registry

    index = discover_skills()
    registry = Registry(index)

    active = registry.list_active()
    print(f"\n共 {len(active)} 个可用 skills:\n")

    for name in active:
        meta = registry.info(name)
        if meta:
            print(f"  {name}")
            print(f"    描述: {meta.description}")
            print(f"    目录: {meta.directory}")
            if verbose:
                print(f"    优先级: {meta.priority}")
                print(f"    状态: {meta.state}")
                print()


@app.command()
def info(
    name: str = typer.Argument(..., help="skill 名称"),
):
    """查看 skill 的详细信息"""
    from .routing.discovery import discover_skills
    from .routing.registry import Registry

    index = discover_skills()
    registry = Registry(index)

    skill = registry.load_skill(name)
    if not skill:
        print(f"[ERROR] 未找到 skill: {name}")
        raise typer.Exit(code=1)

    print(f"\nSkill: {skill.metadata.name}")
    print(f"描述: {skill.metadata.description}")
    print(f"目录: {skill.directory}")
    print(f"触发条件: {skill.metadata.when_to_use}")
    print(f"允许工具: {skill.metadata.allowed_tools}")
    print(f"支持文件: {skill.supporting_files}")
    print(f"\nBody ({len(skill.body)} 字符):\n{skill.body[:500]}...")


@app.command()
def match(
    query: str = typer.Argument(..., help="用户输入/查询"),
    explain: bool = typer.Option(False, "--explain", "-e", help="显示每层匹配详情"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示路由详细日志"),
):
    """匹配 skills 到用户输入"""
    from .routing.discovery import discover_skills
    from .routing.registry import Registry
    from .routing.router import Router

    index = discover_skills()
    registry = Registry(index)
    if verbose:
        logging.getLogger("skill_engine.router").setLevel(logging.INFO)
        logging.basicConfig(format="%(message)s", level=logging.INFO)
    router = _create_router(registry, verbose=verbose)

    plan = router.match(query)

    print(f"\n匹配 '{query}' 的结果:\n")
    if plan.primary:
        print(f"  {plan.primary.name} (分数: {plan.score or 'N/A'}, 方法: {plan.method})")
        if plan.uncertain:
            print(f"  ⚠️ 匹配结果不确定")
    elif plan.selections:
        for i, s in enumerate(plan.selections, 1):
            print(f"  {i}. {s.name} (角色: {s.role or '默认'})")
        print(f"  模式: multi ({len(plan.selections)} 个 skill 协同)")
    else:
        print(f"  ❌ 未找到匹配的 skill")
        if plan.reason:
            print(f"  原因: {plan.reason}")

    if explain:
        print(f"\n  ── 匹配详情 ──")
        print(f"  模式: {plan.mode}")
        print(f"  方法: {plan.method}")
        if plan.score is not None:
            print(f"  分数: {plan.score:.4f}")
        if plan.uncertain:
            print(f"  不确定: True")
        if plan.reason:
            print(f"  说明: {plan.reason}")

        if plan.method == "exact":
            print(f"  精确命中: name/alias/shortcut 匹配")
        elif plan.method == "keyword":
            # 展示分词结果和 intention 命中情况
            try:
                from .routing.tokenize import tokenize_query
                qtokens = tokenize_query(query)
                if qtokens.get("verbs_zh"):
                    print(f"  中文动词: {', '.join(qtokens['verbs_zh'])}")
                if qtokens.get("nouns_zh"):
                    print(f"  中文名词: {', '.join(qtokens['nouns_zh'])}")
                if qtokens.get("nouns_en"):
                    print(f"  领域名词: {', '.join(qtokens['nouns_en'])}")
            except Exception:
                pass

            if plan.primary:
                meta = registry.load_meta(plan.primary.name)
                if meta and meta.meta_cache:
                    mc = meta.meta_cache
                    if "intention" in mc:
                        print(f"  命中 intention: {mc['intention']}")
                    if "purpose" in mc:
                        print(f"  目的: {mc['purpose']}")
        elif plan.method == "llm" and plan.reason:
            print(f"  LLM 判定: {plan.reason}")


@app.command()
def scan(
    root: Optional[str] = typer.Option(None, "--root", "-r", help="额外扫描根目录"),
):
    """扫描并显示发现的 skills"""
    from pathlib import Path
    from .routing.discovery import discover_skills

    index = discover_skills(extra_roots=[Path(root)] if root else None)
    print(f"\n发现 {len(index)} 个 skills:\n")
    for name, meta in sorted(index.items(), key=lambda x: (-x[1].priority, x[0])):
        print(f"  {name} (priority={meta.priority}, state={meta.state})")
        print(f"    {meta.description}")
        print(f"    {meta.directory}")
        print()


@app.command()
def clear_cache():
    """清空 skill 编译缓存"""
    from .routing.discovery import discover_skills
    from .routing.registry import Registry

    index = discover_skills()
    registry = Registry(index)
    registry.clear_cache()
    print("缓存已清空")


def _create_router(registry, verbose: bool = False):
    """创建 Router 实例，自动注册领域词到 jieba

    Args:
        registry: Registry 实例
        verbose: 是否启用 Router 详细日志
    """
    from .routing.router import Router
    from .routing.domain_words import register_domain_words
    register_domain_words(registry)
    return Router(registry, preprocessor=None, verbose=verbose)


def _get_llm_client(purpose: str = "cli-chat"):
    """获取 LLM 客户端（档位 A 用）"""
    try:
        from skill_engine.config import get_llm
    except ImportError:
        print("[ERROR] 无法导入 config 模块")
        raise typer.Exit(code=1)
    try:
        return get_llm(purpose=purpose)
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        raise typer.Exit(code=1)


def _normalize_working_root(working_root: Optional[str]) -> Optional[str]:
    """归一化并校验 -w/--working-root。

    Windows 用户常在 Git Bash 里敲 `-w /d/Code/proj`，这类 POSIX 路径本机
    Python 无法识别，会在每条 bash 命令上抛 [WinError 267]。这里提前转成
    原生路径；目录不存在则立刻退出，而不是让模型跑满迭代次数才失败。
    """
    if not working_root:
        return working_root
    native = to_native_path(working_root)
    if native is None or not native.is_dir():
        print(f"[ERROR] 工作目录不存在: {working_root}")
        print(f"        {native_path_hint(working_root)}")
        raise typer.Exit(code=1)
    resolved = str(native.resolve())
    if resolved != str(working_root):
        print(f"[INFO] 工作目录已归一化为原生路径: {resolved}")
    return resolved


def _get_tool_llm_client(purpose: str = "cli-tool"):
    """获取带工具绑定的 LLM 客户端（档位 B tool_dispatch 用）"""
    try:
        from skill_engine.config import get_llm
    except ImportError:
        print("[ERROR] 无法导入 config 模块")
        raise typer.Exit(code=1)
    try:
        llm = get_llm(purpose=purpose)
        return llm
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        raise typer.Exit(code=1)


@app.command()
def run(
    query_or_name: str = typer.Argument(..., help="skill 名称或用户输入"),
    llm: bool = typer.Option(False, "--llm", help="使用 LLM 单次调用（档位 A）"),
    tool_dispatch: bool = typer.Option(False, "--tool-dispatch", "-td", help="使用 tool_dispatch 循环（档位 B，CC 原生 skill 兼容）"),
    steps: bool = typer.Option(False, "--steps", help="使用 Steps DSL 确定性执行（自动检测 body 中的 ## Steps）"),
    max_iterations: int = typer.Option(30, "--max-iter", help="档位 B 最大迭代次数"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只编译不执行（输出 prompt）"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="非交互模式，ATTENTION→BLOCK"),
    working_root: Optional[str] = typer.Option(None, "--working-root", "-w", help="要修改的目标项目目录（默认引擎 cwd）"),
    state_path: Optional[str] = typer.Option(None, "--state-path", "-s", help="P2-3 运行状态落盘路径（支持断点续跑）"),
    resume_from: Optional[str] = typer.Option(None, "--resume-from", "-r", help="P2-3 从指定状态文件续跑"),
    args: str = typer.Option("", "--args", "-a", help="用户实际请求参数（当指定 skill name 时使用）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示引擎调试日志（迭代/历史条数/LLM 响应）"),
    debug: bool = typer.Option(False, "--debug", help="记录调试轨迹（上下文/状态栏/交互流程）到 JSONL 日志"),
    debug_log: Optional[str] = typer.Option(None, "--debug-log", help="调试日志路径；或设环境变量 SKILL_ENGINE_DEBUG_LOG（CI 优先）"),
):
    """执行 skill

    执行模式（优先级从高到低）：
    1. --steps: Steps DSL 确定性执行（自动检测 body 中的 ## Steps）
    2. --dry-run: 只编译，输出 prompt
    3. --tool-dispatch: 档位 B，tool_dispatch loop（CC 原生 skill）
    4. --llm: 档位 A，单次 LLM 调用
    5. 默认: 纯编译（pipe 模式）
    """
    from .routing.registry import Registry
    from .routing.router import Router
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .routing.discovery import discover_skills

    working_root = _normalize_working_root(working_root)
    tracer = _resolve_debug_tracer(debug, debug_log, working_root)

    # 1. 发现 + 注册（默认扫描 skills/ 目录，含内置/用户级/项目级）
    index = discover_skills(working_root=working_root)
    registry = Registry(index)

    # 2. 匹配
    router = _create_router(registry)

    # --args 模式：query_or_name 是 skill 名（精确匹配），args 是用户实际请求
    # 否则 query_or_name 是自然语言查询，走三步路由
    # 修复：此处曾有 `llm = _get_llm_client()`，把 --llm 布尔 flag 覆盖成客户端对象，
    # 导致 ① 不带 --llm 也强制走 LLM 分支；② 无 LLM 配置时纯编译模式也直接退出。
    if args:
        # 用 skill 名做匹配（精确命中 name exact 路由），用 args 做 $ARGUMENTS
        plan = router.match(query_or_name)
        match_query = args
    else:
        match_query = query_or_name
        plan = router.match(match_query)

    tracer.event("route", method=plan.method,
                 primary=(plan.primary.name if plan.primary else None),
                 score=plan.score, uncertain=plan.uncertain, reason=plan.reason)
    if not plan.primary and not plan.selections:
        print(f"[ERROR] 未找到匹配的 skill: {query_or_name}")
        if plan.reason:
            print(f"  原因: {plan.reason}")
        raise typer.Exit(code=1)

    if plan.uncertain:
        print(f"[WARN] 匹配结果不确定（方法: {plan.method}）")

    # 3. 编译 + 执行
    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose, tracer=tracer)

    if dry_run:
        # 只编译，不执行
        if plan.primary:
            skill = registry.load_skill(plan.primary.name)
            if skill:
                from .execution.tool_defs import parse_named_params
                _args = {"$ARGUMENTS": match_query, "$0": match_query, **parse_named_params(match_query)}
                prompt = assembler.assemble(skill, _args)
                print(f"\n{'='*60}")
                print(f"Skill: {skill.metadata.name}")
                print(f"分数: {plan.score or 1.0:.2f}")
                print(f"{'='*60}")
                print(prompt[:2000])
        tracer.close()
        return

    # v3: baseline run -td 埋 token（与 MOA 同款 CountingLLM 透明包装）。
    # 仅 tool_dispatch 分支（即 --tool-dispatch，基线对比走的路径）统计；
    # 计数值经 counter dict 传出，run_plan 结束后打印，供 code-agent-eval driver 解析。
    td_counter = None

    if tool_dispatch:
            td_llm = _get_tool_llm_client()
            if not td_llm:
                print("[ERROR] --tool-dispatch 需要 LLM 配置")
                print("  请设置环境变量: SKILL_ENGINE_LLM_MODEL, SKILL_ENGINE_LLM_BASE_URL, SKILL_ENGINE_LLM_API_KEY")
                print("  或去掉 --tool-dispatch 使用纯编译模式")
                raise typer.Exit(code=1)
            print(f"[INFO] 使用 tool_dispatch 模式 (档位 B), 最大迭代 {max_iterations} 次")
            from pathlib import Path as _Path
            from .execution.counting_llm import CountingLLM
            td_counter = {"calls": 0, "prompt": 0, "completion": 0, "total": 0}
            td_llm = CountingLLM(td_llm, td_counter)
            result = runner.run_plan(plan, registry, query=match_query, tool_dispatch=td_llm,
                                      max_iterations=max_iterations,
                                      working_root=working_root or str(_Path.cwd()),
                                      state_path=state_path, resume_from=resume_from,
                                      trusted_root=working_root)
    elif llm:
        llm_client = _get_llm_client()
        result = runner.run_plan(plan, registry, query=match_query, llm=llm_client,
                                 working_root=working_root, state_path=state_path, resume_from=resume_from,
                                 trusted_root=working_root)
    elif steps:
        print(f"[INFO] 使用 Steps DSL 确定性执行模式")
        result = runner.run_plan(plan, registry, query=match_query,
                                 working_root=working_root, state_path=state_path, resume_from=resume_from,
                                 trusted_root=working_root)
    else:
        result = runner.run_plan(plan, registry, query=match_query, trusted_root=working_root)

    # v3: baseline run -td 的 token 用量（与 MOA cli 同格式，driver 统一解析）
    if td_counter is not None:
        print(f"\n{'='*60}")
        print(f"  LLM 调用: {td_counter['calls']}  ·  "
              f"Token: {td_counter['total']} "
              f"(in={td_counter['prompt']}, out={td_counter['completion']})")

    # 输出结果（多 skill 时显示 all_outputs）
    print(f"\n{'='*60}")
    if "all_outputs" in result:
        print(f"多 skill 协同执行结果:")
        for i, r in enumerate(result["all_outputs"], 1):
            print(f"  [{i}] {r.get('skill_name', '?')}")
            if r.get('output'):
                print(f"      输出: {str(r['output'])[:200]}")
        print(f"{'='*60}")
    else:
        print(f"Skill: {result['skill_name']}")
        if 'iterations' in result:
            print(f"迭代: {result['iterations']} 次")
        print(f"{'='*60}")
        print(result.get("output", ""))
        if result.get("files_created"):
            print(f"\n创建的文件:")
            for f in result["files_created"]:
                print(f"  {f}")
    tracer.close()
    print()


def _get_llm_client(purpose: str = "cli-chat"):
    """获取 LLM 客户端（档位 A 用）

    通过 config.get_llm 获取模型实例，返回裸模型。
    """
    try:
        from skill_engine.config import get_llm
    except ImportError:
        print("[ERROR] 无法导入 config 模块")
        raise typer.Exit(code=1)

    try:
        return get_llm(purpose=purpose)
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        raise typer.Exit(code=1)


def _get_tool_llm_client(purpose: str = "cli-tool"):
    """获取带工具绑定的 LLM 客户端（档位 B tool_dispatch 用）

    通过 config.get_llm 获取裸模型，再用 bind_tools 绑定内建工具，
    使 LLM 能够返回 tool_calls。
    """
    try:
        from skill_engine.config import get_llm
    except ImportError:
        print("[ERROR] 无法导入 config 模块")
        raise typer.Exit(code=1)

    try:
        llm = get_llm(purpose=purpose)
        return llm
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        raise typer.Exit(code=1)


@app.command()
def install(
    source: str = typer.Argument(..., help="skill 来源：本地路径 / git URL / npx 包名"),
    target: str = typer.Option("~/.skills", "--target", "-t", help="安装目标目录"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 skill"),
):
    """安装 skill 到本地目录

    支持的来源：
    - 本地路径：skill-engine install ~/.claude/skills/my-skill
    - Git URL：skill-engine install https://github.com/user/skill.git
    - npx 包名：skill-engine install npx:@anthropic-ai/claude-code-skills
    """
    from pathlib import Path
    import shutil
    import subprocess
    import re

    target_dir = Path(target).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. npx 包名：先 npx 下载，再安装
    if source.startswith("npx:"):
        package_name = source[4:]  # 去掉 "npx:" 前缀
        print(f"[INFO] 通过 npx 下载 {package_name}...")
        result = subprocess.run(
            ["npx", "--yes", package_name],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"[ERROR] npx 下载失败: {result.stderr[:500]}")
            raise typer.Exit(code=1)
        # npx 通常会下载到 node_modules，尝试找到 SKILL.md
        # 常见路径：node_modules/<pkg>/skills/<skill-name>/
        npx_root = Path.cwd() / "node_modules"
        if npx_root.exists():
            for skill_dir in npx_root.rglob("SKILL.md"):
                parent = skill_dir.parent
                skill_name = parent.name
                dest = target_dir / skill_name
                if dest.exists() and not force:
                    print(f"[WARN] 已存在: {dest}（加 --force 覆盖）")
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(parent, dest)
                print(f"已安装: {skill_name} → {dest}")
        return

    # 2. 本地路径
    source_path = Path(source)
    if source_path.exists():
        # 如果传入的是目录，检查是否有 SKILL.md
        skill_dirs = []
        if (source_path / "SKILL.md").exists():
            skill_dirs = [source_path]
        else:
            # 递归查找所有包含 SKILL.md 的子目录
            for sk in source_path.rglob("SKILL.md"):
                skill_dirs.append(sk.parent)

        if not skill_dirs:
            print(f"[ERROR] 未在 {source} 中找到 SKILL.md")
            raise typer.Exit(code=1)

        for skill_dir in skill_dirs:
            skill_name = skill_dir.name
            dest = target_dir / skill_name
            if dest.exists() and not force:
                print(f"[WARN] 已存在: {dest}（跳过，加 --force 覆盖）")
                continue
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            print(f"已安装: {skill_name} → {dest}")
        return

    # 3. Git URL
    print(f"[INFO] 克隆 {source} → {target_dir}")
    skill_name = source.rstrip("/").split("/")[-1].replace(".git", "")
    dest = target_dir / skill_name
    result = subprocess.run(
        ["git", "clone", "--depth", "1", source, str(dest)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"[ERROR] 克隆失败: {result.stderr[:500]}")
        raise typer.Exit(code=1)
    print(f"已安装: {skill_name} → {dest}")


@app.command()
def update(
    name: str = typer.Argument(..., help="skill 名称"),
    target: str = typer.Option("~/.skills", "--target", "-t", help="目标目录"),
):
    """更新已安装的 skill"""
    from pathlib import Path
    import subprocess

    target_dir = Path(target).expanduser()
    skill_dir = target_dir / name

    if not skill_dir.exists():
        print(f"[ERROR] 未找到 skill: {name}")
        raise typer.Exit(code=1)

    result = subprocess.run(
        ["git", "pull"],
        cwd=str(skill_dir),
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        print(f"已更新: {name}")
    else:
        print(f"[WARN] 更新失败: {result.stderr}")
        print(f"提示: 该 skill 可能不是 git 仓库，手动删除后 reinstall")


@app.command()
def uninstall(
    name: str = typer.Argument(..., help="skill 名称"),
    target: str = typer.Option("~/.skills", "--target", "-t", help="目标目录"),
):
    """卸载已安装的 skill"""
    from pathlib import Path
    import shutil

    target_dir = Path(target).expanduser()
    skill_dir = target_dir / name

    if not skill_dir.exists():
        print(f"[ERROR] 未找到 skill: {name}")
        raise typer.Exit(code=1)

    shutil.rmtree(skill_dir)
    print(f"已卸载: {name}")


@app.command()
def index(
    build_meta: bool = typer.Option(False, "--build-meta", help="强制全量构建 .skill-meta.yaml（首次使用）"),
    rebuild_meta: bool = typer.Option(False, "--rebuild-meta", help="强制全量重抽 .skill-meta.yaml（SKILL.md 大改后）"),
):
    """扫描 skills 并预处理元数据

    默认增量模式：只有 SKILL.md 内容变更时才重新抽取 intention/synonyms。
    首次使用建议加 --build-meta 强制全量构建。

    使用示例：
    \b
      # 增量预处理
      skill-engine index

      # 首次：全量构建
      skill-engine index --build-meta

      # 强制重抽
      skill-engine index --rebuild-meta
    """
    from pathlib import Path
    from .routing.registry import Registry
    from .config import get_llm
    from .creator.preprocessor import Preprocessor
    from .routing.discovery import discover_skills

    # 1. 扫描
    print("[INFO] 正在扫描 skills...")
    index = discover_skills()
    registry = Registry(index)
    active = registry.list_active()

    if not active:
        print("[WARN] 未发现任何 skill")
        return

    print(f"[INFO] 发现 {len(active)} 个 skills")

    # 2. 获取 LLM
    print("[INFO] 获取 LLM 客户端...")
    try:
        llm = get_llm(purpose="cli-index")
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        print("  请设置环境变量: LLM_MODEL, LLM_BASE_URL, LLM_API_KEY")
        raise typer.Exit(code=1)

    # 3. 预处理
    preprocessor = Preprocessor(llm=llm)
    force = build_meta or rebuild_meta

    success = 0
    skipped = 0
    failed = 0

    print(f"[INFO] 开始{'全量' if force else '增量'}预处理...")
    for name in active:
        skill = registry.load_skill(name)
        if not skill:
            print(f"  [WARN] 跳过 {name}：无法加载")
            failed += 1
            continue

        skill_dir = Path(skill.directory)
        meta_path = skill_dir / ".skill-meta.yaml"

        if not force and meta_path.exists():
            # 增量模式：检查 hash
            from .creator.preprocessor import Preprocessor as P
            current_hash = P._hash_skill(skill)
            try:
                import yaml
                old = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
                if old.get("source_hash") == current_hash:
                    print(f"  [SKIP] {name}：未变更")
                    skipped += 1
                    continue
            except Exception:
                pass  # 文件损坏，重抽

        if rebuild_meta:
            # 强制重抽：删旧文件，不走 hash 缓存
            meta_path = skill_dir / ".skill-meta.yaml"
            if meta_path.exists():
                meta_path.unlink()

        if force:
            print(f"  [BUILD] {name}：正在抽取 intention...", end=" ", flush=True)
        else:
            print(f"  [META] {name}：正在抽取 intention...", end=" ", flush=True)

        try:
            meta = preprocessor.ensure_meta(skill)
            intention = meta.get("intention", [])
            purpose = meta.get("purpose", "")
            print(f"✅ intention={intention}, purpose={purpose}")
            success += 1
        except Exception as e:
            print(f"❌ {e}")
            failed += 1

    print(f"\n[INFO] 完成: {success} 成功, {skipped} 跳过, {failed} 失败")


@app.command()
def create(
    intent: str = typer.Argument(..., help="自然语言描述你想要创建的 skill"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="指定 skill 名称（覆盖 LLM 自动生成的名称）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只打印生成的 design 不写入文件"),
):
    """通过自然语言创建 Skill（Phase 11: LLM-Native）

    使用 LLM 生成完整的 skill 定义，包括 SKILL.md、scripts/、assets/。

    使用示例：
    \b
      # 基本用法
      skill-engine create "帮我写一个分析 Python 代码质量的 skill"

      # 指定名称覆盖 LLM 自动生成的
      skill-engine create "帮我写一个分析代码的 skill" --name code-analyzer

      # 调试：只打印 design 不写文件
      skill-engine create "帮我写一个分析代码的 skill" --dry-run
    """
    from .config import get_llm
    from .execution.runner import Runner
    from .execution.assembler import Assembler
    from .execution.executor import Executor

    print("[INFO] 获取 LLM 客户端...")
    llm = get_llm(purpose="cli-create")
    print("[INFO] LLM 客户端就绪")

    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor)
    runner = Runner(assembler, executor)

    print("[INFO] 正在调用 LLM 生成 skill 设计（可能需要 30-60 秒）...")
    import sys
    sys.stdout.flush()

    result = runner.create_skill(
        intent=intent,
        llm=llm,
        name=name,
        dry_run=dry_run,
    )

    if dry_run:
        print(json.dumps(result.get("design", {}), indent=2, ensure_ascii=False))
        return

    print(f"\n{'='*60}")
    print(f"  Skill 创建结果")
    print(f"{'='*60}")
    print(f"  名称: {result.get('name', 'N/A')}")
    print(f"  状态: {'✅ 成功' if result.get('valid') else '❌ 失败'}")
    print(f"  路径: {result.get('path', 'N/A')}")

    if result.get("errors"):
        print(f"  错误:")
        for err in result["errors"]:
            print(f"    - {err}")

    if not result.get("valid") and result.get("compile_result", {}).get("errors"):
        print(f"  编译错误:")
        for err in result["compile_result"]["errors"]:
            print(f"    - {err}")

    print(f"{'='*60}")

@app.command()
def scan_security(
    name: Optional[str] = typer.Argument(None, help="skill 名称（可选，不指定则扫全部）"),
    deep: bool = typer.Option(False, "--deep", help="使用 LLM 深度分析"),
    json_output: bool = typer.Option(False, "--json", help="JSON 格式输出"),
):
    """安全扫描 skill

    正则扫描所有 active skill，分析安全性。只提醒，不阻止。
    使用 --deep 可额外使用 LLM 深度分析。
    """
    from .routing.discovery import discover_skills
    from .routing.registry import Registry
    from .security.scanner import scan_skill, scan_skill_deep, scan_all

    index = discover_skills()
    registry = Registry(index)

    if name:
        skill = registry.load_skill(name)
        if not skill:
            print(f"[ERROR] 未找到 skill: {name}")
            raise typer.Exit(code=1)
        findings = scan_skill(skill)
        results = {name: findings}
    else:
        results = scan_all(registry)

    if json_output:
        import json as _json
        output = {}
        for n, findings in results.items():
            meta = registry.info(n)
            output[n] = {
                "findings": [f.to_dict() for f in findings],
                "trust_tag": meta.trust_tag if meta else None,
            }
        print(_json.dumps(output, indent=2, ensure_ascii=False))
        return

    print(f"\n{'='*60}")
    print(f"安全扫描结果")
    print(f"{'='*60}")
    for n, findings in results.items():
        meta = registry.info(n)
        trust = f" ({meta.trust_tag})" if meta and meta.trust_tag else ""
        print(f"\n  {n}{trust}")
        if not findings:
            print(f"    ✅ 未发现风险")
        for f in findings:
            print(f"    {f}")

    if deep:
        print(f"\n{'='*60}")
        print(f"LLM 深度分析")
        print(f"{'='*60}")
        from .config import get_llm
        llm = get_llm(purpose="cli-security")
        targets = [name] if name else registry.list_active()
        for n in targets:
            skill = registry.load_skill(n)
            if skill:
                print(f"\n  {n}:")
                print(f"    {scan_skill_deep(skill, llm)}")

    print()


@app.command()
def session(
    query_or_name: Optional[str] = typer.Argument(
        None, help="初始请求；配合 --skill 时可省略（进入会话后先看 skill 用法提示）"),
    skill: Optional[str] = typer.Option(None, "--skill", "-s", help="直接指定 skill 名称（跳过路由匹配）"),
    max_iterations: int = typer.Option(50, "--max-iter", help="每轮子任务最大迭代次数"),
    working_root: Optional[str] = typer.Option(None, "--working-root", "-w", help="要修改的目标项目目录（默认引擎 cwd）"),
    state_path: Optional[str] = typer.Option(None, "--state-path", help="会话状态落盘路径（每轮写入）"),
    resume_from: Optional[str] = typer.Option(None, "--resume-from", "-r", help="从指定会话状态文件续接"),
    model: Optional[str] = typer.Option(None, "--model", "-m",
                                        help="指定模型 profile（默认 default；可用 profile 见 moa --list-models）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示引擎调试日志（迭代/历史条数/LLM 响应）"),
    debug: bool = typer.Option(False, "--debug", help="记录调试轨迹（上下文/状态栏/交互流程）到 JSONL 日志"),
    debug_log: Optional[str] = typer.Option(None, "--debug-log", help="调试日志路径；或设环境变量 SKILL_ENGINE_DEBUG_LOG（CI 优先）"),
):
    """进入单 skill 持续会话（REPL 模式）

    单个 skill 像 Claude Code 那样多轮交互：完成一个子任务后保持会话，
    继续等待新指令，直到用户输入 /exit 或 /done 退出。

    初始请求可以省略（须配合 -s/--skill），此时进入会话后先展示该 skill 的
    用法提示（用途 / 适用场景 / 参数），再等待你的第一条指令：

        skill-engine session -s code-builder -w /path/to/project

    指定模型：-m <profile>（如 -m default / -m gpt4o），profile 列表见
    `skill-engine moa --list-models`；不指定则用默认 LLM 配置。
    """
    from pathlib import Path
    from .routing.registry import Registry
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .routing.discovery import discover_skills

    working_root = _normalize_working_root(working_root)
    tracer = _resolve_debug_tracer(debug, debug_log, working_root)

    index = discover_skills(working_root=working_root)
    registry = Registry(index)
    router = _create_router(registry)

    if skill:
        plan = router.match(skill)
        match_query = query_or_name or ""
    else:
        if not query_or_name:
            print("[ERROR] 未指定 skill：省略初始请求时必须用 -s/--skill 指定 skill")
            print("  例: skill-engine session -s code-builder -w /path/to/project")
            print("  或: skill-engine session \"给 utils.py 加一个 greet() 函数\"")
            raise typer.Exit(code=1)
        plan = router.match(query_or_name)
        match_query = query_or_name

    tracer.event("route", method=plan.method,
                 primary=(plan.primary.name if plan.primary else None),
                 score=plan.score, uncertain=plan.uncertain, reason=plan.reason)
    if not plan.primary and not plan.selections:
        print(f"[ERROR] 未找到匹配的 skill: {skill or query_or_name}")
        if plan.reason:
            print(f"  原因: {plan.reason}")
        raise typer.Exit(code=1)

    if plan.uncertain:
        print(f"[WARN] 匹配结果不确定（方法: {plan.method}）")

    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose, tracer=tracer)

    if model:
        from .config import get_llm_by_profile, list_model_profiles
        try:
            td_llm = get_llm_by_profile(model)
        except ValueError as e:
            print(f"[ERROR] {e}")
            print(f"  可选 profile: {', '.join(list_model_profiles().keys()) or '（无）'}")
            raise typer.Exit(code=1)
        print(f"[INFO] 使用模型 profile: {model}")
    else:
        td_llm = _get_tool_llm_client()
    if not td_llm:
        print("[ERROR] session 需要 LLM 配置（tool_dispatch 档位 B）")
        print("  请设置环境变量: LLM_MODEL, LLM_BASE_URL, LLM_API_KEY")
        raise typer.Exit(code=1)

    print(f"[INFO] 使用 tool_dispatch 模式, 每轮子任务最大迭代 {max_iterations} 次")
    from .config import get_security_mode
    if get_security_mode() == "strict":
        # 性能诊断建议 2：strict 下 LLM 侧 bash 一律 BLOCK，session 的 agent loop
        # 依赖 bash 的任务会立即快速失败——启动时就讲清楚，避免用户疑惑。
        print("[WARN] 当前安全模式为 strict：LLM 发起的 bash 命令不会自动执行"
              "（遇到即快速终止本轮）。")
        print("       需要 LLM 跑测试/构建等命令时，请设置环境变量"
              " SKILLS_ENGINE_SECURITY_MODE=permissive 后重试。")
    result = runner.run_repl(
        plan, registry, query=match_query, llm=td_llm,
        max_iterations=max_iterations,
        working_root=working_root or str(Path.cwd()),
        state_path=state_path, resume_from=resume_from,
        trusted_root=working_root,
    )

    stopped = result.get("stopped_by")
    if stopped in ("error", "no_match", "load_failed"):
        print(f"[session] 异常退出（{stopped}）: {result.get('output', '')}")
    tracer.close()


def _moa_menu(hio, title: str, options: list, allow_done: bool = False,
              done_label: str = "完成配置（进入指挥官）",
              keys: Optional[list] = None) -> object:
    """通用编号选择菜单。options: list of (value, label)。

    allow_done=True 时额外提供 `d` 选项，返回 None 作为"完成"哨兵。
    支持直接输入 value 原文（大小写不敏感）匹配，便于脚本/记忆。
    keys: 与 options 等长的快捷键列表（如 ["y","x","r","e"]），输入对应
    字母直接选中该选项——让 label 里标注的 (y)/(x)/(r)/(e) 真正生效。
    """
    while True:
        print(f"\n{title}")
        for i, (val, label) in enumerate(options, 1):
            print(f"  {i}. {label}")
        if allow_done:
            print(f"  d. {done_label}")
        choice = (hio.read(prompt="选择> ") or "").strip().lower()
        if allow_done and choice in ("d", "done", "完成"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0]
        if keys:
            for k, (val, _) in zip(keys, options):
                if str(k).lower() == choice:
                    return val
        for val, _ in options:
            if str(val).lower() == choice:
                return val
        print("  [无效选择，请重选]")


def _moa_pick_commander_skill(hio, active_skills: list) -> str:
    """指挥官 skill 只能从固定白名单 MOA_COMMANDER_SKILLS 中选择（问题 3）。

    白名单当前只有一个（moa-commander）→ 不弹菜单，直接使用并给出友好提示；
    未来扩展为多个后自动变为菜单选择；白名单不在 active_skills（off/不存在）
    时回退到「内置 / 任意 skill」菜单。
    """
    from .execution.moa import MOA_COMMANDER_SKILLS
    candidates = [n for n in MOA_COMMANDER_SKILLS if n in active_skills]
    if len(candidates) == 1:
        print(f"  [指挥官] 使用固定指挥官 skill: {candidates[0]}"
              f"（当前唯一，无需选择；后续可扩展）")
        return candidates[0]
    if len(candidates) > 1:
        return _moa_menu(hio, "[指挥官] 选择指挥官 skill（仅限固定白名单）：",
                         [(n, n) for n in candidates])
    print("  [WARN] 固定指挥官 skill 均不可用（未安装或 state=off），"
          "可选内置纯决策大脑或任意 skill。")
    return _moa_menu(hio, "[指挥官] 选择 skill：",
                     [("", "内置（无 skill，纯决策大脑）")] + [(n, n) for n in active_skills])


def _moa_read_instruction(hio, paste_dir: str, prompt: str) -> str:
    """读取一段自由文本指示，复用 session 的 :paste / :load 多行输入能力。"""
    from .execution.runner import _load_file_as_paste, _capture_paste
    raw = hio.read(prompt=prompt)
    if raw.startswith(":load "):
        loaded = _load_file_as_paste(raw[6:].strip(), paste_dir)
        if loaded.startswith("[session] :load 失败"):
            print(loaded)
            return ""
        return loaded
    if raw.strip() == ":paste":
        return _capture_paste(hio, paste_dir)
    return raw


def _moa_banner() -> None:
    print("\n" + "═" * 64)
    print("  MOA · Mixture of Agents — 多模型 / 多 skill 协作")
    print("═" * 64)
    print("  两种协作模式（配置方式不同，编排逻辑一致）：")
    print("    [模式 A] 多 skill 协作：A1/A2/A3 各挂不同 skill")
    print("             （如 VLM 审查 + 代码开发 + 测试），模型也可不同")
    print("    [模式 B] 多模型互审：A1/A2/A3 挂同一 skill、不同模型，")
    print("             互相讨论 / 监督 / 复核")
    print("  你将依次配置 worker（A1..A3）与指挥官（C），每个单元 =")
    print("  「一个模型 × 一个 skill × 一段任务指示」，并用代号引用。")
    print("═" * 64)


def _moa_summary_block(workers: list, commander, query: str) -> None:
    print("\n" + "─" * 64)
    print("  当前 MOA 配置：")
    print(f"  总任务: {query or '（未填）'}")
    for a in workers:
        print(f"   · {a.summary()}")
    print(f"   · {commander.summary()}")
    print("─" * 64)


def _moa_export_config(workers: list, commander, query: str,
                       max_rounds: int, max_agent_iterations: int, max_llm_calls: int,
                       out_dir: str, source: str = "wizard") -> str:
    """把当前 MOA 配置序列化为兼容 --plan 的 JSON 文件，返回写入路径。"""
    from datetime import datetime
    from pathlib import Path
    cfg = {
        "agents": [
            {"alias": a.alias, "model_profile": a.model_profile,
             "skill_name": a.skill_name, "instruction": a.instruction}
            for a in workers
        ],
        "commander": {"alias": commander.alias, "model_profile": commander.model_profile,
                      "skill_name": commander.skill_name,
                      "instruction": commander.instruction},
        "query": query or "",
        "options": {
            "max_rounds": max_rounds,
            "max_agent_iterations": max_agent_iterations,
            "max_llm_calls": max_llm_calls,
        },
        "meta": {"source": source,
                 "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }
    out = Path(out_dir) / f"moa_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


@app.command()
def moa(
    query: Optional[str] = typer.Argument(
        None, help="原始任务描述；交互模式下可留空，向导内再填"),
    plan: Optional[str] = typer.Option(None, "--plan", "-p", help="非交互：从 JSON 文件加载 MOA 配置（agents/commander/query/options）"),
    list_models: bool = typer.Option(False, "--list-models", "-L", help="仅列出当前可用的模型 profile 并退出"),
    max_rounds: int = typer.Option(20, "--max-rounds", help="指挥官决策轮数上限（防死循环闸 #1）"),
    max_iterations: int = typer.Option(60, "--max-iter", help="单个 worker 内层 tool_dispatch 迭代上限（闸 #2）"),
    max_llm_calls: int = typer.Option(500, "--max-llm-calls", help="全局 LLM 调用次数上限（闸 #3）"),
    working_root: Optional[str] = typer.Option(None, "--working-root", "-w", help="目标项目目录（默认引擎 cwd）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示引擎调试日志"),
    debug: bool = typer.Option(False, "--debug", help="记录调试轨迹（上下文/状态栏/交互流程）到 JSONL 日志"),
    debug_log: Optional[str] = typer.Option(None, "--debug-log", help="调试日志路径；或设环境变量 SKILL_ENGINE_DEBUG_LOG（CI 优先）"),
    state_path: Optional[str] = typer.Option(None, "--state-path", "-s", help="MOA 运行状态落盘路径（每轮检查点，支持断点续跑）"),
    resume_from: Optional[str] = typer.Option(None, "--resume-from", "-r", help="从指定状态文件续跑 MOA（崩溃恢复）；状态文件由上次运行生成于 --state-path 或默认工作目录 moa_session_state.json"),
):
    """进入 MOA 多模型 / 多 skill 协作（向导式引导配置 + 指挥官驱动执行）

    交互引导：先为每个 worker 选「模型 → skill → 任务指示」（代号 A1..A3），
    再为指挥官选「模型 → skill → 指示」（代号 C），确认后由指挥官逐轮指派
    下一个行动的 agent，直到它判定任务完成或触达防死循环上限。

    非交互：用 --plan <file.json> 直接加载配置（适合 CI / 脚本）。
    查看可配置的模型：--list-models。
    """
    from pathlib import Path
    from .routing.registry import Registry
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .execution.human_io import CliHumanIO
    from .execution.moa import MoaOrchestrator, MoaAgent
    from .config import list_model_profiles
    from .routing.discovery import discover_skills

    working_root = _normalize_working_root(working_root)
    tracer = _resolve_debug_tracer(debug, debug_log, working_root)

    index = discover_skills(working_root=working_root)
    registry = Registry(index)

    profiles = list_model_profiles()
    if not profiles:
        print("[ERROR] 未配置任何可用模型。请在 .env 设置 SKILL_ENGINE_LLM_MODEL "
              "（default）或 SKILL_ENGINE_MODELS 声明多个模型。")
        raise typer.Exit(code=1)

    if list_models:
        print("\n可用的模型 profile：")
        for name, cfg in profiles.items():
            vision = " · 支持视觉" if cfg.get("vision") else ""
            print(f"  · {name}: model={cfg['model']} provider={cfg['model_provider']} "
                  f"base_url={cfg['base_url'] or '默认'}{vision}")
        return

    # ── 非交互：从 JSON 加载 ──
    if plan:
        import json as _json
        try:
            cfg = _json.loads(Path(plan).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[ERROR] 无法读取 plan 文件: {e}")
            raise typer.Exit(code=1)
        if not cfg.get("agents"):
            print("[ERROR] plan 文件未配置任何 worker（agents 为空）")
            raise typer.Exit(code=1)
        if any(not a.get("model_profile") for a in cfg["agents"]):
            print("[ERROR] plan 文件存在缺少 model_profile 字段的 worker")
            raise typer.Exit(code=1)
        workers = [
            MoaAgent(alias=a.get("alias", f"A{i+1}"), model_profile=a["model_profile"],
                     skill_name=a.get("skill_name", ""), instruction=a.get("instruction", ""),
                     role="worker")
            for i, a in enumerate(cfg.get("agents", []))
        ]
        c = cfg.get("commander", {})
        if not c or not c.get("model_profile"):
            print("[ERROR] plan 文件缺少 commander 配置（需含 model_profile 字段）")
            raise typer.Exit(code=1)
        commander = MoaAgent(alias=c.get("alias", "C"), model_profile=c["model_profile"],
                             skill_name=c.get("skill_name", ""), instruction=c.get("instruction", ""),
                             role="commander")
        # plan.query 为空（未配置或显式 ""）时，回退到位置参数 query（driver 注入的 task.prompt）；
        # 不能用 cfg.get("query", query or "")，否则 plan 里写了 query:"" 会掩盖位置参数。
        q = cfg.get("query") or (query or "")
        opts = cfg.get("options", {})
        _moa_execute(registry, workers, commander, q, working_root,
                     max_rounds=opts.get("max_rounds", max_rounds),
                     max_agent_iterations=opts.get("max_agent_iterations", max_iterations),
                     max_llm_calls=opts.get("max_llm_calls", max_llm_calls),
                     verbose=verbose, export_after=True, export_source="plan",
                     trusted_root=working_root,
                     state_path=state_path, resume_from=resume_from,
                     tracer=tracer)
        return

    # ── 交互向导 ──
    hio = CliHumanIO(paste_dir=str(Path(working_root or Path.cwd()) / "pastes"))
    _moa_banner()

    active_skills = registry.list_active()
    if not active_skills:
        print("[WARN] 当前未发现任何 skill（cwd/skills 为空）。worker 仍可选「内置(无 skill)」做纯模型任务。")

    # 1) 先定总任务（问题 1：先任务后模型，避免配置完模型才想起没填任务）
    q = query or ""
    if not q.strip():
        q = _moa_read_instruction(
            hio, str(Path(working_root or Path.cwd()) / "pastes"),
            prompt="[总任务] 这次 MOA 要解决的原始任务是什么？\n你> ")
    print(f"  总任务已记录: {q[:60]}{'…' if len(q) > 60 else ''}")

    workers: list[MoaAgent] = []
    for i in range(1, 4):  # 最多 3 个 worker：A1, A2, A3
        alias = f"A{i}"
        _moa_summary_block(workers, MoaAgent(alias="C", model_profile="?", role="commander"), q)
        print(f"\n── 配置 Worker {alias} ──")
        # 1) 选模型
        model_opts = [(name, f"{name}  ({cfg['model']})"
                       + (" · 支持视觉" if cfg.get("vision") else ""))
                      for name, cfg in profiles.items()]
        model_profile = _moa_menu(hio, f"[Worker {alias}] 选择模型：", model_opts)
        # 2) 选 skill（含内置选项）
        skill_opts = [("", "内置（无 skill，按描述自动匹配 / 纯模型）")] + [(n, n) for n in active_skills]
        skill_name = _moa_menu(hio, f"[Worker {alias}] 选择 skill（留空=按指示自动匹配）：", skill_opts)
        # 3) 填指示
        instr = _moa_read_instruction(
            hio, str(Path(working_root or Path.cwd()) / "pastes"),
            prompt=f"[Worker {alias}] 该模型+skill 要做什么？（可 :paste 多行 / :load <文件>）\n你> ")
        if not instr.strip():
            print("  [指示为空，已跳过本 worker]")
            break
        workers.append(MoaAgent(alias=alias, model_profile=model_profile,
                                skill_name=skill_name or "", instruction=instr, role="worker"))
        # 完成配置？A1 之后允许进入指挥官
        if i < 3:
            cont = _moa_menu(hio, f"继续添加下一个 Worker（A{i+1}）？",
                             [("next", "继续添加"), ("done", "完成配置，进入指挥官")])
            if cont == "done":
                break

    if not workers:
        print("[ERROR] 未配置任何 worker，退出。")
        raise typer.Exit(code=1)

    # ── 指挥官 ──
    _moa_summary_block(workers, MoaAgent(alias="C", model_profile="?", role="commander"), q)
    print("\n── 配置指挥官（Commander） ──")
    c_model = _moa_menu(hio, "[指挥官] 选择模型：", model_opts)
    c_skill = _moa_pick_commander_skill(hio, active_skills)
    c_instr = _moa_read_instruction(
        hio, str(Path(working_root or Path.cwd()) / "pastes"),
        prompt="[指挥官] 它的指挥策略 / 终止条件是什么？（如：达到质量门禁即 STOP）\n你> ")
    commander = MoaAgent(alias="C", model_profile=c_model, skill_name=c_skill or "",
                         instruction=c_instr, role="commander")

    # ── 确认 ──
    while True:
        _moa_summary_block(workers, commander, q)
        print(f"\n  防死循环上限：{max_rounds} 轮 / {max_llm_calls} 次 LLM 调用 / "
              f"单 worker {max_iterations} 迭代")
        decision = _moa_menu(hio, "确认开始任务？",
                             [("start", "开始执行 (y)"), ("export", "仅导出配置 JSON (x)"),
                              ("reconfig", "重新配置 (r)"), ("exit", "退出 (e)")],
                             keys=["y", "x", "r", "e"])
        if decision == "exit":
            print("已退出。")
            return
        if decision == "export":
            path = _moa_export_config(
                workers, commander, q, max_rounds, max_iterations, max_llm_calls,
                working_root or str(Path.cwd()), source="wizard")
            print(f"[INFO] 配置已导出（未执行任何任务）: {path}")
            print(f'[INFO] 复用方式: skill-engine moa --plan "{path}"')
            return
        if decision == "reconfig":
            # 重新进入向导（递归会新建 tracer，先关闭外层避免句柄泄漏）
            tracer.close()
            return moa(query=q, working_root=working_root, max_rounds=max_rounds,
                       max_iterations=max_iterations, max_llm_calls=max_llm_calls,
                       verbose=verbose, debug=debug, debug_log=debug_log)
        if decision == "start":
            break

    _moa_execute(registry, workers, commander, q, working_root,
                 max_rounds=max_rounds, max_agent_iterations=max_iterations,
                 max_llm_calls=max_llm_calls, verbose=verbose,
                 export_after=True, export_source="wizard",
                 trusted_root=working_root,
                 state_path=state_path, resume_from=resume_from,
                 tracer=tracer)


def _moa_execute(registry, workers: list, commander, query: str,
                 working_root: Optional[str], max_rounds: int,
                 max_agent_iterations: int, max_llm_calls: int, verbose: bool,
                 export_after: bool = False, export_source: str = "executed",
                 trusted_root: Optional[str] = None,
                 state_path: Optional[str] = None,
                 resume_from: Optional[str] = None,
                 tracer=None) -> None:
    """构造运行环境并执行 MOA，打印最终报告；export_after 时导出配置 JSON。

    trusted_root：用户显式指定的受信任工作目录（-w 非空时启用）——其内的文件
    读写自动放行免审批；目录外操作维持原审批。未指定时为 None（不启用）。

    state_path / resume_from：Phase 3 崩溃续跑。state_path 为每轮检查点落盘
    路径（省略时引擎默认工作目录 moa_session_state.json）；resume_from 从指定
    状态文件载入断点继续整轮协作。
    """
    from pathlib import Path
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .execution.moa import MoaOrchestrator
    from .execution.human_io import CliHumanIO

    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose)

    # 执行期复用 CliHumanIO 语义通道（颜色/图标/截断），与 session 模式观感一致；
    # 交互已在向导阶段完成，这里仅用于输出（emit 系），不读输入。
    hio = CliHumanIO(paste_dir=str(Path(working_root or Path.cwd()) / "pastes"))
    hio.set_plain_text(True)

    orch = MoaOrchestrator(
        executor=executor, assembler=assembler,
        approval_fn=runner._check_approval,
        human_io=hio,
        working_root=working_root or str(Path.cwd()),
        plain_text=True, verbose=verbose,
        trusted_root=trusted_root,
        tracer=tracer,
    )
    if resume_from:
        print(f"[INFO] 续跑模式：从状态文件 {resume_from} 载入断点，继续整轮协作")
    elif state_path:
        print(f"[INFO] 检查点：{state_path}（每轮落盘；崩溃后可加 --resume-from 续跑）")
    from .config import get_security_mode
    if get_security_mode() == "strict":
        # 性能诊断建议 2：strict 下 worker 的 bash 工具调用会被 BLOCK 并快速
        # 失败（不再空转耗尽迭代），启动时先亮明，避免中途一头雾水。
        print("[WARN] 当前安全模式为 strict：worker 发起的 bash 命令不会自动执行"
              "（遇到即快速终止该 worker）。")
        print("       需要 LLM 跑测试/构建等命令时，请设置环境变量"
              " SKILLS_ENGINE_SECURITY_MODE=permissive 后重试。")
    result = orch.run(
        workers, commander, registry, query=query or "",
        max_rounds=max_rounds, max_agent_iterations=max_agent_iterations,
        max_llm_calls=max_llm_calls,
        resume_from=resume_from, state_path=state_path,
    )

    print("\n" + "═" * 64)
    print("  MOA 执行结果")
    print("═" * 64)
    print(f"  轮次: {result['rounds']}  ·  LLM 调用: {result['llm_calls']}  ·  "
          f"Token: {result.get('tokens_total', 0)} "
          f"(in={result.get('tokens_prompt', 0)}, out={result.get('tokens_completion', 0)})  ·  "
          f"停止原因: {result['stopped_by']}")
    if result.get("files_created"):
        print(f"  产出文件: {len(result['files_created'])} 个")
        for f in result["files_created"][:20]:
            print(f"    - {f}")
    print(f"\n{result.get('output', '')}")
    print()

    if export_after:
        path = _moa_export_config(
            workers, commander, query, max_rounds, max_agent_iterations,
            max_llm_calls, working_root or str(Path.cwd()), source=export_source)
        print(f"[INFO] 本次 MOA 配置已导出: {path}")
        print(f'[INFO] 复用方式: skill-engine moa --plan "{path}"')

    if tracer is not None:
        tracer.close()


def main() -> None:
    """程序入口。

    三种等价调用方式：
      1. skill-engine ...              （安装后的 console_scripts，见 pyproject [project.scripts]）
      2. python -m skill_engine ...    （走 __main__.py，免安装调试推荐）
      3. python -m skill_engine.cli ...（直接跑本模块）
    """
    app()


if __name__ == "__main__":
    main()