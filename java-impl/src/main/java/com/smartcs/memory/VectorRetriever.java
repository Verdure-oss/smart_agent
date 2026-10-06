package com.smartcs.memory;

import java.util.List;
import java.util.Map;

/**
 * 向量召回路抽象（VECTOR RETRIEVER SPI）。
 *
 * 把「文档→向量 → 相似度召回」这一后端从混合检索中解耦，便于在纯本地近乎向量（内存）
 * 与 Milvus 等真实向量数据库之间一键切换，而 {@link LongTermMemoryService} 的
 * BM25 + RRF 融合逻辑无需改动。
 *
 * 实现：
 *   - {@link TfVectorRetriever}   （默认）纯本地 2-gram TF 向量 + 余弦，零外部依赖
 *   - {@link MilvusVectorRetriever}（可选）Milvus 向量库 + embedding
 */
public interface VectorRetriever {

    /** 后端类型（用于日志与配置标识）：memory / milvus。 */
    String backend();

    /** 重新装载全部文档（入库/启动时调用；实现自行构建向量索引）。 */
    void load(List<Map<String, Object>> documents);

    /**
     * 向量召回：返回候选文档（含 content/source），附带相似度分（越高越相关）。
     * 命中集合可能小于 topK。
     */
    List<Map<String, Object>> search(String query, int topK);
}