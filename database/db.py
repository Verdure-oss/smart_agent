"""
简单 SQLite 数据库 — 持久化订单、工单等业务数据
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "app.db"


def get_connection() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """初始化数据库表"""
    conn = get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                product TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'created',
                summary TEXT NOT NULL,
                details TEXT DEFAULT '',
                order_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );
        """)
        conn.commit()
    finally:
        conn.close()


# ─── 订单操作 ───

def create_order(user_id: str, product: str, amount: float, status: str = "pending") -> dict:
    """创建订单"""
    order_id = f"ORD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO orders (order_id, user_id, product, amount, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, user_id, product, amount, status, now, now),
        )
        conn.commit()
        return {
            "order_id": order_id,
            "user_id": user_id,
            "product": product,
            "amount": amount,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        conn.close()


def query_order(order_id: str) -> dict | None:
    """查询订单"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def query_orders_by_user(user_id: str) -> list[dict]:
    """查询用户的所有订单"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_order_status(order_id: str, status: str) -> bool:
    """更新订单状态"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, datetime.now().isoformat(), order_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ─── 工单操作 ───

def create_ticket(user_id: str, ticket_type: str, priority: str, summary: str, details: str = "", order_id: str | None = None) -> dict:
    """创建工单"""
    ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tickets (ticket_id, user_id, type, priority, status, summary, details, order_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, user_id, ticket_type, priority, "created", summary, details, order_id, now, now),
        )
        conn.commit()
        return {
            "ticket_id": ticket_id,
            "user_id": user_id,
            "type": ticket_type,
            "priority": priority,
            "status": "created",
            "summary": summary,
            "details": details,
            "order_id": order_id,
            "created_at": now,
            "updated_at": now,
        }
    finally:
        conn.close()


def query_ticket(ticket_id: str) -> dict | None:
    """查询工单"""
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def query_tickets_by_user(user_id: str) -> list[dict]:
    """查询用户的所有工单"""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM tickets WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_ticket_status(ticket_id: str, status: str) -> bool:
    """更新工单状态"""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
            (status, datetime.now().isoformat(), ticket_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ─── 初始化示例数据 ───

def seed_data() -> None:
    """初始化示例订单数据"""
    conn = get_connection()
    try:
        # 检查是否已有数据
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if count > 0:
            return

        now = datetime.now().isoformat()

        # 示例订单
        orders = [
            ("ORD-20260620-001", "user_001", "理财产品A", 50000.0, "completed"),
            ("ORD-20260620-002", "user_001", "理财产品B", 100000.0, "pending"),
            ("ORD-20260620-003", "user_002", "智能存款C", 200000.0, "completed"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO orders (order_id, user_id, product, amount, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(o[0], o[1], o[2], o[3], o[4], now, now) for o in orders],
        )

        conn.commit()
    finally:
        conn.close()
