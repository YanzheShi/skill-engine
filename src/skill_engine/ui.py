"""
Gradio Web UI — skill-engine 的使用界面

提供四个面板：
1. Skill 列表：展示所有已安装的 skills（含分组信息）
2. Skill 匹配：输入查询，展示匹配的 skills（支持 explain 详情）
3. Skill 直接执行：输入查询，自动匹配并执行
4. Skill 手动执行：选择 skill 并执行（支持 args + 多种模式）
5. 管理：安装/卸载/更新 skills
6. Trace：路由匹配记录

依赖：gradio >= 6.0
"""

import gradio as gr
from pathlib import Path
from typing import Optional


def _get_project_skills_dir() -> Optional[Path]:
    """从模块文件路径推导项目根目录下的 skills/ 目录"""
    try:
        mod_path = Path(__file__).resolve()
        project_root = mod_path.parent.parent.parent
        skills_dir = project_root / "skills"
        if skills_dir.is_dir():
            return skills_dir
    except NameError:
        pass
    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.is_dir():
        return cwd_skills
    return None


def _get_engine(roots: Optional[list[Path]] = None):
    """懒加载 engine 组件"""
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

    from skill_engine.domain_words import register_domain_words
    register_domain_words(registry)

    preprocessor = None
    try:
        from skill_engine.config import get_llm
        from skill_engine.preprocessor import Preprocessor
        llm = get_llm()
        preprocessor = Preprocessor(llm=llm)
    except Exception:
        pass

    router = Router(registry, preprocessor=preprocessor)
    executor = Executor(timeout=30, allow_all=True)
    assembler = Assembler(executor=executor, command_timeout=30)
    runner = Runner(assembler, executor)
    return index, registry, router, executor, assembler, runner


def _get_llm_client():
    """获取 LLM 客户端"""
    try:
        from skill_engine.config import get_llm
        return get_llm()
    except Exception:
        return None


def list_skills(verbose: bool = False) -> str:
    """列出所有可用 skills"""
    index, registry, *_ = _get_engine()
    active = registry.list_active()

    if not active:
        scanned = []
        mod_skills = _get_project_skills_dir()
        if mod_skills:
            scanned.append(f"  - {mod_skills}（项目 skills 目录）")
        paths = "\n".join(scanned)
        return f"暂无可用 skills。\n\n扫描路径：\n{paths}\n\n提示：确保 skills/ 目录存在且包含 SKILL.md 的子目录。"

    lines = [f"## 共 {len(active)} 个可用 skills\n"]
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


def match_skills(query: str, explain: bool = False) -> str:
    """匹配 skills 到用户输入"""
    if not query or not query.strip():
        return "请输入查询内容。"

    _, _, router, *_ = _get_engine()
    llm = _get_llm_client()
    plan = router.match(query, llm=llm)

    if not plan.primary and not plan.selections:
        result = f"未找到与 '{query}' 匹配的 skill。"
        if plan.reason:
            result += f"\n\n原因: {plan.reason}"
        return result

    lines = [f"## 匹配 '{query}' 的结果\n"]
    if plan.primary:
        lines.append(f"### 单 skill: {plan.primary.name}")
        lines.append(f"- 方法: {plan.method}")
        if plan.score is not None:
            lines.append(f"- 得分: {plan.score:.2f}")
        if plan.uncertain:
            lines.append("- ⚠️ 匹配结果不确定")
    elif plan.selections:
        lines.append(f"### 多 skill 协同（{len(plan.selections)} 个）")
        for i, s in enumerate(plan.selections, 1):
            lines.append(f"{i}. {s.name}（角色: {s.role or '默认'}）")
        lines.append(f"- 方法: {plan.method}")

    if plan.reason:
        lines.append(f"- 说明: {plan.reason}")

    if explain:
        lines.append("\n---\n### 匹配详情")
        lines.append(f"- 模式: {plan.mode}")
        lines.append(f"- 方法: {plan.method}")
        if plan.uncertain:
            lines.append("- 不确定: True")
        if plan.reason:
            lines.append(f"- 原因: {plan.reason}")

    return "\n".join(lines)


def run_skill(skill_name: str, query: str, mode: str = "compile", progress: gr.Progress = gr.Progress()) -> str:
    """执行 skill（手动选择）"""
    if not skill_name:
        return "请选择一个 skill。"
    if not query or not query.strip():
        return "请输入查询内容。"

    progress(0.1, desc="正在匹配 skill...")
    index, registry, router, executor, assembler, runner = _get_engine()

    llm = _get_llm_client()
    plan = router.match(skill_name, llm=llm)
    if not plan.primary:
        return f"未找到 skill: {skill_name}"

    if mode == "dry-run":
        progress(0.5, desc="正在编译 Prompt...")
        skill = registry.load_skill(plan.primary.name)
        if not skill:
            return f"无法加载 skill: {skill_name}"
        final_prompt = assembler.assemble(skill, {"$ARGUMENTS": query, "$0": query})
        progress(1.0, desc="完成")
        return f"## 编译后的 Prompt（{skill_name}）\n\n```\n{final_prompt[:5000]}\n```\n"

    elif mode == "llm":
        progress(0.3, desc="正在初始化 LLM...")
        llm = _get_llm_client()
        if not llm:
            return "## 档位 A 需要 LLM 配置\n\n请设置环境变量:\n- AGNES_MODEL\n- AGNES_BASE_URL\n- AGNES_API_KEY"
        progress(0.5, desc="正在调用 LLM...")
        result = runner.run_plan(plan, registry, query=query, llm=llm)
        progress(1.0, desc="完成")
        return f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n**输出：**\n\n{result.get('output', '')[:5000]}"

    elif mode == "steps":
        progress(0.3, desc="正在执行 Steps DSL...")
        result = runner.run_plan(plan, registry, query=query)
        progress(1.0, desc="完成")
        output = result.get("output", "")[:5000]
        files = result.get("files_created", [])
        text = f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n```\n{output}\n```\n"
        if files:
            text += "\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
        return text

    else:
        progress(0.3, desc="正在编译...")
        result = runner.run_plan(plan, registry, query=query)
        output = result.get("output", "")[:5000]
        files = result.get("files_created", [])
        progress(1.0, desc="完成")
        text = f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n```\n{output}\n```\n"
        if files:
            text += "\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
        return text


def run_skill_direct(query: str, mode: str = "compile", progress: gr.Progress = gr.Progress()) -> str:
    """直接输入查询执行 skill（自动匹配）"""
    if not query or not query.strip():
        return "请输入查询内容。"

    progress(0.1, desc="正在匹配 skill...")
    index, registry, router, executor, assembler, runner = _get_engine()

    llm = _get_llm_client()
    plan = router.match(query, llm=llm)
    if not plan.primary:
        result = "未找到匹配的 skill。"
        if plan.reason:
            result += f"\n原因: {plan.reason}"
        if plan.uncertain:
            result += "\n⚠️ 匹配结果不确定"
        return result

    if mode == "dry-run":
        progress(0.5, desc="正在编译 Prompt...")
        skill = registry.load_skill(plan.primary.name)
        if not skill:
            return f"无法加载 skill: {plan.primary.name}"
        final_prompt = assembler.assemble(skill, {"$ARGUMENTS": query, "$0": query})
        progress(1.0, desc="完成")
        return f"## 匹配: {plan.primary.name}（得分: {plan.score or 'N/A'}）\n\n```\n{final_prompt[:5000]}\n```\n"

    elif mode == "llm":
        progress(0.3, desc="正在初始化 LLM...")
        llm = _get_llm_client()
        if not llm:
            return "## 档位 A 需要 LLM 配置\n\n请设置环境变量:\n- AGNES_MODEL\n- AGNES_BASE_URL\n- AGNES_API_KEY"
        progress(0.5, desc="正在调用 LLM...")
        result = runner.run_plan(plan, registry, query=query, llm=llm)
        progress(1.0, desc="完成")
        return f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n匹配得分: {plan.score or 'N/A'}\n\n**输出：**\n\n{result.get('output', '')[:5000]}"

    elif mode == "steps":
        progress(0.3, desc="正在执行 Steps DSL...")
        result = runner.run_plan(plan, registry, query=query)
        progress(1.0, desc="完成")
        output = result.get("output", "")[:5000]
        files = result.get("files_created", [])
        text = f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n匹配得分: {plan.score or 'N/A'}\n\n```\n{output}\n```\n"
        if files:
            text += "\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
        return text

    else:
        progress(0.3, desc="正在编译...")
        result = runner.run_plan(plan, registry, query=query)
        output = result.get("output", "")[:5000]
        files = result.get("files_created", [])
        progress(1.0, desc="完成")
        text = f"## Skill: {result.get('skill_name', plan.primary.name)}\n\n匹配得分: {plan.score or 'N/A'}\n\n```\n{output}\n```\n"
        if files:
            text += "\n**创建的文件：**\n" + "\n".join(f"  - {f}" for f in files)
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
            # Tab 1: Skill 列表
            with gr.Tab("Skill 列表"):
                skill_list_md = gr.Markdown(value=list_skills())
                with gr.Row():
                    refresh_list_btn = gr.Button("刷新列表", variant="secondary")
                    verbose_check = gr.Checkbox(label="详细输出", value=False)
                refresh_list_btn.click(list_skills, inputs=[verbose_check], outputs=[skill_list_md])

            # Tab 2: Skill 匹配
            with gr.Tab("Skill 匹配"):
                match_query = gr.Textbox(label="查询", placeholder="输入关键词，如 '部署应用'")
                with gr.Row():
                    explain_check = gr.Checkbox(label="显示匹配详情（--explain）", value=False)
                match_btn = gr.Button("匹配", variant="primary")
                match_result_md = gr.Markdown()
                match_btn.click(match_skills, inputs=[match_query, explain_check], outputs=[match_result_md])

            # Tab 3: 直接执行
            with gr.Tab("直接执行"):
                exec_query = gr.Textbox(label="输入查询", placeholder="如 '帮我生成第49题的题解'")
                exec_mode = gr.Radio(choices=["compile", "llm", "steps", "dry-run"], value="compile", label="执行模式")
                exec_btn = gr.Button("执行", variant="primary")
                exec_result_md = gr.Markdown()
                exec_btn.click(run_skill_direct, inputs=[exec_query, exec_mode], outputs=[exec_result_md])

            # Tab 4: 手动执行
            with gr.Tab("手动执行"):
                initial_skills = list_skills_for_dropdown()
                skill_select = gr.Dropdown(
                    choices=initial_skills, label="选择 Skill",
                    value=initial_skills[0] if initial_skills else None,
                    interactive=True, allow_custom_value=True,
                )
                with gr.Row():
                    refresh_dropdown_btn = gr.Button("刷新列表", size="sm")
                    refresh_dropdown_btn.click(fn=_refresh_dropdown, outputs=[skill_select])

                manual_query = gr.Textbox(label="查询/参数", placeholder="输入参数（如 '49'）或查询文本")
                manual_mode = gr.Radio(choices=["compile", "llm", "steps", "dry-run"], value="compile", label="执行模式")
                manual_btn = gr.Button("执行", variant="primary")
                manual_result_md = gr.Markdown()
                manual_btn.click(run_skill, inputs=[skill_select, manual_query, manual_mode], outputs=[manual_result_md])

            # Tab 5: 管理
            with gr.Tab("管理"):
                gr.Markdown("### 安装 Skill")
                with gr.Row():
                    install_source = gr.Textbox(label="来源", placeholder="本地路径 / Git URL", scale=3)
                    install_target = gr.Textbox(label="目标目录", value="~/.skills", scale=1)
                install_btn = gr.Button("安装", variant="primary")
                install_result = gr.Markdown()
                install_btn.click(install_skill, inputs=[install_source, install_target], outputs=[install_result])

                gr.Markdown("---")
                gr.Markdown("### 卸载 Skill")
                with gr.Row():
                    uninstall_name = gr.Textbox(label="Skill 名称", placeholder="要卸载的 skill 名称", scale=3)
                    uninstall_target = gr.Textbox(label="目标目录", value="~/.skills", scale=1)
                uninstall_btn = gr.Button("卸载", variant="stop")
                uninstall_result = gr.Markdown()
                uninstall_btn.click(uninstall_skill, inputs=[uninstall_name, uninstall_target], outputs=[uninstall_result])

            # Tab 6: Trace
            with gr.Tab("Trace"):
                gr.Markdown("### 路由匹配记录\n\n查看最近的路由匹配结果。")
                trace_query = gr.Textbox(label="查询", placeholder="输入查询查看匹配结果")
                with gr.Row():
                    trace_explain = gr.Checkbox(label="显示详情", value=False)
                    trace_btn = gr.Button("查询", variant="primary")
                trace_result_md = gr.Markdown()
                trace_btn.click(match_skills, inputs=[trace_query, trace_explain], outputs=[trace_result_md])

    return demo


if __name__ == "__main__":
    demo = create_demo()
    demo.launch(server_name="127.0.0.1", server_port=7860, theme=gr.themes.Soft())