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
"""

import json
import sys
import typer
from typing import Optional

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
    from .discovery import discover
    from .registry import Registry

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
    from .discovery import discover
    from .registry import Registry

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
):
    """匹配 skills 到用户输入"""
    from pathlib import Path
    from .discovery import discover
    from .registry import Registry
    from .router import Router

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
    registry = Registry(index)
    router = _create_router(registry)

    llm = _get_llm_client()
    plan = router.match(query, llm=llm)

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
                from .tokenize import tokenize_query
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
    from .discovery import discover

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
    from .registry import Registry
    from .discovery import discover

    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
    registry = Registry(index)
    registry.clear_cache()
    print("缓存已清空")


def _create_router(registry):
    """创建 Router 实例，可选挂载 Preprocessor 实现 lazy 预处理

    Preprocessor 只在有 LLM 配置时创建，不影响 Router 正常运行。
    使用 lazy 创建：首次需要时才会调 get_llm()。
    """
    from .router import Router
    return Router(registry, preprocessor=None)


@app.command()
def run(
    query_or_name: str = typer.Argument(..., help="skill 名称或用户输入"),
    llm: bool = typer.Option(False, "--llm", help="使用 LLM 单次调用（档位 A）"),
    tool_dispatch: bool = typer.Option(False, "--tool-dispatch", "-td", help="使用 tool_dispatch 循环（档位 B，CC 原生 skill 兼容）"),
    steps: bool = typer.Option(False, "--steps", help="使用 Steps DSL 确定性执行（自动检测 body 中的 ## Steps）"),
    max_iterations: int = typer.Option(10, "--max-iter", help="档位 B 最大迭代次数"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只编译不执行（输出 prompt）"),
    args: str = typer.Option("", "--args", "-a", help="用户实际请求参数（当指定 skill name 时使用）"),
):
    """执行 skill

    执行模式（优先级从高到低）：
    1. --steps: Steps DSL 确定性执行（自动检测 body 中的 ## Steps）
    2. --dry-run: 只编译，输出 prompt
    3. --tool-dispatch: 档位 B，tool_dispatch loop（CC 原生 skill）
    4. --llm: 档位 A，单次 LLM 调用
    5. 默认: 纯编译（pipe 模式）
    """
    from .discovery import discover
    from .registry import Registry
    from .router import Router
    from .assembler import Assembler
    from .executor import Executor
    from .runner import Runner

    # 1. 发现 + 注册（默认扫描 skills/ 目录）
    from pathlib import Path
    project_skills = Path.cwd() / "skills"
    roots = [str(project_skills)] if project_skills.exists() else []
    index = discover(roots=roots)
    registry = Registry(index)

    # 2. 匹配
    router = _create_router(registry)

    # --args 模式：query_or_name 是 skill 名，args 是用户请求
    # 否则 query_or_name 是自然语言查询，走三步路由
    match_query = args if args else query_or_name
    llm = _get_llm_client()
    plan = router.match(match_query, llm=llm)

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
    runner = Runner(assembler, executor)

    if dry_run:
        # 只编译，不执行
        if plan.primary:
            skill = registry.load_skill(plan.primary.name)
            if skill:
                prompt = assembler.assemble(skill, {"$ARGUMENTS": match_query, "$0": match_query})
                print(f"\n{'='*60}")
                print(f"Skill: {skill.metadata.name}")
                print(f"分数: {plan.score or 1.0:.2f}")
                print(f"{'='*60}")
                print(prompt)
            else:
                print(f"[ERROR] 无法加载 skill: {plan.primary.name}")
        elif plan.selections:
            for s in plan.selections:
                skill = registry.load_skill(s.name)
                if skill:
                    prompt = assembler.assemble(skill, {"$ARGUMENTS": match_query, "$0": match_query})
                    print(f"\n{'='*60}")
                    print(f"Skill: {skill.metadata.name}")
                    print(f"{'='*60}")
                    print(prompt[:2000])
        return

    if tool_dispatch:
        td_llm = _get_tool_llm_client()
        if not td_llm:
            print("[ERROR] --tool-dispatch 需要 LLM 配置")
            print("  请设置环境变量: AGNES_MODEL, AGNES_BASE_URL, AGNES_API_KEY")
            print("  或去掉 --tool-dispatch 使用纯编译模式")
            raise typer.Exit(code=1)
        print(f"[INFO] 使用 tool_dispatch 模式 (档位 B), 最大迭代 {max_iterations} 次")
        result = runner.run_plan(plan, registry, query=match_query, tool_dispatch=td_llm, max_iterations=max_iterations)
    elif llm:
        llm_client = _get_llm_client()
        result = runner.run_plan(plan, registry, query=match_query, llm=llm_client)
    elif steps:
        print(f"[INFO] 使用 Steps DSL 确定性执行模式")
        result = runner.run_plan(plan, registry, query=match_query)
    else:
        result = runner.run_plan(plan, registry, query=match_query)

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
        print(f"分数: {result.get('score', 0):.2f}")
        if 'steps' in result and result['steps']:
            print(f"步骤:")
            for s in result['steps']:
                status = "OK" if s.get("exit_code", 0) == 0 or s.get("type") in ("llm", "write") else f"ERR(exit={s.get('exit_code')})"
                print(f"  - {s.get('name')} ({s.get('type')}): {status}")
                if 'output' in s and s['output']:
                    out = str(s['output'])[:200]
                    print(f"    输出: {out}")
                if 'error' in s and s['error']:
                    print(f"    错误: {str(s['error'])[:200]}")
        if 'iterations' in result:
            print(f"迭代: {result['iterations']} 次")
        print(f"{'='*60}")
        print(result.get("output", ""))
        if result.get("files_created"):
            print(f"\n创建的文件:")
            for f in result["files_created"]:
                print(f"  {f}")
        print()


def _get_llm_client():
    """获取 LLM 客户端（档位 A 用）

    通过 config.get_llm 从环境变量读取 AGNES_* 配置，返回裸模型。
    """
    try:
        from skill_engine.config import get_llm
    except ImportError:
        print("[ERROR] 无法导入 config 模块")
        raise typer.Exit(code=1)

    try:
        return get_llm()
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        raise typer.Exit(code=1)


def _get_tool_llm_client():
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
        llm = get_llm()
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
    from .discovery import discover
    from .registry import Registry
    from .config import get_llm
    from .preprocessor import Preprocessor

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
        llm = get_llm()
    except Exception as e:
        print(f"[ERROR] 获取 LLM 配置失败: {e}")
        print("  请设置环境变量: AGNES_MODEL, AGNES_BASE_URL, AGNES_API_KEY")
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
            from .preprocessor import Preprocessor as P
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
    from .runner import Runner
    from .assembler import Assembler
    from .executor import Executor

    print("[INFO] 获取 LLM 客户端...")
    llm = get_llm()
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
