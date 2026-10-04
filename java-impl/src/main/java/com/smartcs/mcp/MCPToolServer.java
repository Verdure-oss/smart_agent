package com.smartcs.mcp;

import com.smartcs.biz.OrderRepository;
import com.smartcs.biz.TicketRepository;
import com.smartcs.memory.LongTermMemoryService;
import org.springframework.stereotype.Component;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * MCP工具协议服务端 — Java实现。
 * 提供工具注册/发现/调用能力，遵循JSON-RPC 2.0规范（tools/list + tools/call）。
 *
 * 四个默认工具均已实现真实逻辑（对齐 Python 版 mcp_server.py）：
 * - order_query        订单查询（经 OrderRepository 落库数据）
 * - ticket_create      工单创建（经 TicketRepository 落库）
 * - risk_check         风控检查（金额阈值规则）
 * - knowledge_search   知识库检索（混合召回：BM25 + TF向量 + RRF）
 */
@Component
public class MCPToolServer {

    private final Map<String, ToolDefinition> tools = new ConcurrentHashMap<>();
    private final List<Map<String, Object>> callLog = Collections.synchronizedList(new ArrayList<>());

    private final OrderRepository orderRepository;
    private final TicketRepository ticketRepository;
    private final LongTermMemoryService longTermMemory;

    public MCPToolServer(OrderRepository orderRepository,
                         TicketRepository ticketRepository,
                         LongTermMemoryService longTermMemory) {
        this.orderRepository = orderRepository;
        this.ticketRepository = ticketRepository;
        this.longTermMemory = longTermMemory;
        registerDefaultTools();
    }

    private void registerDefaultTools() {
        register(new ToolDefinition(
                "order_query",
                "查询订单信息：按订单ID或用户ID查询订单状态、金额",
                Map.of("order_id", "string", "user_id", "string"),
                "order"
        ));
        register(new ToolDefinition(
                "ticket_create",
                "创建客服工单并落库",
                Map.of("user_id", "string", "description", "string", "priority", "string"),
                "ticket"
        ));
        register(new ToolDefinition(
                "risk_check",
                "金融风控检查：按金额与动作判断风险等级",
                Map.of("user_id", "string", "action", "string", "amount", "number"),
                "compliance"
        ));
        register(new ToolDefinition(
                "knowledge_search",
                "搜索企业知识库（混合召回）",
                Map.of("query", "string", "top_k", "integer"),
                "knowledge"
        ));
    }

    public void register(ToolDefinition tool) {
        tools.put(tool.name(), tool);
    }

    public List<Map<String, Object>> listTools() {
        return tools.values().stream()
                .map(t -> Map.<String, Object>of(
                        "name", t.name(),
                        "description", t.description(),
                        "inputSchema", t.inputSchema(),
                        "category", t.category()
                ))
                .toList();
    }

    public java.util.Optional<ToolDefinition> findTool(String name) {
        return java.util.Optional.ofNullable(tools.get(name));
    }

    /** 调用工具（JSON-RPC tools/call 的 handler）。 */
    public Map<String, Object> callTool(String name, Map<String, Object> arguments) {
        long start = System.currentTimeMillis();
        try {
            Map<String, Object> result = switch (name) {
                case "order_query" -> handleOrderQuery(arguments);
                case "ticket_create" -> handleTicketCreate(arguments);
                case "risk_check" -> handleRiskCheck(arguments);
                case "knowledge_search" -> handleKnowledgeSearch(arguments);
                default -> Map.of("success", false, "error", "Tool not found: " + name);
            };
            callLog.add(Map.of("tool", name, "arguments", arguments,
                    "timestamp", new Date().toString(), "duration_ms", System.currentTimeMillis() - start));
            return result;
        } catch (Exception e) {
            callLog.add(Map.of("tool", name, "arguments", arguments,
                    "timestamp", new Date().toString(), "error", e.getMessage()));
            return Map.of("success", false, "error", e.getMessage());
        }
    }

    private Map<String, Object> handleOrderQuery(Map<String, Object> args) {
        String orderId = str(args.get("order_id"));
        String userId = str(args.get("user_id"));

        if (orderId != null && !orderId.isBlank()) {
            Map<String, Object> order = orderRepository.queryById(orderId);
            if (order == null) {
                return Map.of("success", false, "error", "订单不存在: " + orderId);
            }
            return Map.of("success", true, "result", order);
        }
        if (userId != null && !userId.isBlank()) {
            List<Map<String, Object>> orders = orderRepository.queryByUser(userId);
            return Map.of("success", true, "result", Map.of("user_id", userId, "orders", orders));
        }
        return Map.of("success", false, "error", "order_id 或 user_id 至少提供一个");
    }

    private Map<String, Object> handleTicketCreate(Map<String, Object> args) {
        String userId = str(args.getOrDefault("user_id", "anonymous"));
        String description = str(args.get("description"));
        String priority = str(args.get("priority"));
        if (description == null || description.isBlank()) {
            return Map.of("success", false, "error", "description 不能为空");
        }
        String ticketId = ticketRepository.create(userId, description, priority);
        return Map.of("success", true, "result", ticketRepository.query(ticketId));
    }

    private Map<String, Object> handleRiskCheck(Map<String, Object> args) {
        String action = str(args.get("action"));
        double amount = num(args.get("amount"));

        String riskLevel = "low";
        List<String> reasons = new ArrayList<>();

        if ("refund".equals(action) && amount >= 5000) {
            riskLevel = "high";
            reasons.add("大额退款(" + amount + "元)，需人工复核");
        } else if (amount >= 50000) {
            riskLevel = "high";
            reasons.add("单笔金额超5万，触发高风险");
        } else if (amount >= 10000) {
            riskLevel = "medium";
            reasons.add("金额1万-5万，中风险");
        }

        return Map.of(
                "success", true,
                "result", Map.of(
                        "risk_level", riskLevel,
                        "reasons", reasons,
                        "action", action != null ? action : "",
                        "amount", amount
                )
        );
    }

    private Map<String, Object> handleKnowledgeSearch(Map<String, Object> args) {
        String query = str(args.get("query"));
        int topK = (int) num(args.getOrDefault("top_k", 3));
        if (query == null || query.isBlank()) {
            return Map.of("success", false, "error", "query 不能为空");
        }
        List<Map<String, Object>> docs = longTermMemory.search(query, topK);
        return Map.of("success", true, "result", docs);
    }

    public List<Map<String, Object>> getCallLog() {
        synchronized (callLog) {
            return new ArrayList<>(callLog);
        }
    }

    private String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private double num(Object o) {
        if (o == null) return 0;
        if (o instanceof Number n) return n.doubleValue();
        try {
            return Double.parseDouble(String.valueOf(o));
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    public record ToolDefinition(String name, String description, Map<String, String> inputSchema, String category) {}
}