package com.smartcs.tracing;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;

/**
 * Agent调用追踪器 — 为每个Agent调用记录耗时、成功率与Token消耗。
 *
 * 指标：total_calls / avg_duration_ms / error_rate / total_prompt_tokens /
 *      total_completion_tokens / total_tokens / avg_tokens_per_call。
 * 对齐 Python 版 trace_token_count：调用点从 ChatResponse 的 Usage 提取 token；
 * 网关未返回 usage 时跳过（记录日志，不伪造数字）。
 */
@Component
public class AgentTracer {

    private static final Logger log = LoggerFactory.getLogger(AgentTracer.class);

    private final Map<String, AgentMetric> metrics = new ConcurrentHashMap<>();

    public <T> T trace(String agentName, String method, Supplier<T> action) {
        long start = System.currentTimeMillis();
        boolean success = true;

        try {
            T result = action.get();
            return result;
        } catch (Exception e) {
            success = false;
            throw e;
        } finally {
            long duration = System.currentTimeMillis() - start;
            recordMetric(agentName, duration, success);
            log.info("Agent[{}].{} completed in {}ms, success={}", agentName, method, duration, success);
        }
    }

    /** 从 ChatResponse 提取 usage 记录 token（网关未返回则不记录）。 */
    public void recordUsage(String agentName, String method, ChatResponse response) {
        if (response == null || response.getMetadata() == null || response.getMetadata().getUsage() == null) {
            log.info("Agent[{}].{} no usage metadata from gateway, skip token", agentName, method);
            return;
        }
        var usage = response.getMetadata().getUsage();
        int prompt = usage.getPromptTokens() != null ? usage.getPromptTokens() : 0;
        int completion = usage.getCompletionTokens() != null ? usage.getCompletionTokens() : 0;
        if (prompt <= 0 && completion <= 0) {
            log.info("Agent[{}].{} usage empty (prompt={}, completion={})", agentName, method, prompt, completion);
            return;
        }
        recordUsage(agentName, method, prompt, completion);
    }

    /** 显式记录一次调用的 token 消耗。 */
    public void recordUsage(String agentName, String method, int promptTokens, int completionTokens) {
        metrics.computeIfAbsent(agentName, k -> new AgentMetric()).recordUsage(promptTokens, completionTokens);
        log.info("Agent[{}].{} tokens: prompt={}, completion={}", agentName, method, promptTokens, completionTokens);
    }

    private void recordMetric(String agentName, long durationMs, boolean success) {
        metrics.computeIfAbsent(agentName, k -> new AgentMetric())
                .record(durationMs, success);
    }

    public Map<String, Object> getMetricsSummary() {
        Map<String, Object> summary = new ConcurrentHashMap<>();
        metrics.forEach((name, metric) -> summary.put(name, metric.toMap()));
        return summary;
    }

    private static class AgentMetric {
        private long totalCalls = 0;
        private long totalDurationMs = 0;
        private long errorCount = 0;
        private long totalPromptTokens = 0;
        private long totalCompletionTokens = 0;

        synchronized void record(long durationMs, boolean success) {
            totalCalls++;
            totalDurationMs += durationMs;
            if (!success) errorCount++;
        }

        synchronized void recordUsage(int promptTokens, int completionTokens) {
            totalPromptTokens += promptTokens;
            totalCompletionTokens += completionTokens;
        }

        Map<String, Object> toMap() {
            long total = totalPromptTokens + totalCompletionTokens;
            return Map.of(
                    "total_calls", totalCalls,
                    "avg_duration_ms", totalCalls > 0 ? totalDurationMs / totalCalls : 0,
                    "error_rate", totalCalls > 0 ? (double) errorCount / totalCalls : 0.0,
                    "total_prompt_tokens", totalPromptTokens,
                    "total_completion_tokens", totalCompletionTokens,
                    "total_tokens", total,
                    "avg_tokens_per_call", totalCalls > 0 ? total / totalCalls : 0
            );
        }
    }
}