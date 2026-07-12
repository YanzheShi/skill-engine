"""
Gradio Web UI — skill-engine 的使用界面

提供四个面板：
1. Skill 列表：展示所有已安装的 skills（含分组信息）
2. Skill 匹配：输入查询，展示匹配的 skills（支持 keyword/name/llm 方法）
3. Skill 直接执行：输入查询，自动匹配并执行
4. Skill 手动执行：选择 skill 并执行（支持 args + 多种模式）
5. 管理：安装/卸载/更新 skills

依赖：gradio >= 6.0
"""

import gradio as gr
from pathlib import Path
from typing import Optional


def _get_project_skills_dir() -> Optional[Path]:
    """从模块文件路径推导项目根目录下的 skills/ 目录

    模块在 src/skill_engine/ui.py，项目根是 src/../.. 即 ui.py/../../..
    如果不存在，则回退到 CWD。
    """
    try:
        # ui.py 在 src/skill_engine/ui.py，项目根是 3 层上级
        mod_path = Path(__file__).resolve()
        project_root = mod_path.parent.parent.parent  # src/skill_engine -> src/ -> 项目根
        skills_dir = project_root / "skills"
        if skills_dir.is_dir():
            return skills_dir
    except NameError:
        pass  # __file__ 可能不存在（交互式环境）

    # 回退：CWD 下的 skills/
    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.is_dir():
        return cwd_skills

    return None


def _get_engine(roots: Optional[list[Path]] = None):
    """懒加载 engine 组件

    Args:
        roots: 额外的扫描根目录
    """
    from skill_engine.discovery import discover
    from skill_engine.registry import Registry
    from skill_engine.router import Router
    from skill_engine.executor import Executor
    from skill_engine.assembler import Assembler
    from skill_engine.runner import Runner

    if roots is None:
        skills_dir = _get_project_skills_dir()
        roots = [skills_dir] if skills_dir else []

    index = discover(roots=roots)
    registry = Registry(index)
    router = Router(registry)
    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor)
    return index, registry, router, executor, assembler, runner


def _get_llm_client():
    """获取 LLM 客户端（用于档位 A 和 LLM 匹配）"""
    try:
        from skill_engine.config import get_llm
        return get_llm()
    except Exception as e:
        return None


def list_skills(verbose: bool = False) -> str:
    """列出所有可用 skills（含分组信息）"""
    index, registry, *_ = _get_engine()
    active = registry.list_active()

    if not active:
        # 显示真实的扫描路径
        scanned = []
        scanned.append(f"  - {Path.home() / '.claude' / 'skills'}（Claude Code 标准）")
        scanned.append(f"  - {Path.home() / '.skill-engine' / 'skills'}（skill-engine）")
        scanned.append(f"  - {Path.cwd() / '.claude' / 'skills'}（Claude Code 标准）")
        scanned.append(f"  - {Path.cwd() / '.skill-engine' / 'skills'}（skill-engine）")
        mod_skills = _get_project_skills_dir()
        if mod_skills:
            scanned.append(f"  - {mod_skills}（模块推导）")
        cwd_skills = Path.cwd() / "skills"
        if cwd_skills != mod_skills:
            scanned.append(f"  - {cwd_skills}（CWD）")
        paths = "\n".join(scanned)
        return f"暂无可用 skills。\n\n扫描路径：\n{paths}\n\n提示：确保 skills/ 目录存在且包含 SKILL.md 的子目录。"

    lines = [f"## 共 {len(active)} 个可用 skills\n"]

    # 分组展示
    groups = registry.get_groups()
    for group_name, skill_names in sorted(groups.items()):
        display_name = "未分组" if group_name == "__ungrouped__" else group_name
        lines.append(f"### [{display_name}]")
        for name in sorted(skill_names):
            meta = registry.info(name)
            if meta:
                lines.append(f"  - **{name}**：{meta.description}")
        lines.append("")

    return "\n".join(lines)


def list_skills_for_dropdown() -> list[str]:
    """获取 skill 名称列表（供 dropdown 使用）"""
    index, registry, *_ = _get_engine()
    return registry.list_active()


def match_skills(query: str, top_k: int = 5, method: str = "keyword") -> str:
    """匹配 skills 到用户输入"""
    if not query or not query.strip():
        return "请输入查询内容。"

    _, _, router, *_ = _get_engine()
    results = router.match(query, method=method, top_k=top_k)

    if not results:
        return f"未找到与 '{query}' 匹配的 skills（方法: {method}）。"

    lines = [f"## 匹配 '{query}' 的结果（方法: {method}）\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r.skill.metadata.name}（得分: {r.score:.2f}）")
        lines.append(f"- 描述: {r.skill.metadata.description}")
        if r.skill.metadata.when_to_use:
            lines.append(f"- 触发: {r.skill.metadata.when_to_use}")
        groups = r.skill.metadata.groups
        if groups:
            lines.append(f"- 分组: {', '.join(groups)}")
        lines.append(f"- 方法: {r.method}")
        lines.append("")

    return "\n".join(lines)


def run_skill(skill_name: str, query: str, mode: str = "compile", progress: gr.Progress = gr.Progress()) -> str:
    """执行 skill（手动选择）

    Args:
        skill_name: skill 名称
        query: 用户输入/参数
        mode: 执行模式 — compile / llm / dry-run

    Returns:
        执行结果文本
    """
    if not skill_name:
        return "请选择一个 skill。"
    if not query or not query.strip():
        return "请输入查询内容。"

    progress(0.1, desc="正在匹配 skill...")
    index, registry, router, executor, assembler, runner = _get_engine()

    # 先精确匹配 name
    results = router.match(skill_name, method="name", top_k=1)
    if not results:
        results = router.match(skill_name, method="keyword", top_k=1)

    if not results:
        return f"未找到 skill: {skill_name}"

    match_result = results[0]
    # 设置用户请求参数
    match_result.arguments["$ARGUMENTS"] = query
    match_result.arguments["$0"] = query

    if mode == "dry-run":
        progress(0.5, desc="正在编译 Prompt...")
        final_prompt = assembler.assemble(match_result.skill, match_result.arguments)
        progress(1.0, desc="完成")
        return f"## 编译后的 Prompt（{skill_name}）\n\n```\n{final_prompt[:5000]}\n```\n"

    elif mode == "llm":
        progress(0.3, desc="正在初始化 LLM...")
        llm = _get_llm_client()
        if not llm:
            return "## 档位 A 需要 LLM 配置\n\n请设置环境变量:\n- AGNES_MODEL\n- AGNES_BASE_URL\n- AGNES_API_KEY"
        progress(0.5, desc="正在调用 LLM...")
        result = runner.run(match_result, llm=llm)
        progress(1.0, desc="完成")
        return f"## Skill: {result['skill_name']}\n\n**输出：**\n\n{result['output'][:5000]}"

    else:
        progress(0.3, desc="正在编译...")
        result = runner.run(match_result)
        output = result["output"][:5000]
        files = result.get("files_created", [])
        progress(1.0, desc="完成")
        text = f"## Skill: {result['skill_name']}\n\n```\n{output}\n```\n"
        if files:
            text += f"\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
        return text


def run_skill_direct(query: str, mode: str = "compile", method: str = "keyword", progress: gr.Progress = gr.Progress()) -> str:
    """直接输入查询执行 skill（自动匹配）

    Args:
        query: 用户输入
        mode: 执行模式 — compile / llm / dry-run
        method: 匹配方法 — keyword / name / llm

    Returns:
        执行结果文本
    """
    if not query or not query.strip():
        return "请输入查询内容。"

    progress(0.1, desc="正在匹配 skill...")
    index, registry, router, executor, assembler, runner = _get_engine()

    # 匹配策略：先 name 再 keyword，最后 llm
    results = router.match(query, method="name", top_k=1)
    if not results:
        results = router.match(query, method=method, top_k=1)
    if not results and method != "llm":
        results = router.match(query, method="llm", top_k=1)

    if not results:
        return f"未找到匹配的 skill。查询: {query}"

    match_result = results[0]
    match_result.arguments["$ARGUMENTS"] = query
    match_result.arguments["$0"] = query

    if mode == "dry-run":
        progress(0.5, desc="正在编译 Prompt...")
        final_prompt = assembler.assemble(match_result.skill, match_result.arguments)
        progress(1.0, desc="完成")
        return f"## 匹配: {match_result.skill.metadata.name}（得分: {match_result.score:.2f}）\n\n```\n{final_prompt[:5000]}\n```\n"

    elif mode == "llm":
        progress(0.3, desc="正在初始化 LLM...")
        llm = _get_llm_client()
        if not llm:
            return "## 档位 A 需要 LLM 配置\n\n请设置环境变量:\n- AGNES_MODEL\n- AGNES_BASE_URL\n- AGNES_API_KEY"
        progress(0.5, desc="正在调用 LLM...")
        result = runner.run(match_result, llm=llm)
        progress(1.0, desc="完成")
        return f"## Skill: {result['skill_name']}\n\n匹配得分: {match_result.score:.2f}\n\n**输出：**\n\n{result['output'][:5000]}"

    else:
        progress(0.3, desc="正在编译...")
        result = runner.run(match_result)
        output = result["output"][:5000]
        files = result.get("files_created", [])
        progress(1.0, desc="完成")
        text = f"## Skill: {result['skill_name']}\n\n匹配得分: {match_result.score:.2f}\n\n```\n{output}\n```\n"
        if files:
            text += f"\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
        return text


def _refresh_dropdown():
    """刷新 skill 下拉框选项"""
    skills = list_skills_for_dropdown()
    return gr.Dropdown(choices=skills, value=skills[0] if skills else None)


def install_skill(source: str, target: str = "~/.skills") -> str:
    """安装 skill"""
    if not source or not source.strip():
        return "请输入 skill 来源。"

    from pathlib import Path
    import shutil
    import subprocess

    target_dir = Path(target).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    # 本地路径
    source_path = Path(source)
    if source_path.exists():
        skill_dirs = []
        if (source_path / "SKILL.md").exists():
            skill_dirs = [source_path]
        else:
            for sk in source_path.rglob("SKILL.md"):
                skill_dirs.append(sk.parent)

        if not skill_dirs:
            return f"未在 {source} 中找到 SKILL.md"

        installed = []
        for skill_dir in skill_dirs:
            skill_name = skill_dir.name
            dest = target_dir / skill_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)
            installed.append(skill_name)
        return f"已安装: {', '.join(installed)} → {target_dir}"

    # Git URL
    if source.startswith(("http://", "https://", "git@")):
        skill_name = source.rstrip("/").split("/")[-1].replace(".git", "")
        dest = target_dir / skill_name
        result = subprocess.run(
            ["git", "clone", "--depth", "1", source, str(dest)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return f"克隆失败: {result.stderr[:500]}"
        return f"已安装: {skill_name} → {dest}"

    return f"不支持的来源: {source}"


def uninstall_skill(name: str, target: str = "~/.skills") -> str:
    """卸载 skill"""
    from pathlib import Path
    import shutil

    target_dir = Path(target).expanduser()
    skill_dir = target_dir / name

    if not skill_dir.exists():
        return f"未找到 skill: {name}"

    shutil.rmtree(skill_dir)
    return f"已卸载: {name}"


def create_demo() -> gr.Blocks:
    """创建 Gradio 演示应用"""
    with gr.Blocks(title="Skills Engine") as demo:
        gr.Markdown("# Skills Engine")
        gr.Markdown("独立的 skills 解析和路由工具")

        with gr.Tabs():
            # ============================================================
            # Tab 1: Skill 列表
            # ============================================================
            with gr.Tab("Skill 列表"):
                skill_list_md = gr.Markdown(value=list_skills())
                with gr.Row():
                    refresh_list_btn = gr.Button("刷新列表", variant="secondary")
                    verbose_check = gr.Checkbox(label="详细输出", value=False)
                refresh_list_btn.click(
                    list_skills,
                    inputs=[verbose_check],
                    outputs=[skill_list_md],
                )

            # ============================================================
            # Tab 2: Skill 匹配
            # ============================================================
            with gr.Tab("Skill 匹配"):
                match_query = gr.Textbox(
                    label="查询",
                    placeholder="输入关键词，如 '部署应用'",
                )
                with gr.Row():
                    match_top_k = gr.Slider(
                        minimum=1, maximum=10, value=5, step=1, label="返回数量",
                    )
                    match_method = gr.Radio(
                        choices=["keyword", "name", "llm"],
                        value="keyword",
                        label="匹配方法",
                    )
                match_btn = gr.Button("匹配", variant="primary")
                match_result_md = gr.Markdown()
                match_btn.click(
                    match_skills,
                    inputs=[match_query, match_top_k, match_method],
                    outputs=[match_result_md],
                )

            # ============================================================
            # Tab 3: 直接执行（自动匹配）
            # ============================================================
            with gr.Tab("直接执行"):
                exec_query = gr.Textbox(
                    label="输入查询",
                    placeholder="如 '帮我生成第49题的题解'",
                )
                with gr.Row():
                    exec_mode = gr.Radio(
                        choices=["compile", "llm", "dry-run"],
                        value="compile",
                        label="执行模式",
                    )
                    exec_method = gr.Radio(
                        choices=["keyword", "name", "llm"],
                        value="keyword",
                        label="匹配方法",
                    )
                exec_btn = gr.Button("执行", variant="primary")
                exec_result_md = gr.Markdown()
                exec_btn.click(
                    run_skill_direct,
                    inputs=[exec_query, exec_mode, exec_method],
                    outputs=[exec_result_md],
                )

            # ============================================================
            # Tab 4: 手动选择 skill 执行
            # ============================================================
            with gr.Tab("手动执行"):
                # 初始加载时获取 skills 列表
                initial_skills = list_skills_for_dropdown()

                skill_select = gr.Dropdown(
                    choices=initial_skills,
                    label="选择 Skill",
                    value=initial_skills[0] if initial_skills else None,
                    interactive=True,
                    allow_custom_value=True,
                )
                with gr.Row():
                    refresh_dropdown_btn = gr.Button("刷新列表", size="sm")
                    refresh_dropdown_btn.click(
                        fn=_refresh_dropdown,
                        outputs=[skill_select],
                    )

                manual_query = gr.Textbox(
                    label="查询/参数",
                    placeholder="输入参数（如 '49'）或查询文本",
                )
                manual_mode = gr.Radio(
                    choices=["compile", "llm", "dry-run"],
                    value="compile",
                    label="执行模式",
                )
                manual_btn = gr.Button("执行", variant="primary")
                manual_result_md = gr.Markdown()
                manual_btn.click(
                    run_skill,
                    inputs=[skill_select, manual_query, manual_mode],
                    outputs=[manual_result_md],
                )

            # ============================================================
            # Tab 5: 管理（安装/卸载/更新）
            # ============================================================
            with gr.Tab("管理"):
                gr.Markdown("### 安装 Skill")
                with gr.Row():
                    install_source = gr.Textbox(
                        label="来源",
                        placeholder="本地路径 / Git URL",
                        scale=3,
                    )
                    install_target = gr.Textbox(
                        label="目标目录",
                        value="~/.skills",
                        scale=1,
                    )
                install_btn = gr.Button("安装", variant="primary")
                install_result = gr.Markdown()
                install_btn.click(
                    install_skill,
                    inputs=[install_source, install_target],
                    outputs=[install_result],
                )

                gr.Markdown("---")
                gr.Markdown("### 卸载 Skill")
                with gr.Row():
                    uninstall_name = gr.Textbox(
                        label="Skill 名称",
                        placeholder="要卸载的 skill 名称",
                        scale=3,
                    )
                    uninstall_target = gr.Textbox(
                        label="目标目录",
                        value="~/.skills",
                        scale=1,
                    )
                uninstall_btn = gr.Button("卸载", variant="stop")
                uninstall_result = gr.Markdown()
                uninstall_btn.click(
                    uninstall_skill,
                    inputs=[uninstall_name, uninstall_target],
                    outputs=[uninstall_result],
                )

    return demo


if __name__ == "__main__":
    demo = create_demo()
    demo.launch(server_name="127.0.0.1", server_port=7860, theme=gr.themes.Soft())