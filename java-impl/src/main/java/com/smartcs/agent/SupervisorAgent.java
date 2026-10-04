package com.smartcs.agent;

import com.alibaba.cloud.ai.graph.CompiledGraph;
import com.alibaba.cloud.ai.graph.GraphRepresentation;
import com.alibaba.cloud.ai.graph.KeyStrategyFactory;
import com.alibaba.cloud.ai.graph.OverAllState;
import com.alibaba.cloud.ai.graph.StateGraph;
import com.alibaba.cloud.ai.graph.action.AsyncEdgeAction;
import com.alibaba.cloud.ai.graph.action.AsyncNodeAction;
import com.alibaba.cloud.ai.graph.state.strategy.ReplaceStrategy;
import com.smartcs.tracing.AgentTracer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.stereotype.Component;

import jakarta.annotation.PostConstruct;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.StringJoiner;

/**
 * Supervisor编排Agent — 中央协调者（Spring AI Alibaba Graph 版）。
 *
 * 用 StateGraph 平移 Python 版(LangGraph)的编排拓扑：
 *
 * START → supervisor_decompose → intent_router → dispatch_step
 *           → (条件边) knowledge_rag | ticket_handler
 *           → collect_step → (条件边：还有未完成子任务?) ── 是 → dispatch_step（循环）
 *                                                    └── 否 → compliance_check → synthesize → END
 *
 * 状态：AgentState POJO 存于 OverAllState 的 "agentState" key（ReplaceStrategy），
 * 与 Python 版"AgentState 作为数据总线"一致；多轮 messages 由 Controller 回填（修复上下文丢失）。
 */
@Component
public class SupervisorAgent {

    private static final Logger log = LoggerFactory.getLogger(SupervisorAgent.class);

    private static final String STATE_KEY = "agentState";
    private static final int MAX_ITERATIONS = 10;
    private static final int TRANSCRIPT_LIMIT = 8;

    private static final String DECOMPOSE_SYSTEM_PROMPT = """
            你是客服系统的任务拆解 Supervisor。把用户诉求拆解为 1~3 个可独立执行的子任务，只返回 JSON。
            JSON格式：
            {"subTasks":[{"id":"t1","description":"子任务描述","agent":"knowledge_rag|ticket_handler","dependsOn":[]}]}
            规则：
            1. description 必须自包含：结合对话历史还原代词与省略（如"那个产品"→具体产品名），脱离上下文也能执行
            2. agent 只能取 knowledge_rag（咨询/查询类）或 ticket_handler（办理/工单类）；不确定时用 auto
            3. 简单问题只拆 1 个子任务，dependsOn 为空数组
            4. 只返回 JSON，不要任何多余文本
            """;

    private final ChatClient chatClient;
    private final IntentRouterAgent intentRouter;
    private final KnowledgeRAGAgent knowledgeAgent;
    private final TicketHandlerAgent ticketAgent;
    private final ComplianceCheckerAgent complianceAgent;
    private final AgentTracer tracer;

    private volatile CompiledGraph compiledGraph;

    public SupervisorAgent(
            ChatClient.Builder chatClientBuilder,
            IntentRouterAgent intentRouter,
            KnowledgeRAGAgent knowledgeAgent,
            TicketHandlerAgent ticketAgent,
            ComplianceCheckerAgent complianceAgent,
            AgentTracer tracer) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.intentRouter = intentRouter;
        this.knowledgeAgent = knowledgeAgent;
        this.ticketAgent = ticketAgent;
        this.complianceAgent = complianceAgent;
        this.tracer = tracer;
    }

    @PostConstruct
    void buildGraph() {
        try {
            KeyStrategyFactory keyStrategyFactory = () -> Map.of(STATE_KEY, new ReplaceStrategy());
            StateGraph graph = new StateGraph(keyStrategyFactory);

            graph.addNode("supervisor_decompose", AsyncNodeAction.node_async(this::decompose));
            graph.addNode("intent_router", AsyncNodeAction.node_async(this::routeIntent));
            graph.addNode("dispatch_step", AsyncNodeAction.node_async(this::dispatchStep));
            graph.addNode("knowledge_rag", AsyncNodeAction.node_async(this::runKnowledgeRag));
            graph.addNode("ticket_handler", AsyncNodeAction.node_async(this::runTicketHandler));
            graph.addNode("collect_step", AsyncNodeAction.node_async(this::collectStep));
            graph.addNode("compliance_check", AsyncNodeAction.node_async(this::runCompliance));
            graph.addNode("synthesize", AsyncNodeAction.node_async(this::synthesizeNode));

            graph.addEdge(StateGraph.START, "supervisor_decompose");
            graph.addEdge("supervisor_decompose", "intent_router");
            graph.addEdge("intent_router", "dispatch_step");
            graph.addConditionalEdges("dispatch_step", AsyncEdgeAction.edge_async(this::routeByAgent),
                    Map.of("knowledge_rag", "knowledge_rag", "ticket_handler", "ticket_handler"));
            graph.addEdge("knowledge_rag", "collect_step");
            graph.addEdge("ticket_handler", "collect_step");
            graph.addConditionalEdges("collect_step", AsyncEdgeAction.edge_async(this::hasMoreSteps),
                    Map.of("dispatch_step", "dispatch_step", "compliance_check", "compliance_check"));
            graph.addEdge("compliance_check", "synthesize");
            graph.addEdge("synthesize", StateGraph.END);

            this.compiledGraph = graph.compile();
            log.info("[SupervisorGraph] 编排图构建完成: decompose -> intent -> dispatch<->collect 循环 -> compliance -> synthesize");
        } catch (Exception e) {
            throw new IllegalStateException("构建 Supervisor 状态图失败", e);
        }
    }

    /**
     * 完整编排流程：入口保持不变，内部改为状态图驱动。
     */
    public AgentState orchestrate(AgentState state) {
        return tracer.trace("supervisor", "orchestrate", () -> {
            try {
                Optional<OverAllState> out = compiledGraph.invoke(Map.of(STATE_KEY, state));
                OverAllState finalState = out.orElseThrow(
                        () -> new IllegalStateException("编排图执行未返回最终状态"));
                AgentState result = finalState.value(STATE_KEY, (AgentState) null);
                return result != null ? result : state;
            } catch (Exception e) {
                log.error("[SupervisorGraph] 编排执行异常: {}", e.getMessage(), e);
                state.setCompliancePassed(false);
                state.setFinalResponse("抱歉，系统繁忙，请稍后重试。");
                return state;
            }
        });
    }

    /** 图结构（Mermaid），用于调试/可视化。 */
    public String graphDiagram() {
        try {
            GraphRepresentation rep = compiledGraph.getGraph(GraphRepresentation.Type.MERMAID, "supervisor-graph");
            return rep.content();
        } catch (Exception e) {
            return "diagram unavailable: " + e.getMessage();
        }
    }

    // ==================== 节点实现 ====================

    private Map<String, Object> decompose(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        try {
            String transcript = buildTranscript(s.getMessages(), TRANSCRIPT_LIMIT);
            String summaryPart = (s.getHistorySummary() != null && !s.getHistorySummary().isBlank())
                    ? "历史摘要: " + s.getHistorySummary() + "\n\n" : "";
            String wmPart = (s.getWorkingInfo() != null && !s.getWorkingInfo().isBlank())
                    ? "已知补充信息: " + s.getWorkingInfo() + "\n\n" : "";
            SupervisorPlan plan = chatClient.prompt()
                    .system(DECOMPOSE_SYSTEM_PROMPT)
                    .user(summaryPart + wmPart
                            + (transcript.isEmpty() ? "" : "对话历史:\n" + transcript + "\n\n")
                            + "当前用户消息: " + s.getUserMessage())
                    .call()
                    .entity(SupervisorPlan.class);

            List<SubTask> tasks = (plan != null && plan.getSubTasks() != null)
                    ? plan.getSubTasks().stream()
                        .filter(t -> t.getId() != null && t.getDescription() != null && !t.getDescription().isBlank())
                        .limit(3)
                        .toList()
                    : List.of();
            if (tasks.isEmpty()) {
                tasks = List.of(SubTask.of("t1", s.getUserMessage(), "auto"));
            }
            s.setSubTasks(new java.util.ArrayList<>(tasks));
        } catch (Exception e) {
            log.warn("[decompose] 任务拆解失败，回退单子任务: {}", e.getMessage());
            s.setSubTasks(new java.util.ArrayList<>(List.of(SubTask.of("t1", s.getUserMessage(), "auto"))));
        }
        s.setIterationCount(0);
        s.setCompletedTaskIds(new java.util.LinkedHashSet<>());
        s.setTaskResults(new java.util.LinkedHashMap<>());
        log.info("[decompose] 拆解出 {} 个子任务: {}", s.getSubTasks().size(),
                s.getSubTasks().stream().map(SubTask::getId).toList());
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> routeIntent(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        s.setCurrentAgent("intent_router");
        intentRouter.process(s);
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> dispatchStep(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        s.setIterationCount(s.getIterationCount() + 1);
        SubTask next = s.findNextRunnable();
        if (next == null) {
            s.setCurrentSubTaskId(null);
            s.setCurrentAgent("dispatch_step");
            return Map.of(STATE_KEY, s);
        }
        s.setCurrentSubTaskId(next.getId());
        s.setCurrentSubTaskDescription(next.getDescription());
        String agent = next.getAgent();
        if (agent == null || agent.isBlank() || "auto".equals(agent)) {
            agent = "ticket_handler".equals(s.getIntent()) ? "ticket_handler" : "knowledge_rag";
        }
        if (!"ticket_handler".equals(agent)) {
            agent = "knowledge_rag";
        }
        s.setCurrentAgent(agent);
        log.info("[dispatch] step={} agent={} desc={}", next.getId(), agent,
                abbreviate(next.getDescription()));
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> runKnowledgeRag(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        try {
            knowledgeAgent.process(s);
        } catch (Exception e) {
            log.warn("[knowledge_rag] 执行失败: {}", e.getMessage());
            s.addSubResult("knowledge_rag", "抱歉，暂时无法查询知识库，请稍后重试或转人工客服。");
        }
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> runTicketHandler(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        try {
            ticketAgent.process(s);
        } catch (Exception e) {
            log.warn("[ticket_handler] 执行失败: {}", e.getMessage());
            s.addSubResult("ticket_handler", "抱歉，工单服务暂时不可用，请稍后重试。");
        }
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> collectStep(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        String stepKey = s.getCurrentSubTaskId();
        if (stepKey != null) {
            Object result = s.getSubResults().get(s.getCurrentAgent());
            if (result != null) {
                s.getTaskResults().put(stepKey, result);
            }
            s.getCompletedTaskIds().add(stepKey);
            s.setCurrentSubTaskId(null);
        }
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> runCompliance(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        s.setCurrentAgent("compliance_checker");
        complianceAgent.process(s);
        return Map.of(STATE_KEY, s);
    }

    private Map<String, Object> synthesizeNode(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        s.setCurrentAgent("synthesize");

        if (!s.isCompliancePassed()) {
            s.setFinalResponse("抱歉，您的请求涉及敏感内容，已转交人工客服处理。工单编号已自动生成，请留意后续通知。");
            return Map.of(STATE_KEY, s);
        }

        // 若合规审查做了 PII 脱敏，优先使用脱敏后的完整文本作为最终响应（对齐 Python masked_response）
        Object comp = s.getSubResults().get("compliance");
        if (comp instanceof Map<?, ?> compMap) {
            Object masked = compMap.get("masked_response");
            if (masked instanceof String ms && !ms.isBlank()) {
                s.setFinalResponse(ms);
                return Map.of(STATE_KEY, s);
            }
        }

        // 按子任务顺序汇总各步结果
        StringJoiner joiner = new StringJoiner("\n\n");
        for (SubTask t : s.getSubTasks()) {
            Object r = s.getTaskResults().get(t.getId());
            if (r instanceof String val && !val.isBlank()) {
                joiner.add(val);
            }
        }
        // 兜底：子任务结果为空时回退 subResults
        if (joiner.length() == 0) {
            for (Map.Entry<String, Object> entry : s.getSubResults().entrySet()) {
                if (entry.getValue() instanceof String val && !val.isBlank()
                        && !"compliance".equals(entry.getKey())) {
                    joiner.add(val);
                }
            }
        }
        String response = joiner.toString();
        s.setFinalResponse(response.isEmpty() ? "抱歉，暂时无法处理您的请求，请稍后重试。" : response);
        return Map.of(STATE_KEY, s);
    }

    // ==================== 条件边 ====================

    /** dispatch_step 之后的路由：按当前子任务的 agent 分发。 */
    private String routeByAgent(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        String agent = s.getCurrentAgent();
        return "ticket_handler".equals(agent) ? "ticket_handler" : "knowledge_rag";
    }

    /** collect_step 之后的路由：还有可执行子任务且未超迭代上限 → 继续循环，否则进入合规审查。 */
    private String hasMoreSteps(OverAllState graphState) {
        AgentState s = graphState.value(STATE_KEY, (AgentState) null);
        boolean more = s.findNextRunnable() != null && s.getIterationCount() < MAX_ITERATIONS;
        return more ? "dispatch_step" : "compliance_check";
    }

    // ==================== 工具 ====================

    private String buildTranscript(List<Map<String, String>> messages, int limit) {
        if (messages == null || messages.isEmpty()) {
            return "";
        }
        List<Map<String, String>> recent = messages.size() > limit
                ? messages.subList(messages.size() - limit, messages.size())
                : messages;
        StringJoiner joiner = new StringJoiner("\n");
        for (Map<String, String> m : recent) {
            String role = "assistant".equals(m.get("role")) ? "客服" : "用户";
            joiner.add(role + ": " + m.getOrDefault("content", ""));
        }
        return joiner.toString();
    }

    private String abbreviate(String text) {
        if (text == null) {
            return "";
        }
        return text.length() <= 40 ? text : text.substring(0, 40) + "...";
    }
}
