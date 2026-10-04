package com.smartcs.eval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.smartcs.memory.LongTermMemoryService;
import com.smartcs.memory.Reranker;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.io.File;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 评测端点：跑"召回(top_k=5) → LLM 重排(top_k=3)"的完整链路，输出离线 IR 指标
 * （Precision / Recall / MRR），并导出检索结果供 RAGAS LLM-judge 复用。
 *
 * 用法：启动后 GET /api/eval/rerank?dataset=..\eval\dataset_v2.json
 * 不干扰正常聊天链路（仅评测时调用）。返回新 dump 供 eval/ragas_judge.py --retrieval 判分。
 */
@RestController
@RequestMapping("/api/eval")
public class RerankEvalEndpoint {

    private final LongTermMemoryService longTermMemory;
    private final Reranker reranker;
    private final ObjectMapper mapper = new ObjectMapper();

    public RerankEvalEndpoint(LongTermMemoryService longTermMemory, Reranker reranker) {
        this.longTermMemory = longTermMemory;
        this.reranker = reranker;
    }

    @GetMapping("/rerank")
    public Map<String, Object> rerankEval(
            @RequestParam(defaultValue = "..\\eval\\dataset_v2.json") String dataset,
            @RequestParam(defaultValue = "..\\eval\\java_retrievals_rerank.json") String out) throws Exception {

        File ds = new File(dataset);
        JsonNode root = mapper.readTree(ds);
        JsonNode qaPairs = root.get("qa_pairs");
        int topK = 3;
        int candidateK = 5;
        int total = qaPairs.size();

        List<Map<String, Object>> records = new ArrayList<>();
        double precisionSum = 0, recallSum = 0, mrrSum = 0;
        int hitCount = 0;

        for (int i = 0; i < total; i++) {
            JsonNode qa = qaPairs.get(i);
            String question = qa.get("question").asText();
            String reference = qa.has("answer") ? qa.get("answer").asText() : "";

            List<String> relevant = new ArrayList<>();
            qa.get("relevant").forEach(n -> relevant.add(n.asText()));

            // 召回候选 candidateK → LLM 重排 topK
            List<Map<String, Object>> candidates = longTermMemory.recall(question, candidateK);
            List<Map<String, Object>> docs = reranker.rerank(question, candidates, topK);
            List<String> retrieved = docs.stream()
                    .map(d -> String.valueOf(d.get("source")))
                    .toList();

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

            records.add(Map.of(
                    "question", question,
                    "reference", reference,
                    "chunks", docs.stream().map(d -> String.valueOf(d.get("content"))).toList()
            ));
        }

        mapper.writerWithDefaultPrettyPrinter().writeValue(new File(out), records);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("samples", total);
        result.put("pipeline", "recall(top_k=" + candidateK + ") -> LLM rerank(top_k=" + topK + ")");
        result.put("context_precision", Math.round(precisionSum / total * 10000) / 100.0);
        result.put("context_recall", Math.round(recallSum / total * 10000) / 100.0);
        result.put("mrr", Math.round(mrrSum / total * 10000) / 10000.0);
        result.put("hit_at_3", Math.round((double) hitCount / total * 10000) / 100.0);
        result.put("dump_written", new File(out).getAbsolutePath());
        return result;
    }
}