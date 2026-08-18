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
import sys
import typer
from typing import Optional

from .execution.paths import to_native_path, native_path_hint

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


@app.command()
def list(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
):
    """列出所有可用的 skills"""
    from pathlib import Path
    from .routing.discovery import discover
    from .routing.registry import Registry

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    from pathlib import Path
    from .routing.discovery import discover
    from .routing.registry import Registry

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    from pathlib import Path
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .routing.router import Router

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    from .routing.discovery import discover

    roots = []
    if root:
        roots.append(Path(root))

    index = discover(roots=roots)
    print(f"\n发现 {len(index)} 个 skills:\n")
    for name, meta in sorted(index.items(), key=lambda x: (-x[1].priority, x[0])):
        print(f"  {name} (priority={meta.priority}, state={meta.state})")
        print(f"    {meta.description}")
        print(f"    {meta.directory}")
        print()


@app.command()
def clear_cache():
    """清空 skill 编译缓存"""
    from pathlib import Path
    from .routing.registry import Registry
    from .routing.discovery import discover

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
):
    """执行 skill

    执行模式（优先级从高到低）：
    1. --steps: Steps DSL 确定性执行（自动检测 body 中的 ## Steps）
    2. --dry-run: 只编译，输出 prompt
    3. --tool-dispatch: 档位 B，tool_dispatch loop（CC 原生 skill）
    4. --llm: 档位 A，单次 LLM 调用
    5. 默认: 纯编译（pipe 模式）
    """
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .routing.router import Router
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner

    working_root = _normalize_working_root(working_root)

    # 1. 发现 + 注册（默认扫描 skills/ 目录）
    from pathlib import Path
    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose)

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
                                      state_path=state_path, resume_from=resume_from)
    elif llm:
        llm_client = _get_llm_client()
        result = runner.run_plan(plan, registry, query=match_query, llm=llm_client,
                                 working_root=working_root, state_path=state_path, resume_from=resume_from)
    elif steps:
        print(f"[INFO] 使用 Steps DSL 确定性执行模式")
        result = runner.run_plan(plan, registry, query=match_query,
                                 working_root=working_root, state_path=state_path, resume_from=resume_from)
    else:
        result = runner.run_plan(plan, registry, query=match_query)

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
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .config import get_llm
    from .creator.preprocessor import Preprocessor

    # 1. 扫描
    print("[INFO] 正在扫描 skills...")
    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    from pathlib import Path as _Path
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .security.scanner import scan_skill, scan_skill_deep, scan_all

    project_skills = _Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示引擎调试日志（迭代/历史条数/LLM 响应）"),
):
    """进入单 skill 持续会话（REPL 模式）

    单个 skill 像 Claude Code 那样多轮交互：完成一个子任务后保持会话，
    继续等待新指令，直到用户输入 /exit 或 /done 退出。

    初始请求可以省略（须配合 -s/--skill），此时进入会话后先展示该 skill 的
    用法提示（用途 / 适用场景 / 参数），再等待你的第一条指令：

        skill-engine session -s code-builder -w /path/to/project
    """
    from pathlib import Path
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner

    working_root = _normalize_working_root(working_root)

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
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

    if not plan.primary and not plan.selections:
        print(f"[ERROR] 未找到匹配的 skill: {skill or query_or_name}")
        if plan.reason:
            print(f"  原因: {plan.reason}")
        raise typer.Exit(code=1)

    if plan.uncertain:
        print(f"[WARN] 匹配结果不确定（方法: {plan.method}）")

    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose)

    td_llm = _get_tool_llm_client()
    if not td_llm:
        print("[ERROR] session 需要 LLM 配置（tool_dispatch 档位 B）")
        print("  请设置环境变量: LLM_MODEL, LLM_BASE_URL, LLM_API_KEY")
        raise typer.Exit(code=1)

    print(f"[INFO] 使用 tool_dispatch 模式, 每轮子任务最大迭代 {max_iterations} 次")
    result = runner.run_repl(
        plan, registry, query=match_query, llm=td_llm,
        max_iterations=max_iterations,
        working_root=working_root or str(Path.cwd()),
        state_path=state_path, resume_from=resume_from,
    )

    stopped = result.get("stopped_by")
    if stopped in ("error", "no_match", "load_failed"):
        print(f"[session] 异常退出（{stopped}）: {result.get('output', '')}")


def _moa_menu(hio, title: str, options: list, allow_done: bool = False,
              done_label: str = "完成配置（进入指挥官）") -> object:
    """通用编号选择菜单。options: list of (value, label)。

    allow_done=True 时额外提供 `d` 选项，返回 None 作为"完成"哨兵。
    支持直接输入 value 原文（大小写不敏感）匹配，便于脚本/记忆。
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
        for val, _ in options:
            if str(val).lower() == choice:
                return val
        print("  [无效选择，请重选]")


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
):
    """进入 MOA 多模型 / 多 skill 协作（向导式引导配置 + 指挥官驱动执行）

    交互引导：先为每个 worker 选「模型 → skill → 任务指示」（代号 A1..A3），
    再为指挥官选「模型 → skill → 指示」（代号 C），确认后由指挥官逐轮指派
    下一个行动的 agent，直到它判定任务完成或触达防死循环上限。

    非交互：用 --plan <file.json> 直接加载配置（适合 CI / 脚本）。
    查看可配置的模型：--list-models。
    """
    from pathlib import Path
    from .routing.discovery import discover
    from .routing.registry import Registry
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .execution.human_io import CliHumanIO
    from .execution.moa import MoaOrchestrator, MoaAgent
    from .config import list_model_profiles

    working_root = _normalize_working_root(working_root)

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
    registry = Registry(index)

    profiles = list_model_profiles()
    if not profiles:
        print("[ERROR] 未配置任何可用模型。请在 .env 设置 SKILL_ENGINE_LLM_MODEL "
              "（default）或 SKILL_ENGINE_MODELS 声明多个模型。")
        raise typer.Exit(code=1)

    if list_models:
        print("\n可用的模型 profile：")
        for name, cfg in profiles.items():
            print(f"  · {name}: model={cfg['model']} provider={cfg['model_provider']} "
                  f"base_url={cfg['base_url'] or '默认'}")
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
                     verbose=verbose)
        return

    # ── 交互向导 ──
    hio = CliHumanIO(paste_dir=str(Path(working_root or Path.cwd()) / "pastes"))
    _moa_banner()

    active_skills = registry.list_active()
    if not active_skills:
        print("[WARN] 当前未发现任何 skill（cwd/skills 为空）。worker 仍可选「内置(无 skill)」做纯模型任务。")

    workers: list[MoaAgent] = []
    q = query or ""
    for i in range(1, 4):  # 最多 3 个 worker：A1, A2, A3
        alias = f"A{i}"
        _moa_summary_block(workers, MoaAgent(alias="C", model_profile="?", role="commander"), q)
        print(f"\n── 配置 Worker {alias} ──")
        # 1) 选模型
        model_opts = [(name, f"{name}  ({cfg['model']})") for name, cfg in profiles.items()]
        model_profile = _moa_menu(hio, f"[Worker {alias}] 选择模型：", model_opts)
        # 2) 选 skill（含内置选项）
        skill_opts = [("", "内置（无 skill，纯模型任务）")] + [(n, n) for n in active_skills]
        skill_name = _moa_menu(hio, f"[Worker {alias}] 选择 skill：", skill_opts)
        # 3) 填指示
        instr = _moa_read_instruction(
            hio, str(Path(working_root or Path.cwd()) / "pastes"),
            prompt=f"[Worker {alias}] 该模型+skill 要做什么？（可 :paste 多行 / :load <文件>）\n你> ")
        if not instr.strip():
            print("  [指示为空，已跳过本 worker]")
            break
        if i == 1 and not q.strip():
            # 首个 worker 时顺带问总任务（避免额外一步）
            q = _moa_read_instruction(
                hio, str(Path(working_root or Path.cwd()) / "pastes"),
                prompt="[总任务] 这次 MOA 要解决的原始任务是什么？\n你> ")
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
    c_skill = _moa_menu(hio, "[指挥官] 选择 skill（推荐 moa-commander；也可选内置）：",
                        [("", "内置（无 skill，纯决策大脑）")] + [(n, n) for n in active_skills])
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
                             [("start", "开始执行 (y)"), ("reconfig", "重新配置 (r)"),
                              ("exit", "退出 (e)")])
        if decision == "exit":
            print("已退出。")
            return
        if decision == "reconfig":
            # 重新进入向导
            return moa(query=q, working_root=working_root, max_rounds=max_rounds,
                       max_iterations=max_iterations, max_llm_calls=max_llm_calls,
                       verbose=verbose)
        if decision == "start":
            break

    _moa_execute(registry, workers, commander, q, working_root,
                 max_rounds=max_rounds, max_agent_iterations=max_iterations,
                 max_llm_calls=max_llm_calls, verbose=verbose)


def _moa_execute(registry, workers: list, commander, query: str,
                 working_root: Optional[str], max_rounds: int,
                 max_agent_iterations: int, max_llm_calls: int, verbose: bool) -> None:
    """构造运行环境并执行 MOA，打印最终报告。"""
    from pathlib import Path
    from .execution.assembler import Assembler
    from .execution.executor import Executor
    from .execution.runner import Runner
    from .execution.moa import MoaOrchestrator

    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor, plain_text=True, verbose=verbose)

    orch = MoaOrchestrator(
        executor=executor, assembler=assembler,
        approval_fn=runner._check_approval,
        human_io=None,   # 执行期进度已由 orchestrator 经 print/emit 输出；交互在向导阶段完成
        working_root=working_root or str(Path.cwd()),
        plain_text=True, verbose=verbose,
    )
    result = orch.run(
        workers, commander, registry, query=query or "",
        max_rounds=max_rounds, max_agent_iterations=max_agent_iterations,
        max_llm_calls=max_llm_calls,
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