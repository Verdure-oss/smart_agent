# SmartCS · 多 Agent 智能客服系统

> 一个把「查订单、办退款、问知识、走合规」串成一条流水线的多 Agent 系统：Supervisor 编排 + 三层记忆 + 混合检索 RAG + MCP 工具层 + 双实现（Java / Python）同构复刻。

**定位**：不是 Demo，是一套可讲清「为什么这样设计」的工程化实现——每个能力都有落库证据、评测数字和降级路径。

![Java 21](https://img.shields.io/badge/Java-21-orange?logo=openjdk)
![Spring Boot 3.5.9](https://img.shields.io/badge/Spring_Boot-3.5.9-6DB33F?logo=spring)
![Spring AI 1.1.2](https://img.shields.io/badge/Spring_AI-1.1.2-6DB33F)
![Python 3.11](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-0.3+-green)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-1.49-purple)
![RAGAS](https://img.shields.io/badge/RAGAS-95.83%25%20%2F%2095.14%25-brightgreen)

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [关键数据](#关键数据)
- [系统设计：五层](#系统设计五层)
- [一次对话的完整旅程](#一次对话的完整旅程)
- [双实现矩阵：Java × Python](#双实现矩阵java--python)
- [值得讲的技术决策](#值得讲的技术决策)
- [快速开始](#快速开始)
- [仓库结构](#仓库结构)
- [评测与复现](#评测与复现)
- [已知边界](#已知边界)
- [FAQ](#faq)
- [License](#license)

---

## 它解决什么问题

客服场景有三个真实痛点，本项目的设计逐一回应：

| # | 痛点 | 设计回应 |
|---|------|----------|
| 1 | **复合诉求难承接**：一句「我要投诉，顺便查下订单」拆不出子任务，单 Agent 答不全 | **Supervisor 编排**：自动 decompose 子任务 → dispatch ⇄ collect 循环按依赖推进 → 合规汇聚 |
| 2 | **多轮上下文易丢**：聊到第 8 轮，模型忘了「刚才说的那个产品」 | **三层记忆 + 滚动摘要**：工作记忆（单轮推理态）+ Redis 短期（TTL 30min，滚动摘要+最近原文）+ 长期检索，按需注入替代全量历史 |
| 3 | **知识检索靠不住**：向量检索对专有名词（产品名/政策编号）召回差，回答没有来源 | **混合检索 RAG**：Query 改写 → BM25+向量双路 → RRF 融合 → 重排 → 引用标注，评测可复现 |

> 回答质量是「组织纪律」的产物：任何回复都必须过合规节点、带来源标注、有兜底话术——这些约束在代码里是强制边，不是提示词约定。

---

## 关键数据

| 指标 | 数值 | 说明 |
|------|------|------|
| RAGAS Context Precision | **95.83%** | 24 样本，LLM-as-judge，与 Python 版同一套评测脚本同口径 |
| RAGAS Context Recall | **95.14%** | 同上；Python 版归档为 94.79%，Java 反超 |
| 离线 IR MRR | **0.9792** | rerank 前 0.9514 → rerank 后提升排序质量 |
| 离线 IR Hit@3 | **100%** | 24/24 命中相关文档 |
| 长会话 Token 节省 | **0% → 50%** | 滚动摘要 + 按需注入，替代全量历史入 Prompt |
| 编排断点 | **每会话 8 checkpoint** | MemorySaver 按 sessionId=thread_id 落盘，中断可续 |
| 全链路追踪 | **同一 traceId 贯穿根** | supervisor 根 span → 子 Agent span，含 token 计量 |

---

## 系统设计：五层

```
┌──────────────────────────────────────────────────────────────┐
│ 接入层   POST /api/chat · /api/tools · /api/tools/call        │
│          /api/metrics · /api/checkpoint/{sessionId} · /health │
├──────────────────────────────────────────────────────────────┤
│ 编排层   StateGraph: decompose → intent → dispatch⇄collect    │
│          （条件边循环，依赖满足/迭代上限）→ compliance → synth  │
│          Checkpoint(MemorySaver) · 图结构可导出 Mermaid       │
├──────────────────────────────────────────────────────────────┤
│ Agent 层 intent_router · knowledge_rag · ticket_handler ·     │
│          compliance_checker                                   │
├──────────────────────────────────────────────────────────────┤
│ 工具层   MCP 协议: order_query · ticket_create · risk_check · │
│          knowledge_search（Function Calling 自主路由）        │
├──────────────────────────────────────────────────────────────┤
│ 基座     Redis(降级内存) · SQLite/MySQL(方言层) · SQLite 工单   │
│          Apache Tika 解析 · OpenTelemetry · 滑动窗口风控       │
└──────────────────────────────────────────────────────────────┘
```

### 编排层：一张有向图，而不是 if-else

系统核心是 `StateGraph`（LangGraph / Spring AI Alibaba Graph 同构）：

```
START → supervisor_decompose → intent_router → dispatch_step
        dispatch_step ──条件──→ knowledge_rag / ticket_handler
        每个子 Agent ──→ collect_step ──条件──→ 还有子任务? dispatch_step
                                         └── 全部完成 → compliance_check → synthesize → END
```

- **条件边**决定走向：意图路由决定走哪个 Agent，`collect_step` 判断是否还有依赖子任务或达到迭代上限。
- **断点续接**：每次 invoke 携带 `thread_id=sessionId`，8 个节点各存一个 checkpoint，进程中断后同一会话可续。
- **图即文档**：`graphDiagram()` 输出 Mermaid，架构图从代码生成，不靠画图。

### 记忆层：三层分工 + 按需注入

| 层 | 载体 | 生命周期 | 用途 |
|----|------|----------|------|
| 工作记忆 | 进程内 | 单次请求 | 当前推理状态、子任务汇总 |
| 短期记忆 | Redis（降级内存） | TTL 30min | 滚动摘要 + 最近 N 轮原文，按需注入 |
| 长期记忆 | BM25 + TF 向量 + RRF（纯本地） | 永久 | 知识库/文档语义检索，接口可平滑换真实 embedding |

Token 优化路径：全量 20 轮历史 ≈ 长文本入 Prompt → 压缩为「滚动摘要 + 最近 2 轮 + 当前消息」，实测长会话 Token 消耗下降 **0% → 50%**。

### 工具层：MCP 协议 + Function Calling 自主路由

- 四个工具走统一的 MCP 风格注册/发现/调用（`tools/list` 返回 schema，`tools/call` 执行）。
- **Function Calling**：`ticket_handler` 不再硬编码调 `ticket_create`，而是把工具声明为 ToolCallback，LLM 自主决定「建单/查单/风控」并抽取参数（user_id 由系统注入，避免缺参反问）。
- 每个工具都有真实落库/真实逻辑，不是 mock：订单查 SQLite、工单写 tickets 表、风控走滑动窗口。

### 合规层：规则引擎 + LLM 二阶段 + 否定豁免

```
content ──→ 规则引擎(禁词/PII, <2ms) ──通过──→ LLM 深度审查(~600ms) ──通过──→ 放行
                   │ 命中                           │ 命中                      │
                   ▼                                ▼                          ▼
              脱敏+违规原因 ──────→ 转人工/拒绝回复，违规原因回传 synthesize
```

一个容易被忽略但很关键的细节：**否定豁免**。知识库原文「不承诺保本保息」含禁词「保本保息」，朴素规则引擎会误杀。规则层扫描禁词前 3 个字符内的否定词（不/无/非/并非…），免责表述放行——这是上线后真实踩到的坑。

### 基座层：风控窗口、多格式入库、可观测、存储可切换

- **滑动时间窗口风控**：`risk_check` 用 Redis ZSet 统计 5 分钟窗口内事务频率与累计金额（事务入窗、查询只读，避免客服查询污染画像），Redis 不可用降级内存窗口；单笔规则 + 窗口画像叠加。
- **知识入库流水线**：扫描 `knowledge_base/`（子目录即分类），`.md/.txt` 直读、PDF/Word/PPT/HTML/RTF 走 Apache Tika 抽取，512 字切块 + 128 重叠；目录为空回退内置知识库。
- **存储方言层**：`StorageDialect` 统一 SQLite（本地零部署）与 MySQL（生产）的 DDL/主键/INSERT 语法差异，`SMARTCS_DB_TYPE=mysql` 一键切换。
- **可观测性**：AgentTracer 接 OpenTelemetry SDK，每个 Agent 调用生成 span（agent/duration/success/token 属性），配 OTLP endpoint 导 Jaeger/Tempo，缺省降级日志导出。

---

## 一次对话的完整旅程

以「帮我创建一个投诉工单，服务态度很差」为例：

```
① 接入层      POST /api/chat (sessionId=xxx)
② 编排层      Supervisor.decompose   → 拆出子任务 [创建投诉工单]
③ 编排层      IntentRouter           → intent = ticket_handler
④ 工具层      TicketHandler(function calling)
              → LLM 自主选 ticket_create + 抽参 {user_id, description, priority}
              → SQLite 落库，返回工单号 TK-20261006-28C190
⑤ 合规层      rules(禁词/PII) pass → LLM review pass → compliance_passed=true
⑥ 编排层      synthesize 汇总 → 返回「工单已创建，编号…」
⑦ 可观测      supervisor 根 span + ticket_handler span 同 traceId，token 已计量
⑧ Checkpoint 该 sessionId 又追加一条 checkpoint
```

每一步都可下钻验证：日志有 `[dispatch] step=…`、`[RAG] Step2/3`、`[compliance]`，指标有 `/api/metrics`，断点有 `/api/checkpoint/{sessionId}`。

---

## 双实现矩阵：Java × Python

| 模块 | Java（java-impl/） | Python（根目录） |
|------|--------------------|------------------|
| 编排 | Spring AI Alibaba Graph `StateGraph` | LangGraph `StateGraph` |
| 拓扑 | decompose→intent→dispatch⇄collect→compliance→synthesize | 同拓扑 |
| 短期记忆 | Redis StringRedisTemplate（降级内存） | Redis AIORedis（降级内存） |
| 长期检索 | BM25 + 向量 + RRF（**VectorRetriever SPI**，默认 memory-tf，可切 Milvus） | BM25(jieba) + FAISS + sentence-transformers + RRF |
| 重排 | LLM rerank（对齐 Python `_llm_rerank`） | bge-reranker-base（无模型降级 LLM） |
| 工具层 | MCP 风格工具 + **Function Calling** | MCP 工具 + 硬编码分发 |
| 风控 | 金额规则 + **Redis 滑动窗口** | 金额规则 |
| 知识入库 | **目录扫描 + Apache Tika 多格式解析** + 512/128 切块 | 内置文档 + 固定切块 |
| 持久化 | **SQLite/MySQL 方言层可切换** | SQLite |
| 断点续接 | **graph-core MemorySaver**（sessionId=thread_id） | LangGraph MemorySaver |
| 追踪 | **OpenTelemetry SDK**（OTLP/日志降级，含 token 计量） | OpenTelemetry + Jaeger |
| 评测口径 | 同一套 RAGAS judge 脚本，24 样本同库 | 同口径 |

> 结论：Java 版是「同构复刻 + 工程深化」——编排/评测对齐 Python，同时补了 Function Calling、滑动窗口、Tika 入库、MySQL 方言、Checkpoint、OTel 六项工程能力。

---

## 值得讲的技术决策

面试官问「为什么」，这些是你能展开讲的真实决策（都对应代码，不是包装）：

1. **为什么用图编排而不是 LLM 循环调用？** 图把「下一步走哪」变成可观测的条件边，子任务依赖、迭代上限、合规强制边都是显式结构，出错有统一的兜底与断点。
2. **为什么重排用 LLM 而不是 cross-encoder？** 环境无模型、零部署优先；评测（95.83/95.14 vs Python 95.83/94.79）证明数值相当，接口预留可平滑换 bge-reranker。→ 简历上写清口径，别混「含重排/不含重排」。
3. **为什么向量路用 TF 近似？** 纯本地零依赖跑通全链路；RRF 接口与真实 embedding（Spring AI / Milvus）平滑兼容，验收时不被模型下载卡住。面试主动说「这是近似、生产可换」比被动承认强。
4. **为什么规则引擎要否定豁免？** 「不承诺保本保息」是免责表述，朴素禁词会让合规误杀 23% 的负面知识文档 → 前 3 字符否定词检测，误判清零（chat-smoke 全绿）。
5. **为什么存储做方言层？** SQLite/MySQL 的 DDL、`INSERT OR IGNORE` vs `INSERT IGNORE`、主键类型差异全部收敛在一个类，业务 SQL 通用；本地开发零部署、生产切 MySQL 只改环境变量。
6. **为什么事务入窗、查询只读？** 风控窗口统计的是「行为」不是「咨询」——客服回复也走 risk_check，若查询也入窗，高频咨询会污染风控画像。

---

## 快速开始

### 前置

- JDK 21、Maven 3.9+（或使用 `mvnw`）
- 一个 OpenAI 兼容 API Key（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME`，见 `.env.example`）
- Redis（可选：不可用时短期记忆/风控窗口自动降级内存）

### 启动 Java 版（推荐，工程能力最全）

```powershell
cd java-impl
# 有代理时先设置
# $env:MAVEN_OPTS="-Dhttp.proxyHost=127.0.0.1 -Dhttp.proxyPort=7890 -Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=7890"
./mvnw.cmd -B -ntp package -DskipTests
java -jar target\smart-cs-agent-1.0.0.jar --server.port=18080
```

验证：`GET http://localhost:18080/health` → `{"status":"healthy"}`。

常用接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 多 Agent 对话（自动编排） |
| POST | `/api/tools/call` | 工具调用（MCP 风格） |
| GET | `/api/tools` | 工具发现（schema） |
| GET | `/api/metrics` | Agent 指标 + Token 计量 |
| GET | `/api/checkpoint/{sessionId}` | 断点续接信息 |
| GET | `/api/eval/rerank` | 检索评测（召回+重排，导出 RAGAS 输入） |

内置验收脚本（`java-impl/*.ps1`）：`boot-smoke` / `chat-smoke`（3 轮真实对话）/ `tools-smoke`（4 工具）/ `persistence-smoke`（重启不丢数据）/ `checkpoint-otel-smoke`（断点+追踪）。

### 启动 Python 版

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m api.main
```

访问 `http://localhost:8000/docs`（Swagger）。

### 切 MySQL（可选）

```powershell
$env:SMARTCS_DB_TYPE="mysql"
$env:SMARTCS_DB_URL="jdbc:mysql://localhost:3306/smartcs?useSSL=false&serverTimezone=Asia/Shanghai"
$env:SMARTCS_DB_USERNAME="root"
$env:SMARTCS_DB_PASSWORD="xxx"
```

---

## 仓库结构

```text
smart-cs-multi-agent/
├── java-impl/                    # Java 版（工程能力最全，推荐入口）
│   ├── pom.xml                   # Spring Boot 3.5.9 · Spring AI 1.1.2 · graph-core · Tika · OTel
│   ├── README.md                 # Java 版架构/API/对齐差异/阶段记录
│   ├── src/main/java/com/smartcs/
│   │   ├── agent/                # Supervisor/IntentRouter/KnowledgeRAG/TicketHandler/Compliance
│   │   ├── memory/               # ShortTerm(Redis·降级) · LongTerm(混合检索) · Reranker
│   │   ├── mcp/                  # MCPToolServer · MCPToolCallbacks(Function Calling)
│   │   ├── biz/                  # Order/Ticket Repository · RiskWindowService · StorageDialect
│   │   ├── tracing/              # AgentTracer(OpenTelemetry + token 计量)
│   │   └── config/               # ChatController · 配置
│   └── *-smoke.ps1               # boot/chat/tools/persistence/checkpoint-otel 验收脚本
├── agents/                       # Python 版 Agent 编排
├── api/                          # Python 版 FastAPI 入口
├── memory/                       # Python 版三层记忆
├── mcp/                          # Python 版 MCP 工具
├── tracing/                      # Python 版 OpenTelemetry
├── database/                     # Python 版 SQLite
├── eval/                         # 检索评测(RAGAS/IR) · token 优化评测 · Java 检索结果 dump
├── knowledge_base/               # 知识库目录（子目录=分类，24 篇 md + HTML 示例）
├── frontend/                     # React + Vite 联调页面
└── docs/                         # 架构/代码讲解/部署/interview 材料
```

---

## 评测与复现

检索评测（24 样本，top_k=3，知识库与 Python 同一份 `dataset_v2.json`）：

```powershell
# 1. Java 版导出检索结果（含重排）
#    先启动 Java 服务，再：
Invoke-RestMethod "http://localhost:18080/api/eval/rerank"  # 已在服务内置

# 2. Java 结果跑 RAGAS LLM-judge（同一套脚本，与 Python 版同口径）
$env:PYTHONPATH="D:\code\smart-cs-multi-agent"
& E:\miniconda3\envs\pytorchcuda\python.exe eval\ragas_judge.py `
    --dataset eval\dataset_v2.json --retrieval eval\java_retrievals_rerank.json

# 结果：Context Precision 95.83% / Context Recall 95.14%
```

评测归档：`eval/results_ragas_java_rerank.json`。判分逻辑、prompt、缓存全部在 `eval/ragas_judge.py`，可审计。

---

## 已知边界

诚实清单（面试主动说出来加分）：

1. **向量路默认是 TF 近似**，非真实 embedding：小库上够用，生产切真实 embedding + Milvus（`VectorRetriever` SPI，`SMARTCS_VECTOR_BACKEND=milvus`），RRF 接口不变。
2. **MySQL 分支语法正确、可切换**，但未在真实 MySQL 实例冒烟（本地无 MySQL）。
3. **Supervisor decompose 的 token 计量为 0**（BeanOutputConverter 路径拿不到 usage），其余 Agent 计量完整。
4. **编排为串行循环**：并行调度与 Human-in-the-Loop 能力已铺垫（dispatch_mode / Checkpoint），当前按依赖串行执行。
5. **RAGAS 数字含 LLM judge 随机性**：temperature=0 仍可能小幅波动，面试讲「24 样本、同口径实测」即可。

---

## FAQ

**Q：为什么用 Supervisor 模式而不是 Agent 互相对话？**
A：集中控制便于追踪/调试；避免 Agent 间循环依赖；Supervisor 可做全局决策（依赖顺序、迭代上限、合规强制边）；出错有统一兜底与断点恢复。

**Q：三层记忆怎么解决上下文丢失？**
A：核心是「按需注入」——不是堆数据，是选择性给。滚动摘要保留长期要点、最近 N 轮保留细节、其余丢弃；实测长会话 Token 消耗降 50% 且 T2 指代（「那最低投多少钱」）命中正确产品。

**Q：RAG 链路为什么是改写→双路→RRF→重排？**
A：改写解决口语化→检索友好；BM25 保专有名词、向量保语义，两路互补；RRF 按排序位置融合不依赖分数可比性；重排把最相关的排到 top-3。落点：Hit@3=100%、RAGAS 95.83/95.14。

**Q：合规审查怎么防止误判和漏判？**
A：规则引擎毫秒级先拦（禁词/PII），通过后才进入 LLM 深度审查（成本可控）；规则层做否定豁免降低误杀，LLM 兜隐晦违规；PII 脱敏后回传 synthesize，违规原因随响应透出。

---

## License

[MIT License](./LICENSE) — 自由使用、修改、分发。

---

<div align="center">

**如果这个项目对你有帮助，欢迎 ⭐ Star。**

</div>