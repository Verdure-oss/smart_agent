"""
工单处理Agent — 工单CRUD与流转
负责创建、查询、更新工单，对接工单系统，处理退款/理赔/开户等业务办理类需求。
使用SQLite数据库持久化工单数据。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call
from database import db

logger = logging.getLogger(__name__)


class TicketStatus(str, Enum):
    CREATED = "created"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    RESOLVED = "resolved"
    CLOSED = "closed"
    ESCALATED = "escalated"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


TICKET_SYSTEM_PROMPT = """你是一个专业的工单处理Agent，负责处理客户的业务办理请求。

你的职责：
1. 分析用户需求，判断是否需要创建工单
2. 提取工单关键信息（类型、优先级、描述、订单号）
3. 创建工单并返回工单号
4. 查询现有工单状态
5. 如果缺少必要信息，提醒用户补充

工单类型：
- refund: 退款申请（需要订单号）
- claim: 理赔申请（需要订单号）
- account_open: 开户申请（需要身份证号）
- account_change: 账户变更（需要账户号）
- complaint: 投诉工单
- general: 通用工单

优先级判断规则：
- urgent: 资金安全、账户被盗
- high: 退款超时、理赔争议
- medium: 常规业务办理
- low: 信息咨询类

重要规则：
- 当用户意图是查询已有工单状态时，action 必须返回 "query"
- 当 action = "query" 时，必须返回 ticket_id 字段，并从用户原消息中原样提取工单号
- 如果用户未提供明确工单号，不要伪造 ticket_id
- 不要把"查询工单"误判为"创建工单"
- 如果用户提供了订单号（如ORD-xxx），必须提取到order_id字段

缺少信息处理规则（非常重要）：
- 退款申请(refund)需要订单号：如果用户没有提供订单号，action 返回 "need_info"
- 理赔申请(claim)需要订单号：如果用户没有提供订单号，action 返回 "need_info"
- 开户申请(account_open)需要身份证号：如果用户没有提供，action 返回 "need_info"
- 当 action = "need_info" 时，message 字段说明需要补充什么信息

请以JSON格式返回：
{
    "action": "create|query|update|need_info",
    "ticket_id": "工单号；query/update 场景必须返回，没有则为空字符串",
    "order_id": "订单号；如果用户提供了订单号则提取，没有则为空字符串",
    "ticket_type": "refund|claim|account_open|...",
    "priority": "low|medium|high|urgent",
    "summary": "工单摘要",
    "details": "详细描述",
    "message": "需要用户补充的信息（仅 need_info 时返回）"
}
"""


class TicketHandlerAgent:
    """工单处理Agent（通过MCP工具调用数据库）"""

    def __init__(self, llm: ChatOpenAI, mcp_server=None):
        self.llm = llm
        self.mcp_server = mcp_server

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict:
        """分析用户需求，提取工单信息"""
        messages = [
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            HumanMessage(content=f"用户消息: {user_message}"),
        ]

        response = await self.llm.ainvoke(messages)

        import json
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return {
                "action": "create",
                "ticket_type": "general",
                "priority": "medium",
                "summary": user_message[:100],
                "details": user_message,
            }

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict, user_id: str) -> str:
        """创建工单（通过MCP工具调用）"""
        if self.mcp_server:
            logger.info("[工单] 调用MCP工具 ticket_create")
            result = await self.mcp_server.call_tool("ticket_create", {
                "user_id": user_id,
                "ticket_type": ticket_info.get("ticket_type", "general"),
                "summary": ticket_info.get("summary", ""),
                "details": ticket_info.get("details", ""),
                "priority": ticket_info.get("priority", "medium"),
            })
            if result.success:
                ticket = result.result
                logger.info("[工单] MCP工具创建成功: %s", ticket.get("ticket_id"))
            else:
                logger.error("[工单] MCP工具调用失败: %s", result.error)
                return f"工单创建失败: {result.error}"
        else:
            # 兜底：直接调用数据库
            logger.info("[工单] 使用数据库直接创建")
            ticket = db.create_ticket(
                user_id=user_id,
                ticket_type=ticket_info.get("ticket_type", "general"),
                priority=ticket_info.get("priority", "medium"),
                summary=ticket_info.get("summary", ""),
                details=ticket_info.get("details", ""),
            )

        priority_label = {
            "low": "普通", "medium": "中等", "high": "高", "urgent": "紧急"
        }.get(ticket["priority"], "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    @trace_agent_call("query_user_orders")
    async def query_user_orders(self, user_id: str) -> str:
        """查询用户的订单列表（退款前查询）"""
        if self.mcp_server:
            logger.info("[工单] 调用MCP工具 order_query 查询用户订单: %s", user_id)
            result = await self.mcp_server.call_tool("order_query", {
                "user_id": user_id,
            })
            if result.success and "orders" in result.result:
                orders = result.result["orders"]
                if not orders:
                    return ""

                order_list = []
                for i, order in enumerate(orders, 1):
                    status_label = {
                        "pending": "待处理",
                        "completed": "已完成",
                        "cancelled": "已取消",
                    }.get(order["status"], order["status"])
                    order_list.append(
                        f"{i}. 📋 订单号: {order['order_id']}\n"
                        f"   📦 产品: {order['product']}\n"
                        f"   💰 金额: {order['amount']}\n"
                        f"   📊 状态: {status_label}"
                    )

                return (
                    f"查询到您有 {len(orders)} 笔订单：\n\n"
                    + "\n\n".join(order_list)
                    + "\n\n请提供您要退款的订单号。"
                )

        # 兜底：从数据库查询
        orders = db.query_orders_by_user(user_id)
        if not orders:
            return ""

        order_list = []
        for i, order in enumerate(orders, 1):
            status_label = {
                "pending": "待处理",
                "completed": "已完成",
                "cancelled": "已取消",
            }.get(order["status"], order["status"])
            order_list.append(
                f"{i}. 📋 订单号: {order['order_id']}\n"
                f"   📦 产品: {order['product']}\n"
                f"   💰 金额: {order['amount']}\n"
                f"   📊 状态: {status_label}"
            )

        return (
            f"查询到您有 {len(orders)} 笔订单：\n\n"
            + "\n\n".join(order_list)
            + "\n\n请提供您要退款的订单号。"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str) -> str:
        """查询工单状态（通过MCP工具查询）"""
        if self.mcp_server:
            logger.info("[工单] 调用MCP工具 order_query: %s", ticket_id)
            result = await self.mcp_server.call_tool("order_query", {
                "order_id": ticket_id,
            })
            if result.success and "error" not in result.result:
                order = result.result
                logger.info("[工单] MCP工具查询成功: %s", order.get("order_id"))
                return (
                    f"订单查询结果：\n\n"
                    f"📋 订单号: {order['order_id']}\n"
                    f"📊 状态: {order['status']}\n"
                    f"💰 金额: {order['amount']}\n"
                    f"📦 产品: {order['product']}\n"
                    f"🕐 创建时间: {order['created_at']}"
                )
            else:
                logger.warning("[工单] MCP工具查询失败或未找到: %s", result.error)

        # 兜底：从数据库查询工单
        logger.info("[工单] 使用数据库直接查询: %s", ticket_id)
        ticket = db.query_ticket(ticket_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"

        status_label = {
            "created": "已创建",
            "processing": "处理中",
            "pending_review": "待审核",
            "resolved": "已解决",
            "closed": "已关闭",
            "escalated": "已升级",
        }.get(ticket["status"], ticket["status"])

        return (
            f"工单查询结果：\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📊 状态: {status_label}\n"
            f"📝 类型: {ticket['type']}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n"
            f"🔄 更新时间: {ticket['updated_at']}"
        )

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为Graph节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return state

        last_message = messages[-1].content
        ticket_info = await self.analyze_request(last_message)

        action = ticket_info.get("action", "create")
        ticket_type = ticket_info.get("ticket_type", "")

        # 判断是否是退款/理赔相关的请求
        is_refund_or_claim = (
            ticket_type in ("refund", "claim") or
            "退款" in last_message or
            "理赔" in last_message
        )

        # 提取订单号（从LLM返回或从消息中提取）
        order_id = ticket_info.get("order_id", "")
        if not order_id:
            # 从消息中提取订单号（ORD-xxx格式）
            import re
            order_match = re.search(r'ORD-\d{8}-\d{3}', last_message)
            if order_match:
                order_id = order_match.group()

        logger.info("[工单] 提取到订单号: %s", order_id)

        # 退款/理赔操作：有订单号则直接创建工单，没有则先查询用户订单
        if is_refund_or_claim:
            if order_id:
                # 有订单号，直接创建退款工单
                ticket_info["order_id"] = order_id
                result = await self.create_ticket(ticket_info, user_id)

                # 更新订单状态为"退款中"
                logger.info("[工单] 更新订单状态: %s → refunding", order_id)
                db.update_order_status(order_id, "refunding")
            else:
                # 没有订单号，查询用户订单列表
                orders_result = await self.query_user_orders(user_id)
                if orders_result:
                    result = orders_result
                else:
                    result = f"⚠️ 未找到您的订单记录，无法处理退款请求。"
        elif action == "query" and "ticket_id" in ticket_info:
            result = await self.query_ticket(ticket_info["ticket_id"])
        elif action == "need_info":
            # 缺少必要信息，提醒用户补充
            missing_info = ticket_info.get("message", "请提供更多信息以便处理您的请求")
            result = f"⚠️ {missing_info}"
        else:
            result = await self.create_ticket(ticket_info, user_id)

        return {
            **state,
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }
