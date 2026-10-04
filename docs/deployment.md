# 部署指南

## 1. 当前仓库结构

当前分支将 Python 实现放在仓库根目录，后端入口：

- `api/main.py`
- `agents/`
- `memory/`
- `mcp/`
- `tracing/`
- `database/`
- `eval/`（RAG 检索评测 + Token 优化评测）

> 如果你看到旧文档提到 `python-impl/`，那是最早的目录，现在已经提升到仓库根目录。所有命令默认在仓库根目录执行。

## 2. 本地启动后端

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# 在 .env 里填好 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME
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

## 3. 依赖注意事项（重要）

`requirements.txt` 除 LLM/编排依赖外，还包含 **RAG 检索链路** 相关依赖：

- `sentence-transformers` —— 向量 embedding（缺失时降级为哈希向量，语义检索退化为近似，务必安装）
- `rank-bm25` —— BM25 关键词检索（缺失时降级为纯 Python `_SimpleBM25`，效果略差）
- `jieba` —— 中文分词（缺失时降级为汉字单字切分，效果变差）
- `faiss-cpu` —— 向量索引
- `tiktoken` —— Token 计量（按需注入效果量化）

此外 embedding 模型默认使用 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 维，本地加载），首次运行会从 HuggingFace 下载；可提前缓存或通过环境变量切换到其他模型。

## 4. 本地前后端联调

前端位于 `frontend/`，使用 React + Vite，默认通过开发代理把 `/api` 和 `/health` 转发到 `http://localhost:8000`。

### 启动步骤

1. 先在仓库根目录启动后端：`python -m api.main`
2. 再打开一个新终端，进入前端目录并启动开发服务器

```powershell
cd frontend
npm install
npm run dev
```

启动后访问：

- 前端页面: `http://localhost:5173`

联调行为：

- 首次发送消息时，前端调用 `POST /api/chat`
- 后端返回 `session_id` 后，前端会把它保存到浏览器本地
- 页面刷新后，前端会调用 `GET /api/history/{session_id}` 恢复上下文
- 点击"新会话"按钮只会清除本地 `session_id`，不会改动后端 Agent 逻辑

## 5. API 接口说明

常用接口：

- `POST /api/chat`
- `GET /api/history/{session_id}`
- `GET /api/tools` / `POST /api/tools/call`
- `GET /api/metrics`（Agent 指标，含 token 计量）
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
  "intent": "consultation",
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

### GET /api/metrics

```json
{
  "agent_metrics": {
    "knowledge_rag": {
      "total_calls": 12,
      "avg_duration_ms": 1800,
      "error_rate": 0,
      "total_input_tokens": 48000,
      "total_output_tokens": 9000,
      "avg_input_tokens": 4000
    }
  },
  "tool_call_log": [ ... ]
}
```

## 6. 评测脚本（从项目根目录运行）

```bash
# 1) 离线 IR 指标：向量 / BM25 / 混合(RRF) 三路对比
python eval/rag_eval.py
python eval/rag_eval.py --dataset eval/dataset_v2.json --output eval/results_v2.json

# 2) RAGAS 风格 LLM-as-Judge（需 .env 配置好 LLM，24 条 QA 会调用多次 LLM）
python eval/ragas_judge.py --dataset eval/dataset_v2.json --samples 24

# 3) Token 优化（全量 vs 按需注入）
python eval/token_compression_eval.py
```

## 7. 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| OPENAI_API_KEY | LLM API 密钥 | 无 |
| OPENAI_BASE_URL | OpenAI 兼容 API 端点 | https://api.openai.com/v1 |
| MODEL_NAME | 模型名称 | gpt-4o |
| REDIS_URL | Redis 地址 | redis://localhost:6379/0 |
| FAISS_INDEX_PATH | FAISS 索引路径 | ./vector_store/faiss_index |
| OTEL_SERVICE_NAME | 追踪服务名 | smart-cs-multi-agent |
| OTEL_EXPORTER_OTLP_ENDPOINT | OTLP 端点 | http://localhost:4317 |

> 安全：`.env` 含真实凭据，不要提交到版本库（`.gitignore` 已忽略）。`.env.example` 仅提供占位符示例。