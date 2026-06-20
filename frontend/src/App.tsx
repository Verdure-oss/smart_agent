import { startTransition, useEffect, useState } from "react";
import { checkHealth, fetchHistory, sendChatMessage } from "./api/chat";
import { MessageInput } from "./components/MessageInput";
import { MessageList } from "./components/MessageList";
import type { ChatMessage, HistoryMessage } from "./types/api";

type HealthStatus = "checking" | "online" | "offline";

const DEFAULT_USER_ID = "demo-user";
const SESSION_STORAGE_KEY = "smart-cs.session-id";
const USER_STORAGE_KEY = "smart-cs.user-id";

function createMessage(
  role: ChatMessage["role"],
  content: string,
  options: Partial<ChatMessage> = {},
): ChatMessage {
  const randomId =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${role}-${Date.now()}-${Math.random().toString(16).slice(2)}`;

  return {
    id: randomId,
    role,
    content,
    timestamp: new Date().toISOString(),
    ...options,
  };
}

function mapHistoryMessage(message: HistoryMessage): ChatMessage {
  const role =
    message.role === "user" || message.role === "assistant"
      ? message.role
      : "system";

  return createMessage(role, message.content, {
    timestamp: message.timestamp,
  });
}

function getHealthLabel(status: HealthStatus): string {
  if (status === "online") {
    return "后端在线";
  }
  if (status === "offline") {
    return "后端离线 (自动重试中...)";
  }
  return "检查连接中";
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "请求失败，请检查后端服务是否已启动。";
}

export default function App() {
  const [userId, setUserId] = useState(DEFAULT_USER_ID);
  const [sessionId, setSessionId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [error, setError] = useState("");
  const [healthStatus, setHealthStatus] = useState<HealthStatus>("checking");
  const [isSending, setIsSending] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);

  useEffect(() => {
    const storedUserId = window.localStorage.getItem(USER_STORAGE_KEY);
    const storedSessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);

    if (storedUserId) {
      setUserId(storedUserId);
    }

    if (storedSessionId) {
      setSessionId(storedSessionId);
      void restoreHistory(storedSessionId);
    }

    void refreshHealth();
  }, []);

  useEffect(() => {
    const nextUserId = userId.trim() || DEFAULT_USER_ID;
    window.localStorage.setItem(USER_STORAGE_KEY, nextUserId);
  }, [userId]);

  useEffect(() => {
    if (sessionId) {
      window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
      return;
    }

    window.localStorage.removeItem(SESSION_STORAGE_KEY);
  }, [sessionId]);

  async function refreshHealth() {
    setHealthStatus("checking");

    try {
      await checkHealth();
      setHealthStatus("online");
    } catch {
      setHealthStatus("offline");
    }
  }

  // 自动重试：离线时每5秒检查一次后端状态
  useEffect(() => {
    if (healthStatus !== "offline") return;

    const interval = setInterval(async () => {
      try {
        await checkHealth();
        setHealthStatus("online");
        clearInterval(interval);
      } catch {
        // 仍然离线，继续重试
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [healthStatus]);

  async function restoreHistory(targetSessionId: string) {
    if (!targetSessionId) {
      return;
    }

    setIsLoadingHistory(true);
    setError("");

    try {
      const history = await fetchHistory(targetSessionId);
      setHealthStatus("online");
      startTransition(() => {
        setMessages(history.messages.map(mapHistoryMessage));
      });
    } catch (restoreError) {
      setError(getErrorMessage(restoreError));
      setHealthStatus("offline");
    } finally {
      setIsLoadingHistory(false);
    }
  }

  function resetSession() {
    setSessionId("");
    setMessages([]);
    setError("");
  }

  async function handleSendMessage() {
    const content = inputValue.trim();
    if (!content || isSending) {
      return;
    }

    const effectiveUserId = userId.trim() || DEFAULT_USER_ID;
    const userMessage = createMessage("user", content);
    const pendingMessage = createMessage("assistant", "正在整理回复，请稍候...", {
      pending: true,
    });

    setError("");
    setInputValue("");
    setIsSending(true);
    setMessages((current) => [...current, userMessage, pendingMessage]);

    try {
      const response = await sendChatMessage({
        message: content,
        user_id: effectiveUserId,
        session_id: sessionId || undefined,
      });

      setHealthStatus("online");
      setSessionId(response.session_id);
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingMessage.id
            ? {
                ...message,
                content: response.response,
                meta: `${response.intent} · ${response.compliance_passed ? "合规通过" : "需要复核"}`,
                pending: false,
                timestamp: new Date().toISOString(),
              }
            : message,
        ),
      );
    } catch (sendError) {
      const nextError = getErrorMessage(sendError);
      setError(nextError);
      setHealthStatus("offline");
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingMessage.id
            ? {
                ...message,
                role: "system",
                content: `请求失败：${nextError}\n\n后端可能正在重启，系统将自动重试连接...`,
                pending: false,
                meta: "等待后端恢复后，刷新页面或重新发送消息",
                timestamp: new Date().toISOString(),
              }
            : message,
        ),
      );
    } finally {
      setIsSending(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="hero">
        <div>
          <p className="hero__eyebrow">SMART CS MULTI-AGENT</p>
          <h1 className="hero__title">本地联调聊天控制台</h1>
          <p className="hero__body">
            前端直接复用现有 FastAPI 接口，不改 Supervisor、Memory 和 Tracing 主链路。
          </p>
        </div>
        <div className={`status-pill status-pill--${healthStatus}`}>
          {getHealthLabel(healthStatus)}
        </div>
      </header>

      <section className="dashboard-grid">
        <div className="panel">
          <div className="panel__header">
            <h2>会话配置</h2>
            <button className="ghost-button" onClick={() => void refreshHealth()} type="button">
              重新检测
            </button>
          </div>
          <label className="field">
            <span className="field__label">用户 ID</span>
            <input
              className="field__input"
              onChange={(event) => setUserId(event.target.value)}
              placeholder={DEFAULT_USER_ID}
              value={userId}
            />
          </label>
          <div className="info-row">
            <span>当前 Session</span>
            <code>{sessionId || "尚未建立"}</code>
          </div>
          <div className="action-row">
            <button className="ghost-button" onClick={resetSession} type="button">
              新会话
            </button>
            <button
              className="ghost-button"
              disabled={!sessionId}
              onClick={() => void restoreHistory(sessionId)}
              type="button"
            >
              重新加载历史
            </button>
          </div>
          {error ? <p className="error-text">{error}</p> : null}
        </div>

        <div className="panel panel--steps">
          <div className="panel__header">
            <h2>联调步骤</h2>
          </div>
          <ol className="step-list">
            <li>在仓库根目录运行 `python -m api.main`。</li>
            <li>在 `frontend/` 目录运行 `npm install` 和 `npm run dev`。</li>
            <li>打开 `http://localhost:5173`，发送首条消息建立 session。</li>
            <li>刷新页面后，前端会自动调用 <code>/api/history/{"{session_id}"}</code> 恢复对话。</li>
          </ol>
        </div>
      </section>

      <main className="chat-layout">
        <section className="panel panel--chat">
          <div className="panel__header">
            <h2>会话窗口</h2>
          </div>
          <MessageList
            isLoadingHistory={isLoadingHistory}
            messages={messages}
          />
        </section>

        <section className="panel">
          <div className="panel__header">
            <h2>发送消息</h2>
          </div>
          <MessageInput
            disabled={isSending}
            onChange={setInputValue}
            onSubmit={() => void handleSendMessage()}
            value={inputValue}
          />
        </section>
      </main>
    </div>
  );
}
