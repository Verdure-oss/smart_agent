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
 * Java 版检索结果导出 —— 为 RAGAS 风格 LLM-judge 评测准备输入。
 *
 * 与 Python eval/ragas_judge.py 同口径：导出每个样本的
 *   { question, reference(参考答案), chunks(检索到的 top_k 内容) }
 * 再由 eval/ragas_judge_external.py 复用 RagasJudge 判分，
 * 得到与简历同口径的 Context Precision / Context Recall。
 *
 * 用法：java -cp ... com.smartcs.eval.RetrievalRagasDump [dataset.json] [out.json] [top_k]
 */
public class RetrievalRagasDump {

    public static void main(String[] args) throws Exception {
        String datasetPath = args.length > 0 ? args[0] : "..\\eval\\dataset_v2.json";
        String outPath = args.length > 1 ? args[1] : "..\\eval\\java_retrievals.json";
        int topK = args.length > 2 ? Integer.parseInt(args[2]) : 3;

        File in = new File(datasetPath);
        File out = new File(outPath);

        ObjectMapper mapper = new ObjectMapper();
        JsonNode root = mapper.readTree(in);
        JsonNode qaPairs = root.get("qa_pairs");

        LongTermMemoryService memory = new LongTermMemoryService(new TfVectorRetriever());

        List<Map<String, Object>> records = new ArrayList<>();
        for (JsonNode qa : qaPairs) {
            String question = qa.get("question").asText();
            String reference = qa.has("answer") ? qa.get("answer").asText() : "";

            List<Map<String, Object>> docs = memory.search(question, topK);
            List<String> chunks = docs.stream()
                    .map(d -> String.valueOf(d.get("content")))
                    .toList();

            Map<String, Object> rec = new LinkedHashMap<>();
            rec.put("question", question);
            rec.put("reference", reference);
            rec.put("chunks", chunks);
            records.add(rec);
        }

        mapper.writerWithDefaultPrettyPrinter().writeValue(out, records);
        System.out.println("已导出 " + records.size() + " 条检索结果 -> " + out.getAbsolutePath());
    }
}