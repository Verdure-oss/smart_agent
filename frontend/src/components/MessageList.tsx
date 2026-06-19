import type { ChatMessage } from "../types/api";

interface MessageListProps {
  isLoadingHistory: boolean;
  messages: ChatMessage[];
}

function formatTimestamp(timestamp?: string): string {
  if (!timestamp) {
    return "";
  }

  const value = new Date(timestamp);
  if (Number.isNaN(value.getTime())) {
    return "";
  }

  return value.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function MessageList({
  isLoadingHistory,
  messages,
}: MessageListProps) {
  if (isLoadingHistory && messages.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title">正在恢复会话历史</p>
        <p className="empty-state__body">如果本地已有 session_id，这里会自动加载历史对话。</p>
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title">前端已就绪，等待第一条消息</p>
        <p className="empty-state__body">可以先试一句“我想了解理财产品A”或“帮我查询订单状态”。</p>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((message) => (
        <article
          className={`message-card message-card--${message.role}`}
          key={message.id}
        >
          <div className="message-card__meta">
            <span className="message-card__role">
              {message.role === "user" ? "用户" : message.role === "assistant" ? "助手" : "系统"}
            </span>
            <span className="message-card__time">{formatTimestamp(message.timestamp)}</span>
          </div>
          <div className="message-card__content">{message.content}</div>
          {(message.meta || message.pending) && (
            <div className="message-card__footer">
              {message.meta ? <span>{message.meta}</span> : null}
              {message.pending ? <span className="message-card__pending">处理中</span> : null}
            </div>
          )}
        </article>
      ))}
    </div>
  );
}
