package com.smartcs.biz;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 实时风险画像 — 滑动时间窗口统计（对齐"实时风险画像机制"设计）。
 *
 * 用 Redis ZSet 实现滑动窗口（score=事件时间戳，member=action:amount:nonce）：
 *   - 清理窗口外事件 → 统计窗口内操作次数与累计金额 → 追加当前事件
 *   - 超过「次数阈值 / 累计金额阈值」判定风险等级并给出理由
 *
 * Redis 不可用时自动降级为进程内滑动窗口（保证核心风控链路不中断）。
 */
@Service
public class RiskWindowService {

    /** 滑动窗口长度：5 分钟 */
    private static final long WINDOW_MS = 5 * 60 * 1000L;
    /** 窗口内操作次数阈值（≥ 触发高风险） */
    private static final int EVENT_THRESHOLD = 5;
    /** 窗口内累计金额高风险线（元） */
    private static final double AMOUNT_HIGH = 100_000.0;
    /** 窗口内累计金额中风险线（元） */
    private static final double AMOUNT_MEDIUM = 50_000.0;
    /** 事务类动作（需计入窗口）；查询/检查类不追加，避免污染高频统计 */
    private static final Set<String> TX_ACTIONS =
            Set.of("refund", "transfer", "pay", "purchase", "withdraw", "order");

    private final StringRedisTemplate redisTemplate;
    /** Redis 不可用时的进程内降级存储：userId → 窗口内事件 */
    private final Map<String, List<Event>> fallback = new ConcurrentHashMap<>();

    public RiskWindowService(StringRedisTemplate redisTemplate) {
        this.redisTemplate = redisTemplate;
    }

    /**
     * 评估一次用户操作的风险（滑动窗口）。返回风险等级、触发原因与窗口统计。
     */
    public RiskResult evaluate(String userId, String action, double amount) {
        long now = System.currentTimeMillis();
        String uid = (userId == null || userId.isBlank()) ? "anonymous" : userId;
        String key = "smartcs:risk_window:" + uid;

        boolean isTx = action != null && TX_ACTIONS.contains(action.toLowerCase());
        int eventCount;
        double windowAmount;

        try {
            long windowStart = now - WINDOW_MS;
            // 清理窗口外事件；仅事务动作才追加当前事件（查询/检查不污染窗口）
            redisTemplate.opsForZSet().removeRangeByScore(key, 0, windowStart);
            if (isTx) {
                String member = action + ":" + amount + ":" + System.nanoTime();
                redisTemplate.opsForZSet().add(key, member, now);
                redisTemplate.expire(key, Duration.ofMillis(WINDOW_MS * 2));
            }

            Set<String> members = redisTemplate.opsForZSet().rangeByScore(key, windowStart, now);
            eventCount = members == null ? 0 : members.size();
            double sum = 0.0;
            if (members != null) {
                for (String m : members) {
                    int first = m.indexOf(':');
                    int second = m.indexOf(':', first + 1);
                    if (first >= 0 && second > first) {
                        try {
                            sum += Double.parseDouble(m.substring(first + 1, second));
                        } catch (NumberFormatException ignore) {
                            // 忽略无法解析的 member
                        }
                    }
                }
            }
            windowAmount = sum;
        } catch (Exception e) {
            // Redis 不可用 → 进程内滑动窗口降级
            List<Event> list = fallback.computeIfAbsent(uid,
                    k -> Collections.synchronizedList(new ArrayList<>()));
            synchronized (list) {
                long windowStart = now - WINDOW_MS;
                list.removeIf(ev -> ev.timestamp() < windowStart);
                if (isTx) {
                    list.add(new Event(amount, now));
                }
                eventCount = list.size();
                double sum = 0.0;
                for (Event ev : list) {
                    sum += ev.amount();
                }
                windowAmount = sum;
            }
        }

        // 风险判定
        String riskLevel = "low";
        List<String> reasons = new ArrayList<>();
        if (eventCount >= EVENT_THRESHOLD) {
            riskLevel = "high";
            reasons.add("窗口内操作 " + eventCount + " 次（≥" + EVENT_THRESHOLD + " 次），异常高频");
        }
        if (windowAmount >= AMOUNT_HIGH) {
            riskLevel = "high";
            reasons.add("窗口内累计金额 " + windowAmount + " 元，超高风险线");
        } else if (windowAmount >= AMOUNT_MEDIUM && !"high".equals(riskLevel)) {
            riskLevel = "medium";
            reasons.add("窗口内累计金额 " + windowAmount + " 元，超中风险线");
        }

        return new RiskResult(riskLevel, reasons, eventCount, windowAmount);
    }

    private record Event(double amount, long timestamp) {}

    /** 风险评估结果 */
    public record RiskResult(String riskLevel, List<String> reasons, int windowEventCount, double windowAmount) {}
}