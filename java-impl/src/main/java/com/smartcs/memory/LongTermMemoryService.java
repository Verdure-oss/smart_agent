package com.smartcs.memory;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Service;

import java.io.File;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import org.apache.tika.metadata.Metadata;
import org.apache.tika.parser.AutoDetectParser;
import org.apache.tika.sax.BodyContentHandler;

/**
 * 长期记忆服务 — 混合检索（BM25 + TF向量 + RRF），对齐 Python 版 hybrid_search。
 *
 * 双路召回：
 *  1) BM25 路：2-gram 分词 + BM25 打分，擅长专有名词/关键词精确匹配
 *  2) 向量路  ：2-gram TF 向量余弦相似度（纯本地近似向量，无外部 embedding 依赖）
 *  RRF 融合：score = Σ 1/(60 + rank)，按排序位置而不是绝对分数合并
 *
 * 生产环境可平滑升级：把"TF 向量路"替换为真实 embedding（Spring AI / Milvus），RRF 接口不变。
 */
@Service
public class LongTermMemoryService {

    private static final int RRF_K = 60;
    private static final double BM25_K1 = 1.5;
    private static final double BM25_B = 0.75;

    private final List<Map<String, Object>> documents = new ArrayList<>();
    private final List<Map<String, Double>> docTf = new ArrayList<>();     // 每篇文档的 2-gram 词频
    private final Map<String, Integer> df = new HashMap<>();                // 每个 gram 的文档频率
    private double avgDocLen = 0.0;

    public LongTermMemoryService() {
        loadKnowledgeBase();
        rebuildIndex();
    }

    /**
     * 知识入库流水线：优先从知识库目录扫描加载 .md / .txt 文档（按目录 / 文件名作为来源），
     * 目录不存在或为空时回退到内置默认知识库（对齐 Python 版 dataset_v2 24 篇）。
     *
     * 知识库目录顺序（取第一个存在的）：
     *   1) 环境变量 SMARTCS_KB_DIR
     *   2) ../knowledge_base（项目根目录，与 Python 版共用）
     *   3) ./knowledge_base
     */
    private void loadKnowledgeBase() {
        List<Path> kbDirs = new ArrayList<>();
        String envDir = System.getenv("SMARTCS_KB_DIR");
        if (envDir != null && !envDir.isBlank()) kbDirs.add(Paths.get(envDir));
        kbDirs.add(Paths.get("..", "knowledge_base"));
        kbDirs.add(Paths.get("knowledge_base"));

        for (Path dir : kbDirs) {
            int loaded = loadFromDirectory(dir);
            if (loaded > 0) {
                System.out.println("[KB] 从目录加载 " + loaded + " 篇文档: " + dir.toAbsolutePath());
                return;
            }
        }
        // 目录为空或不存在 → 内置默认知识库
        loadDefaultKnowledgeBase();
        System.out.println("[KB] 未找到知识库目录，使用内置默认知识库 " + documents.size() + " 篇");
    }

    /** 扫描目录（含子目录）加载知识文档，返回加载篇数（按切块后 chunk 计）。MD/TXT 直读，PDF/Word 走 Tika。 */
    private int loadFromDirectory(Path dir) {
        if (!Files.isDirectory(dir)) return 0;
        int count = 0;
        try (Stream<Path> walk = Files.walk(dir)) {
            List<Path> files = walk
                    .filter(Files::isRegularFile)
                    .filter(this::isSupportedDoc)
                    .sorted()
                    .toList();
            for (Path f : files) {
                try {
                    String content = extractText(f).trim();
                    if (content.isEmpty()) continue;
                    // 按 512 字符切块（含 128 重叠），对齐 Python 版分块策略
                    List<String> chunks = chunkText(content, 512, 128);
                    String source = dir.relativize(f).toString().replace(File.separatorChar, '/');
                    for (String chunk : chunks) {
                        addDocument(chunk, source);
                        count++;
                    }
                } catch (Exception e) {
                    System.err.println("[KB] 读取失败: " + f + " -> " + e.getMessage());
                }
            }
        } catch (Exception e) {
            System.err.println("[KB] 扫描目录失败: " + dir + " -> " + e.getMessage());
            return 0;
        }
        return count;
    }

    /** 是否支持的知识文档格式：.md/.txt 直读；PDF/Word/PPT/HTML/RTF 走 Tika。 */
    private boolean isSupportedDoc(Path p) {
        String n = p.getFileName().toString().toLowerCase();
        return n.endsWith(".md") || n.endsWith(".txt") || n.endsWith(".markdown")
                || n.endsWith(".pdf") || n.endsWith(".doc") || n.endsWith(".docx")
                || n.endsWith(".ppt") || n.endsWith(".pptx") || n.endsWith(".html") || n.endsWith(".rtf");
    }

    /** 提取文档纯文本：MD/TXT 直读（并清掉 markdown 标题）；其他格式经 Apache Tika AutoDetectParser 抽取。 */
    private String extractText(Path f) throws Exception {
        String n = f.getFileName().toString().toLowerCase();
        if (n.endsWith(".md") || n.endsWith(".txt") || n.endsWith(".markdown")) {
            return cleanMarkdown(Files.readString(f, StandardCharsets.UTF_8));
        }
        try (InputStream in = Files.newInputStream(f)) {
            BodyContentHandler handler = new BodyContentHandler(-1);
            Metadata metadata = new Metadata();
            new AutoDetectParser().parse(in, handler, metadata);
            return handler.toString();
        }
    }

    /** 简单清洗：去掉 markdown 标题行（# 开头），避免检索内容混入无意义标题 token。 */
    private static String cleanMarkdown(String text) {
        StringBuilder sb = new StringBuilder(text.length());
        for (String line : text.split("\n", -1)) {
            if (line.trim().startsWith("#")) continue;
            sb.append(line).append('\n');
        }
        return sb.toString();
    }

    /** 文本分块：固定长度 + 重叠窗口（对齐 Python LongTermMemory._chunk_text）。 */
    private static List<String> chunkText(String text, int chunkSize, int overlap) {
        List<String> chunks = new ArrayList<>();
        if (text.length() <= chunkSize) {
            chunks.add(text);
            return chunks;
        }
        int start = 0;
        while (start < text.length()) {
            int end = Math.min(start + chunkSize, text.length());
            chunks.add(text.substring(start, end).trim());
            if (end >= text.length()) break;
            start = end - overlap;
        }
        return chunks;
    }

    private void loadDefaultKnowledgeBase() {
        // 与 Python 版评测数据集 eval/dataset_v2.json 的 24 篇知识库逐字对齐（同库同口径）
        addDocument("【金葵理财】收益说明：金葵理财年化收益率3.5%-5.2%，支持6个月至3年持有周期，开放日前可提交赎回申请，到期一次性兑付本息。", "p1_wealth_income.md");
        addDocument("【金葵理财】起投要求：金葵理财最低投资金额10000元，追加投资按1000元整数倍递增，支持自动续投功能。", "p2_wealth_min.md");
        addDocument("【金葵理财】风险提示：金葵理财为净值型产品，理财非存款，不承诺保本保息，投资须谨慎。", "p3_wealth_risk.md");
        addDocument("【无忧退款】政策说明：无忧退款服务支持购买后7天内无理由退款，超过7天需提供合理原因，按实付金额原路退回。", "r1_refund_policy.md");
        addDocument("【无忧退款】流程说明：提交退款申请后，平台审核通过即原路退回支付账户，退款处理周期3-5个工作日。", "r2_refund_flow.md");
        addDocument("【无忧退款】到账时间：微信支付退款约1-3个工作日，支付宝约1-3个工作日，银行卡约3-5个工作日到账。", "r3_refund_time.md");
        addDocument("【合盈开户】所需材料：合盈开户需身份证原件、开户申请表、视频认证确认本人操作、设置交易密码与资金密码。", "a1_open_material.md");
        addDocument("【合盈开户】流程介绍：填写申请表→视频认证→设置交易密码→完成风险评估问卷，全程约15-30分钟。", "a2_open_flow.md");
        addDocument("【合盈开户】适用范围：境内个人投资者可在线自助开户，企业客户需到柜台办理，未成年人需监护人陪同。", "a3_open_scope.md");
        addDocument("【闪电购】订单查询：提供订单号或用户ID即可查询订单状态、物流进度与购买金额，支持近两年历史记录。", "o1_order_query.md");
        addDocument("【闪电购】物流信息：发货后可在订单详情页查看物流轨迹，每2小时更新一次，签收后自动确认收货。", "o2_order_logistics.md");
        addDocument("【闪电购】换货服务：闪电购订单签收7天内可申请无理由换货，超期更换需联系人工客服审核，食品与定制类不适用。", "o3_order_after.md");
        addDocument("【智评风测】有效期：智评风测测评结果有效期为两年，期间收入、资产或投资经验发生重大变化建议重新测评。", "k1_risk_valid.md");
        addDocument("【智评风测】等级划分：保守型、稳健型、平衡型、进取型四档，不同等级对应不同的可购买产品范围。", "k2_risk_level.md");
        addDocument("【智评风测】问卷内容：包含投资经验、收入来源、风险承受能力、投资周期偏好等维度，共20道题目。", "k3_risk_quiz.md");
        addDocument("【快汇转账】限额说明：快汇转账单笔限额5万元，单日累计限额20万元，大额转账需通过视频认证后办理。", "t1_transfer_limit.md");
        addDocument("【快汇转账】笔数规则：快汇转账单笔最小金额1元，每日最多发起30笔交易，单日出款总限额与账户安全等级挂钩。", "t2_transfer_rule.md");
        addDocument("【快汇转账】跨境业务：需提供资金来源与用途证明，单笔限额5万美元等值，支持美元、欧元、港币等币种。", "t3_transfer_cross.md");
        addDocument("【慧投定投】机制说明：慧投定投每月固定日期从绑定银行卡自动扣款申购，可平摊持仓成本，适合长期投资积累。", "f1_fund_ding.md");
        addDocument("【慧投定投】费率与扣款：慧投定投申购费率为0.15%，每笔定投按约定金额自动扣款，份额确认后生效，不收取账户管理费。", "f2_fund_fee.md");
        addDocument("【慧投定投】赎回规则：T+1日确认份额，赎回资金在确认后的第3天可支取，货币基金支持快速赎回应急支用。", "f3_fund_redeem.md");
        addDocument("【安心保】犹豫期：安心保保险犹豫期一般为15天，犹豫期内退保全额退还已交保费，用户无任何资金损失。", "i1_ins_cooling.md");
        addDocument("【安心保】现金价值：超过犹豫期退保按保单现金价值退付，前期现金价值较低，退保前请谨慎评估。", "i2_ins_cashvalue.md");
        addDocument("【安心保】满期领取：保单满期后，可通过APP自助领取满期金或携带身份证到柜台办理领取手续。", "i3_ins_maturity.md");
    }

    public void addDocument(String content, String source) {
        Map<String, Object> doc = new HashMap<>();
        doc.put("id", UUID.randomUUID().toString().substring(0, 12));
        doc.put("content", content);
        doc.put("source", source);
        documents.add(doc);
    }

    /** 重新构建分词索引（新增文档后调用） */
    public void rebuildIndex() {
        docTf.clear();
        df.clear();
        long totalLen = 0;
        for (Map<String, Object> doc : documents) {
            Map<String, Double> tf = tokenize((String) doc.get("content"));
            docTf.add(tf);
            totalLen += tf.values().stream().mapToDouble(Double::doubleValue).sum();
            for (String g : tf.keySet()) {
                df.merge(g, 1, Integer::sum);
            }
        }
        avgDocLen = documents.isEmpty() ? 0.0 : (double) totalLen / documents.size();
    }

    /**
     * 混合召回（放大候选池）— 对齐 Python 版 hybrid_search 的召回阶段。
     * 返回 candidateK 个候选（默认 5），不打 RRF 截到 topK，交给下游 Reranker 精排。
     * 字段含 score(RRF)。
     */
    public List<Map<String, Object>> recall(String query, int candidateK) {
        return rrfHybrid(query, candidateK);
    }

    /**
     * 混合检索（召回 → RRF 截断到 topK）— 兼容旧接口，等效于不重排的 RRF 检索。
     */
    public List<Map<String, Object>> search(String query, int topK) {
        return rrfHybrid(query, topK);
    }

    private List<Map<String, Object>> rrfHybrid(String query, int topK) {
        Map<String, Double> queryTf = tokenize(query);
        if (queryTf.isEmpty()) {
            return List.of();
        }
        double queryNorm = norm(queryTf);
        int n = documents.size();

        // 1) BM25 打分
        Map<String, Double> bm25Scores = new HashMap<>();
        for (int i = 0; i < n; i++) {
            double score = bm25(queryTf, docTf.get(i), n);
            bm25Scores.put(docId(i), score);
        }

        // 2) TF 向量余弦
        Map<String, Double> vecScores = new HashMap<>();
        for (int i = 0; i < n; i++) {
            double score = cosine(queryTf, queryNorm, docTf.get(i));
            vecScores.put(docId(i), score);
        }

        // 3) RRF 融合
        List<String> bm25Ranked = rankByScore(bm25Scores);
        List<String> vecRanked = rankByScore(vecScores);
        Map<String, Double> rrf = new HashMap<>();
        for (int i = 0; i < bm25Ranked.size(); i++) {
            rrf.merge(bm25Ranked.get(i), 1.0 / (RRF_K + i + 1), Double::sum);
        }
        for (int i = 0; i < vecRanked.size(); i++) {
            rrf.merge(vecRanked.get(i), 1.0 / (RRF_K + i + 1), Double::sum);
        }

        List<String> top = rrf.entrySet().stream()
                .sorted((a, b) -> Double.compare(b.getValue(), a.getValue()))
                .limit(topK)
                .map(Map.Entry::getKey)
                .collect(Collectors.toList());

        return top.stream().map(this::toResult).collect(Collectors.toList());
    }

    // ---------- BM25 ----------

    private double bm25(Map<String, Double> queryTf, Map<String, Double> docTf, int n) {
        double docLen = docTf.values().stream().mapToDouble(Double::doubleValue).sum();
        double score = 0.0;
        for (Map.Entry<String, Double> q : queryTf.entrySet()) {
            String gram = q.getKey();
            Double tf = docTf.get(gram);
            if (tf == null) continue;
            int docFreq = df.getOrDefault(gram, 0);
            double idf = Math.log(1.0 + (n - docFreq + 0.5) / (docFreq + 0.5));
            double denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * docLen / (avgDocLen > 0 ? avgDocLen : 1.0));
            score += idf * tf * (BM25_K1 + 1) / denom;
        }
        return score;
    }

    // ---------- TF 向量余弦 ----------

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
        String s = text.toLowerCase();
        // 中文 2-gram + 英文/数字整词，构成统一 token 空间
        String[] alnumWords = s.split("[^a-z0-9]+");
        List<String> grams = new ArrayList<>();
        for (String w : alnumWords) {
            if (!w.isEmpty()) grams.add(w);
        }
        // 中文连续段按 2-gram 切
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

    private List<String> rankByScore(Map<String, Double> scores) {
        return scores.entrySet().stream()
                .filter(e -> e.getValue() > 0)
                .sorted((a, b) -> Double.compare(b.getValue(), a.getValue()))
                .map(Map.Entry::getKey)
                .collect(Collectors.toList());
    }

    private String docId(int i) {
        return String.valueOf(i);
    }

    private Map<String, Object> toResult(String docIdx) {
        Map<String, Object> doc = documents.get(Integer.parseInt(docIdx));
        Map<String, Object> result = new HashMap<>(doc);
        return result;
    }
}