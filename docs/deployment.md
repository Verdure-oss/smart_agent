# 部署指南

## 1. 当前仓库结构

当前分支已经将原 `python-impl/` 下的 Python 代码提升到了仓库根目录，后端入口直接是：

- `api/main.py`
- `agents/`
- `memory/`
- `mcp/`
- `tracing/`

如果你按照旧文档执行 `cd python-impl`，现在会找不到目录。后续所有命令都默认在仓库根目录执行。

## 2. 本地启动后端

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m api.main
```

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m api.main
```

访问地址：

- Swagger UI: `http://localhost:8000/docs`
- 健康检查: `http://localhost:8000/health`

说明：

- 后端默认运行在 `8000` 端口。
- `memory/short_term.py` 内置了 Redis 不可用时的内存回退逻辑，所以第一轮联调即使没启动 Redis，也能先把聊天链路跑通。

## 3. 本地前后端联调

新增的前端位于 `frontend/`，使用 React + Vite，默认通过开发代理把 `/api` 和 `/health` 转发到 `http://localhost:8000`。

### 启动步骤

1. 先在仓库根目录启动后端：`python -m api.main`
2. 再打开一个新终端，进入前端目录并启动开发服务器

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

启动后访问：

- 前端页面: `http://localhost:5173`

联调行为：

- 首次发送消息时，前端调用 `POST /api/chat`
- 后端返回 `session_id` 后，前端会把它保存到浏览器本地
- 页面刷新后，前端会调用 `GET /api/history/{session_id}` 恢复上下文
- 点击“新会话”按钮只会清除本地 `session_id`，不会改动后端 Agent 逻辑

## 4. API 接口说明

当前前端联调主要使用这三个接口：

- `POST /api/chat`
- `GET /api/history/{session_id}`
- `GET /health`

### POST /api/chat

```json
// Request
{
  "message": "我想了解一下理财产品A",
  "user_id": "user_001",
  "session_id": "optional-session-id"
}

// Response
{
  "response": "关于理财产品A...",
  "session_id": "xxx",
  "intent": "knowledge_rag",
  "compliance_passed": true
}
```

### GET /api/history/{session_id}

```json
{
  "session_id": "xxx",
  "messages": [
    {
      "role": "user",
      "content": "我想了解一下理财产品A",
      "timestamp": "2026-04-17T10:00:00"
    }
  ]
}
```

## 5. 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| OPENAI_API_KEY | LLM API密钥 | 无 |
| OPENAI_BASE_URL | API端点 | https://api.openai.com/v1 |
| MODEL_NAME | 模型名称 | gpt-4o |
| REDIS_URL | Redis地址 | redis://localhost:6379/0 |
| OTEL_SERVICE_NAME | 追踪服务名 | smart-cs-multi-agent |
| OTEL_EXPORTER_OTLP_ENDPOINT | OTLP端点 | http://localhost:4317 |
