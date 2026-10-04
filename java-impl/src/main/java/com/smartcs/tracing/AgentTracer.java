package com.smartcs.tracing;

import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.logging.LoggingSpanExporter;
import io.opentelemetry.exporter.otlp.trace.OtlpGrpcSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SdkTracerProviderBuilder;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.ai.chat.model.ChatResponse;
import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Supplier;

/**
 * Agent调用追踪器 — 对齐 Python 版 OpenTelemetry 全链路追踪。
 *
 * 每个 Agent 调用生成一个 Span（agentName.method），记录：
 * - duration_ms / success / error
 * - prompt_tokens / completion_tokens（4.1 起支持，前端/监控可按 agent 聚合）
 *
 * 导出策略：
 * - 设置 OTEL_EXPORTER_OTLP_ENDPOINT 时走 OTLP gRPC 上报（对接 Jaeger/Tempo/OTel Collector）
 * - 未设置时降级到 LoggingSpanExporter（打印到日志，零外部依赖，便于本地验收）
 */
@Component
public class AgentTracer {

    private static final Logger log = LoggerFactory.getLogger(AgentTracer.class);

    private final Map<String, AgentMetric> metrics = new ConcurrentHashMap<>();
    private final Tracer otelTracer;
    private final OpenTelemetry openTelemetry;

    public AgentTracer() {
        String otlpEndpoint = System.getenv("OTEL_EXPORTER_OTLP_ENDPOINT");
        SdkTracerProviderBuilder tracerProviderBuilder = SdkTracerProvider.builder();

        if (otlpEndpoint != null && !otlpEndpoint.isBlank()) {
            try {
                OtlpGrpcSpanExporter exporter = OtlpGrpcSpanExporter.builder()
                        .setEndpoint(otlpEndpoint)
                        .build();
                tracerProviderBuilder.addSpanProcessor(BatchSpanProcessor.builder(exporter).build());
                log.info("[AgentTracer] OTel OTLP 导出已启用: {}", otlpEndpoint);
            } catch (Exception e) {
                log.warn("[AgentTracer] OTLP 初始化失败，降级 Console 导出: {}", e.getMessage());
                tracerProviderBuilder.addSpanProcessor(
                        SimpleSpanProcessor.create(LoggingSpanExporter.create()));
            }
        } else {
            tracerProviderBuilder.addSpanProcessor(
                    SimpleSpanProcessor.create(LoggingSpanExporter.create()));
            log.info("[AgentTracer] 未设置 OTEL_EXPORTER_OTLP_ENDPOINT，使用 Console(日志) 导出");
        }

        SdkTracerProvider tracerProvider = tracerProviderBuilder.build();
        this.openTelemetry = OpenTelemetrySdk.builder().setTracerProvider(tracerProvider).build();
        this.otelTracer = openTelemetry.getTracer("smart-cs-agent");
    }

    public <T> T trace(String agentName, String method, Supplier<T> action) {
        long start = System.currentTimeMillis();
        boolean success = true;
        Span span = otelTracer.spanBuilder(agentName + "." + method).startSpan();

        try (Scope scope = span.makeCurrent()) {
            T result = action.get();
            return result;
        } catch (Exception e) {
            success = false;
            span.setStatus(StatusCode.ERROR, e.getMessage());
            span.recordException(e);
            throw e;
        } finally {
            long duration = System.currentTimeMillis() - start;
            span.setAttribute("agent", agentName);
            span.setAttribute("duration_ms", duration);
            span.setAttribute("success", success);
            recordMetric(agentName, duration, success);
            span.end();
            log.info("Agent[{}].{} completed in {}ms, success={}, traceId={}",
                    agentName, method, duration, success, span.getSpanContext().getTraceId());
        }
    }

    /**
     * 便捷重载：从 ChatResponse 提取 usage 后转调 int 版。
     * 网关不返回 usage（或 ChatResponse 无 meta）时按 0 记录，不影响主流程。
     */
    public void recordUsage(String agentName, String method, ChatResponse response) {
        int prompt = 0, completion = 0;
        if (response != null && response.getMetadata() != null && response.getMetadata().getUsage() != null) {
            var usage = response.getMetadata().getUsage();
            if (usage.getPromptTokens() != null) prompt = usage.getPromptTokens();
            if (usage.getCompletionTokens() != null) completion = usage.getCompletionTokens();
        }
        recordUsage(agentName, method, prompt, completion);
    }

    /**
     * 记录一次 LLM 调用的 token 用量（对齐 Python 版 plotly token 指标）。
     * prompt_tokens / completion_tokens 会累计到 AgentMetric 并作为 span 属性。
     */
    public void recordUsage(String agentName, String method, int promptTokens, int completionTokens) {
        Span current = Span.current();
        if (current.isRecording()) {
            current.setAttribute("prompt_tokens", promptTokens);
            current.setAttribute("completion_tokens", completionTokens);
        }
        metrics.computeIfAbsent(agentName, k -> new AgentMetric())
                .recordUsage(promptTokens, completionTokens);
        log.info("Agent[{}].{} tokens: prompt={}, completion={}", agentName, method, promptTokens, completionTokens);
    }

    /** 兼容：直接传 ChatResponse 的便捷重载由上层转换后再调此方法。 */
    private void recordMetric(String agentName, long durationMs, boolean success) {
        metrics.computeIfAbsent(agentName, k -> new AgentMetric())
                .record(durationMs, success);
    }

    public Map<String, Object> getMetricsSummary() {
        Map<String, Object> summary = new ConcurrentHashMap<>();
        metrics.forEach((name, metric) -> summary.put(name, metric.toMap()));
        return summary;
    }

    public OpenTelemetry getOpenTelemetry() {
        return openTelemetry;
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
            long totalTokens = totalPromptTokens + totalCompletionTokens;
            return Map.of(
                    "total_calls", totalCalls,
                    "avg_duration_ms", totalCalls > 0 ? totalDurationMs / totalCalls : 0,
                    "error_rate", totalCalls > 0 ? (double) errorCount / totalCalls : 0.0,
                    "total_prompt_tokens", totalPromptTokens,
                    "total_completion_tokens", totalCompletionTokens,
                    "total_tokens", totalTokens,
                    "avg_tokens_per_call", totalCalls > 0 ? totalTokens / totalCalls : 0
            );
        }
    }
}