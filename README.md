# 🤖 智能客服多Agent系统

> **双语言实现：Python (LangGraph + FastAPI) 主线 + Java (Spring AI Alibaba Graph + Spring Boot) 完整复刻**，并补充本地前后端联调页面与配套面试材料，方便你直接上手调试完整链路。

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![Java](https://img.shields.io/badge/Java-21-orange?logo=openjdk)](https://openjdk.org/)
[![Spring Boot](https://img.shields.io/badge/Spring%20Boot-3.5.9-6DB33F?logo=spring)](https://spring.io/projects/spring-boot)
[![Spring AI](https://img.shields.io/badge/Spring%20AI-1.1.2-6DB33F?logo=spring)](https://spring.io/projects/spring-ai)
[![React](https://img.shields.io/badge/React-18-blue?logo=react)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-5-646CFF?logo=vite)](https://vitejs.dev/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.3+-green)](https://github.com/langchain-ai/langgraph)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## 📋 目录

- [项目简介](#-项目简介)
- [系统架构](#-系统架构)
- [核心功能](#-核心功能)
- [技术栈](#-技术栈)
- [当前实现说明](#-当前实现说明)
- [快速开始](#-快速开始)
- [项目结构](#-项目结构)
- [核心代码解析](#-核心代码解析)
- [面试准备材料](#-面试准备材料)
- [参考项目](#-参考项目)
- [安全说明](#-安全说明)

---

## 🎯 项目简介

本项目是一个**企业级多Agent智能客服系统**，模拟真实金融/电商公司的客服场景。系统由多个专业AI Agent协同工作，自动处理用户咨询、工单创建、知识检索等任务。

**这个项目能帮你做什么？**

- ✅ **面试加分项**：拥有一个真实完整的多Agent项目，不再只是CRUD
- ✅ **Python主线清晰**：当前仓库保留可运行的 Python 后端实现（LangGraph + FastAPI）
- ✅ **Java版完整复刻**：`java-impl/` 用 Spring AI Alibaba Graph 平移相同拓扑，编排/记忆/RAG/合规全对齐
- ✅ **联调闭环完整**：新增 `frontend/` 聊天页面，方便前后端一起调试
- ✅ **面试材料齐全**：简历模板、STAR话术、八股文题库一应俱全
- ✅ **学习参考**：代码有详细注释，架构文档有图文说明

**适合人群：**
- 准备AI/后端岗位面试的同学
- 想了解多Agent系统架构的开发者
- 对LangGraph/Spring AI/Eino感兴趣的工程师

---

## 🏗️ 系统架构

### 整体架构图

```
用户 (Web/App/API)
        │  HTTP/SSE
        ▼
┌──────────────────────┐
│   API Gateway        │  ← 认证、限流、日志
│   (FastAPI / Spring) │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────────────────────────────────┐
│              Supervisor 编排 Agent                │
│  ┌─────────────┐         ┌────────────────────┐  │
│  │  分层记忆系统  │         │  全链路追踪          │  │
│  │ • 工作记忆    │         │  (OpenTelemetry)   │  │
│  │ • 短期(Redis) │         │  Agent调用链可视化  │  │
│  │ • 长期(向量库) │         └────────────────────┘  │
│  └─────────────┘                                  │
└──────┬──────────┬──────────┬──────────┬───────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
  ┌─────────┐┌─────────┐┌─────────┐┌─────────┐
  │ 意图路由  ││ 知识检索  ││ 工单处理  ││ 合规审查  │
  │  Agent   ││  Agent   ││  Agent   ││  Agent   │
  │ (分类)   ││ (RAG)    ││ (CRUD)   ││ (规则+LLM)│
  └─────────┘└─────────┘└─────────┘└─────────┘
                  │              │
                  ▼              ▼
           ┌──────────────────────────────┐
           │         MCP 工具协议层         │
           │  订单查询 | 工单CRUD | 风控接口  │
           │  知识库搜索 | 用户画像查询       │
           └──────────────────────────────┘
```

### 请求处理流程

```
① 用户发送消息："我的订单什么时候到？"
        ↓
② Supervisor 分析意图 → 路由决策
        ↓
③ 意图路由 Agent 识别意图: "order_query"
        ↓
④ 知识检索 Agent → 调用MCP工具查询订单
        ↓
⑤ 合规审查 Agent → 检查回复内容合规性
        ↓
⑥ Supervisor 汇总结果 → 返回最终回复
```

---

## ✨ 核心功能

### 1. Supervisor 编排模式
**什么是Supervisor？** 就像一个项目经理，接到需求后分配给不同专家处理，最后汇总结果。

| 特性 | 说明 |
|------|------|
| 中央协调 | 由Supervisor统一调度，子Agent只做专业工作 |
| 子任务拆解 | 将复杂诉求自动拆解为带依赖关系的子任务，并按依赖条件顺序推进 |
| 循环调度 | dispatch_step ⇄ collect_step 循环执行，直至依赖满足 |
| 合规汇聚 | 所有业务结果统一经合规审查后汇总 |
| 断点恢复 | 使用LangGraph Checkpoint，对话可中断续接 |

> 说明：并行调度与 Human-in-the-Loop 的底层能力已铺垫（`dispatch_mode`/Checkpoint），当前实际以串行循环 + 依赖条件执行。

### 2. 分层记忆系统
**为什么需要三层记忆？** 类似人类记忆：工作桌(工作记忆) + 笔记本(短期) + 大脑长期记忆。

| 记忆层 | 存储位置 | 生命周期 | 延迟 | 用途 |
|--------|----------|----------|------|------|
| **工作记忆** | 进程内存 (dict) | 单次请求 | <1ms | 当前推理状态、跨轮补充信息收集 |
| **短期记忆** | Redis（不可用降级内存） | TTL 30分钟 | 1-5ms | 多轮对话上下文（滚动摘要 + 最近几轮原文） |
| **长期记忆** | FAISS + sentence-transformers | 永久 | 10-50ms | 知识库、用户画像、历史工单（BM25+向量混合检索） |

**按需注入优化 Token**：注入 Prompt 时用「滚动摘要 + 最近几轮 + 当前消息」替代全量 20 轮历史，用 tiktoken 实测量化。

### 3. MCP 工具协议
**什么是MCP？** Model Context Protocol，AI模型调用外部工具的标准协议，类似HTTP规范了Web通信。

```json
{
  "name": "order_query",
  "description": "查询订单信息",
  "inputSchema": {
    "type": "object",
    "properties": {
      "order_id": {"type": "string", "description": "订单ID"}
    },
    "required": ["order_id"]
  }
}
```

已实现的MCP工具：
- `order_query` — 查询订单状态、金额
- `ticket_create` — 工单创建
- `risk_check` — 金融风控（金额阈值规则）
- `knowledge_search` — 知识库检索（FAISS + BM25 + RRF 混合召回）

> 工单 Agent 通过 `order_query` / `ticket_create` 实际落库，合规 Agent 调用 `risk_check`，知识 Agent 优先走 `knowledge_search`。

### 4. RAG 知识检索
**什么是RAG？** Retrieval-Augmented Generation，先从知识库检索相关内容，再让AI生成回答，避免AI"瞎编"。

```
用户问题: "怎么退款？"
    ↓ Query改写（扩展关键词）
"退款 政策 申请 流程 时限"
    ↓ 双路召回
      · FAISS 向量检索（语义匹配）Top-N
      · BM25 关键词检索（专有名词）Top-N
    ↓ RRF 融合（两路按排序位置合并）
    ↓ Rerank 精排（Cross Encoder，离线降级 LLM）→ Top-3
    ↓ 上下文注入（文档内容 + 用户问题 + 来源标注）
    ↓ LLM生成（基于文档、约束回答边界）
最终回答 + 引用来源标注
```

**为什么混合召回？** 向量擅长语义近义，BM25 擅长精确匹配专有名词（产品名、订单号），RRF 融合兼顾召回与精确。可运行 `python eval/rag_eval.py` 横向对比三路，`python eval/ragas_judge.py --samples 24` 做 RAGAS 风格评测。

### 5. 全链路追踪 (OpenTelemetry)
可以清楚地看到每次请求经过哪些Agent、每个步骤耗时多少、消耗了多少Token：

```
[Root] user_request (总耗时: 2.8s, 总Token: 1850)
  ├── [Span] supervisor.route_decision     → 800ms, 150 tokens
  ├── [Span] knowledge_rag.process         → 1.9s
  │     ├── rag.query_rewrite              → 200ms
  │     ├── rag.vector_search              → 15ms
  │     ├── rag.rerank                     → 500ms
  │     └── rag.generate_answer            → 1200ms, 1200 tokens
  ├── [Span] compliance_checker.process    → 600ms, 400 tokens
  └── [Span] supervisor.synthesize         → 50ms
```

### 6. 合规审查
专为金融场景设计：
- **敏感词检测**：自动识别违规词汇
- **PII保护**：过滤身份证、银行卡等隐私信息
- **越权访问治理**：防止用户绕过权限限制
- **双重审查**：规则引擎（快，<2ms）+ LLM审查（准，~600ms）

---

## 🛠️ 技术栈

| 层次 | 技术选型 | 说明 |
|------|----------|------|
| **AI框架** | LangGraph / Spring AI / Eino | 多Agent编排 |
| **LLM** | GPT-4o / Claude 3.5 | 大语言模型 |
| **向量数据库** | FAISS (开发) / Milvus (生产) | 知识检索 |
| **缓存** | Redis | 短期记忆、会话管理 |
| **追踪** | OpenTelemetry + Jaeger | 全链路追踪 |
| **API** | FastAPI / Spring Boot / Gin | REST接口 |
| **容器** | Docker + Docker Compose | 一键部署（已移除，见说明） |
| **协议** | MCP (Model Context Protocol) | 工具调用标准 |

---

## 🧭 当前实现说明

| 模块 | 当前状态 | 说明 |
|------|----------|------|
| Python 后端 | ✅ 已在仓库根目录 | LangGraph + FastAPI，多Agent 主链路 |
| Java 后端 | ✅ [`java-impl/`](./java-impl/) | Spring AI Alibaba Graph + Spring Boot 3.5.9，Python 拓扑完整平移 |
| 前端联调 | ✅ 新增 [`frontend/`](./frontend/) | React + Vite 聊天页，直接复用现有 API |
| Docker | ⏸️ 已移除 | Docker 相关配置已清理，如需可自行添加 |
| Go | ⏸️ 未包含 | 如需可用 Eino 复刻，架构文档已给出思路 |

### Java 版（java-impl/）速览

- **编排**：`StateGraph` 平移 Python 拓扑 —— `decompose → intent_router → dispatch_step ⇄ collect_step（条件边循环，依赖满足或迭代上限）→ compliance_check → synthesize`
- **记忆**：混合检索（BM25 + TF向量 + RRF，纯本地零依赖）+ 滚动摘要按需注入（长会话 Token 节省实测 0→50%）+ 工作记忆
- **RAG**：Query 改写 / 引用来源标注 / 无文档兜底
- **MCP**：`order_query` / `ticket_create` / `risk_check` / `knowledge_search` 四工具真实逻辑 + `POST /api/tools/call`
- **合规**：规则引擎（禁词+PII脱敏）+ LLM 二阶段审查（含产品条款豁免），结果经 `masked_response` 回传 synthesize
- **持久化**：SQLite（`data/smartcs.db`），工单/订单重启不丢
- **检索评测**（24 样本，同 Python `eval/dataset_v2.json` 同库）：完整链路含 RAGAS 同口径 **Context Precision 95.83% / Recall 95.14%**；离线 MRR 0.9792 / Hit@3 100%

> 完整 Java 版说明见 [`java-impl/README.md`](./java-impl/README.md)。

---

## 🚀 快速开始

### 前置条件
- Python 3.11+
- Node.js 18+
- 一个 OpenAI API Key（或其他 LLM 的 Key）
- Redis（本地安装或远程服务）

### 方式一：直接运行后端

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m api.main
```

访问：

- API 文档: `http://localhost:8000/docs`
- 健康检查: `http://localhost:8000/health`

测试接口：

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/chat" -ContentType "application/json" -Body (@{ user_id = "user_001"; message = "我想查询订单状态" } | ConvertTo-Json)
curl.exe --% -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" -d "{\"user_id\":\"user_001\",\"message\":\"我想查询订单状态\"}"
```

### 方式二：启动前端联调页面

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

访问：

- 前端页面: `http://localhost:5173`

默认情况下，前端会通过 Vite 开发代理把 `/api` 和 `/health` 转发到 `http://localhost:8000`，因此不需要改现有 FastAPI 路由。

### 方式三：启动 Java 版后端（Spring AI）

前置：JDK 21。复制根 `.env` 到 java-impl 同级即可读取（脚本自动读取仓库根 `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME`）。

```powershell
cd java-impl
# 使用自带 Maven Wrapper（自动下载 3.9.9）；如需代理先设置
# $env:MAVEN_OPTS="-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort=7890 -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7890"
./mvnw.cmd -B -ntp package -DskipTests
java -jar target\smart-cs-agent-1.0.0.jar --server.port=18080
```

访问：

- 健康检查: `http://localhost:18080/health`
- 聊天接口: `POST http://localhost:18080/api/chat`
- 工具接口: `POST http://localhost:18080/api/tools/call`、`GET /api/tools`
- 指标接口: `GET http://localhost:18080/api/metrics`

> Java 版冒烟验收脚本已内置：`boot-smoke.ps1`（启动）、`chat-smoke.ps1`（3轮真实对话）、`chat-smoke-st2.ps1`（8轮长会话+压缩）、`tools-smoke.ps1`（4工具）、`persistence-smoke.ps1`（重启持久化）。

---

## 📁 项目结构

```text
smart-cs-multi-agent/
├── README.md                       ← 项目说明与快速开始
├── requirements.txt                ← Python 依赖
├── .env.example                    ← 后端环境变量模板
├── agents/                         ← 核心 Agent 编排与子 Agent
├── api/                            ← FastAPI 入口与接口层
├── memory/                         ← 工作记忆 / 短期记忆 / 长期记忆
├── mcp/                            ← MCP 工具注册、发现与调用
├── tracing/                        ← OpenTelemetry 配置与指标汇总
├── database/                       ← SQLite（orders / tickets 持久化）
├── eval/                           ← RAG 检索评测 + Token 优化评测
├── frontend/                       ← React + Vite 前端联调页面
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
├── java-impl/                      ← Java 版完整复刻（Spring AI Alibaba Graph）
│   ├── pom.xml                     ← Spring Boot 3.5.9 + Spring AI 1.1.2 + graph-core
│   ├── README.md                   ← Java 版说明（架构/API/对齐差异/阶段记录）
│   ├── src/main/java/com/smartcs/  ← agent / memory / mcp / biz / tracing / config
│   └── *-smoke.ps1                 ← boot / chat / tools / persistence 冒烟验收脚本
├── docs/                           ← 项目文档
│   ├── deployment.md               ← 当前部署与联调指南
│   ├── 架构.md                     ← 系统架构说明
│   ├── 核心代码讲解.md             ← 关键代码解析
│   ├── 半天速成精读表.md           ← 快速理解路径
│   └── interview/                  ← 面试准备材料
│       ├── 简历模板.md
│       ├── star_面试话术.md
│       ├── 八股文.md
│       └── 项目问答.md
└── LICENSE
```

---

## 💻 核心代码解析

### Supervisor 编排核心逻辑（Python）

这是整个系统最重要的部分，理解了这个代码，面试时就能讲清楚多Agent协作原理：

```python
# agents/supervisor.py

# 1. 定义全局状态（类似"黑板"，所有Agent共享）
class AgentState(TypedDict):
    messages: list[BaseMessage]    # 对话历史
    user_id: str                   # 用户ID
    intent: str                    # 识别到的意图
    sub_results: dict[str, Any]    # 各Agent处理结果
    compliance_passed: bool        # 合规是否通过
    final_response: str            # 最终回复

# 2. 构建有向图（定义Agent之间的流转关系）
def create_supervisor_graph():
    graph = StateGraph(AgentState)
    
    # 添加节点（每个Agent是一个节点）
    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)
    
    # 设置入口
    graph.set_entry_point("supervisor_route")
    
    # 条件路由（根据意图决定走哪条路）
    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,              # 路由函数
        {
            "knowledge_rag": "knowledge_rag",
            "ticket_handler": "ticket_handler",
        }
    )
    
    # 所有Agent处理后都经过合规审查
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)
    
    return graph.compile(checkpointer=MemorySaver())
```

**面试时怎么讲这段代码？**
> "我们用LangGraph的StateGraph构建了一个有向图，Supervisor作为中心节点负责路由，子Agent各司其职。所有回复都强制经过合规审查节点，这是金融场景的合规要求。MemorySaver提供检查点功能，支持对话断点续接。"

### MCP工具协议实现

```python
# mcp/mcp_server.py

# MCP工具的核心：描述工具能力，让AI知道什么时候用这个工具
order_query_tool = {
    "name": "order_query",
    "description": "查询用户订单的状态、物流、金额等信息",
    "inputSchema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string", 
                "description": "订单编号，如 ORD-2024-001"
            },
            "user_id": {
                "type": "string",
                "description": "用户ID，用于权限验证"
            }
        },
        "required": ["order_id", "user_id"]
    }
}
```

---

## 📚 面试准备材料

配套完整面试资料，帮你从"能看懂代码"到"面试时能流畅讲清楚"：

| 文档 | 内容说明 | 链接 |
|------|----------|------|
| **简历模板** | STAR法则项目经历写法，覆盖Python/Java/Go不同岗位角度 | [查看](./docs/interview/简历模板.md) |
| **STAR面试话术** | "请介绍你的项目"等高频问题的标准回答模板 | [查看](./docs/interview/star_面试话术.md) |
| **八股文题库** | 30+高频面试题 + 详细答案 + 追问应对策略 | [查看](./docs/interview/八股文.md) |
| **项目深度追问** | 面试官最爱问的20+深度问题 + 踩坑分享 | [查看](./docs/interview/项目问答.md) |
| **架构设计文档** | 完整流程图、时序图、技术选型对比分析 | [查看](./docs/架构.md) |
| **代码讲解文档** | 核心模块逐行解析，设计模式说明 | [查看](./docs/核心代码讲解.md) |
| **部署指南** | 本地启动、前后端联调、环境变量配置 | [查看](./docs/deployment.md) |
| **Java版说明** | Spring AI Alibaba Graph 编排平移、排坑记录、评测结果 | [查看](./java-impl/README.md) |

### 常见面试问题预览

**Q: 为什么用Supervisor模式而不是让Agent直接互相调用？**
> A: Supervisor模式的优势在于集中控制，便于追踪和调试；避免Agent之间形成循环依赖；Supervisor可以做全局优化，比如并行调度多个Agent；出错时有统一的错误处理和回退机制。

**Q: 三层记忆的设计原则是什么？**
> A: 参考了人类认知的记忆模型。工作记忆对应当前注意力焦点，速度最快但容量有限；短期记忆用Redis实现30分钟TTL，保持对话上下文连贯性；长期记忆用向量数据库存储知识库和用户历史，支持语义相似度检索。

**Q: MCP协议相比直接调用函数有什么优势？**
> A: MCP是标准化协议，工具描述用JSON Schema，AI可以自动发现和理解工具能力，不需要硬编码工具调用逻辑；支持动态工具注册，新增工具不需要修改Agent代码；协议层做了权限控制和参数验证。

---

## 📖 参考项目

本项目设计参考了以下企业级开源项目，建议结合阅读：

| 项目 | Stars | 参考内容 |
|------|-------|----------|
| [AWS Agent Squad](https://github.com/awslabs/agent-squad) | 7,500+ | 智能意图分类 + SupervisorAgent 设计 |
| [LangGraph Supervisor](https://github.com/langchain-ai/langgraph-supervisor-py) | — | Supervisor模式预构建库，官方最佳实践 |
| [Spring AI Alibaba](https://github.com/alibaba/spring-ai-alibaba) | 9,000+ | Java多Agent编排，阿里巴巴生产实践 |
| [Eino (CloudWeGo)](https://github.com/cloudwego/eino) | 10,300+ | 字节跳动Go企业级Agent框架 |
| [Multi-Agent Enterprise CRM](https://github.com/Mrgig7/Multi-Agent-Enterprise-CRM) | — | LangGraph + Kafka 生产级客服方案 |

---

## 🔒 安全说明

- 本项目**不包含任何真实的API Key、Token或密码**
- 所有敏感配置通过环境变量注入（见 `.env.example`）
- `.env.example` 仅提供占位符示例，**不要直接使用**
- 请勿将含有真实凭据的 `.env` 文件提交到版本控制

---

## 📄 License

[MIT License](./LICENSE) — 自由使用、修改、分发，保留原始版权声明即可。

---

<div align="center">

**如果这个项目对你有帮助，欢迎 ⭐ Star 支持一下！**

</div>
