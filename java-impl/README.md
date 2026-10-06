# Smart CS Multi-Agent — Java 实现（java-impl）

企业级智能客服多 Agent 系统的 **Java 版**，与根目录 Python 版(LangGraph) 功能对齐，编排引擎采用 **Spring AI Alibaba Graph**（LangGraph 风格状态图），RAG/记忆/合规/工具/MCP 全部真实化。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言/运行时 | Java 21 LTS |
| 框架 | Spring Boot 3.5.9 |
| LLM 接入 | Spring AI 1.1.2（`spring-ai-starter-model-openai`，OpenAI 兼容网关） |
| 编排引擎 | `com.alibaba.cloud.ai:spring-ai-alibaba-graph-core:1.1.2.4-security-fix`（StateGraph / OverAllState / CompiledGraph / KeyStrategy） |
| 记忆 | 短期：Redis（自动降级内存）+ 滚动摘要；长期：BM25 + TF向量 + RRF 混合检索；工作记忆：进程内 |
| 持久化 | SQLite（`org.xerial:sqlite-jdbc`，`data/smartcs.db`） |
| 合规 | 规则引擎（禁词/PII）+ LLM 深度二阶段审查 + PII 脱敏回传 |
| 指标/追踪 | AgentTracer（耗时/成功率/**token 计量** + **OpenTelemetry span**，OTel exporter 可配） |

## 快速开始

```bash
# 环境要求: JDK 21；首次构建需联网（或配置 Maven 代理）
./mvnw -q package -DskipTests
java -jar target/smart-cs-agent-1.0.0.jar --server.port=8080
```

LLM 配置（Spring AI 约定 base-url 不含 `/v1`；例如网关 `https://chickener.top` 时）：

```bash
# application.yml 或环境变量
export SPRING_AI_OPENAI_BASE_URL=https://chickener.top
export SPRING_AI_OPENAI_API_KEY=sk-xxxx
export SPRING_AI_OPENAI_CHAT_OPTIONS_MODEL=gpt-6-luna
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 聊天（多轮上下文 + 摘要注入 + 合规审查） |
| GET | `/api/history/{sessionId}` | 会话历史 |
| GET/POST | `/api/tools` / `/api/tools/call` | MCP 工具发现 / 调用 |
| GET | `/api/metrics` | Agent 指标（含 token） |
| GET | `/api/checkpoint/{sessionId}` | MemorySaver 断点续接信息（对齐 Python thread_id） |
| GET | `/health` | 健康检查（根路径，与前端契约一致） |

## 编排架构（StateGraph）

```
START → decompose → intent_router → dispatch_step ⇄ collect_step(条件边循环, ≤10)
                                      │
              knowledge_rag / ticket_handler（由 dispatch 路由）
                                      ↓
                             compliance_check → synthesize → END
```

状态：`AgentState`（含 `messages` 多轮上下文 / `subTasks` / `taskResults` / `completedTaskIds` / 摘要 / 工作记忆）封装进 `OverAllState`；条件边 `check_more_steps` 判断依赖可执行与迭代上限。

## 对齐与差异（vs Python 版）

### 已对齐
- 编排拓扑（decompose→dispatch⇄collect→compliance→synthesize）与 Python LangGraph 一一对应
- 多轮上下文修复（历史回填 `messages`；T2 指代"那最低投多少钱"正确命中金葵理财）
- 滚动摘要压缩 + 按需注入（长会话 Token 节省实测 **0%→50%**）
- 混合检索+重排（BM25+向量+RRF+LLM rerank）24 样本评测：
  - RAGAS LLM-judge 口径：**Context Precision 95.83% / Context Recall 95.14%**（完整链路含重排，与简历同口径同公式）
  - 离线 IR 口径：MRR 0.9792 / Hit@3 100%（rerank 前 MRR 0.9514）
- MCP 四工具真实化（order_query / ticket_create / risk_check / knowledge_search），工单落 SQLite
- 合规：规则引擎 + LLM 二阶段 + 豁免转述产品条款 + PII 脱敏回传
- **强化（对齐企业级叙事）**：
  - MCP 工具 **Function Calling 意图路由**（LLM 自主选工具 + 抽参数，ToolCallback 封装，非硬编码分发）
  - `risk_check` **滑动时间窗口行为风控**（Redis ZSet 短时频率/累计金额，查询只读不入窗，Redis 不可用降级内存窗口）
  - **知识入库流水线**：扫描 `knowledge_base/` 目录（子目录=分类），`.md/.txt` 直读 + Apache Tika 解析 PDF/Word/HTML/RTF，512 字符切块 128 重叠，目录为空回退内置 24 篇

### 差异/说明
- 向量路用 **TF 向量**近似（纯本地零依赖），RRF 接口与真实 embedding（Spring AI / Milvus）平滑兼容
- Supervisor decompose 走 `BeanOutputConverter`（prompt 内嵌 schema，规避网关 json_schema 兼容问题，与 Python 踩坑结论一致）
- 追踪已接 **OpenTelemetry**（span + traceId + token 属性；配 `OTEL_EXPORTER_OTLP_ENDPOINT` 走 OTLP，缺省降级日志导出）
- 编排已接 **MemorySaver Checkpoint**（sessionId=thread_id，每步一个 checkpoint，`GET /api/checkpoint/{sessionId}` 可见）
- 工单/订单存 SQLite（`data/`），Python 版存 `database/app.db`，部署需各自建库

## 阶段落地记录

| 阶段 | 内容 | 验收 |
|---|---|---|
| 0 | Maven wrapper、Spring Boot 3.5.9 + Spring AI 1.1.2 + graph-core、`/health` 根路径 | compile + 启动冒烟 |
| 1 | StateGraph 编排对齐 + 上下文回填修复 | 3 轮真实对话（知识/指代/工单）全通 |
| 2 | 混合检索 + 滚动摘要 + 工作记忆 | 8 轮长会话、Token 节省 50% |
| 3 | RAG：Query 改写 + 引用标注 + 无文档兜底 | 回答带 `[p1_*.md]` 来源 |
| 4 | MCP 四工具真实化 + `/api/tools/call` | 工具冒烟 8/8 |
| 5 | 合规 LLM 二阶段 + token 计量（含 Spring AI 1.1 三个坑的规避） | `/metrics` 含 token，误判豁免修复 |
| 6 | SQLite 持久化 | 重启后工单仍在（持久化实证） |
| 7 | 检索评测（24 样本）：RAGAS LLM-judge 95.83%/95.14%（含重排）；离线 MRR 0.9792 | 见上指标 |

## 目录结构

```
src/main/java/com/smartcs/
  agent/        AgentState / SupervisorAgent(StateGraph) / 四个子 Agent
  biz/          OrderRepository / TicketRepository (SQLite)
  config/       ChatController / HealthController / ChatClientConfig
  eval/         RetrievalEval（检索评测，读 ../eval/dataset_v2.json）
  mcp/          MCPToolServer（真实工具）
  memory/       LongTermMemory(混合检索) / ShortTermMemory(摘要) / WorkingMemory
  tracing/      AgentTracer（token 计量）
```