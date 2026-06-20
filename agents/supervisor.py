"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，拆解为子任务，通过Intent Router规划执行链路，逐步执行Agent。
采用LangGraph StateGraph + 循环调度实现链路执行引擎。
"""

from __future__ import annotations

import os
import logging
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send
from pydantic import BaseModel, Field

from agents.intent_router import IntentRouterAgent
from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent
from agents.compliance_checker import ComplianceCheckerAgent
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)


# ─── Pydantic Model（Supervisor 结构化输出） ───

class SubTask(BaseModel):
    """Supervisor 拆解出的单个子任务"""
    id: str = Field(description="任务唯一标识，如 task_1, task_2")
    description: str = Field(description="任务的清晰描述，供下游 Agent 理解和执行")
    entities: dict[str, str] = Field(
        description="从用户消息中提取的关键实体，如订单号、产品名、金额",
        default_factory=dict,
    )


class TaskDependency(BaseModel):
    """子任务间的依赖关系"""
    from_task: str = Field(description="依赖的前置任务ID")
    to_task: str = Field(description="当前任务ID")
    condition: str = Field(description="执行条件，基于前置任务的结果判断，如'收益率 > 5%'")


class SupervisorOutput(BaseModel):
    """Supervisor 任务拆解的结构化输出"""
    sub_tasks: list[SubTask] = Field(description="拆解后的子任务列表")
    dependencies: list[TaskDependency] = Field(
        description="任务间的依赖关系。无依赖时为空列表",
        default_factory=list,
    )
    needs_parallel: bool = Field(
        description="子任务之间是否可以并行执行。无依赖时为 true，有依赖时为 false"
    )


# ─── 状态定义 ───

def merge_dict(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """合并字典"""
    return {**existing, **update}


def merge_list(existing: list[str], update: list[str]) -> list[str]:
    """合并列表，去重"""
    return list(set(existing + update))


class AgentState(TypedDict):
    """Supervisor编排的全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    intent: str
    sub_results: Annotated[dict[str, Any], merge_dict]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int
    _last_dispatched_agent: str

    # 子任务相关
    sub_tasks: list[dict[str, Any]]
    dependencies: list[dict[str, Any]]
    needs_parallel: bool
    dispatch_mode: str  # "chain" | "parallel" | "single"

    # 链路执行相关
    task_chains: Annotated[dict[str, list[str]], merge_dict]
    # task_id → Agent链路，如 {"task_1": ["knowledge_rag", "ticket_handler"]}
    current_sub_task_id: str
    # 当前正在执行的子任务 ID
    current_step_index: int
    # 当前执行到链路的第几步
    task_results: Annotated[dict[str, Any], merge_dict]
    # task_id → 执行结果
    completed_task_ids: Annotated[list[str], merge_list]
    # 已完成的子任务 ID 列表


# ─── Prompt 定义 ───

SUPERVISOR_DECOMPOSE_PROMPT = """你是一个智能客服系统的 Supervisor（主管编排Agent）。

你的职责是将用户消息拆解为独立的子任务。每个子任务应该是一个原子操作，可以被单独执行。

规则：
1. 从用户消息中识别所有独立的请求
2. 为每个子任务提取关键实体（订单号、产品名、金额等）
3. 判断子任务之间是否有依赖关系
4. 如果只有一个意图，返回单个子任务即可

依赖关系说明：
- 当一个任务的执行依赖于另一个任务的结果时，需要声明依赖
- 依赖必须附带条件，说明在什么情况下才执行后续任务
- 例如："查理财收益，超过5%就买" → task_2 依赖 task_1，条件: "收益率 > 5%"

金融场景示例：

用户: "帮我查订单TK-001，再看看理财产品A的收益"
→ sub_tasks: [
    {id: "task_1", description: "查询订单TK-001的当前状态", entities: {order_id: "TK-001"}},
    {id: "task_2", description: "查询理财产品A的年化收益率", entities: {product: "理财产品A"}}
  ]
→ dependencies: []
→ needs_parallel: true

用户: "查查理财产品A收益多少，超过5%就帮我买10万"
→ sub_tasks: [
    {id: "task_1", description: "查询理财产品A的年化收益率", entities: {product: "理财产品A"}},
    {id: "task_2", description: "购买理财产品A 10万元", entities: {product: "理财产品A", "amount": "100000"}}
  ]
→ dependencies: [
    {from_task: "task_1", to_task: "task_2", condition: "收益率 > 5%"}
  ]
→ needs_parallel: false

用户: "怎么退款"
→ sub_tasks: [
    {id: "task_1", description: "查询退款政策和流程", entities: {}}
  ]
→ dependencies: []
→ needs_parallel: false
"""

CONDITION_EVAL_PROMPT = """你是一个条件评估Agent。根据前置任务的执行结果，判断是否满足执行后续任务的条件。

前置任务结果: {from_result}
执行条件: {condition}

请判断条件是否满足。只返回 "true" 或 "false"。
"""


# ─── Supervisor节点 ───

class SupervisorNode:
    """Supervisor决策节点：任务拆解 + 结果汇总"""

    def __init__(self, llm: ChatOpenAI, working_memory: WorkingMemory):
        self.llm = llm
        self.llm_with_structure = llm.with_structured_output(SupervisorOutput)
        self.working_memory = working_memory

    @trace_agent_call("supervisor_decompose")
    async def decompose(self, state: AgentState) -> dict[str, Any]:
        """将用户消息拆解为子任务列表"""
        messages = state["messages"]
        session_id = state.get("session_id", "default")

        context = self.working_memory.get_context(session_id)

        prompt = [
            SystemMessage(content=SUPERVISOR_DECOMPOSE_PROMPT),
            SystemMessage(content=f"当前工作记忆上下文: {context}"),
            *messages,
        ]

        output: SupervisorOutput = await self.llm_with_structure.ainvoke(prompt)

        sub_tasks = [
            {
                "id": task.id,
                "description": task.description,
                "entities": task.entities,
            }
            for task in output.sub_tasks
        ]

        dependencies = [
            {
                "from_task": dep.from_task,
                "to_task": dep.to_task,
                "condition": dep.condition,
            }
            for dep in output.dependencies
        ]

        # 判断派发模式
        if dependencies:
            dispatch_mode = "dependent"
        else:
            dispatch_mode = "chain"

        self.working_memory.update(session_id, {
            "sub_tasks": sub_tasks,
            "dependencies": dependencies,
            "dispatch_mode": dispatch_mode,
        })

        logger.info(
            "Supervisor 拆解为 %d 个子任务, 模式=%s, 依赖=%d",
            len(sub_tasks), dispatch_mode, len(dependencies),
        )

        return {
            "intent": dispatch_mode,
            "current_agent": "supervisor",
            "sub_tasks": sub_tasks,
            "dependencies": dependencies,
            "needs_parallel": output.needs_parallel,
            "dispatch_mode": dispatch_mode,
            "task_chains": {},
            "task_results": {},
            "completed_task_ids": [],
            "current_sub_task_id": "",
            "current_step_index": 0,
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> dict[str, Any]:
        """汇总子Agent结果，生成最终回复"""
        task_results = state.get("task_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
                "工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            # 只使用汇总结果（task_id 为 key），跳过步骤级别结果（含 _step_）
            for key, result in task_results.items():
                if "_step_" in key:
                    continue
                if isinstance(result, str) and result.strip():
                    result_parts.append(result)
            final_response = "\n\n".join(result_parts) if result_parts else "抱歉，暂时无法处理您的请求，请稍后重试。"

        return {
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


# ─── 意图路由节点 ───

@trace_agent_call("intent_router_node")
async def intent_router_node(state: AgentState, llm: ChatOpenAI) -> dict[str, Any]:
    """对当前子任务进行意图分类，规划执行链路"""
    intent_router = IntentRouterAgent(llm)

    sub_tasks = state.get("sub_tasks", [])
    completed_ids = set(state.get("completed_task_ids", []))
    task_chains = dict(state.get("task_chains", {}))
    dependencies = state.get("dependencies", [])

    # 找到下一个未完成的子任务
    next_task = None
    for task in sub_tasks:
        if task["id"] not in completed_ids:
            # 检查依赖是否满足
            if _check_dependencies_met(task["id"], dependencies, completed_ids):
                next_task = task
                break

    if next_task is None:
        # 所有任务已完成
        return {
            "current_sub_task_id": "",
            "current_step_index": 0,
        }

    task_id = next_task["id"]
    description = next_task["description"]

    # 意图分类 + 链路规划
    intent_result = await intent_router.classify(description)
    chain = intent_result.agent_chain

    task_chains[task_id] = chain

    logger.info("子任务 %s 链路规划: %s → %s", task_id, description[:30], chain)

    return {
        "current_sub_task_id": task_id,
        "current_step_index": 0,
        "task_chains": task_chains,
        "intent": intent_result.primary_intent,
    }


def _check_dependencies_met(task_id: str, dependencies: list[dict], completed_ids: set[str]) -> bool:
    """检查任务的所有依赖是否已满足"""
    for dep in dependencies:
        if dep["to_task"] == task_id:
            if dep["from_task"] not in completed_ids:
                return False
    return True


# ─── 链路执行引擎 ───

async def dispatch_step(state: AgentState, llm: ChatOpenAI) -> list[Send]:
    """执行链路中的当前步骤"""
    task_chains = state.get("task_chains", {})
    current_task_id = state.get("current_sub_task_id", "")
    current_step = state.get("current_step_index", 0)

    if not current_task_id or current_task_id not in task_chains:
        return [Send("compliance_check", state)]

    chain = task_chains[current_task_id]

    if current_step >= len(chain):
        return [Send("compliance_check", state)]

    agent_name = chain[current_step]

    # 构建上下文：用户消息 + 前面步骤的结果
    context = _build_step_context(state, current_task_id, current_step)

    logger.info("执行步骤: %s [%d/%d] → %s", current_task_id, current_step + 1, len(chain), agent_name)

    return [Send(agent_name, {
        "messages": [HumanMessage(content=context)],
        "current_agent": agent_name,
        "_last_dispatched_agent": agent_name,
    })]


def _build_step_context(state: AgentState, task_id: str, step_index: int) -> str:
    """构建当前步骤的上下文：原始子任务描述 + 前面步骤的结果"""
    sub_tasks = state.get("sub_tasks", [])
    task_results = state.get("task_results", {})
    task_chains = state.get("task_chains", {})

    # 找到当前子任务的描述
    task_desc = ""
    for task in sub_tasks:
        if task["id"] == task_id:
            task_desc = task["description"]
            break

    # 收集前面步骤的结果
    chain = task_chains.get(task_id, [])
    prev_results = []
    for i in range(step_index):
        step_key = f"{task_id}_step_{i}"
        if step_key in task_results:
            prev_results.append(f"[{chain[i]}]: {task_results[step_key]}")

    context = f"子任务: {task_desc}"
    if prev_results:
        context += "\n\n前面步骤的结果:\n" + "\n".join(prev_results)

    return context


def collect_step(state: AgentState) -> dict[str, Any]:
    """收集当前步骤的结果，推进步骤索引"""
    current_task_id = state.get("current_sub_task_id", "")
    current_step = state.get("current_step_index", 0)
    task_chains = state.get("task_chains", {})
    sub_results = state.get("sub_results", {})

    if not current_task_id:
        logger.warning("[collect_step] current_sub_task_id 为空，跳过")
        return {}

    logger.info("[collect_step] 收集 %s 步骤 %d 的结果", current_task_id, current_step)

    # 精确提取当前 Agent 的结果（避免累积的 sub_results 干扰）
    last_agent = state.get("_last_dispatched_agent", "")
    agent_result = ""
    logger.info("[collect_step] last_agent=%s, sub_results keys=%s", last_agent, list(sub_results.keys()))
    if last_agent and last_agent in sub_results:
        agent_result = sub_results[last_agent]
        logger.info("[collect_step] 从 %s 提取结果: %s", last_agent, agent_result[:50])
    else:
        # 兜底：取第一个 string 类型的值
        for key, value in sub_results.items():
            if isinstance(value, str) and value.strip():
                agent_result = value
                logger.info("[collect_step] 兜底从 %s 提取结果: %s", key, agent_result[:50])
                break

    chain = task_chains.get(current_task_id, [])
    step_key = f"{current_task_id}_step_{current_step}"

    result = {
        "task_results": {step_key: agent_result},
        "current_step_index": current_step + 1,
    }

    # 如果是链路的最后一步，标记子任务完成
    if current_step + 1 >= len(chain):
        # 汇总该子任务所有步骤的结果
        all_step_results = []
        for i in range(len(chain)):
            k = f"{current_task_id}_step_{i}"
            if k in state.get("task_results", {}):
                all_step_results.append(state["task_results"][k])
            elif i == current_step:
                all_step_results.append(agent_result)

        final_result = "\n".join(all_step_results) if all_step_results else agent_result
        result["task_results"][current_task_id] = final_result
        result["completed_task_ids"] = [current_task_id]

    return result


def check_more_steps(state: AgentState) -> str:
    """检查是否还有更多步骤或子任务需要执行"""
    task_chains = state.get("task_chains", {})
    current_task_id = state.get("current_sub_task_id", "")
    current_step = state.get("current_step_index", 0)
    sub_tasks = state.get("sub_tasks", [])
    completed_ids = set(state.get("completed_task_ids", []))
    dependencies = state.get("dependencies", [])

    logger.info(
        "[check_more_steps] current_task=%s, step=%d, completed=%s, sub_tasks=%s",
        current_task_id, current_step, completed_ids, [t["id"] for t in sub_tasks],
    )

    # 当前链路还有步骤
    if current_task_id and current_task_id in task_chains:
        chain = task_chains[current_task_id]
        if current_step < len(chain):
            logger.info("[check_more_steps] → dispatch_step (链路还有步骤)")
            return "dispatch_step"

    # 还有子任务未执行
    for task in sub_tasks:
        if task["id"] not in completed_ids:
            if _check_dependencies_met(task["id"], dependencies, completed_ids):
                logger.info("[check_more_steps] → intent_router (还有子任务 %s)", task["id"])
                return "intent_router"

    # 检查依赖模式下是否有任务需要条件评估
    if dependencies:
        for task in sub_tasks:
            if task["id"] not in completed_ids:
                logger.info("子任务 %s 依赖未满足，跳过", task["id"])

    # 全部完成
    return "compliance_check"


async def evaluate_condition(llm: ChatOpenAI, from_result: str, condition: str) -> bool:
    """用 LLM 判断前置任务结果是否满足执行条件"""
    messages = [
        SystemMessage(content=CONDITION_EVAL_PROMPT.format(
            from_result=from_result, condition=condition,
        )),
    ]

    response = await llm.ainvoke(messages)
    result = response.content.strip().lower()
    return result == "true"


# ─── 构建Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    short_term_memory: ShortTermMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    enable_checkpointing: bool = True,
) -> StateGraph:
    """
    构建Supervisor编排的多Agent StateGraph。

    流程: supervisor_decompose → intent_router → dispatch_step → [Agent] → collect_step → check_more_steps → loop or compliance_check → synthesize

    Args:
        llm: 语言模型实例
        working_memory: 工作记忆
        short_term_memory: 短期记忆
        long_term_memory: 长期记忆
        enable_checkpointing: 是否启用检查点（支持断点恢复）
    """
    if llm is None:
        llm = ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-4o"), temperature=0)
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory)

    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory)
    ticket_agent = TicketHandlerAgent(llm)
    compliance_agent = ComplianceCheckerAgent(llm)

    graph = StateGraph(AgentState)

    # 添加节点
    graph.add_node("supervisor_decompose", supervisor.decompose)
    async def intent_router_edge(state: AgentState) -> dict[str, Any]:
        return await intent_router_node(state, llm)

    graph.add_node("intent_router", intent_router_edge)
    graph.add_node("dispatch_step", lambda state: {})  # 占位，实际由条件边派发
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("collect_step", collect_step)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)

    # 入口
    graph.set_entry_point("supervisor_decompose")

    # supervisor_decompose → intent_router
    graph.add_edge("supervisor_decompose", "intent_router")

    # intent_router → dispatch_step
    graph.add_edge("intent_router", "dispatch_step")

    # dispatch_step → 派发到对应 Agent
    async def dispatch_step_edge(state: AgentState) -> list[Send]:
        return await dispatch_step(state, llm)

    graph.add_conditional_edges(
        "dispatch_step",
        dispatch_step_edge,
    )

    # 业务 Agent → collect_step（继续循环）
    graph.add_edge("knowledge_rag", "collect_step")
    graph.add_edge("ticket_handler", "collect_step")

    # collect_step → check_more_steps
    graph.add_conditional_edges(
        "collect_step",
        check_more_steps,
        {
            "dispatch_step": "dispatch_step",
            "intent_router": "intent_router",
            "compliance_check": "compliance_check",
        },
    )

    # compliance_check → synthesize → END
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None

    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
