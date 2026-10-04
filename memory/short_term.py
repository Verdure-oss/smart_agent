"""
短期记忆 — 基于Redis的会话级记忆
存储最近N轮对话上下文，设置TTL自动过期。
适合维护多轮对话的连续性。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class ShortTermMemory:
    """
    短期记忆：基于Redis的会话缓存。

    特点：
    - Redis存储，支持分布式部署
    - TTL自动过期（默认30分钟）
    - 保留最近N轮对话
    - 支持滑动窗口淘汰
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
    ):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._redis_url = redis_url
        self._redis: Any = None
        self._fallback_store: dict[str, list] = {}
        # 滚动摘要：session_id → {"text": 摘要文本, "compacted_upto": 已压缩到的消息条数}
        self._summary_store: dict[str, dict] = {}

    async def _get_redis(self):
        """懒加载Redis连接"""
        # 第一次用到时才连接 Redis
        if self._redis is None:
            if aioredis is None:
                return None
            try:
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception:
                self._redis = None
        return self._redis

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    def _summary_key(self, session_id: str) -> str:
        return f"smartcs:short_term_summary:{session_id}"

    # 把一条对话消息存到“会话记忆”（优先 Redis，否则内存）
    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """添加一条对话消息"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            # 加到列表尾（像聊天记录）
            await r.rpush(key, json.dumps(message, ensure_ascii=False))
            
            # 只保留最近 N 条（防止无限增长
            await r.ltrim(key, -self.max_turns, -1)
            
            # 设置过期时间（会话自动清理）
            await r.expire(key, self.ttl_seconds)
        else:
            print('未找到可用redis！！')
            if session_id not in self._fallback_store:
                self._fallback_store[session_id] = []
            self._fallback_store[session_id].append(message)
            if len(self._fallback_store[session_id]) > self.max_turns:
                self._fallback_store[session_id] = self._fallback_store[session_id][-self.max_turns:]

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史"""
        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            n = last_n or self.max_turns
            raw = await r.lrange(key, -n, -1)
            return [json.loads(item) for item in raw]
        else:
            history = self._fallback_store.get(session_id, [])
            if last_n:
                return history[-last_n:]
            return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定session的短期记忆"""
        r = await self._get_redis()
        if r is not None:
            await r.delete(self._session_key(session_id))
        else:
            self._fallback_store.pop(session_id, None)

    async def get_context_window(self, session_id: str, max_tokens: int = 4000) -> str:
        """获取适配上下文窗口大小的对话历史文本"""
        history = await self.get_history(session_id)

        context_parts = []
        estimated_tokens = 0

        for msg in reversed(history):
            msg_text = f"{msg['role']}: {msg['content']}"
            msg_tokens = len(msg_text) // 2  # 粗略估算
            if estimated_tokens + msg_tokens > max_tokens:
                break
            context_parts.insert(0, msg_text)
            estimated_tokens += msg_tokens

        return "\n".join(context_parts)

    # ─── 滚动摘要（按需注入核心） ───

    async def set_summary(self, session_id: str, text: str, compacted_upto: int) -> None:
        """保存会话滚动摘要，标记已压缩到的消息条数。"""
        payload = json.dumps(
            {"text": text, "compacted_upto": int(compacted_upto)},
            ensure_ascii=False,
        )
        r = await self._get_redis()
        if r is not None:
            await r.set(self._summary_key(session_id), payload, ex=self.ttl_seconds)
        else:
            self._summary_store[session_id] = {
                "text": text,
                "compacted_upto": int(compacted_upto),
            }

    async def get_summary(self, session_id: str) -> tuple[str, int]:
        """获取会话滚动摘要，返回 (摘要文本, compacted_upto)；无摘要时返回 ("", 0)。"""
        r = await self._get_redis()
        if r is not None:
            raw = await r.get(self._summary_key(session_id))
            if raw:
                data = json.loads(raw)
                return data.get("text", ""), int(data.get("compacted_upto", 0))
            return "", 0
        data = self._summary_store.get(session_id)
        if data:
            return data.get("text", ""), int(data.get("compacted_upto", 0))
        return "", 0

    async def get_injection_context(
        self, session_id: str, recent_turns: int = 4
    ) -> tuple[str, list[dict]]:
        """
        按需注入：返回 (滚动摘要文本, 最近 recent_turns 条消息列表)。

        由调用方决定如何组装进 Prompt —— 摘要作为精简上下文，最近消息保持原文，
        替代"全量历史入 Prompt"，从而控制 Prompt Token 长度。
        """
        summary_text, _ = await self.get_summary(session_id)
        recent = await self.get_history(session_id, last_n=recent_turns * 2)
        return summary_text, recent
