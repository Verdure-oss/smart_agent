package com.smartcs.agent;

import com.smartcs.mcp.MCPToolServer;
import com.smartcs.tracing.AgentTracer;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Map;

/**
 * 工单处理Agent — 工单创建/查询/流转。
 * 处理退款、理赔、开户等业务办理类需求。
 * 实际落库统一经 MCP 工具层 ticket_create（对齐 Python 版）。
 */
@Component
public class TicketHandlerAgent implements BaseAgent {

    private final ChatClient chatClient;
    private final AgentTracer tracer;
    private final MCPToolServer mcpServer;

    public TicketHandlerAgent(ChatClient.Builder chatClientBuilder, AgentTracer tracer, MCPToolServer mcpServer) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.tracer = tracer;
        this.mcpServer = mcpServer;
    }

    @Override
    public AgentState process(AgentState state) {
        return tracer.trace("ticket_handler", "process", () -> {
            // 优先用 Supervisor 拆解后的自包含子任务描述，否则用原始用户消息
            String desc = state.getCurrentSubTaskDescription();
            String description = (desc != null && !desc.isBlank()) ? desc : state.getUserMessage();

            // 经 MCP 工具层创建工单并落库
            @SuppressWarnings("unchecked")
            Map<String, Object> call = mcpServer.callTool("ticket_create", Map.of(
                    "user_id", state.getUserId() != null ? state.getUserId() : "anonymous",
                    "description", description,
                    "priority", "medium"
            ));

            if (!Boolean.TRUE.equals(call.get("success"))) {
                state.addSubResult("ticket_handler",
                        "抱歉，工单创建失败：" + call.getOrDefault("error", "未知原因") + "，请稍后重试或转人工。");
                return state;
            }

            @SuppressWarnings("unchecked")
            Map<String, Object> ticket = (Map<String, Object>) call.get("result");
            String ticketId = String.valueOf(ticket.getOrDefault("ticket_id", "TK-UNKNOWN"));

            String result = String.format(
                    "工单已创建成功！\n\n" +
                    "工单号: %s\n" +
                    "状态: %s\n" +
                    "优先级: %s\n" +
                    "创建时间: %s\n\n" +
                    "我们将尽快处理您的请求，请保存好工单号以便后续查询。",
                    ticketId,
                    ticket.getOrDefault("status", "created"),
                    ticket.getOrDefault("priority", "medium"),
                    LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss"))
            );

            state.addSubResult("ticket_handler", result);
            return state;
        });
    }

    @Override
    public String getName() {
        return "ticket_handler";
    }
}