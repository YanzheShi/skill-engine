"""tool call 参数调用指纹 — 防模型 tool call 死循环。

痛点：模型在循环里反复以**完全相同的参数**调用同一个工具（典型：edit_file
失败后原样重试、bash 同一命令反复跑、read_file 死读同一文件不消化结果），
直到 max_iterations 烧完预算。max_iterations 是全局兜底，无法区分「有进展
的多轮探索」和「零进展的原地打转」。

方案：每轮对解析出的 tool_calls 计算
    fingerprint = hash(tool_name + 归一化后参数)
由 LoopGuard 维护「连续出现轮数」计数——某指纹在第 i 轮和第 i+1 轮都出现
则该指纹计数 +1，缺席一轮即清零。达到阈值分两级响应（由 dispatch 层执行）：
    - warn：向 messages 追加一条**持久** user 反馈（模型下轮可见），不中断；
    - stop：不再执行本轮工具，提前 return（stopped_by="loop_detected"）。

阈值按工具性质区分：只读工具（read/search/time）重复往往是合法的
（edit→read→verify 流程天然重复读同一文件），放宽；写/执行类收紧。

状态存活于单次 run() 内（轮内语义），不跨轮、不持久化——跨轮重置反而是
正确行为：上一条用户指令的循环与本条无关。

配置（env 优先，config.yml settings 同名键经 _SETTINGS_ENV_MAP 回填）：
    SKILLS_ENGINE_LOOP_GUARD=off        整体关闭
    SKILLS_ENGINE_LOOP_GUARD_LIMIT=N    写类工具硬停阈值覆盖（默认 3）
"""

import hashlib
import json
import os
import re

# 连续同指纹计数达到该值 → 硬停（写/执行类）
_DEFAULT_STOP_LIMIT = 3
# 只读工具放宽：合法的 edit→read→verify 流程可能连续读同一文件
_READONLY_STOP_LIMIT = 6
# warn 提前量：达到 stop_limit - warn_lead 轮时先注入反馈
_WARN_LEAD = 1

# 只读白名单：重复调用无副作用（或副作用可忽略），阈值放宽
_READONLY_TOOLS = {
    "read_file", "search_files", "get_current_time", "web_search",
    "view_image",
}

_WS_RE = re.compile(r"\s+")


def _normalize_value(v):
    """归一化单个参数值：字符串折叠空白（bash 命令的多空格/换行视为等价），
    递归处理 dict/list；其余类型原样。只影响指纹，不影响真实执行。"""
    if isinstance(v, str):
        return _WS_RE.sub(" ", v).strip()
    if isinstance(v, dict):
        return {k: _normalize_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(x) for x in v]
    return v


def tool_call_fingerprint(name: str, args) -> str:
    """计算单条 tool call 的参数调用指纹。

    口径：sha1(tool_name + json.dumps(归一化 args, sort_keys=True))。
    sort_keys 保证键序无关；字符串值折叠空白，避免 `ls  -la` 与
    `ls -la`（多打一个空格）被误判为「已改变策略」。
    失败兜底：args 不可序列化时退化为 str()，保证检测永不抛异常。
    """
    try:
        norm = _normalize_value(args if isinstance(args, (dict, list)) else {"v": args})
        payload = json.dumps([name, norm], sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = f"{name}|{args!r}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _stop_limit_for(name: str, override: "int|None") -> int:
    if override and override > 0:
        return override
    return _READONLY_STOP_LIMIT if name in _READONLY_TOOLS else _DEFAULT_STOP_LIMIT


class LoopGuard:
    """单次 run() 内的连续同指纹循环检测器。

    feed_round() 每轮喂入该轮全部指纹（含并行批工具）；同一指纹连续出现
    的轮数达到 stop_limit → "stop"，达到 warn 阈值 → "warn"，否则 "ok"。
    """

    def __init__(self, limit_override: "int|None" = None, enabled: "bool|None" = None):
        if enabled is None:
            toggle = os.getenv("SKILLS_ENGINE_LOOP_GUARD", "").strip().lower()
            enabled = toggle not in ("off", "0", "false", "no", "disable")
        self.enabled = bool(enabled)
        if limit_override is None:
            raw = os.getenv("SKILLS_ENGINE_LOOP_GUARD_LIMIT", "").strip()
            try:
                limit_override = int(raw) if raw else None
            except ValueError:
                limit_override = None
        self.limit_override = limit_override
        self.prev_fps: set = set()      # 上一轮出现的指纹集合
        self.counts: dict = {}          # fp → 连续出现轮数
        self.warned: set = set()        # 已注入过软警告的指纹（每指纹只警告一次）

    def feed_round(self, fps: "list[str]") -> tuple:
        """喂入一轮的指纹列表，返回 (action, info)。

        action: "ok" / "warn" / "stop"
        info:   触发时的 {"fingerprint", "tool", "streak"}；未触发为 None。
                fps 里需携带指纹对应的工具名以生成可读 info，
                故实际入参为 [(fp, tool_name), ...]。
        """
        if not self.enabled:
            return "ok", None
        if not fps:
            # 空轮（模型纯文本回合，如 human_in_loop 追问）：连续性中断，重置计数
            self.prev_fps = set()
            self.counts.clear()
            return "ok", None
        cur = {fp for fp, _ in fps}
        for fp, tool in fps:
            self.counts[fp] = self.counts.get(fp, 0) + 1 if fp in self.prev_fps else 1
        # 本轮缺席的指纹：连续性中断，清零（删除即可，下次出现从 1 重计）
        for fp in list(self.counts):
            if fp not in cur:
                del self.counts[fp]
        self.prev_fps = cur

        # 取连续轮数最长且超阈的指纹判定。同轮多指纹同超阈时取 streak 更大者。
        worst = None
        for fp, tool in fps:
            limit = _stop_limit_for(tool, self.limit_override)
            streak = self.counts.get(fp, 0)
            if streak < limit - _WARN_LEAD:
                continue
            if worst is None or streak > worst[2]:
                # tool 名只用于展示/白名单判定，取首次出现的名字即可
                worst = (fp, tool, streak, limit)
        if worst is None:
            return "ok", None
        fp, tool, streak, limit = worst
        info = {"fingerprint": fp, "tool": tool, "streak": streak}
        if streak >= limit:
            return "stop", info
        # 软警告：同一指纹只警告一次，避免反馈消息刷屏挤占上下文
        if fp in self.warned:
            return "ok", None
        self.warned.add(fp)
        return "warn", info


def loop_feedback_message(tool: str, streak: int) -> str:
    """软警告反馈消息（持久追加进 messages，模型下一轮可见）。"""
    return (
        f"[loop warning] 检测到你已连续 {streak} 轮以**完全相同的参数**调用 {tool}，"
        f"结果不会因此改变。请立即改变策略：调整参数、换用其它工具，或基于已有结果"
        f"输出总结/调用 stop。继续原样重试将被强制中断。"
    )


def loop_stop_message(tool: str, streak: int) -> str:
    """硬停止时的最终输出（stopped_by="loop_detected"）。"""
    return (
        f"[已强制中断：检测到 tool call 死循环] 你已连续 {streak} 轮以完全相同的参数"
        f"调用 {tool} 且无任何进展。请基于已有信息人工评估下一步；如需继续，"
        f"请在续跑时改变调用参数或换用其它工具。"
    )
