"""
LangGraph 编排器

用 LangGraph 的 StateGraph 替代 while 循环，实现：
- 结构化状态管理
- 异步执行
- 可扩展的节点路由

使用方式：
>>> from skill_engine.execution.orchestrator import Orchestrator
>>> orch = Orchestrator(assembler, executor)
>>> result = await orch.run_async(user_input, registry, llm_client)
"""

import json
import asyncio
from typing import TypedDict, Literal, Optional, Any
from pathlib import Path


# ================================================================
# 状态定义
# ================================================================

class OrchestrationState(TypedDict, total=False):
    """编排图的状态"""
    # 输入
    user_input: str
    catalog: str
    
    # 规划阶段
    planning_messages: list[dict]
    plan: list[dict]          # [{"skill": ..., "args": ..., "description": ...}]
    reasoning: str
    planning_iterations: int
    
    # 执行阶段
    chain_results: list[dict]
    all_outputs: str
    files_created: list[str]
    prev_outputs: dict
    
    # 结果
    output: str
    stopped_by: Literal["complete", "direct_answer", "max_planning_steps", "error"]
    
    # 内部字段（LangGraph 不保留以 _ 开头的字段，需要显式声明）
    _llm: Any
    _registry: Any
    _max_planning_steps: int


# ================================================================
# 节点函数
# ================================================================

def _parse_orchestration_plan(llm_response: str) -> dict:
    """解析 LLM 返回的编排计划"""
    plan = {"plan": [], "reasoning": llm_response}
    try:
        data = json.loads(llm_response)
        if isinstance(data, dict):
            plan["plan"] = data.get("plan", [])
            plan["reasoning"] = data.get("reasoning", llm_response)
    except (json.JSONDecodeError, TypeError):
        pass
    return plan


async def plan_node(state: OrchestrationState) -> dict:
    """规划节点：调用 LLM 决定用哪些 skill"""
    messages = state.get("planning_messages", [])
    llm = state.get("_llm")
    
    if not llm:
        return {
            "plan": [],
            "reasoning": "LLM client not available",
            "planning_iterations": state.get("planning_iterations", 0) + 1,
        }
    
    # 支持 list[dict] 或 str
    if isinstance(messages, list) and len(messages) > 0:
        llm_messages = list(messages)
    else:
        llm_messages = [{"role": "user", "content": str(messages)}]
    
    resp = llm.invoke(llm_messages)
    
    if isinstance(resp, dict):
        resp_text = resp.get("content", "")
    else:
        resp_text = str(resp)
    
    plan_data = _parse_orchestration_plan(resp_text)
    
    return {
        "plan": plan_data.get("plan", []),
        "reasoning": plan_data.get("reasoning", ""),
        "planning_iterations": state.get("planning_iterations", 0) + 1,
    }


def execute_chain_node(state: OrchestrationState, assembler, executor, registry) -> dict:
    """执行节点：按编排链依次执行各 skill（同步版本）"""
    import asyncio
    
    chain = state.get("plan", [])
    if not chain:
        return {
            "chain_results": [],
            "all_outputs": "",
            "files_created": [],
        }
    
    chain_results = []
    all_outputs = []
    files_created = []
    prev_outputs = state.get("prev_outputs", {})
    llm = state.get("_llm")
    
    # 使用现有的事件循环
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    
    for step in chain:
        skill_name = step.get("skill", "")
        step_args = step.get("args", {})
        step_desc = step.get("description", "")
        
        # 加载 skill
        skill = registry.load_skill(skill_name)
        if not skill:
            chain_results.append({
                "skill": skill_name, "status": "error",
                "args": step_args,
                "error": f"未找到 skill: {skill_name}",
            })
            all_outputs.append(f"[{skill_name}] 未找到")
            continue
        
        # 合并参数
        merged_args = {**prev_outputs, **step_args}
        
        # 编译 prompt
        prompt = assembler.assemble(skill, merged_args)
        
        # 调用 LLM
        try:
            if llm:
                # 如果 LLM 是异步的，用 to_thread 包装
                if loop:
                    output = loop.run_until_complete(asyncio.to_thread(llm.invoke, prompt))
                else:
                    output = llm.invoke(prompt)
                chain_results.append({
                    "skill": skill_name, "status": "success",
                    "description": step_desc, "args": step_args,
                    "output_len": len(output),
                })
                all_outputs.append(f"## [{skill_name}] {step_desc}\n{output}")
        except Exception as e:
            chain_results.append({
                "skill": skill_name, "status": "error",
                "args": step_args, "error": str(e),
            })
            all_outputs.append(f"[{skill_name}] 执行失败: {e}")
    
    return {
        "chain_results": chain_results,
        "all_outputs": "\n\n---\n\n".join(all_outputs),
        "files_created": files_created,
    }


def should_continue_node(state: OrchestrationState) -> str:
    """条件路由：决定是否继续规划"""
    plan = state.get("plan", [])
    if not plan:
        return "direct_answer"
    
    iterations = state.get("planning_iterations", 0)
    max_iterations = state.get("_max_planning_steps", 5)
    
    if iterations >= max_iterations:
        return "max_planning_steps"
    
    return "execute"


def format_result_node(state: OrchestrationState) -> dict:
    """格式化最终输出"""
    plan = state.get("plan", [])
    chain_results = state.get("chain_results", [])
    all_outputs = state.get("all_outputs", "")
    reasoning = state.get("reasoning", "")
    
    if not plan:
        return {
            "output": reasoning,
            "stopped_by": "direct_answer",
        }
    
    final_output = f"# 编排结果\n\n"
    final_output += f"**推理**: {reasoning}\n\n"
    final_output += f"**执行链**:\n"
    for step_result in chain_results:
        status = "✅" if step_result["status"] == "success" else "❌"
        final_output += f"  {status} {step_result['skill']}: {step_result.get('description', '')}\n"
    final_output += f"\n---\n\n{all_outputs}\n"
    
    return {
        "output": final_output,
        "stopped_by": "complete",
    }


# ================================================================
# Orchestrator 类
# ================================================================

class Orchestrator:
    """基于 LangGraph 的技能编排器
    
    用 StateGraph 替代 while 循环，提供：
    - 结构化状态管理
    - 异步执行支持
    - 可扩展的节点路由
    """
    
    def __init__(self, assembler, executor):
        self.assembler = assembler
        self.executor = executor
        self._graph = None
    
    def _build_graph(self):
        """构建 LangGraph 状态图"""
        from langgraph.graph import StateGraph, END
        
        graph = StateGraph(OrchestrationState)
        
        # 添加节点
        graph.add_node("plan", plan_node)
        # execute 节点需要 assembler, executor, registry
        # registry 通过 state["_registry"] 传递
        graph.add_node("execute", lambda s: execute_chain_node(s, self.assembler, self.executor, s.get("_registry", {})))
        graph.add_node("format", format_result_node)
        
        # 入口节点
        graph.set_entry_point("plan")
        
        # 条件路由
        graph.add_conditional_edges(
            "plan",
            should_continue_node,
            {
                "direct_answer": "format",
                "execute": "execute",
                "max_planning_steps": "format",
            }
        )
        
        # execute 之后回到 plan（可以继续规划更多 skill）
        graph.add_edge("execute", "plan")
        
        # format 之后结束
        graph.add_edge("format", END)
        
        self._graph = graph
    
    @property
    def graph(self):
        if self._graph is None:
            self._build_graph()
        return self._graph
    
    async def run_async(
        self,
        user_input: str,
        registry,
        llm=None,
        max_planning_steps: int = 5,
        max_chain_steps: int = 10,
    ) -> dict:
        """异步运行编排
        
        Args:
            user_input: 用户输入
            registry: Skill 注册表
            llm: LLM 客户端（默认用 config.get_llm()）
            max_planning_steps: 最大规划轮数
            max_chain_steps: 最大链执行步数
        
        Returns:
            编排结果 dict
        """
        # 默认使用配置的模型
        if llm is None:
            from skill_engine.config import get_llm
            llm = get_llm(purpose="orchestrator")
        
        # 构建 catalog
        catalog = self._build_catalog(registry)
        
        # 构建初始状态
        initial_state: OrchestrationState = {
            "user_input": user_input,
            "catalog": catalog,
            "planning_messages": [{
                "role": "user",
                "content": self._build_system_prompt(catalog, user_input),
            }],
            "plan": [],
            "reasoning": "",
            "planning_iterations": 0,
            "chain_results": [],
            "all_outputs": "",
            "files_created": [],
            "prev_outputs": {},
            "_llm": llm,
            "_registry": registry,
            "_max_planning_steps": max_planning_steps,
        }
        
        # 运行图
        app = self.graph.compile()
        result = await app.ainvoke(initial_state)
        
        # 清理内部字段
        result.pop("_llm", None)
        result.pop("_registry", None)
        result.pop("_max_planning_steps", None)
        
        # 标准化输出
        return {
            "skill_name": "orchestrator",
            "score": 1.0,
            "chain": result.get("chain_results", []),
            "reasoning": result.get("reasoning", ""),
            "output": result.get("output", ""),
            "files_created": result.get("files_created", []),
            "iterations": result.get("planning_iterations", 0),
            "stopped_by": result.get("stopped_by", "error"),
        }
    
    def run(
        self,
        user_input: str,
        registry,
        llm=None,
        max_planning_steps: int = 5,
        max_chain_steps: int = 10,
    ) -> dict:
        """同步运行编排（内部用 asyncio）"""
        return asyncio.run(self.run_async(
            user_input, registry, llm,
            max_planning_steps=max_planning_steps,
            max_chain_steps=max_chain_steps,
        ))
    
    def _build_catalog(self, registry) -> str:
        """构建 skill 目录（复用 Runner 的逻辑）"""
        groups = registry.get_groups()
        lines = ["## 可用技能清单\n"]
        
        for group_name, skill_names in groups.items():
            if group_name == "__ungrouped__":
                for name in skill_names:
                    fm = registry.info_full(name)
                    if not fm:
                        continue
                    desc = fm.get("description", "")
                    lines.append(f"- **{name}**")
                    if desc and desc != name:
                        lines.append(f"  - 描述: {desc}")
                    when_to_use = fm.get("when_to_use", "")
                    if when_to_use:
                        lines.append(f"  - 适用场景: {when_to_use}")
                    arg_hint = fm.get("argument_hint", "")
                    if arg_hint:
                        lines.append(f"  - 参数提示: {arg_hint}")
            else:
                lines.append(f"### 分组: {group_name}")
                lines.append(f"- 包含 {len(skill_names)} 个技能:")
                for name in skill_names:
                    fm = registry.info_full(name)
                    desc = fm.get("description", "") if fm else ""
                    if desc and desc != name:
                        lines.append(f"  - **{name}**: {desc}")
                    else:
                        lines.append(f"  - **{name}**")
        
        return "\n".join(lines)
    
    def _build_system_prompt(self, catalog: str, user_input: str) -> str:
        """构建编排 prompt"""
        return f"""\
你是一个技能编排器。你有以下技能可用：

{catalog}

你的任务：
1. 理解用户的需求
2. 从可用技能中选择需要的
3. 编排调用顺序
4. 依次调用，传递中间结果

请以 JSON 格式返回你的决策：
{{
  "plan": [
    {{"skill": "skill名称", "args": {{}}, "description": "这一步的作用"}},
    ...
  ],
  "reasoning": "你为什么这样编排"
}}

如果不需要任何技能，直接回答用户即可：
{{
  "plan": [],
  "reasoning": "直接回答的原因"
}}

用户需求: {user_input}
"""
