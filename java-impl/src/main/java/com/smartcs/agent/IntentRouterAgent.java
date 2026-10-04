package com.smartcs.agent;

import com.smartcs.tracing.AgentTracer;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.stereotype.Component;

import java.util.Map;

/**
 * 意图路由Agent — 分析用户输入，识别意图并决定路由目标。
 *
 * 支持的意图分类：
 * - knowledge_rag: 知识咨询（产品信息、政策查询）
 * - ticket_handler: 业务办理（退款、理赔、开户）
 * - compliance_checker: 合规相关（举报、账户安全）
 */
@Component
public class IntentRouterAgent implements BaseAgent {

    private static final String SYSTEM_PROMPT = """
            你是意图识别Agent，分析用户消息并返回路由目标。
            只返回以下之一: knowledge_rag, ticket_handler, compliance_checker
            
            路由规则：
            - 产品咨询、利率查询、政策了解 → knowledge_rag
            - 退款、理赔、开户、投诉 → ticket_handler
            - 资金安全、账户异常、欺诈举报 → compliance_checker
            """;

    private final ChatClient chatClient;
    private final AgentTracer tracer;

    public IntentRouterAgent(ChatClient.Builder chatClientBuilder, AgentTracer tracer) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.tracer = tracer;
    }

    @Override
    public AgentState process(AgentState state) {
        return tracer.trace("intent_router", "process", () -> {
            // 携带多轮历史，修复上下文丢失（如"那个产品"类指代需要历史才能正确分类）
            String history = buildTranscript(state.getMessages(), 6);
            String summaryPart = (state.getHistorySummary() != null && !state.getHistorySummary().isBlank())
                    ? "历史摘要: " + state.getHistorySummary() + "\n\n" : "";
            var call = chatClient.prompt()
                    .system(SYSTEM_PROMPT)
                    .user(summaryPart
                            + (history.isEmpty() ? "" : "对话历史:\n" + history + "\n\n")
                            + state.getUserMessage())
                    .call();
            // 只调用一次终端方法：chatResponse() 拿到响应后，文本与 usage 都从它提取；
            // 再调 content() 会二次驱动同一 advisor 链导致 "No CallAdvisors available"
            ChatResponse cr = call.chatResponse();
            tracer.recordUsage("intent_router", "process", cr);
            String responseText = cr != null && cr.getResult() != null && cr.getResult().getOutput() != null
                    ? cr.getResult().getOutput().getText() : "";

            String intent = responseText.trim().toLowerCase();
            if (!intent.equals("knowledge_rag") && !intent.equals("ticket_handler")
                    && !intent.equals("compliance_checker")) {
                intent = "knowledge_rag";
            }

            state.setIntent(intent);
            return state;
        });
    }

    private String buildTranscript(java.util.List<Map<String, String>> messages, int limit) {
        if (messages == null || messages.isEmpty()) {
            return "";
        }
        java.util.List<Map<String, String>> recent = messages.size() > limit
                ? messages.subList(messages.size() - limit, messages.size())
                : messages;
        java.util.StringJoiner joiner = new java.util.StringJoiner("\n");
        for (Map<String, String> m : recent) {
            String role = "assistant".equals(m.get("role")) ? "客服" : "用户";
            joiner.add(role + ": " + m.getOrDefault("content", ""));
        }
        return joiner.toString();
    }

    @Override
    public String getName() {
        return "intent_router";
    }
}
