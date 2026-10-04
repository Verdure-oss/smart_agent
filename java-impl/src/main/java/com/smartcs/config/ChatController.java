package com.smartcs.config;

import com.smartcs.agent.AgentState;
import com.smartcs.agent.SupervisorAgent;
import com.smartcs.memory.ShortTermMemoryService;
import com.smartcs.memory.ShortTermMemoryService.InjectionContext;
import com.smartcs.memory.ShortTermMemoryService.SummaryState;
import com.smartcs.memory.WorkingMemoryService;
import com.smartcs.mcp.MCPToolServer;
import com.smartcs.tracing.AgentTracer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.client.advisor.SimpleLoggerAdvisor;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * REST API控制器 — 提供聊天、历史、工具、指标接口。
 *
 * 阶段 2 变更：
 * - 滚动摘要压缩（对齐 Python 版 api/main.py 的 _maybe_compact_history）：
 *   未压缩消息超过 COMPACT_THRESHOLD 时，把旧消息用 LLM 压成摘要，仅保留最近几轮原文；
 * - 按需注入：给图注入「摘要 + 最近几轮原文 + 当前消息」，替代全量历史塞 Prompt；
 * - Token 计量：用简单估算对比"全量基线 vs 按需注入"，经日志与 /api/metrics 观察。
 */
@RestController
@RequestMapping("/api")
@CrossOrigin(origins = "*")
public class ChatController {

    private static final Logger log = LoggerFactory.getLogger(ChatController.class);

    /** 未压缩新消息超过该值触发一次滚动压缩 */
    static final int COMPACT_THRESHOLD = 6;
    /** 注入时保留的最近对话轮数（每轮=用户+客服两条） */
    static final int RECENT_TURNS = 4;
    /** 回填 AgentState.messages 的最大轮数（拆解/意图路由可看到的历史窗口） */
    static final int STATE_MESSAGE_TURNS = 5;

    private static final String SUMMARY_PROMPT = """
            你是客服对话摘要器。请把下面的历史对话压缩成一段100字以内的摘要。
            要求：
            1. 保留所有关键实体：产品名、订单号、金额、时间、用户诉求、待办事项
            2. 丢弃寒暄和重复信息
            3. 如果已有"历史摘要："开头的内容，请合并进新摘要，不要遗漏旧实体
            只输出摘要本身，不要任何前缀。

            待压缩内容：
            %s
            """;

    private final SupervisorAgent supervisor;
    private final ShortTermMemoryService shortTermMemory;
    private final WorkingMemoryService workingMemory;
    private final MCPToolServer mcpServer;
    private final AgentTracer tracer;
    private final ChatClient chatClient;

    public ChatController(
            SupervisorAgent supervisor,
            ShortTermMemoryService shortTermMemory,
            WorkingMemoryService workingMemory,
            MCPToolServer mcpServer,
            AgentTracer tracer,
            ChatClient.Builder chatClientBuilder) {
        this.supervisor = supervisor;
        this.shortTermMemory = shortTermMemory;
        this.workingMemory = workingMemory;
        this.mcpServer = mcpServer;
        this.tracer = tracer;
        this.chatClient = chatClientBuilder.defaultAdvisors(new SimpleLoggerAdvisor()).build();
    }

    @PostMapping("/chat")
    public ResponseEntity<Map<String, Object>> chat(@RequestBody Map<String, String> request) {
        String message = request.getOrDefault("message", "");
        String userId = request.getOrDefault("user_id", "anonymous");
        String sessionId = request.getOrDefault("session_id", UUID.randomUUID().toString());

        shortTermMemory.addMessage(sessionId, "user", message);

        // 1) 滚动摘要压缩（仅在新增未压缩消息数 > 阈值时调一次 LLM）
        maybeCompactHistory(sessionId);

        // 2) 按需注入：摘要 + 最近几轮原文（对齐 Python get_injection_context）
        AgentState state = new AgentState(userId, sessionId, message);
        InjectionContext ctx = shortTermMemory.getInjectionContext(sessionId, RECENT_TURNS);
        state.setHistorySummary(ctx.hasSummary() ? ctx.summaryText() : null);
        List<Map<String, String>> messages = new ArrayList<>();
        for (Map<String, Object> m : ctx.recentMessages()) {
            messages.add(Map.of(
                    "role", String.valueOf(m.getOrDefault("role", "user")),
                    "content", String.valueOf(m.getOrDefault("content", ""))));
        }
        // 工作记忆补充信息（跨轮收集）并入会话信息
        String wm = workingMemory.dump(sessionId);
        if (!wm.isEmpty()) {
            state.setWorkingInfo(wm);
        }
        state.setMessages(messages);

        // 3) Token 对比（估算口径：全量历史 vs 按需注入）
        List<Map<String, Object>> fullHistory = shortTermMemory.getHistory(sessionId);
        int baselineTokens = estimateTokens(fullHistory);
        int injectedTokens = estimateTokens(messages);
        int savedPct = baselineTokens > 0 ? (int) Math.round((baselineTokens - injectedTokens) * 100.0 / baselineTokens) : 0;
        log.info("[token] session={} baseline={} injected={} saved={}%", sessionId, baselineTokens, injectedTokens, savedPct);

        AgentState result = supervisor.orchestrate(state);

        shortTermMemory.addMessage(sessionId, "assistant", result.getFinalResponse());

        // 4) 简单补充信息收集：命中"工单/订单"上下文时记录订单号等（demo 口径）
        String fid = extractId(message, "订单号|订单|单号|TK-|ORD-");
        if (fid != null) {
            workingMemory.append(sessionId, "last_order", fid);
        }

        return ResponseEntity.ok(Map.of(
                "response", result.getFinalResponse(),
                "session_id", sessionId,
                "intent", result.getIntent() != null ? result.getIntent() : "unknown",
                "compliance_passed", result.isCompliancePassed(),
                "token_saved_pct", savedPct
        ));
    }

    /**
     * 滚动摘要压缩 — 对齐 Python 版 _maybe_compact_history。
     * 只压缩"未压缩的旧消息"，保留最近 RECENT_TURNS 轮原文；压缩失败不影响主流程。
     */
    private void maybeCompactHistory(String sessionId) {
        try {
            List<Map<String, Object>> history = shortTermMemory.getHistory(sessionId);
            SummaryState summary = shortTermMemory.getSummary(sessionId);
            int compactedUpto = Math.max(0, summary.compactedUpto());
            if (history.size() - compactedUpto <= COMPACT_THRESHOLD) {
                return; // 新消息未超阈值，不压缩
            }
            int cutoff = Math.max(compactedUpto, history.size() - RECENT_TURNS * 2);
            if (cutoff <= compactedUpto) {
                return;
            }
            List<Map<String, Object>> oldMsgs = history.subList(Math.min(compactedUpto, history.size()), Math.min(cutoff, history.size()));
            if (oldMsgs.isEmpty()) {
                return;
            }
            StringBuilder sb = new StringBuilder();
            if (summary.text() != null && !summary.text().isBlank()) {
                sb.append("历史摘要：").append(summary.text()).append("\n\n");
            }
            for (Map<String, Object> m : oldMsgs) {
                String role = "assistant".equals(m.get("role")) ? "客服" : "用户";
                sb.append(role).append(": ").append(m.getOrDefault("content", "")).append("\n");
            }
            var summaryResp = chatClient.prompt()
                    .system(SUMMARY_PROMPT)
                    .user(sb.toString())
                    .call();
            var cr = summaryResp.chatResponse();
            tracer.recordUsage("rolling_summary", "compact", cr);
            String newSummary = cr != null && cr.getResult() != null && cr.getResult().getOutput() != null
                    ? cr.getResult().getOutput().getText() : "";
            shortTermMemory.setSummary(sessionId, newSummary, cutoff);
            log.info("[summary] session={} compacted_upto={} old={} → new={}", sessionId, compactedUpto, cutoff,
                    newSummary == null ? "" : newSummary.replaceAll("\\s+", " "));
        } catch (Exception e) {
            // 压缩失败不阻塞聊天主流程
            log.warn("[summary] 压缩失败，跳过: {}", e.getMessage());
        }
    }

    /** 简单 Token 估算（中文按字、英文按 4 字符，近似口径；生产可换 tiktoken） */
    private int estimateTokens(List<?> items) {
        int tokens = 0;
        for (Object item : items) {
            if (item instanceof Map<?, ?> m) {
                Object c = m.get("content");
                if (c != null) tokens += estimateTokens(String.valueOf(c));
            } else if (item instanceof Map.Entry<?, ?> e) {
                tokens += estimateTokens(String.valueOf(e.getValue()));
            } else if (item != null) {
                tokens += estimateTokens(String.valueOf(item));
            }
        }
        return tokens;
    }

    private int estimateTokens(String text) {
        if (text == null) return 0;
        int cjk = 0;
        int other = 0;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c >= 0x4e00 && c <= 0x9fff) cjk++;
            else other++;
        }
        return cjk + (int) Math.ceil(other / 4.0);
    }

    /** demo 口径的实体抽取：从消息里抓订单号/工单号 */
    private String extractId(String text, String pattern) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("([A-Z]{2}-[0-9A-Za-z-]{4,})").matcher(text);
        if (m.find()) return m.group(1);
        return null;
    }

    @GetMapping("/history/{sessionId}")
    public ResponseEntity<Map<String, Object>> getHistory(@PathVariable String sessionId) {
        List<Map<String, Object>> history = shortTermMemory.getHistory(sessionId);
        SummaryState summary = shortTermMemory.getSummary(sessionId);
        return ResponseEntity.ok(Map.of(
                "session_id", sessionId,
                "messages", history,
                "summary", summary.text() == null ? "" : summary.text(),
                "compacted_upto", summary.compactedUpto()));
    }

    @GetMapping("/tools")
    public ResponseEntity<Map<String, Object>> listTools() {
        return ResponseEntity.ok(Map.of("tools", mcpServer.listTools()));
    }

    /** 工具调用（对齐 Python /api/tools/call） */
    @PostMapping("/tools/call")
    public ResponseEntity<Map<String, Object>> callTool(@RequestBody Map<String, Object> request) {
        String name = String.valueOf(request.getOrDefault("name", ""));
        @SuppressWarnings("unchecked")
        Map<String, Object> arguments = (Map<String, Object>) request.getOrDefault("arguments", Map.of());
        return ResponseEntity.ok(mcpServer.callTool(name, arguments));
    }

    @GetMapping("/metrics")
    public ResponseEntity<Map<String, Object>> getMetrics() {
        return ResponseEntity.ok(Map.of("agent_metrics", tracer.getMetricsSummary()));
    }
}