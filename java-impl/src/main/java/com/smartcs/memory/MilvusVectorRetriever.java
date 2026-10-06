package com.smartcs.memory;

import org.springframework.ai.document.Document;
import org.springframework.ai.vectorstore.SearchRequest;
import org.springframework.ai.vectorstore.VectorStore;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;

/**
 * Milvus 向量召回实现（可选后端，生产增强）。
 *
 * 激活条件：配置 {@code SMARTCS_VECTOR_BACKEND=milvus} 且 Spring AI 已自动配置好
 * MilvusVectorStore（需 Milvus 连接 + EmbeddingModel）。此时作为
 * {@link VectorRetriever#search} 的实现，替换默认 TF 近似后端，上层 BM25+RRF 不变。
 *
 * 反向保证零部署：
 *   - 默认（未配置或后端=memory）不激活本类，服务用 TF 内存向量，无需 Milvus/embedding。
 *   - 即便配置了 Milvus 但连接缺失，也回退结构给出空结果并记日志，不抛启动异常。
 */
@Component
@Primary
@ConditionalOnProperty(name = "smartcs.vector.backend", havingValue = "milvus")
public class MilvusVectorRetriever implements VectorRetriever {

    private final VectorStore vectorStore;
    private final boolean available;

    public MilvusVectorRetriever(ObjectProvider<VectorStore> vectorStoreProvider) {
        VectorStore store = vectorStoreProvider.getIfAvailable();
        this.available = store != null;
        this.vectorStore = store;
        if (!available) {
            System.err.println("[Vector] 配置了 SMARTCS_VECTOR_BACKEND=milvus 但无 MilvusVectorStore Bean，检索将返回空");
        } else {
            System.out.println("[Vector] Milvus 向量回后端已启用");
        }
    }

    @Override
    public String backend() {
        return "milvus";
    }

    @Override
    public void load(List<Map<String, Object>> documents) {
        if (!available || documents == null) return;
        List<Document> docs = documents.stream()
                .map(d -> Document.builder()
                        .id(String.valueOf(d.get("id")))
                        .text(String.valueOf(d.get("content")))
                        .metadata(Map.of("source", String.valueOf(d.getOrDefault("source", ""))))
                        .build())
                .toList();
        vectorStore.add(docs);
    }

    @Override
    public List<Map<String, Object>> search(String query, int topK) {
        if (!available) return List.of();
        List<Document> hits = vectorStore.similaritySearch(
                SearchRequest.builder().query(query).topK(topK).build());
        List<Map<String, Object>> result = new java.util.ArrayList<>();
        for (Document d : hits) {
            var m = new java.util.HashMap<String, Object>();
            m.put("id", d.getId());
            m.put("content", d.getText());
            m.put("source", String.valueOf(d.getMetadata().getOrDefault("source", "")));
            m.put("vector_score",
                    d.getMetadata().getOrDefault("distance",
                            d.getMetadata().getOrDefault("score", 0.0)));
            result.add(m);
        }
        return result;
    }
}