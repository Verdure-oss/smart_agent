package com.smartcs.biz;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;

/**
 * 数据库方言层 — 同一套业务代码在 SQLite（本地零部署）与 MySQL（生产）间切换。
 *
 * 通过环境变量/配置选择：
 *   SMARTCS_DB_TYPE=sqlite（默认，零依赖本地库） | mysql
 *  MySQL 还支持 SMARTCS_DB_URL / SMARTCS_DB_USERNAME / SMARTCS_DB_PASSWORD。
 *
 * 差异点集中在此：连接串、建表 DDL（主键类型/金额类型）、INSERT OR IGNORE（SQLite）vs
 * INSERT IGNORE（MySQL）、自增与索引。业务 SQL 其它部分保持通用。
 */
@Component
public class StorageDialect {

    public enum DbType { SQLITE, MYSQL }

    private final DbType type;
    private final String mysqlUrl;
    private final String mysqlUser;
    private final String mysqlPassword;

    public StorageDialect(
            @Value("${smartcs.db.type:sqlite}") String type,
            @Value("${smartcs.db.url:}") String mysqlUrl,
            @Value("${smartcs.db.username:root}") String mysqlUser,
            @Value("${smartcs.db.password:}") String mysqlPassword) {
        this.type = "mysql".equalsIgnoreCase(type) ? DbType.MYSQL : DbType.SQLITE;
        this.mysqlUrl = mysqlUrl;
        this.mysqlUser = mysqlUser;
        this.mysqlPassword = mysqlPassword == null ? "" : mysqlPassword;
    }

    public DbType type() {
        return type;
    }

    public boolean isMySql() {
        return type == DbType.MYSQL;
    }

    /** 打开连接（SQLite 自动建 data 目录）。 */
    public Connection connect() throws Exception {
        if (type == DbType.MYSQL) {
            if (mysqlUrl == null || mysqlUrl.isBlank()) {
                throw new IllegalStateException(
                        "SMARTCS_DB_TYPE=mysql 但未设置 SMARTCS_DB_URL（如 jdbc:mysql://localhost:3306/smartcs?useSSL=false&serverTimezone=Asia/Shanghai）");
            }
            return DriverManager.getConnection(mysqlUrl, mysqlUser, mysqlPassword);
        }
        Path dir = Paths.get("data");
        if (!Files.exists(dir)) {
            Files.createDirectories(dir);
        }
        return DriverManager.getConnection("jdbc:sqlite:data/smartcs.db");
    }

    /** 建表 DDL（幂等）。 */
    public void createTables(Connection conn) throws Exception {
        try (Statement st = conn.createStatement()) {
            if (type == DbType.MYSQL) {
                st.executeUpdate("""
                        CREATE TABLE IF NOT EXISTS orders (
                            order_id   VARCHAR(64)  NOT NULL,
                            user_id    VARCHAR(64)  NOT NULL,
                            product    VARCHAR(255) NOT NULL,
                            amount     DECIMAL(12,2) NOT NULL,
                            status     VARCHAR(32)  NOT NULL,
                            created_at VARCHAR(64)  NOT NULL,
                            updated_at VARCHAR(64),
                            PRIMARY KEY (order_id),
                            KEY idx_orders_user (user_id, created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """);
                st.executeUpdate("""
                        CREATE TABLE IF NOT EXISTS tickets (
                            ticket_id   VARCHAR(64) NOT NULL,
                            user_id     VARCHAR(64) NOT NULL,
                            description TEXT        NOT NULL,
                            priority    VARCHAR(32) NOT NULL DEFAULT 'medium',
                            status      VARCHAR(32) NOT NULL DEFAULT 'created',
                            created_at  VARCHAR(64) NOT NULL,
                            PRIMARY KEY (ticket_id),
                            KEY idx_tickets_user (user_id, created_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """);
            } else {
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
                st.executeUpdate("""
                        CREATE TABLE IF NOT EXISTS tickets (
                            ticket_id   TEXT PRIMARY KEY,
                            user_id     TEXT NOT NULL,
                            description TEXT NOT NULL,
                            priority    TEXT NOT NULL DEFAULT 'medium',
                            status      TEXT NOT NULL DEFAULT 'created',
                            created_at  TEXT NOT NULL
                        )
                        """);
            }
        }
    }

    /** INSERT 前的 ignore 前缀：SQLite 用 OR IGNORE，MySQL 用 IGNORE。 */
    public String insertIgnoreKeyword() {
        return type == DbType.MYSQL ? "INSERT IGNORE" : "INSERT OR IGNORE";
    }
}