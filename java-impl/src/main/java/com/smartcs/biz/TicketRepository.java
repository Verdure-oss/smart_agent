package com.smartcs.biz;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Repository;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 工单存储 — 经 StorageDialect 方言层，SQLite（本地）或 MySQL（生产）可切换。
 * - 启动建表 tickets（幂等）
 * - 重启后数据仍在（持久化实证）
 */
@Repository
public class TicketRepository {

    private static final Logger log = LoggerFactory.getLogger(TicketRepository.class);

    private final StorageDialect dialect;

    public TicketRepository(StorageDialect dialect) {
        this.dialect = dialect;
        init();
    }

    private void init() {
        try (Connection conn = dialect.connect()) {
            dialect.createTables(conn);
            log.info("[TicketRepository] {} 就绪", dialect.type());
        } catch (Exception e) {
            throw new IllegalStateException("存储初始化失败", e);
        }
    }

    private Connection connect() throws Exception {
        return dialect.connect();
    }

    public String create(String userId, String description, String priority) {
        String ticketId = "TK-" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd"))
                + "-" + UUID.randomUUID().toString().substring(0, 6).toUpperCase();
        String pri = priority != null && !priority.isBlank() ? priority : "medium";
        String createdAt = LocalDateTime.now().toString();

        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement(
                     "INSERT INTO tickets(ticket_id, user_id, description, priority, status, created_at) VALUES(?,?,?,?,?,?)")) {
            ps.setString(1, ticketId);
            ps.setString(2, userId);
            ps.setString(3, description);
            ps.setString(4, pri);
            ps.setString(5, "created");
            ps.setString(6, createdAt);
            ps.executeUpdate();
        } catch (Exception e) {
            log.error("[TicketRepository] create 失败", e);
            throw new IllegalStateException("工单写入失败", e);
        }
        return ticketId;
    }

    public Map<String, Object> query(String ticketId) {
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement("SELECT * FROM tickets WHERE ticket_id = ?")) {
            ps.setString(1, ticketId);
            try (ResultSet rs = ps.executeQuery()) {
                if (rs.next()) {
                    return rowToMap(rs);
                }
            }
        } catch (Exception e) {
            log.error("[TicketRepository] query 失败", e);
        }
        return null;
    }

    public List<Map<String, Object>> listByUser(String userId) {
        List<Map<String, Object>> result = new ArrayList<>();
        try (Connection conn = connect();
             PreparedStatement ps = conn.prepareStatement(
                     "SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at DESC")) {
            ps.setString(1, userId);
            try (ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    result.add(rowToMap(rs));
                }
            }
        } catch (Exception e) {
            log.error("[TicketRepository] listByUser 失败", e);
        }
        return result;
    }

    private Map<String, Object> rowToMap(ResultSet rs) throws Exception {
        Map<String, Object> ticket = new LinkedHashMap<>();
        ticket.put("ticket_id", rs.getString("ticket_id"));
        ticket.put("user_id", rs.getString("user_id"));
        ticket.put("description", rs.getString("description"));
        ticket.put("priority", rs.getString("priority"));
        ticket.put("status", rs.getString("status"));
        ticket.put("created_at", rs.getString("created_at"));
        return ticket;
    }
}