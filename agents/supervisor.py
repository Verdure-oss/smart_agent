"""
Supervisor编排Agent — 中央协调者
负责接收用户请求，拆解为子任务，通过Intent Router路由到对应Agent并行/串行执行，汇总结果返回。
支持子任务间的依赖关系和条件执行。
采用LangGraph StateGraph + Send()实现并行扇出和循环调度。
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
    dependencies: list[dict[str, Any]]
    needs_parallel: bool
    task_results: Annotated[dict[str, Any], merge_sub_results]
    completed_task_ids: Annotated[list[str], merge_completed_ids]
    dispatch_mode: str  # "parallel" | "dependent" | "single"


def merge_completed_ids(existing: list[str], update: list[str]) -> list[str]:
    """合并已完成任务ID列表，去重"""
    return list(set(existing + update))


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
        elif output.needs_parallel and len(sub_tasks) > 1:
            dispatch_mode = "parallel"
        else:
            dispatch_mode = "single"

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
            "task_results": {},
            "completed_task_ids": [],
        }

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> dict[str, Any]:
        """汇总子Agent结果，生成最终回复"""
        sub_results = state.get("sub_results", {})
        task_results = state.get("task_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，您的请求涉及敏感内容，已转交人工客服处理。"
                "工单编号已自动生成，请留意后续通知。"
            )
        else:
            result_parts = []
            # 优先使用 task_results（依赖模式下的结果）
            results_to_merge = task_results if task_results else sub_results
            for agent_name, result in results_to_merge.items():
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


# ─── 依赖调度函数 ───

def get_ready_tasks(state: AgentState) -> list[dict[str, Any]]:
    """获取当前可以执行的任务（依赖已满足的任务）"""
    sub_tasks = state.get("sub_tasks", [])
    dependencies = state.get("dependencies", [])
    completed_ids = set(state.get("completed_task_ids", []))
    task_results = state.get("task_results", {})

    # 构建依赖映射: to_task → [(from_task, condition), ...]
    dep_map: dict[str, list[tuple[str, str]]] = {}
    for dep in dependencies:
        dep_map.setdefault(dep["to_task"], []).append(
            (dep["from_task"], dep["condition"])
        )

    ready = []
    for task in sub_tasks:
        task_id = task["id"]

        # 已完成的跳过
        if task_id in completed_ids:
            continue

        # 没有依赖，直接就绪
        if task_id not in dep_map:
            ready.append(task)
            continue

        # 检查所有依赖是否满足
        all_deps_met = True
        for from_task, condition in dep_map[task_id]:
            if from_task not in completed_ids:
                all_deps_met = False
                break
            # 依赖已完成，但需要标记条件（条件评估在 dispatch 中进行）
            task["_pending_condition"] = {
                "from_task": from_task,
                "from_result": task_results.get(from_task, ""),
                "condition": condition,
            }

        if all_deps_met:
            ready.append(task)

    return ready


async def dispatch_ready_tasks(state: AgentState, llm: ChatOpenAI) -> list[Send]:
    """派发当前就绪的任务"""
    ready_tasks = get_ready_tasks(state)

    if not ready_tasks:
        return []

    sends = []
    for task in ready_tasks:
        # 检查是否有待评估的条件
        pending_condition = task.get("_pending_condition")
        if pending_condition:
            condition_met = await evaluate_condition(
                llm,
                pending_condition["from_result"],
                pending_condition["condition"],
            )
            if not condition_met:
                logger.info("子任务 %s 条件不满足，跳过: %s", task["id"], pending_condition["condition"])
                # 标记为已完成（跳过），结果为空
                sends.append(Send("collect_results", {
                    "task_results": {task["id"]: f"[条件不满足，已跳过: {pending_condition['condition']}]"},
                    "completed_task_ids": [task["id"]],
                }))
                continue

        # 路由到目标 Agent
        agent_name = await classify_subtask(llm, task["description"])
        logger.info("子任务 %s → %s", task["id"], agent_name)

        # 将任务ID和依赖结果注入到消息中
        task_context = f"[任务ID: {task['id']}] {task['description']}"

        sends.append(Send(agent_name, {
            "messages": [HumanMessage(content=task_context)],
            "current_agent": agent_name,
            "_current_task_id": task["id"],
        }))

    return sends


def collect_results(state: AgentState) -> dict[str, Any]:
    """收集任务执行结果，标记已完成"""
    # 这个节点主要起汇聚作用
    # task_results 和 completed_task_ids 通过 reducer 自动合并
    return {}


def check_more_tasks(state: AgentState) -> str:
    """检查是否还有更多任务需要执行"""
    sub_tasks = state.get("sub_tasks", [])
    completed_ids = set(state.get("completed_task_ids", []))

    all_done = all(t["id"] in completed_ids for t in sub_tasks)

    if all_done:
        return "compliance_check"
    else:
        return "dispatch_ready_tasks"


# ─── Agent 包装函数（支持依赖模式下的 task_id 结果存储） ───

def make_agent_wrapper(agent_process_fn):
    """包装 Agent 的 process 方法，在依赖模式下将结果存入 task_results[task_id]"""

    async def wrapped_process(state: dict[str, Any]) -> dict[str, Any]:
        task_id = state.get("_current_task_id")
        result = await agent_process_fn(state)

        if task_id:
            # 从 sub_results 中提取该 Agent 的结果
            agent_result = None
            for key, value in result.get("sub_results", {}).items():
                if isinstance(value, str) and value.strip():
                    agent_result = value
                    break

            if agent_result:
                return {
                    **result,
                    "task_results": {task_id: agent_result},
                    "completed_task_ids": [task_id],
                }

        return result

    return wrapped_process


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

    流程:
    - 无依赖: supervisor_decompose → dispatch → [Agent] → compliance_check → synthesize
    - 有依赖: supervisor_decompose → dispatch_ready_tasks → [Agent] → collect → check → loop or compliance_check

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

    # 添加节点（Agent 节点使用包装函数，支持依赖模式下的 task_id 结果存储）
    graph.add_node("supervisor_decompose", supervisor.decompose)
    graph.add_node("knowledge_rag", make_agent_wrapper(knowledge_agent.process))
    graph.add_node("ticket_handler", make_agent_wrapper(ticket_agent.process))
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)

    # 依赖调度相关节点
    graph.add_node("dispatch_ready_tasks", lambda state: {})  # 占位，实际由条件边派发
    graph.add_node("collect_results", collect_results)

    # 入口
    graph.set_entry_point("supervisor_decompose")

    # supervisor_decompose → 根据模式选择派发方式
    async def decompose_edge(state: AgentState) -> list[Send]:
        dispatch_mode = state.get("dispatch_mode", "parallel")

        if dispatch_mode == "dependent":
            # 有依赖模式：进入循环调度
            return [Send("dispatch_ready_tasks", state)]
        else:
            # 无依赖模式：一次派发所有任务
            return await dispatch(state, llm)

    graph.add_conditional_edges(
        "supervisor_decompose",
        decompose_edge,
    )

    # dispatch_ready_tasks → 派发就绪任务
    async def dispatch_ready_edge(state: AgentState) -> list[Send]:
        return await dispatch_ready_tasks(state, llm)

    graph.add_conditional_edges(
        "dispatch_ready_tasks",
        dispatch_ready_edge,
    )

    # 所有 Agent 汇入 collect_results（依赖模式）或 compliance_check（无依赖模式）
    def agent_next_node(state: AgentState) -> str:
        if state.get("dispatch_mode") == "dependent":
            return "collect_results"
        return "compliance_check"

    graph.add_conditional_edges("knowledge_rag", agent_next_node)
    graph.add_conditional_edges("ticket_handler", agent_next_node)

    # collect_results → check_more_tasks
    graph.add_conditional_edges(
        "collect_results",
        check_more_tasks,
        {
            "dispatch_ready_tasks": "dispatch_ready_tasks",
            "compliance_check": "compliance_check",
        },
    )

    # 合规 → 汇总 → 结束
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    checkpointer = MemorySaver() if enable_checkpointing else None

    compiled = graph.compile(checkpointer=checkpointer)

    return compiled
