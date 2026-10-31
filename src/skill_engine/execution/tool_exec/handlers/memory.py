"""长期记忆工具 handler（memory_search/get/write/entity_resolve/resource_resolve/forget）。"""

from __future__ import annotations

import json
from typing import Any

from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


def _svc(ctx: ToolContext):
    svc = getattr(ctx, "memory", None)
    if svc is None or not getattr(svc, "enabled", False):
        return None
    return svc


def _payload(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


class _MemoryHandler(BaseHandler):
    def _result(self, tc: dict, ctx: ToolContext, payload: Any, *, line: str = "") -> ToolResult:
        content = _payload(payload)
        if ctx.truncate_msg:
            content = ctx.truncate_msg(content, max_chars=8000)
        return ToolResult(
            tool_call_id=tc["id"],
            name=self.name,
            content=content,
            step={
                "name": f"{self.name}_{tc['id']}",
                "type": self.name,
                "output": _payload(payload)[:1000],
            },
            print_line=line,
        )

    def _disabled(self, tc: dict, ctx: ToolContext) -> ToolResult:
        return self._result(tc, ctx, {
            "status": "disabled",
            "message": "memory is disabled for this session",
        })


class MemorySearchHandler(_MemoryHandler):
    name = "memory_search"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        inp = tc.get("input") or {}
        try:
            hits = svc.search(
                str(inp.get("query", "")),
                scope=str(inp.get("scope") or "auto"),
                record_type=str(inp.get("type") or ""),
                domain=str(inp.get("domain") or ""),
                as_of=str(inp.get("as_of") or ""),
                top_k=int(inp.get("top_k") or 3),
                include_history=bool(inp.get("include_history") or False),
                include_private=bool(inp.get("include_private") or False),
            )
        except Exception as e:  # noqa: BLE001
            return self._result(tc, ctx, {"status": "error", "message": str(e)})
        data = [h.to_dict() for h in hits]
        return self._result(tc, ctx, {"status": "ok", "results": data},
                            line=f"     memory_search: {len(data)} hit(s)")


class MemoryGetHandler(_MemoryHandler):
    name = "memory_get"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        inp = tc.get("input") or {}
        record_id = str(inp.get("id") or "")
        record = svc.get(record_id, include_private=bool(inp.get("include_private") or False))
        if record is None:
            return self._result(tc, ctx, {
                "status": "not_found_or_forbidden", "id": record_id,
            })
        return self._result(tc, ctx, {
            "status": "ok",
            "record": {
                "id": record.id,
                "type": record.type,
                "title": record.title,
                "body": record.body,
                "scope": record.scope,
                "domain": record.domain,
                "valid_until": record.valid_until,
                "source": record.source,
                "sensitivity": record.sensitivity,
            },
        })


class MemoryWriteHandler(_MemoryHandler):
    name = "memory_write"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        inp = tc.get("input") or {}
        if str(inp.get("scope") or "project") == "user" and not bool(inp.get("confirm") or False):
            return self._result(tc, ctx, {
                "status": "confirmation_required",
                "message": "scope=user requires confirm=true after the user agrees",
            })
        data = {}
        if inp.get("path"):
            data["path"] = str(inp.get("path"))
        if inp.get("aliases"):
            data["aliases"] = [
                x.strip() for x in str(inp.get("aliases")).split(",") if x.strip()
            ]
        try:
            record = svc.add(
                type=str(inp.get("type") or "fact"),
                title=str(inp.get("title") or ""),
                body=str(inp.get("body") or ""),
                scope=str(inp.get("scope") or "project"),
                domain=str(inp.get("domain") or ""),
                subject=str(inp.get("entity") or ""),
                data=data,
                valid_until=str(inp.get("valid_until") or ""),
                confidence=str(inp.get("confidence") or "medium"),
                importance=float(inp.get("importance") or 0.5),
                sensitivity=str(inp.get("sensitivity") or "L2"),
                source="agent",
            )
        except Exception as e:  # noqa: BLE001
            return self._result(tc, ctx, {"status": "error", "message": str(e)})
        return self._result(tc, ctx, {
            "status": "ok",
            "id": record.id,
            "type": record.type,
            "scope": record.scope,
        }, line=f"     memory_write: {record.id}")


class EntityResolveHandler(_MemoryHandler):
    name = "entity_resolve"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        name = str((tc.get("input") or {}).get("name") or "")
        return self._result(tc, ctx, svc.resolve_entity(name))


class ResourceResolveHandler(_MemoryHandler):
    name = "resource_resolve"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        name = str((tc.get("input") or {}).get("name") or "")
        return self._result(tc, ctx, svc.resolve_resource(name))


class MemoryForgetHandler(_MemoryHandler):
    name = "memory_forget"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return self._disabled(tc, ctx)
        inp = tc.get("input") or {}
        record_id = str(inp.get("id") or "")
        if not bool(inp.get("confirm") or False):
            return self._result(tc, ctx, {
                "status": "confirmation_required",
                "message": "pass confirm=true only after the user explicitly agrees",
                "id": record_id,
            })
        ok = svc.forget(record_id)
        return self._result(tc, ctx, {
            "status": "ok" if ok else "not_found",
            "id": record_id,
        })


MEMORY_HANDLERS = [
    MemorySearchHandler(),
    MemoryGetHandler(),
    MemoryWriteHandler(),
    EntityResolveHandler(),
    ResourceResolveHandler(),
    MemoryForgetHandler(),
]
