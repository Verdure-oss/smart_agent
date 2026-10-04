package com.smartcs.memory;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.*;

/**
 * 短期记忆服务 — 基于Redis的会话缓存。
 * 存储最近N轮对话，TTL自动过期。
 * 当Redis不可用时自动降级为内存存储。
 */
@Service
public class ShortTermMemoryService {

    private final StringRedisTemplate redisTemplate;
    private final ObjectMapper objectMapper;
    private final Map<String, List<Map<String, String>>> fallbackStore = new HashMap<>();
    private final Map<String, Map<String, String>> fallbackSummary = new HashMap<>();

    private static final int MAX_TURNS = 20;
    private static final Duration TTL = Duration.ofMinutes(30);

    public ShortTermMemoryService(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
        this.objectMapper = new ObjectMapper();
    }

    public void addMessage(String sessionId, String role, String content) {
        Map<String, String> message = Map.of(
                "role", role,
                "content", content,
                "timestamp", LocalDateTime.now().toString()
        );

        try {
            String key = "smartcs:short_term:" + sessionId;
            String json = objectMapper.writeValueAsString(message);
            redisTemplate.opsForList().rightPush(key, json);
            redisTemplate.opsForList().trim(key, -MAX_TURNS, -1);
            redisTemplate.expire(key, TTL);
        } catch (Exception e) {
            fallbackStore.computeIfAbsent(sessionId, k -> new ArrayList<>()).add(message);
            List<Map<String, String>> list = fallbackStore.get(sessionId);
            if (list.size() > MAX_TURNS) {
                fallbackStore.put(sessionId, new ArrayList<>(list.subList(list.size() - MAX_TURNS, list.size())));
            }
        }
    }

    public List<Map<String, Object>> getHistory(String sessionId) {
        try {
            String key = "smartcs:short_term:" + sessionId;
            List<String> raw = redisTemplate.opsForList().range(key, 0, -1);
            if (raw == null) return List.of();

            List<Map<String, Object>> result = new ArrayList<>();
            for (String json : raw) {
                @SuppressWarnings("unchecked")
                Map<String, Object> msg = objectMapper.readValue(json, Map.class);
                result.add(msg);
            }
            return result;
        } catch (Exception e) {
            List<Map<String, String>> fallback = fallbackStore.getOrDefault(sessionId, List.of());
            return new ArrayList<>(fallback.stream().map(m -> (Map<String, Object>) new HashMap<String, Object>(m)).toList());
        }
    }

    // ==================== 滚动摘要（对齐 Python 版） ====================

    /**
     * 保存滚动摘要与已压缩到哪条消息（compactedUpto = 已压缩进摘要的消息条数）。
     */
    public void setSummary(String sessionId, String summaryText, int compactedUpto) {
        try {
            String key = "smartcs:summary:" + sessionId;
            redisTemplate.opsForHash().put(key, "text", summaryText == null ? "" : summaryText);
            redisTemplate.opsForHash().put(key, "compacted_upto", String.valueOf(compactedUpto));
            redisTemplate.expire(key, TTL);
        } catch (Exception e) {
            Map<String, String> m = new HashMap<>();
            m.put("text", summaryText == null ? "" : summaryText);
            m.put("compacted_upto", String.valueOf(compactedUpto));
            fallbackSummary.put(sessionId, m);
        }
    }

    /**
     * 读取滚动摘要。返回 [摘要文本, 已压缩条数] 或 [null, 0]（无摘要）。
     */
    public SummaryState getSummary(String sessionId) {
        try {
            String key = "smartcs:summary:" + sessionId;
            Map<Object, Object> raw = redisTemplate.opsForHash().entries(key);
            if (raw == null || raw.isEmpty()) {
                return SummaryState.empty();
            }
            String text = String.valueOf(raw.getOrDefault("text", ""));
            int upto = Integer.parseInt(String.valueOf(raw.getOrDefault("compacted_upto", "0")));
            return new SummaryState(text, upto);
        } catch (Exception e) {
            Map<String, String> m = fallbackSummary.get(sessionId);
            if (m == null) return SummaryState.empty();
            try {
                return new SummaryState(
                        m.getOrDefault("text", ""),
                        Integer.parseInt(m.getOrDefault("compacted_upto", "0")));
            } catch (NumberFormatException ex) {
                return SummaryState.empty();
            }
        }
    }

    /**
     * 按需注入上下文：摘要文本 + 最近 recentTurns 轮的原文（role/content 列表，保持时序）。
     */
    public InjectionContext getInjectionContext(String sessionId, int recentTurns) {
        SummaryState summary = getSummary(sessionId);
        List<Map<String, Object>> history = getHistory(sessionId);
        int keep = recentTurns * 2;
        List<Map<String, Object>> recent = history.size() > keep
                ? new ArrayList<>(history.subList(history.size() - keep, history.size()))
                : history;
        return new InjectionContext(summary.text(), recent);
    }

    /** 滚动摘要状态（摘要文本 + 已压缩条数） */
    public record SummaryState(String text, int compactedUpto) {
        static SummaryState empty() {
            return new SummaryState(null, 0);
        }
    }

    /** 注入上下文（摘要 + 近几轮消息） */
    public record InjectionContext(String summaryText, List<Map<String, Object>> recentMessages) {
        public boolean hasSummary() {
            return summaryText != null && !summaryText.isBlank();
        }
    }
}
