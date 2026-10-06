package com.smartcs.memory;

import java.util.List;
import java.util.Map;

/**
 * 检索验证（临时工具）：验证 BM25+TF向量+RRF 混合检索在品牌词问题上的 top-3 命中。
 * 运行：mvn -q compile 后 java -cp "target/classes;..." 或由测试脚本起 Spring 上下文。
 */
public class RetrievalCheck {

    public static void main(String[] args) {
        LongTermMemoryService svc = new LongTermMemoryService(new TfVectorRetriever());
        String[] queries = {
                "金葵理财的收益率是多少",
                "金葵理财最低要投多少钱",
                "退款多久到账",
                "开户需要什么材料",
                "快汇转账单笔限额多少",
                "慧投定投是什么",
                "保险犹豫期多久",
                "风险评估问卷有效期"
        };
        for (String q : queries) {
            List<Map<String, Object>> docs = svc.search(q, 3);
            StringBuilder sb = new StringBuilder();
            for (Map<String, Object> d : docs) {
                String src = String.valueOf(d.get("source"));
                String content = String.valueOf(d.get("content"));
                String head = content.length() > 28 ? content.substring(0, 28) : content;
                sb.append("[").append(src).append("] ").append(head).append(" | ");
            }
            System.out.println("Q: " + q);
            System.out.println("    " + sb);
        }
    }
}