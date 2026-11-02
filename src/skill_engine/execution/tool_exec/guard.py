"""截断守卫：拦住"参数本身残缺"的写类 tool_call，避免写出空/坏文件。

分工（重要）：
- 守卫只管**参数结构**——content 键丢失、args 不是对象、edits 残缺。这类调用
  执行了也只会产出空文件，必须拒。
- **内容语义是否完整**（HTML 是否闭合、占位符是否填完）不归守卫判，交给
  ``atomic_write`` 的暂存判定：不完整就留在 ``*.partial``、不转正，让模型继续
  append/填槽。否则"故意写的半成品骨架"会被误杀，模型只能空转重试。
- ``finish_reason==length`` 不再一律拒执行：思考文本挤占输出预算时，同一轮里
  的小写入完全可能是完整的。它只作为"是否允许转正"的信号传给 handler。
"""

# 只有这两类会写盘。
WRITE_TOOL_TYPES = frozenset({"write_file", "edit_file"})

_TRUNCATED_FINISH = frozenset({"length", "max_tokens", "max_output_tokens"})


def is_truncated_turn(finish_reason) -> bool:
    """本轮输出是否撞到上限（用于决定是否允许转正，而非是否拒执行）。"""
    return str(finish_reason or "").strip().lower() in _TRUNCATED_FINISH


def args_complete(tc: dict) -> bool:
    """参数结构完整性：只看键在不在、类型对不对，不看内容语义。"""
    t = tc.get("type")
    inp = tc.get("input")
    if not isinstance(inp, dict):
        return False                      # args 是残缺的 JSON 字符串 / 非对象
    if t == "write_file":
        content = inp.get("content")
        if content is None:
            return False                  # content 键丢失 = 截断
        if content == "":
            return False                  # 有 path 无正文：绝大多数是截断
        return True
    if t == "edit_file":
        edits = inp.get("edits")
        if not isinstance(edits, list) or not edits:
            return False
        return all(
            isinstance(e, dict) and e.get("oldText") and e.get("newText") is not None
            for e in edits
        )
    return True


def should_refuse(tc: dict, finish_reason="") -> bool:
    """该条调用是否不可执行（参数结构残缺）。"""
    if tc.get("type") not in WRITE_TOOL_TYPES:
        return False
    return not args_complete(tc)
