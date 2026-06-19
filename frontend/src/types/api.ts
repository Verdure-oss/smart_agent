export interface ChatRequest {
  message: string;
  user_id: string;
  session_id?: string;
}

export interface ChatResponse {
  response: string;
  session_id: string;
  intent: string;
  compliance_passed: boolean;
}

export interface HistoryMessage {
  role: string;
  content: string;
  timestamp?: string;
}

export interface HistoryResponse {
  session_id: string;
  messages: HistoryMessage[];
}

export interface HealthResponse {
  status: string;
  version: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: string;
  meta?: string;
  pending?: boolean;
}
