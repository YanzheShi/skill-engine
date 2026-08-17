"""透明 LLM 调用计数 + token 用量累计（成本统计核心，被 moa 与 run 命令共用）。

CountingLLM 包装任意 langchain chat model：
- 每次 ``invoke`` 自增 ``counter['calls']`` 并从返回消息累计 token；
- ``bind_tools`` 绑定后仍返回 CountingLLM 包装，使 tool_dispatch 内层每一轮
  invoke 也计入（与 MOA 同一套机制）；
- 其余属性/方法（temperature 等）经 ``__getattr__`` 透传到被包装对象。

counter 形态： ``{"calls": int, "prompt": int, "completion": int, "total": int}``。
"""

from __future__ import annotations


def _accumulate_tokens(counter: dict, resp) -> None:
    """从 langchain 返回的 AIMessage 提取 token 用量并累加到 counter。

    兼容两种来源：
    - langchain 标准 ``usage_metadata``（input_tokens/output_tokens/total_tokens）
    - openai 风格 ``response_metadata['token_usage']``（prompt_tokens/...）
    提取不到时静默跳过，不影响 llm_calls 计数。
    """
    if resp is None:
        return
    p = c = t = 0
    um = getattr(resp, "usage_metadata", None)
    if isinstance(um, dict):
        p = int(um.get("input_tokens") or um.get("prompt_tokens") or 0)
        c = int(um.get("output_tokens") or um.get("completion_tokens") or 0)
        t = int(um.get("total_tokens") or 0)
    if (p, c, t) == (0, 0, 0):
        rm = getattr(resp, "response_metadata", None)
        tu = (rm or {}).get("token_usage") if isinstance(rm, dict) else None
        if isinstance(tu, dict):
            p = int(tu.get("prompt_tokens") or 0)
            c = int(tu.get("completion_tokens") or 0)
            t = int(tu.get("total_tokens") or 0)
    if t == 0:
        t = p + c
    counter["prompt"] += p
    counter["completion"] += c
    counter["total"] += t


class CountingLLM:
    """透明包装任意 langchain chat model，每次 invoke 自增计数器。

    支持 ``bind_tools``：绑定后仍返回 CountingLLM 包装，使 tool_dispatch 内层
    的每一轮 invoke 也计入全局成本上限。其余属性/方法（temperature 等）透传到
    被包装对象。
    """

    def __init__(self, llm, counter):
        self._llm = llm
        self._counter = counter  # 可变容器，dict {calls,prompt,completion,total}

    def invoke(self, *args, **kwargs):
        self._counter["calls"] += 1
        resp = self._llm.invoke(*args, **kwargs)
        _accumulate_tokens(self._counter, resp)
        return resp

    def bind_tools(self, tools, **kwargs):
        self._counter["calls"] += 1
        bound = self._llm.bind_tools(tools, **kwargs)
        return CountingLLM(bound, self._counter)

    def __getattr__(self, name):
        return getattr(self._llm, name)
