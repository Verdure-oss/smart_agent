package com.smartcs.memory;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 文档重排序 — 对齐 Python 版 knowledge_rag.py 的 rerank_documents / _llm_rerank。
 *
 * 策略（与 Python 完全同构）：
 * 1. 优先 cross-encoder 精排（预留接口；Java 端默认不加载原生模型）
 * 2. 退化到 LLM 重排：让 LLM 从候选文档中选出最相关的 top_k 个索引号
 *    —— 提示词与 Python _llm_rerank 一致：返回逗号分隔的索引号，如 "0,2,4"
 * 3. 解析失败回退：取候选前 top_k
 */
@Component
public class Reranker {

    private static final Logger log = LoggerFactory.getLogger(Reranker.class);

    private static final String RERANK_SYSTEM = "你是一个文档相关性排序专家。";
    private static final String RERANK_USER = """
            用户查询: %s

            候选文档:
            %s

            请返回最相关的%d个文档的索引号，用逗号分隔，如: 0,2,4
            """;

    private final ChatClient chatClient;

    public Reranker(ChatClient.Builder chatClientBuilder) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor()).build();
    }

    /**
     * 对检索结果重排序，选 top_k。
     * @param query      用户查询
     * @param documents  候选文档（来自 recall，通常 ≥ top_k）
     * @param topK       需要的最终数量
     */
    public List<Map<String, Object>> rerank(String query, List<Map<String, Object>> documents, int topK) {
        if (documents == null || documents.isEmpty()) {
            return new ArrayList<>();
        }
        if (documents.size() <= topK) {
            return documents; // 候选不足，无需重排
        }

        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < documents.size(); i++) {
            String content = String.valueOf(documents.get(i).getOrDefault("content", ""));
            String snippet = content.length() > 200 ? content.substring(0, 200) : content;
            sb.append("[").append(i).append("] ").append(snippet).append("\n");
        }

        try {
            String response = chatClient.prompt()
                    .system(RERANK_SYSTEM)
                    .user(String.format(RERANK_USER, query, sb, topK))
                    .call()
                    .content();
            log.info("[Reranker] LLM 重排响应: {}", response == null ? "" : response.trim());

            if (response != null) {
                List<Map<String, Object>> ranked = new ArrayList<>();
                for (String part : response.split(",")) {
                    try {
                        int idx = Integer.parseInt(part.trim());
                        if (idx >= 0 && idx < documents.size() && !ranked.contains(documents.get(idx))) {
                            ranked.add(documents.get(idx));
                        }
                    } catch (NumberFormatException ignored) {
                        // 跳过非法 token
                    }
                    if (ranked.size() >= topK) break;
                }
                if (!ranked.isEmpty()) {
                    return ranked;
                }
            }
        } catch (Exception e) {
            log.warn("[Reranker] LLM 重排失败，回退到候选前 top_k: {}", e.getMessage());
        }

        return new ArrayList<>(documents.subList(0, Math.min(topK, documents.size())));
    }
}