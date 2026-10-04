package com.smartcs.agent;

import com.smartcs.mcp.MCPToolServer;
import com.smartcs.tracing.AgentTracer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 合规审查Agent — 金融场景合规检查。
 *
 * 两阶段审查机制（对齐 Python 版 compliance_checker.py）：
 * 1. 规则引擎快速检查（毫秒级，不调用LLM）：敏感词 + PII泄露
 *    —— 违规则快速失败，跳过 LLM（省调用）
 * 2. LLM深度审查（秒级）：规则无法覆盖的隐晦违规（夸大收益、隐瞒风险等）
 *
 * 输出：
 * - passed / risk_level / violations
 * - masked_response：PII 脱敏后的完整回复文本，回传给 synthesize 作为最终响应
 * - tool_risk_level：MCP risk_check（金额规则）结果，供审计/指标
 */
@Component
public class ComplianceCheckerAgent implements BaseAgent {

    private static final Logger log = LoggerFactory.getLogger(ComplianceCheckerAgent.class);

    private static final List<String> FORBIDDEN_TERMS = List.of(
            "保证收益", "稳赚不赔", "零风险", "保本保息",
            "最高收益", "预期收益率", "承诺回报",
            "内部消息", "内幕", "暗箱操作"
    );

    private static final Map<String, Pattern> PII_PATTERNS = Map.of(
            "phone", Pattern.compile("1[3-9]\\d{9}"),
            "id_card", Pattern.compile("\\d{17}[\\dXx]"),
            "bank_card", Pattern.compile("\\d{16,19}")
    );

    private static final String LLM_REVIEW_PROMPT = """
            你是金融合规审查员。审查以下客服回复是否存在违规风险。
            检查维度：
            1. 夸大/保证收益（如"稳赚""包赚""保您赚钱"）——仅当 Agent 主动向用户承诺时才违规
            2. 隐瞒风险（主动推荐理财/投资时未提示"理财非存款、有风险"）
            3. 承诺/诱导（如"赶紧买""内部渠道""名额有限"）
            4. 歧视/辱骂/情绪化表达
            5. 泄露其他用户隐私（如提及他人账户信息）

            重要豁免（以下不算违规）：
            - 转述产品文档/合同条款的客观描述（如"到期一次性兑付本息""年化收益率3.5%-5.2%"），这是引用产品说明书，不是 Agent 的承诺
            - 风险提示语句本身（如"理财非存款，产品有风险，投资须谨慎"）
            - 提示信息不足、建议转人工/咨询官方渠道的表述

            只输出一行结论：
            - 合规则输出：PASS
            - 违规则输出：VIOLATION: <具体原因>

            待审查的客服回复：
            %s
            """;

    private final ChatClient chatClient;
    private final AgentTracer tracer;
    private final MCPToolServer mcpServer;

    public ComplianceCheckerAgent(
            ChatClient.Builder chatClientBuilder,
            AgentTracer tracer,
            MCPToolServer mcpServer) {
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
        this.tracer = tracer;
        this.mcpServer = mcpServer;
    }

    @Override
    public AgentState process(AgentState state) {
        return tracer.trace("compliance_checker", "process", () -> {
            Map<String, Object> subResults = state.getSubResults();
            StringBuilder contentBuilder = new StringBuilder();

            for (Map.Entry<String, Object> entry : subResults.entrySet()) {
                if (entry.getValue() instanceof String val) {
                    contentBuilder.append(val).append("\n");
                }
            }

            String content = contentBuilder.toString().trim();
            if (content.isBlank()) {
                state.setCompliancePassed(true);
                return state;
            }

            // ===== 阶段 1：规则引擎（毫秒级，先跑）=====
            List<String> violations = ruleBasedCheck(content);
            String sanitized = maskPII(content);

            // ===== 阶段 2：LLM 深度审查（仅当规则引擎通过时才调用，节省成本）=====
            boolean llmReviewed = false;
            if (violations.isEmpty()) {
                llmReviewed = true;
                List<String> llmViolations = llmDeepReview(content);
                violations.addAll(llmViolations);
            }

            boolean passed = violations.isEmpty();
            String riskLevel = violations.isEmpty() ? "low" :
                    (violations.stream().anyMatch(v -> v.contains("PII")) ? "high" : "medium");

            // 额外调用 MCP 风控工具（金额规则），叠加到合规结果中供审计/指标
            Map<String, Object> riskCall = mcpServer.callTool("risk_check", Map.<String, Object>of(
                    "user_id", state.getUserId() != null ? state.getUserId() : "anonymous",
                    "action", "reply_check",
                    "amount", 0
            ));
            String toolRisk = "low";
            if (Boolean.TRUE.equals(riskCall.get("success")) && riskCall.get("result") instanceof Map<?, ?> r) {
                Object rl = r.get("risk_level");
                toolRisk = rl != null ? String.valueOf(rl) : "low";
            }

            Map<String, Object> compliance = new LinkedHashMap<>();
            compliance.put("passed", passed);
            compliance.put("risk_level", riskLevel);
            compliance.put("tool_risk_level", toolRisk);
            compliance.put("llm_reviewed", llmReviewed);
            compliance.put("violations", violations);
            // 脱敏后的完整回复 —— 回传给 synthesize 作为最终响应（对齐 Python masked_response）
            if (!sanitized.equals(content)) {
                compliance.put("masked_response", sanitized);
            }

            state.setCompliancePassed(passed);
            state.addSubResult("compliance", compliance);

            if (!passed) {
                log.warn("[compliance] 审查未通过 violations={} llm_reviewed={}", violations, llmReviewed);
            }
            return state;
        });
    }

    /** LLM 深度审查：针对规则引擎无法覆盖的隐晦违规。失败时返回空列表（不阻塞主流程）。 */
    private List<String> llmDeepReview(String content) {
        try {
            var call = chatClient.prompt()
                    .system(LLM_REVIEW_PROMPT)
                    .user(content)
                    .call();
            ChatResponse cr = call.chatResponse();
            tracer.recordUsage("compliance_checker", "llm_review", cr);

            String verdict = cr != null && cr.getResult() != null && cr.getResult().getOutput() != null
                    ? cr.getResult().getOutput().getText() : "";
            if (verdict == null || verdict.isBlank()) {
                log.info("[compliance] LLM审查返回空结果，视为通过");
                return List.of();
            }
            verdict = verdict.trim();
            if (verdict.toUpperCase().startsWith("VIOLATION")) {
                String reason = verdict.substring("VIOLATION".length()).trim();
                if (reason.isEmpty()) reason = "隐晦违规";
                return List.of("LLM深度审查: " + reason);
            }
            return List.of();
        } catch (Exception e) {
            // LLM 审查失败不阻塞主流程，仅记录（对齐 Python 的宽处理）
            log.warn("[compliance] LLM深度审查失败，跳过: {}", e.getMessage());
            return List.of();
        }
    }

    private List<String> ruleBasedCheck(String content) {
        List<String> violations = new ArrayList<>();

        for (String term : FORBIDDEN_TERMS) {
            if (content.contains(term)) {
                violations.add("包含违规金融用语: '" + term + "'");
            }
        }

        for (Map.Entry<String, Pattern> entry : PII_PATTERNS.entrySet()) {
            Matcher matcher = entry.getValue().matcher(content);
            if (matcher.find()) {
                String label = switch (entry.getKey()) {
                    case "phone" -> "手机号";
                    case "id_card" -> "身份证号";
                    case "bank_card" -> "银行卡号";
                    default -> entry.getKey();
                };
                violations.add("检测到PII信息泄露: " + label);
            }
        }

        return violations;
    }

    private String maskPII(String content) {
        String masked = content;
        for (Map.Entry<String, Pattern> entry : PII_PATTERNS.entrySet()) {
            masked = entry.getValue().matcher(masked).replaceAll(match -> {
                String text = match.group();
                if (text.length() <= 4) return "****";
                return text.substring(0, 3) + "*".repeat(text.length() - 6) + text.substring(text.length() - 3);
            });
        }
        return masked;
    }

    @Override
    public String getName() {
        return "compliance_checker";
    }
}