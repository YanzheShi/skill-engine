"""REPL 内的记忆命令处理。

只处理以 ``/`` 开头的记忆命令；返回 False 表示不是记忆命令，交给原有
REPL 流程（/exit、:paste、:load 等）继续处理。
"""

from __future__ import annotations

from .service import MemoryService
from .views import plain_view


def _print_hits(hits) -> None:
    if not hits:
        print("[memory] 没有命中记录")
        return
    for hit in hits:
        d = hit.to_dict()
        print(f"  [{d['id']}] [{d['type']}] {d['title']}: {d['snippet']}  (score={d['score']})")


def handle_repl_command(cmd: str, service: MemoryService | None) -> bool:
    text = (cmd or "").strip()
    if not text.startswith("/"):
        return False
    if text in ("/exit", "/done"):
        return False

    if service is None:
        if text.startswith(("/remember", "/memory", "/profile", "/now", "/phase")):
            print("[memory] 记忆服务不可用（配置未启用或初始化失败）")
            return True
        return False

    if text.startswith("/remember"):
        rest = text[len("/remember"):].strip()
        scope = "project"
        record_type = "fact"
        words: list[str] = []
        for token in rest.split():
            if token == "--user":
                scope = "user"
            elif token.startswith("--scope="):
                scope = token.split("=", 1)[1] or scope
            elif token.startswith("--type="):
                record_type = token.split("=", 1)[1] or record_type
            else:
                words.append(token)
        body = " ".join(words).strip()
        if not body:
            print("[memory] 用法: /remember [--user] [--type=fact] <内容>")
            return True
        title = body[:60]
        try:
            record = service.add(
                type=record_type, title=title, body=body,
                scope=scope, source="repl", confidence="medium",
            )
            print(f"[memory] 已记录 {record.id} ({record.type}/{record.scope})")
        except Exception as e:  # noqa: BLE001
            print(f"[memory] 记录失败: {e}")
        return True

    if text.startswith("/memory"):
        rest = text[len("/memory"):].strip()
        if not rest or rest in ("help", "-h", "--help"):
            print("[memory] search <query> | get <id> | forget <id> | stats | off")
            return True
        sub, _, arg = rest.partition(" ")
        sub = sub.strip().lower()
        arg = arg.strip()
        if sub == "off":
            service.set_enabled(False)
            print("[memory] 本次会话已关闭记忆注入与自动检索")
            return True
        if sub == "stats":
            print(service.stats())
            return True
        if sub == "search":
            if not arg:
                print("[memory] 用法: /memory search <query>")
                return True
            _print_hits(service.search(arg, top_k=5, include_private=True))
            return True
        if sub in ("get", "show"):
            record = service.get(arg, include_private=True)
            if record is None:
                print(f"[memory] 未找到: {arg}")
            else:
                print(f"[{record.id}] {record.title}\n{record.body}")
            return True
        if sub == "forget":
            ok = service.forget(arg)
            print("[memory] 已遗忘" if ok else f"[memory] 未找到: {arg}")
            return True
        print(f"[memory] 未知子命令: {sub}")
        return True

    if text == "/profile" or text.startswith("/profile "):
        view = plain_view(service.render_profile(include_private=True))
        print(view or "[memory] PROFILE 为空")
        return True

    if text == "/now" or text.startswith("/now "):
        as_of = text[len("/now"):].strip() or None
        view = plain_view(service.render_now(as_of=as_of or "", include_private=True))
        print(view or "[memory] NOW 为空")
        return True

    if text.startswith("/phase"):
        rest = text[len("/phase"):].strip()
        sub, _, arg = rest.partition(" ")
        sub = sub.strip().lower()
        arg = arg.strip()
        if sub == "start":
            if not arg:
                print("[memory] 用法: /phase start <阶段名>")
                return True
            record = service.phase_start(arg)
            print(f"[memory] 阶段已开始: {record.id} ({record.title})")
            return True
        if sub == "end":
            record = service.phase_end(arg)
            print(f"[memory] 阶段已归档: {record.id}" if record else f"[memory] 未找到阶段: {arg}")
            return True
        print("[memory] 用法: /phase start <名称> | /phase end <id>")
        return True

    return False
