package com.smartcs.agent;

import com.smartcs.mcp.MCPToolServer;
import com.smartcs.memory.LongTermMemoryService;
import com.smartcs.tracing.AgentTracer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 知识检索Agent — RAG流程实现（对齐 Python 版 knowledge_rag.py）。
 *
 * 完整流程：Query改写 → 混合检索(BM25+TF向量+RRF) → 上下文注入(引用标注) → 生成回答
 *
 * 对齐点：
 * 1. rewriteQuery：口语 → 检索友好（LLM 改写，失败回退原查询）
 * 2. 混合检索：long_term 的 BM25+向量双路召回+RRF（阶段2完成）
 * 3. 回答强制引用来源 [xxx.md]
 * 4. 无文档兜底：直接返回转人工话术，省一次 LLM 调用
 */
@Component
public class KnowledgeRAGAgent implements BaseAgent {

    private static final Logger log = LoggerFactory.getLogger(KnowledgeRAGAgent.class);

    private static final String REWRITE_PROMPT = """
            你是检索查询改写器。把口语化问题改写为检索友好的查询。
            规则：
            1. 提取核心实体和意图（产品名、动作、属性）
            2. 补充同义检索词（如"退钱"→"退款 退回 流程 时限"）
            3. 只输出改写后的查询词，用空格分隔，不要解释
            """;

    private static final String RAG_PROMPT = """
            你是专业的知识库问答Agent。根据检索到的文档回答用户问题。
            规则：
            1. 严格基于文档回答，不编造信息
            2. 回答中在关键信息后标注引用来源，格式如 [product_faq.md]
            3. 文档不足以回答时，明确说明缺少哪部分信息并建议转人工客服
            4. 金融产品信息需标注"以上信息仅供参考，具体以合同条款为准"

            检索到的文档：
            %s

            用户问题：%s
            """;

    private final ChatClient chatClient;
    private final LongTermMemoryService longTermMemory;
    private final MCPToolServer mcpServer;
    private final AgentTracer tracer;

    public KnowledgeRAGAgent(
            ChatClient.Builder chatClientBuilder,
            LongTermMemoryService longTermMemory,
            MCPToolServer mcpServer,
            AgentTracer tracer) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.longTermMemory = longTermMemory;
        this.mcpServer = mcpServer;
        this.tracer = tracer;
    }

    @Override
    public AgentState process(AgentState state) {
        return tracer.trace("knowledge_rag", "process", () -> {
            // 优先用 Supervisor 拆解后的自包含子任务描述（已还原代词），否则用原始用户消息
            String desc = state.getCurrentSubTaskDescription();
            String query = (desc != null && !desc.isBlank()) ? desc : state.getUserMessage();

            // Step 1: Query 改写（口语 → 检索友好；失败回退原查询）
            String rewritten = tracer.trace("knowledge_rag", "rewrite", () -> rewriteQuery(query));

            // Step 2: 混合检索（优先经 MCP 工具层 knowledge_search，失败回退内存直查）
            List<Map<String, Object>> docs;
            try {
                Map<String, Object> call = mcpServer.callTool("knowledge_search", Map.of(
                        "query", rewritten,
                        "top_k", 3
                ));
                if (Boolean.TRUE.equals(call.get("success")) && call.get("result") instanceof List<?> resultList) {
                    @SuppressWarnings("unchecked")
                    List<Map<String, Object>> cast = (List<Map<String, Object>>) resultList;
                    docs = cast;
                } else {
                    docs = longTermMemory.search(rewritten, 3);
                }
            } catch (Exception e) {
                docs = longTermMemory.search(rewritten, 3);
            }

            // 无文档兜底：直接返回转人工话术，省一次 LLM 调用
            if (docs.isEmpty()) {
                log.info("[RAG] 未检索到文档，返回转人工兜底: {}", abbreviate(query));
                state.addSubResult("knowledge_rag",
                        "抱歉，知识库中未检索到相关文档，暂时无法回答该问题。建议您转人工客服咨询，或换个说法描述您的问题。");
                return state;
            }

            // Step 3: 构建上下文（来源标注）
            String context = docs.stream()
                    .map(doc -> String.format("来源: %s\n内容: %s",
                            doc.getOrDefault("source", "未知"),
                            doc.getOrDefault("content", "")))
                    .collect(Collectors.joining("\n---\n"));

            // Step 4: 生成回答（用原始 query，改写词只用于检索；强制引用来源）
            var call = chatClient.prompt()
                    .user(String.format(RAG_PROMPT, context, query))
                    .call();
            ChatResponse cr = call.chatResponse();
            tracer.recordUsage("knowledge_rag", "generate", cr);
            String answer = cr != null && cr.getResult() != null && cr.getResult().getOutput() != null
                    ? cr.getResult().getOutput().getText() : "";

            state.addSubResult("knowledge_rag", answer);
            return state;
        });
    }

    private String rewriteQuery(String query) {
        try {
            var call = chatClient.prompt()
                    .system(REWRITE_PROMPT)
                    .user(query)
                    .call();
            ChatResponse cr = call.chatResponse();
            tracer.recordUsage("knowledge_rag", "rewrite", cr);
            String rewritten = cr != null && cr.getResult() != null && cr.getResult().getOutput() != null
                    ? cr.getResult().getOutput().getText() : "";
            if (rewritten != null && !rewritten.isBlank()) {
                log.info("[RAG] rewrite: {} -> {}", abbreviate(query), abbreviate(rewritten.trim()));
                return rewritten.trim();
            }
        } catch (Exception e) {
            log.warn("[RAG] rewrite 失败，回退原查询: {}", e.getMessage());
        }
        return query;
    }

    private static String abbreviate(String s) {
        if (s == null) return "";
        return s.length() > 40 ? s.substring(0, 40) + "..." : s;
    }

    @Override
    public String getName() {
        return "knowledge_rag";
    }
}
