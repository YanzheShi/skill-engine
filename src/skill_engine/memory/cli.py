"""memory / profile / phase CLI 子命令。"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..execution.paths import to_native_path
from .service import MemoryService
from .views import plain_view

memory_app = typer.Typer(help="个人长期记忆管理", add_completion=False)
profile_app = typer.Typer(help="个人档案视图与资源校验", add_completion=False)
phase_app = typer.Typer(help="阶段生命周期", add_completion=False)
project_app = typer.Typer(help="项目记忆", add_completion=False)


def _service(root: str | None = None) -> MemoryService:
    wr = to_native_path(root) if root else Path.cwd()
    return MemoryService(working_root=wr)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _print_hits(hits) -> None:
    if not hits:
        print("没有命中记录")
        return
    for hit in hits:
        d = hit.to_dict()
        print(f"[{d['id']}] [{d['type']}] {d['title']}: {d['snippet']}  (score={d['score']})")


@memory_app.command("init")
def memory_init(
    root: str | None = typer.Option(None, "--working-root", "-w", help="工作目录"),
    scope: str = typer.Option("both", "--scope", help="user / project / both"),
):
    """初始化记忆目录与索引。"""
    svc = _service(root)
    if not svc.enabled:
        print("[memory] 当前配置为关闭；设置 SKILLS_ENGINE_MEMORY_ENABLED=1 后重试")
        raise typer.Exit(code=1)
    if scope in ("user", "both"):
        svc.user_store.ensure()
    if scope in ("project", "both"):
        svc.project_store.ensure()
    print(f"[memory] user store:    {svc.user_store.root}")
    print(f"[memory] project store: {svc.project_store.root}")
    print(_json(svc.reindex()))
    svc.close()


@memory_app.command("add")
def memory_add(
    title: str = typer.Option(..., "--title", "-t", help="标题"),
    body: str = typer.Option("", "--body", "-b", help="正文"),
    record_type: str = typer.Option("fact", "--type", help="记录类型"),
    scope: str = typer.Option("project", "--scope", help="user / project / skill"),
    domain: str = typer.Option("", "--domain", help="领域标签，逗号分隔"),
    path: str = typer.Option("", "--path", help="资源/项目路径"),
    aliases: str = typer.Option("", "--aliases", help="别名，逗号分隔"),
    valid_until: str = typer.Option("", "--valid-until", help="有效期截止（ISO 日期/时间）"),
    confidence: str = typer.Option("medium", "--confidence", help="high / medium / low"),
    importance: float = typer.Option(0.5, "--importance", help="0-1"),
    sensitivity: str = typer.Option("L2", "--sensitivity", help="L1 / L2 / L3"),
    pinned: bool = typer.Option(False, "--pinned", help="强制进入 PROFILE"),
    user_scope: bool = typer.Option(False, "--user", help="等价于 --scope user"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """写入一条记忆。"""
    svc = _service(root)
    try:
        record = svc.add(
            type=record_type, title=title, body=body,
            scope="user" if user_scope else scope,
            domain=domain, valid_until=valid_until,
            data={
                **({"path": path} if path else {}),
                **({"aliases": [x.strip() for x in aliases.split(",") if x.strip()]}
                   if aliases else {}),
            },
            confidence=confidence, importance=importance,
            sensitivity=sensitivity, pinned=pinned, source="cli",
        )
        print(f"[memory] 已写入 {record.id} ({record.type}/{record.scope})")
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 写入失败: {e}")
        raise typer.Exit(code=1)
    finally:
        svc.close()


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="查询"),
    scope: str = typer.Option("auto", "--scope"),
    record_type: str = typer.Option("", "--type"),
    domain: str = typer.Option("", "--domain"),
    as_of: str = typer.Option("", "--as-of"),
    top_k: int = typer.Option(5, "--top-k", "-k"),
    include_history: bool = typer.Option(False, "--include-history"),
    include_private: bool = typer.Option(False, "--include-private"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
    as_json: bool = typer.Option(False, "--json"),
):
    """检索记忆。"""
    svc = _service(root)
    try:
        hits = svc.search(
            query, scope=scope, record_type=record_type, domain=domain,
            as_of=as_of, top_k=top_k,
            include_history=include_history, include_private=include_private,
        )
        if as_json:
            print(_json([h.to_dict() for h in hits]))
        else:
            _print_hits(hits)
    finally:
        svc.close()


@memory_app.command("get")
def memory_get(
    record_id: str = typer.Argument(..., help="记录 ID"),
    include_private: bool = typer.Option(True, "--include-private/--no-include-private"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
    as_json: bool = typer.Option(False, "--json"),
):
    """按 ID 读取一条记忆。"""
    svc = _service(root)
    try:
        record = svc.get(record_id, include_private=include_private)
        if record is None:
            print(f"[memory] 未找到或无权读取: {record_id}")
            raise typer.Exit(code=1)
        if as_json:
            print(_json(record.to_dict()))
        else:
            print(f"[{record.id}] {record.title}\n{record.body}")
    finally:
        svc.close()


@memory_app.command("show")
def memory_show(
    record_id: str = typer.Argument(..., help="记录 ID"),
    include_private: bool = typer.Option(True, "--include-private/--no-include-private"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
    as_json: bool = typer.Option(False, "--json"),
):
    """按 ID 读取一条记忆（get 的别名）。"""
    memory_get(record_id, include_private, root, as_json)


@memory_app.command("edit")
def memory_edit(
    record_id: str = typer.Argument(..., help="记录 ID"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """打印记录文件路径，便于直接用编辑器修改 Markdown。"""
    svc = _service(root)
    try:
        record = svc.get(record_id, include_private=True)
        if record is None or not record.path:
            print(f"[memory] 未找到: {record_id}")
            raise typer.Exit(code=1)
        print(record.path)
    finally:
        svc.close()


@memory_app.command("forget")
def memory_forget(
    record_id: str = typer.Argument(..., help="记录 ID"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """删除一条记忆并留审计事件。"""
    if not yes:
        typer.confirm(f"确认遗忘 {record_id}？", abort=True)
    svc = _service(root)
    try:
        ok = svc.forget(record_id)
        print("[memory] 已遗忘" if ok else f"[memory] 未找到: {record_id}")
        if not ok:
            raise typer.Exit(code=1)
    finally:
        svc.close()


@memory_app.command("resolve")
def memory_resolve(
    name: str = typer.Argument(..., help="实体/资源名称或别名"),
    kind: str = typer.Option("auto", "--kind", help="auto / resource / entity"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """精确解析实体或资源（不猜、不编造路径）。"""
    svc = _service(root)
    try:
        if kind == "resource":
            result = svc.resolve_resource(name)
        elif kind == "entity":
            result = svc.resolve_entity(name)
        else:
            result = svc.resolve_resource(name)
            if result.get("status") == "not_found":
                result = svc.resolve_entity(name)
        print(_json(result))
    finally:
        svc.close()


@memory_app.command("reindex")
def memory_reindex(
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """从 Markdown 记录重建 SQLite 索引。"""
    svc = _service(root)
    try:
        print(_json(svc.reindex()))
    finally:
        svc.close()


@project_app.command("init")
@memory_app.command("project-init")
def memory_project_init(
    root: str | None = typer.Option(None, "--working-root", "-w"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
):
    """从 README/pyproject/AGENTS.md/git remote 生成项目记忆候选。"""
    svc = _service(root)
    try:
        candidates = svc.project_init(dry_run=True)
        if not candidates:
            print("[memory] 没有可初始化的项目信息")
            return
        preview = [
            {"type": c.get("type"), "title": c.get("title"), "body": c.get("body", "")[:200]}
            for c in candidates
        ]
        print(_json(preview))
        if not yes and not typer.confirm("写入以上候选？", default=False):
            print("[memory] 已取消")
            return
        saved = svc.project_init(dry_run=False)
        print(f"[memory] 已写入 {len(saved)} 条项目记录")
    finally:
        svc.close()


@memory_app.command("list")
def memory_list(
    scope: str = typer.Option("all", "--scope", help="all / user / project"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """列出记忆。"""
    svc = _service(root)
    try:
        records = svc.list_records(scope)
        for record in records:
            print(f"[{record.id}] [{record.type}/{record.scope}] {record.title}")
        print(f"共 {len(records)} 条")
    finally:
        svc.close()


@memory_app.command("stats")
def memory_stats(
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """显示记忆库状态。"""
    svc = _service(root)
    try:
        print(_json(svc.stats()))
    finally:
        svc.close()


@profile_app.command("show")
def profile_show(
    root: str | None = typer.Option(None, "--working-root", "-w"),
    include_private: bool = typer.Option(True, "--include-private/--no-include-private"),
):
    """显示 PROFILE 视图。"""
    svc = _service(root)
    try:
        print(plain_view(svc.render_profile(include_private=include_private)) or "[memory] PROFILE 为空")
    finally:
        svc.close()


@profile_app.command("verify")
def profile_verify(
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """校验所有资源路径是否存在。"""
    svc = _service(root)
    try:
        rows = svc.verify_resources()
        print(_json(rows))
    finally:
        svc.close()


@profile_app.command("edit")
def profile_edit(
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """显示记忆目录，便于直接编辑 Markdown。"""
    svc = _service(root)
    try:
        print(f"user:    {svc.user_store.records_dir}")
        print(f"project: {svc.project_store.records_dir}")
    finally:
        svc.close()


@phase_app.command("start")
def phase_start(
    title: str = typer.Argument(..., help="阶段名称"),
    domain: str = typer.Option("planning", "--domain"),
    body: str = typer.Option("", "--body", "-b"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """开始一个阶段。"""
    svc = _service(root)
    try:
        record = svc.phase_start(title, domain=domain, body=body)
        print(f"[memory] 阶段已开始: {record.id} ({record.title})")
    finally:
        svc.close()


@phase_app.command("end")
def phase_end(
    record_id: str = typer.Argument(..., help="阶段记录 ID"),
    root: str | None = typer.Option(None, "--working-root", "-w"),
):
    """归档一个阶段。"""
    svc = _service(root)
    try:
        record = svc.phase_end(record_id)
        print(f"[memory] 阶段已归档: {record.id}" if record else f"[memory] 未找到阶段: {record_id}")
    finally:
        svc.close()


def now_view(
    *,
    as_of: str | None = None,
    root: str | None = None,
    include_private: bool = True,
) -> None:
    """供顶层 ``skill-engine now`` 命令调用。"""
    svc = _service(root)
    try:
        print(
            plain_view(svc.render_now(as_of=as_of or "", include_private=include_private))
            or "[memory] NOW 为空"
        )
    finally:
        svc.close()
