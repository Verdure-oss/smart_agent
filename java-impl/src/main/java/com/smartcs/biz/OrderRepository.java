package com.smartcs.biz;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Repository;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 订单存储 — SQLite 持久化（阶段 6：从内存 Map 迁移）。
 * - 启动建表 orders + 幂等 seed（INSERT OR IGNORE）
 * - 支持按订单ID / 按用户查询、退款状态流转（markRefunding）
 */
@Repository
public class OrderRepository {

    private static final Logger log = LoggerFactory.getLogger(OrderRepository.class);

    private static final String URL = "jdbc:sqlite:data/smartcs.db";

    public OrderRepository() {
        init();
    }

    private void init() {
        try {
            Path dir = Paths.get("data");
            if (!Files.exists(dir)) {
                Files.createDirectories(dir);
            }
            try (Connection conn = connect(); Statement st = conn.createStatement()) {
                st.executeUpdate("""
                        CREATE TABLE IF NOT EXISTS orders (
                            order_id   TEXT PRIMARY KEY,
                            user_id    TEXT NOT NULL,
                            product    TEXT NOT NULL,
                            amount     REAL NOT NULL,
                            status     TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT
                        )
                        """);
            }
            seed();
            log.info("[OrderRepository] SQLite 就绪");
        } catch (Exception e) {
            throw new IllegalStateException("订单表初始化失败", e);
        }
    }

    private void seed() throws Exception {
        Object[][] rows = {
                {"ORD-20261001-001", "user_001", "金葵理财-半年期", 10000.0, "active", "2026-10-01T10:00:00"},
                {"ORD-20261002-002", "user_001", "无忧退款-定制款", 388.0, "refunding", "2026-10-02T09:30:00"},
                {"ORD-20261003-003", "user_002", "慧投定投-月定投", 2000.0, "active", "2026-10-03T14:00:00"},
        };
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement(
                     "INSERT OR IGNORE INTO orders(order_id, user_id, product, amount, status, created_at) VALUES(?,?,?,?,?,?)")) {
            for (Object[] row : rows) {
                ps.setString(1, (String) row[0]);
                ps.setString(2, (String) row[1]);
                ps.setString(3, (String) row[2]);
                ps.setDouble(4, (Double) row[3]);
                ps.setString(5, (String) row[4]);
                ps.setString(6, (String) row[5]);
                ps.addBatch();
            }
            ps.executeBatch();
        }
    }

    private Connection connect() throws Exception {
        return DriverManager.getConnection(URL);
    }

    public Map<String, Object> queryById(String orderId) {
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement("SELECT * FROM orders WHERE order_id = ?")) {
            ps.setString(1, orderId);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return rowToMap(rs);
                }
            }
        } catch (Exception e) {
            log.error("[OrderRepository] queryById 失败", e);
        }
        return null;
    }

    public List<Map<String, Object>> queryByUser(String userId) {
        List<Map<String, Object>> result = new ArrayList<>();
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement(
                     "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC")) {
            ps.setString(1, userId);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    result.add(rowToMap(rs));
                }
            }
        } catch (Exception e) {
            log.error("[OrderRepository] queryByUser 失败", e);
        }
        return result;
    }

    /** 状态流转：退款/理赔场景把订单标记为 refunding（对齐 Python 版行为）。 */
    public boolean markRefunding(String orderId) {
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement(
                     "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?")) {
            ps.setString(1, "refunding");
            ps.setString(2, LocalDateTime.now().toString());
            ps.setString(3, orderId);
            return ps.executeUpdate() > 0;
        } catch (Exception e) {
            log.error("[OrderRepository] markRefunding 失败", e);
            return false;
        }
    }

    private Map<String, Object> rowToMap(ResultSet rs) throws Exception {
        Map<String, Object> order = new LinkedHashMap<>();
        order.put("order_id", rs.getString("order_id"));
        order.put("user_id", rs.getString("user_id"));
        order.put("product", rs.getString("product"));
        order.put("amount", rs.getDouble("amount"));
        order.put("status", rs.getString("status"));
        order.put("created_at", rs.getString("created_at"));
        String updatedAt = rs.getString("updated_at");
        if (updatedAt != null) {
            order.put("updated_at", updatedAt);
        }
        return order;
    }
}