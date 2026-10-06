package com.smartcs.mcp;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.function.FunctionToolCallback;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;

/**
 * MCP 工具的 Spring AI Function Calling 封装。
 *
 * 将 order_query / ticket_create / risk_check 三个业务工具包装为 ToolCallback，
 * 让 LLM 根据用户意图自主「选工具 + 抽参数」（对齐简历"Tool Calling 工具路由"叙事）。
 *
 * 设计要点：
 *  - 用 @Component（而非 @Bean）提供回调列表，避免被 Spring AI ToolCallingManager
 *    全局收集后污染知识问答/合规等其他 Agent 的调用；
 *  - 回调内部仍走 MCPToolServer.callTool，保证与手动调用同一条落库/风控链路。
 */
@Component
public class MCPToolCallbacks {

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final List<ToolCallback> callbacks;

    public MCPToolCallbacks(MCPToolServer mcpServer) {
        this.callbacks = List.of(
                orderQuery(mcpServer),
                ticketCreate(mcpServer),
                riskCheck(mcpServer)
        );
    }

    public List<ToolCallback> toolCallbacks() {
        return callbacks;
    }

    private ToolCallback orderQuery(MCPToolServer mcpServer) {
        return FunctionToolCallback.<OrderQueryRequest, String>builder("order_query",
                        req -> call(mcpServer, "order_query", Map.of(
                                "order_id", req.orderId() == null ? "" : req.orderId(),
                                "user_id", req.userId() == null ? "" : req.userId())))
                .description("查询订单信息：按订单ID或用户ID查询订单状态、金额。用户想查订单、问物流、核实订单状态时调用。")
                .inputType(OrderQueryRequest.class)
                .build();
    }

    private ToolCallback ticketCreate(MCPToolServer mcpServer) {
        return FunctionToolCallback.<TicketCreateRequest, String>builder("ticket_create",
                        req -> call(mcpServer, "ticket_create", Map.of(
                                "user_id", req.userId() == null ? "anonymous" : req.userId(),
                                "description", req.description() == null ? "" : req.description(),
                                "priority", req.priority() == null ? "medium" : req.priority())))
                .description("创建客服工单并落库。用户投诉、申请退款/理赔、需要人工跟进时调用。")
                .inputType(TicketCreateRequest.class)
                .build();
    }

    private ToolCallback riskCheck(MCPToolServer mcpServer) {
        return FunctionToolCallback.<RiskCheckRequest, String>builder("risk_check",
                        req -> call(mcpServer, "risk_check", Map.of(
                                "user_id", req.userId() == null ? "anonymous" : req.userId(),
                                "action", req.action() == null ? "" : req.action(),
                                "amount", req.amount() == null ? 0.0 : req.amount())))
                .description("金融风控检查：按金额与动作判断风险等级（含滑动窗口行为风控）。涉及大额、退款、转账时调用。")
                .inputType(RiskCheckRequest.class)
                .build();
    }

    private String call(MCPToolServer server, String name, Map<String, Object> args) {
        try {
            return objectMapper.writeValueAsString(server.callTool(name, args));
        } catch (Exception e) {
            return "{\"success\":false,\"error\":\"" + e.getMessage() + "\"}";
        }
    }

    /** 工具入参（Jackson 序列化为 JSON Schema 传给 LLM）。 */
    public record OrderQueryRequest(String orderId, String userId) {}
    public record TicketCreateRequest(String userId, String description, String priority) {}
    public record RiskCheckRequest(String userId, String action, Double amount) {}
}