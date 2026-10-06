package com.smartcs.agent;

import com.smartcs.mcp.MCPToolCallbacks;
import com.smartcs.tracing.AgentTracer;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.stereotype.Component;

/**
 * 工单处理Agent — 工单创建/查询/流转。
 * 处理退款、理赔、开户等业务办理类需求。
 * 实际落库统一经 MCP 工具层 ticket_create（对齐 Python 版）。
 */
@Component
public class TicketHandlerAgent implements BaseAgent {

    /** 业务办理系统提示词：让 LLM 根据诉求自主选工具 + 抽参数（function calling）。 */
    private static final String TOOL_ROUTING_PROMPT = """
            你是业务办理助手。根据用户诉求，自主决定调用哪个工具完成订单查询、工单创建或风控检查，
            并基于工具返回结果给用户友好、简洁的回复。
            规则：
            1. 查询订单 / 问物流 / 核实订单状态 -> 调用 order_query
            2. 投诉 / 退款 / 理赔 / 需要人工跟进 -> 调用 ticket_create
            3. 涉及大额、退款、转账的风控判断 -> 调用 risk_check
            4. 只在工具返回基础上回答，不要编造工具之外信息
            """;

    private final ChatClient chatClient;
    private final AgentTracer tracer;
    private final MCPToolCallbacks toolCallbacks;

    public TicketHandlerAgent(ChatClient.Builder chatClientBuilder, AgentTracer tracer, MCPToolCallbacks toolCallbacks) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.tracer = tracer;
        this.toolCallbacks = toolCallbacks;
    }

    @Override
    public AgentState process(AgentState state) {
        return tracer.trace("ticket_handler", "process", () -> {
            // 优先用 Supervisor 拆解后的自包含子任务描述，否则用原始用户消息
            String desc = state.getCurrentSubTaskDescription();
            String message = (desc != null && !desc.isBlank()) ? desc : state.getUserMessage();
            String userId = state.getUserId() != null && !state.getUserId().isBlank()
                    ? state.getUserId() : "anonymous";

            try {
                // 注入当前 user_id，让 LLM 填入工具参数（避免因缺参与而反问）
                var response = chatClient.prompt()
                        .system(TOOL_ROUTING_PROMPT)
                        .user("当前用户ID: " + userId + "（填充工具参数时使用该ID）\n\n用户诉求: " + message)
                        .toolCallbacks(toolCallbacks.toolCallbacks())
                        .call()
                        .chatResponse();
                tracer.recordUsage("ticket_handler", "process", response);
                String text = response != null && response.getResult() != null
                        && response.getResult().getOutput() != null
                        ? response.getResult().getOutput().getText()
                        : "抱歉，暂时无法处理您的请求，请稍后重试或转人工。";
                state.addSubResult("ticket_handler", text);
            } catch (Exception e) {
                state.addSubResult("ticket_handler", "抱歉，业务办理暂时繁忙，请稍后重试或转人工客服。");
            }
            return state;
        });
    }

    @Override
    public String getName() {
        return "ticket_handler";
    }
}