"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，拆解为子任务，通过Intent Router路由到对应Agent并行/串行执行，汇总结果返回。
采用LangGraph StateGraph + Send()实现并行扇出。
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


class SupervisorOutput(BaseModel):
    """Supervisor 任务拆解的结构化输出"""
    sub_tasks: list[SubTask] = Field(description="拆解后的子任务列表")
    needs_parallel: bool = Field(
        description="子任务之间是否可以并行执行。无依赖时为 true，有条件依赖时为 false"
    )


# ─── 状态定义 ───

def merge_sub_results(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """合并并行 Agent 的子结果"""
    return {**existing, **update}


class AgentState(TypedDict):
    """Supervisor编排的全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    intent: str
    sub_results: Annotated[dict[str, Any], merge_sub_results]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int
    sub_tasks: list[dict[str, Any]]
    needs_parallel: bool


# ─── Prompt 定义 ───

SUPERVISOR_DECOMPOSE_PROMPT = """你是一个智能客服系统的 Supervisor（主管编排Agent）。

你的职责是将用户消息拆解为独立的子任务。每个子任务应该是一个原子操作，可以被单独执行。

规则：
1. 从用户消息中识别所有独立的请求
2. 为每个子任务提取关键实体（订单号、产品名、金额等）
3. 判断子任务之间是否有依赖关系，决定是否可以并行
4. 如果只有一个意图，返回单个子任务即可

金融场景示例：
用户: "帮我查订单TK-001，再看看理财产品A的收益"
→ sub_tasks: [
    {id: "task_1", description: "查询订单TK-001的当前状态", entities: {order_id: "TK-001"}},
    {id: "task_2", description: "查询理财产品A的年化收益率", entities: {product: "理财产品A"}}
  ]
→ needs_parallel: true

用户: "理财产品A收益多少"
→ sub_tasks: [
    {id: "task_1", description: "查询理财产品A的收益率", entities: {product: "理财产品A"}}
  ]
→ needs_parallel: false

用户: "怎么退款"
→ sub_tasks: [
    {id: "task_1", description: "查询退款政策和流程", entities: {}}
  ]
→ needs_parallel: false
"""

INTENT_ROUTER_PROMPT = """你是一个意图识别Agent，负责为每个子任务选择最合适的目标Agent。

可用的Agent：
- knowledge_rag: 知识库检索和回答（产品咨询、政策查询、流程了解）
- ticket_handler: 工单创建和查询（退款、理赔、开户、订单查询）
- compliance_checker: 合规审查（资金安全、账户异常、欺诈举报）

金融场景规则：
- 涉及退款、理赔、开户、订单查询 → ticket_handler
- 涉及产品咨询、利率查询、政策了解 → knowledge_rag
- 涉及资金安全、账户异常、欺诈举报 → compliance_checker

只返回Agent名称，不要其他内容。
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

        self.working_memory.update(session_id, {
            "sub_tasks": sub_tasks,
            "needs_parallel": output.needs_parallel,
        })

        logger.info("Supervisor 拆解为 %d 个子任务, 并行=%s", len(sub_tasks), output.needs_parallel)

        return {
            "intent": "parallel" if output.needs_parallel else "single",
            "current_agent": "supervisor",
            "sub_tasks": sub_tasks,
            "needs_parallel": output.needs_parallel,
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> dict[str, Any]:
        """汇总子Agent结果，生成最终回复"""
        sub_results = state.get("sub_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
                "工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            for agent_name, result in sub_results.items():
                if agent_name == "compliance":
                    continue
                if isinstance(result, str) and result.strip():
                    result_parts.append(result)
            final_response = "\n\n".join(result_parts) if result_parts else "抱歉，暂时无法处理您的请求，请稍后重试。"

        return {
            "final_response": final_response,
            "messages": [AIMessage(content=final_response)],
        }


# ─── Intent Router 路由 ───

async def classify_subtask(llm: ChatOpenAI, subtask_description: str) -> str:
    """对单个子任务调用 Intent Router，返回目标 Agent 名称"""
    messages = [
        SystemMessage(content=INTENT_ROUTER_PROMPT),
        HumanMessage(content=f"子任务: {subtask_description}"),
    ]

    response = await llm.ainvoke(messages)
    agent_name = response.content.strip().lower()

    valid_agents = {"knowledge_rag", "ticket_handler", "compliance_checker"}
    if agent_name not in valid_agents:
        agent_name = "knowledge_rag"

    return agent_name


# ─── 派发函数 ───

async def dispatch(state: AgentState, llm: ChatOpenAI) -> list[Send]:
    """对每个子任务调用 Intent Router 选择 Agent，然后用 Send() 并行派发"""
    sub_tasks = state.get("sub_tasks", [])

    if not sub_tasks:
        return [Send("knowledge_rag", state)]

    dispatch_plan: dict[str, list[str]] = {}
    for task in sub_tasks:
        agent_name = await classify_subtask(llm, task["description"])
        dispatch_plan.setdefault(agent_name, []).append(task["id"])
        logger.info("子任务 %s → %s", task["id"], agent_name)

    sends = []
    for agent_name, task_ids in dispatch_plan.items():
        task_descriptions = [
            t["description"] for t in sub_tasks if t["id"] in task_ids
        ]
        combined_description = "\n".join(f"- {d}" for d in task_descriptions)

        sends.append(Send(agent_name, {
            "messages": [HumanMessage(content=combined_description)],
            "current_agent": agent_name,
        }))

    return sends


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

    流程: supervisor_decompose → dispatch(Send并行) → [Agent执行] → compliance_check → synthesize

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
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)

    # 入口
    graph.set_entry_point("supervisor_decompose")

    # supervisor_decompose → Send() 并行派发到各 Agent
    async def dispatch_edge(state: AgentState) -> list[Send]:
        return await dispatch(state, llm)

    graph.add_conditional_edges(
        "supervisor_decompose",
        dispatch_edge,
    )

    # 所有 Agent 汇入合规检查
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None

    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
