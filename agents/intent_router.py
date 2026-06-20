"""
意图路由Agent — 用户意图识别与分类 + 执行链路规划
负责分析子任务，识别业务意图，规划执行链路（Agent调用顺序）。
支持多级意图分类和链路规划。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from tracing.otel_config import trace_agent_call

logger = logging.getLogger(__name__)


# ─── Pydantic Model ───

class IntentRouterOutput(BaseModel):
    """意图路由的结构化输出"""
    primary_intent: str = Field(
        description="一级意图: consultation(咨询), complaint(投诉), transaction(交易办理), account(账户), compliance(合规)"
    )
    secondary_intent: str = Field(
        description="二级意图: product_inquiry, refund, order_query, account_open, ..."
    )
    confidence: float = Field(description="置信度 0.0-1.0")
    entities: dict[str, str] = Field(
        description="提取的实体，如订单号、产品名、金额",
        default_factory=dict,
    )
    agent_chain: list[str] = Field(
        description="执行链路，按顺序排列的Agent列表。"
                    "可用Agent: knowledge_rag, ticket_handler, compliance_checker"
    )


# ─── 字段映射（兼容不同 LLM 的输出格式） ───

FIELD_MAPPING = {
    # primary_intent 的可能字段名
    "primary_intent": ["primary_intent", "intent_l1", "primary", "intent"],
    # secondary_intent 的可能字段名
    "secondary_intent": ["secondary_intent", "intent_l2", "secondary", "sub_intent"],
    # confidence 的可能字段名
    "confidence": ["confidence", "conf", "score"],
    # entities 的可能字段名
    "entities": ["entities", "entity", "extracted_entities"],
    # agent_chain 的可能字段名
    "agent_chain": ["agent_chain", "chain", "route", "agents", "pipeline"],
}


def _extract_field(data: dict, field_name: str, default: Any = None) -> Any:
    """从 LLM 返回的 dict 中提取字段，支持多种字段名"""
    candidates = FIELD_MAPPING.get(field_name, [field_name])
    for key in candidates:
        if key in data:
            return data[key]
    return default


# ─── Prompt ───

INTENT_ROUTER_PROMPT = """你是一个专业的意图路由Agent，负责分析子任务并规划执行链路。

你的职责：
1. 识别子任务的一级意图和二级意图
2. 提取关键实体（订单号、产品名、金额等）
3. 规划执行链路 — 决定需要哪些Agent按什么顺序处理

可用的Agent（注意：compliance_checker 不在链路中，系统会自动进行合规审查）：
- knowledge_rag: 知识库检索（产品咨询、政策查询、流程了解）— 只用于"查信息"
- ticket_handler: 工单处理（订单查询、退款执行、理赔、开户）— 用于"查工单/订单"或"执行操作"

链路规划规则（非常重要，请严格遵守）：

A. 纯信息查询（只需要 knowledge_rag）：
   - "收益多少"、"怎么退款"、"开户流程"、"退款政策"
   - 用户问"怎么"、"什么"、"多少"等信息类问题

B. 纯工单/订单操作（只需要 ticket_handler）：
   - "查订单TK-001"、"工单状态"、"订单到哪了"
   - 用户提供了订单号/工单号并要求查询

C. 执行操作（需要 knowledge_rag + ticket_handler）：
   - "帮我退款"、"帮我开户"、"帮我理赔"
   - 用户要求"帮我"执行某个操作
   - 子任务描述包含"执行"、"操作"、"办理"等动词

D. 涉及资金安全（只需要 ticket_handler）：
   - "账户被盗"、"异常交易"、"欺诈"

E. 投诉类（只需要 ticket_handler）：
   - "投诉"、"服务态度"

F. 对话历史已包含相关信息（跳过 knowledge_rag）：
   - 如果对话历史中已经包含了执行该任务所需的信息（如退款政策、产品信息等）
   - 则只需要 [ticket_handler]，不需要重复检索
   - 例如：之前已经查过退款政策，现在用户补充订单号 → 只需要 [ticket_handler]

关键判断（必须严格遵守）：
- 如果子任务描述包含"执行"、"操作"、"办理"、"帮我" → 必须是 [knowledge_rag, ticket_handler]
- 如果子任务描述是纯信息查询 → 只需要 [knowledge_rag]
- 如果子任务描述是查询订单/工单 → 只需要 [ticket_handler]
- 如果对话历史已包含相关信息 → 只需要 [ticket_handler]

请以JSON格式返回，字段名必须为: primary_intent, secondary_intent, confidence, entities, agent_chain

示例：
{
    "primary_intent": "consultation",
    "secondary_intent": "product_inquiry",
    "confidence": 0.95,
    "entities": {"product": "理财产品A"},
    "agent_chain": ["knowledge_rag"]
}
"""


# ─── IntentRouterAgent ───

class IntentRouterAgent:
    """意图路由Agent — 分析意图 + 规划执行链路"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    @trace_agent_call("intent_router")
    async def classify(self, subtask_description: str, context: str = "") -> IntentRouterOutput:
        """对子任务进行意图分类并规划执行链路"""
        messages = [
            SystemMessage(content=INTENT_ROUTER_PROMPT),
        ]

        if context:
            messages.append(SystemMessage(content=f"上下文信息: {context}"))

        messages.append(HumanMessage(content=f"子任务: {subtask_description}"))

        response = await self.llm.ainvoke(messages)

        # 解析 JSON，兼容不同 LLM 的输出格式
        try:
            raw = json.loads(response.content)
        except json.JSONDecodeError:
            raw = {}

        # 用字段映射提取值
        primary_intent = _extract_field(raw, "primary_intent", "unknown")
        secondary_intent = _extract_field(raw, "secondary_intent", "unknown")
        confidence = _extract_field(raw, "confidence", 0.0)
        entities = _extract_field(raw, "entities", {})
        agent_chain = _extract_field(raw, "agent_chain", ["knowledge_rag"])

        # 校验 agent_chain 中的 Agent 名称
        valid_agents = {"knowledge_rag", "ticket_handler", "compliance_checker"}
        agent_chain = [a for a in agent_chain if a in valid_agents]

        # 兜底：如果链路为空，默认使用 knowledge_rag
        if not agent_chain:
            agent_chain = ["knowledge_rag"]

        output = IntentRouterOutput(
            primary_intent=str(primary_intent),
            secondary_intent=str(secondary_intent),
            confidence=float(confidence) if confidence else 0.0,
            entities=entities if isinstance(entities, dict) else {},
            agent_chain=agent_chain,
        )

        logger.info(
            "意图路由: %s → %s, 链路=%s, 置信度=%.2f",
            subtask_description[:30], output.primary_intent,
            output.agent_chain, output.confidence,
        )

        return output

    @trace_agent_call("intent_router_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        if not messages:
            return state

        last_message = messages[-1].content if messages else ""
        intent_result = await self.classify(last_message)

        return {
            "intent": intent_result.primary_intent,
            "sub_results": {
                **state.get("sub_results", {}),
                "intent_router": {
                    "primary": intent_result.primary_intent,
                    "secondary": intent_result.secondary_intent,
                    "confidence": intent_result.confidence,
                    "entities": intent_result.entities,
                    "agent_chain": intent_result.agent_chain,
                },
            },
        }
