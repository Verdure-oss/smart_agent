package com.smartcs.eval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.smartcs.memory.LongTermMemoryService;
import com.smartcs.memory.TfVectorRetriever;

import java.io.File;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Java 版检索评测 — 与 Python eval/rag_eval.py 同口径对比。
 *
 * 用法：java -cp target/classes RetrievalEval <dataset.json>
 * 指标（top_k=3）：Context Precision / Context Recall / MRR / Hit@k
 * 数据来源：eval/dataset_v2.json（24 篇知识库 + 24 QA，Java 知识库已逐字对齐）
 */
public class RetrievalEval {

    public static void main(String[] args) throws Exception {
        String datasetPath = args.length > 0 ? args[0]
                : "..\\eval\\dataset_v2.json";
        File f = new File(datasetPath);
        if (!f.exists()) {
            // 兜底：从 java-impl 目录向上找
            f = new File("..\\eval\\dataset_v2.json");
        }
        System.out.println("评测数据集: " + f.getAbsolutePath() + " exists=" + f.exists());

        ObjectMapper mapper = new ObjectMapper();
        JsonNode root = mapper.readTree(f);
        JsonNode qaPairs = root.get("qa_pairs");
        int total = qaPairs.size();
        int topK = 3;

        LongTermMemoryService memory = new LongTermMemoryService(new TfVectorRetriever());

        double precisionSum = 0, recallSum = 0, mrrSum = 0;
        int hitCount = 0;

        for (int i = 0; i < total; i++) {
            JsonNode qa = qaPairs.get(i);
            String question = qa.get("question").asText();
            List<String> relevant = new ArrayList<>();
            qa.get("relevant").forEach(n -> relevant.add(n.asText()));

            List<Map<String, Object>> docs = memory.search(question, topK);
            List<String> retrieved = docs.stream()
                    .map(d -> String.valueOf(d.get("source")))
                    .toList();

            // Precision@k: 命中相关数 / k；Recall@k: 命中相关数 / 相关总数
            int hits = 0;
            double rr = 0;
            for (int r = 0; r < retrieved.size(); r++) {
                if (relevant.contains(retrieved.get(r))) {
                    hits++;
                    if (rr == 0) rr = 1.0 / (r + 1);
                }
            }
            precisionSum += (double) hits / topK;
            recallSum += (double) hits / relevant.size();
            mrrSum += rr;
            if (hits > 0) hitCount++;

            System.out.printf("[%2d] Q: %s%n", i + 1, question);
            System.out.printf("     相关: %s%n", relevant);
            System.out.printf("     检出: %s (hits=%d, rr=%.3f)%n", retrieved, hits, rr);
        }

        double precision = precisionSum / total;
        double recall = recallSum / total;
        double mrr = mrrSum / total;
        double hitRate = (double) hitCount / total;

        System.out.println();
        System.out.println("========== Java 版混合检索评测 (BM25 + TF向量 + RRF, top_k=" + topK + ") ==========");
        System.out.printf("样本数: %d   知识库文档数: 24%n", total);
        System.out.printf("Context Precision: %.2f%%%n", precision * 100);
        System.out.printf("Context Recall:    %.2f%%%n", recall * 100);
        System.out.printf("MRR:               %.4f%n", mrr);
        System.out.printf("Hit@%d:             %.2f%%%n", topK, hitRate * 100);
        System.out.println();
        System.out.println("对照 Python 版 (eval/rag_eval.py, 24样本):");
        System.out.println("  BM25单路  84.72% / 78.47% / 1.0000 / 100.00%");
        System.out.println("  向量单路  59.72% / 56.94% / 0.8681 / 100.00%");
        System.out.println("  混合RRF   75.00% / 70.83% / 0.9850 / 100.00%");
    }
}