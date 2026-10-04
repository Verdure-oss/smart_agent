package com.smartcs.memory;

import org.springframework.stereotype.Service;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 工作记忆 — 进程内、按会话跨轮收集补充信息。
 *
 * 典型用途（对齐 Python 版 WorkingMemory）：
 * - 用户后续轮次补齐的缺失信息（如订单号、金额）逐轮累加；
 * - 快速获取最近一次已知的补充信息，供 Supervisor 拆解 / 工单 Agent 使用。
 *
 * 注意：进程内存储，多实例部署时不共享 —— 生产可替换为 Redis。
 */
@Service
public class WorkingMemoryService {

    private final Map<String, ConcurrentHashMap<String, String>> store = new ConcurrentHashMap<>();

    /**
     * 追加一个补充信息片段（如 "order_id=ORD-123"），按会话累积。
     */
    public void append(String sessionId, String key, String value) {
        store.computeIfAbsent(sessionId, k -> new ConcurrentHashMap<>()).put(key, value);
    }

    /**
     * 读取某个补充信息。
     */
    public String get(String sessionId, String key) {
        var m = store.get(sessionId);
        return m == null ? null : m.get(key);
    }

    /**
     * 以 key=value 形式读取该会话全部补充信息；无则为空串。
     */
    public String dump(String sessionId) {
        var m = store.get(sessionId);
        if (m == null || m.isEmpty()) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        m.forEach((k, v) -> sb.append(k).append("=").append(v).append("; "));
        return sb.toString();
    }

    public void clear(String sessionId) {
        store.remove(sessionId);
    }
}