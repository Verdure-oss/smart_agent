package com.smartcs.memory;

import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * TF 向量召回实现（默认后端，纯本地零依赖）。
 *
 * 与向量存储抽象：将文档用 2-gram 中文 / 英文整词切分后构建稀疏 TF 向量，查询用余弦相似度
 * 计算距离。不依赖任何外部 embedding 模型 / 向量库，保证本地能零部署跑通全链路。
 * 生产可将 DI 替换为 {@link MilvusVectorRetriever}，上层 RRF 融合逻辑不变。
 */
@Component
public class TfVectorRetriever implements VectorRetriever {

    private final List<Map<String, Object>> documents = new ArrayList<>();
    private final List<Map<String, Double>> docTf = new ArrayList<>();

    @Override
    public String backend() {
        return "memory-tf";
    }

    @Override
    public void load(List<Map<String, Object>> docs) {
        documents.clear();
        docTf.clear();
        if (docs != null) {
            documents.addAll(docs);
            for (Map<String, Object> doc : docs) {
                docTf.add(tokenize((String) doc.get("content")));
            }
        }
    }

    @Override
    public List<Map<String, Object>> search(String query, int topK) {
        Map<String, Double> qTf = tokenize(query);
        if (qTf.isEmpty() || documents.isEmpty()) {
            return List.of();
        }
        double qNorm = norm(qTf);

        List<Map<String, Object>> scored = new ArrayList<>();
        for (int i = 0; i < documents.size(); i++) {
            double score = cosine(qTf, qNorm, docTf.get(i));
            Map<String, Object> doc = new HashMap<>(documents.get(i));
            doc.put("vector_score", score);
            scored.add(doc);
        }

        scored.sort((a, b) -> Double.compare(
                ((Number) b.get("vector_score")).doubleValue(),
                ((Number) a.get("vector_score")).doubleValue()));

        return scored.size() <= topK ? scored : scored.subList(0, topK);
    }

    // ---------- TF 向量 + 余弦 ----------

    private double cosine(Map<String, Double> qTf, double qNorm, Map<String, Double> dTf) {
        if (qNorm == 0.0 || dTf.isEmpty()) return 0.0;
        double dot = 0.0;
        for (Map.Entry<String, Double> q : qTf.entrySet()) {
            Double dv = dTf.get(q.getKey());
            if (dv != null) dot += q.getValue() * dv;
        }
        return dot / (qNorm * norm(dTf));
    }

    private double norm(Map<String, Double> tf) {
        double sum = 0.0;
        for (double v : tf.values()) sum += v * v;
        return Math.sqrt(sum);
    }

    private Map<String, Double> tokenize(String text) {
        Map<String, Double> tf = new HashMap<>();
        String s = (text == null ? "" : text).toLowerCase();
        List<String> grams = new ArrayList<>();
        for (String w : s.split("[^a-z0-9]+")) {
            if (!w.isEmpty()) grams.add(w);
        }
        StringBuilder cjk = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            boolean isCjk = c >= 0x4e00 && c <= 0x9fff;
            if (isCjk) cjk.append(c);
            else if (cjk.length() > 0) {
                addGrams(cjk.toString(), grams);
                cjk.setLength(0);
            }
        }
        if (cjk.length() > 0) addGrams(cjk.toString(), grams);
        for (String g : grams) tf.merge(g, 1.0, Double::sum);
        return tf;
    }

    private void addGrams(String cjk, List<String> grams) {
        if (cjk.length() == 1) {
            grams.add(cjk);
        } else {
            for (int i = 0; i + 2 <= cjk.length(); i++) {
                grams.add(cjk.substring(i, i + 2));
            }
        }
    }
}