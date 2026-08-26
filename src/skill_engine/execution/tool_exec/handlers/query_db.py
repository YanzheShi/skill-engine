"""query_db：只读 SQL 查询工具（消临时脚本 write_file+bash 验证）。"""

import re
from pathlib import Path

from skill_engine.execution.paths import resolve_path
from skill_engine.execution.tool_exec.context import ToolContext
from skill_engine.execution.tool_exec.handler import BaseHandler
from skill_engine.execution.tool_exec.result import ToolResult


class QueryDbHandler(BaseHandler):
    name = "query_db"

    def execute(self, tc: dict, ctx: ToolContext) -> ToolResult:
        import sqlite3 as _sqlite3
        sql = tc["input"].get("sql", "").strip()
        db_path_in = tc["input"].get("db_path", "").strip()
        if not sql:
            obs = "[query_db] sql 不能为空"
        # 收紧只读白名单：允许 SELECT / PRAGMA / EXPLAIN，以及以 SELECT/WITH 起步的
        # CTE（WITH x AS (SELECT ...)）；禁止 WITH x AS (DELETE/UPDATE/INSERT ...)
        # 这类数据修改 CTE。
        elif not re.match(
            r"^(SELECT|PRAGMA|EXPLAIN)\b"
            r"|^(WITH\s+\w+\s+AS\s*\(\s*(SELECT|WITH)\b)",
            sql, re.IGNORECASE | re.VERBOSE,
        ):
            obs = ("[query_db] 仅允许只读语句（SELECT / PRAGMA / EXPLAIN / 以 SELECT 或 WITH(SELECT) 起步的 CTE）。"
                   "拒绝执行 DDL/DML 及含数据修改的 CTE，避免误改数据。")
        else:
            # 解析 db 路径：显式指定优先，否则在 base_dir 递归找第一个 *.db
            db_file = None
            if db_path_in:
                db_file = resolve_path(db_path_in, ctx.base_dir)
            else:
                candidates = sorted(Path(ctx.base_dir).rglob("*.db"))
                if candidates:
                    db_file = candidates[0]
            if not db_file or not db_file.exists():
                obs = f"[query_db] 未找到数据库文件（指定={db_path_in or '无'}）。"
            else:
                try:
                    conn = _sqlite3.connect(str(db_file))
                    cur = conn.cursor()
                    cur.execute(sql)
                    # 限制返回行数，避免大表拉几万行撑爆消息
                    MAX_ROWS = 200
                    rows = cur.fetchmany(MAX_ROWS)
                    cols = [d[0] for d in cur.description] if cur.description else []
                    # 判断是否还有更多行
                    extra = cur.fetchone() is not None
                    if not rows:
                        obs = f"[query_db] 查询成功，0 行（列：{cols}）"
                    else:
                        # 列宽看「数据 + 表头」最大值，避免数据比表头宽错位
                        all_vals = [str(c) for c in cols]
                        for r in rows:
                            all_vals.extend(str(v) for v in r)
                        width = max((len(v) for v in all_vals), default=8)
                        width = max(width, 8)
                        header = " | ".join(str(c).ljust(width) for c in cols)
                        sep = "-+-".join("-" * width for _ in cols)
                        body = "\n".join(
                            " | ".join(str(v).ljust(width) for v in r) for r in rows
                        )
                        tail = f"\n（仅显示前 {MAX_ROWS} 行）" if extra else ""
                        obs = f"[query_db] {len(rows)} 行（表：{db_file.name}）\n{header}\n{sep}\n{body}{tail}"
                    conn.close()
                except Exception as e:
                    obs = f"[query_db] 执行失败：{e}"
        print(f"     [query_db] {sql[:60]}")
        # query_db 是结构化数据查询，非 shell 执行，不套 format_observation
        # （那会带 exit_code: 0 / stdout: 前缀，语义不纯）。
        return ToolResult(
            tool_call_id=tc["id"], name="query_db",
            content=obs,
            step={"name": f"query_db_{tc['id']}", "type": "query_db", "sql": sql},
        )
