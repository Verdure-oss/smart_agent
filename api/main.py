"""
FastAPI入口 — 提供REST API + SSE流式响应
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# 配置日志输出到控制台
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from agents.supervisor import create_supervisor_graph
from memory.working_memory import WorkingMemory
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from mcp.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import init_tracer, AgentMetrics
from database.db import init_db, seed_data

load_dotenv()


working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
long_term_memory = LongTermMemory(index_path=os.getenv("FAISS_INDEX_PATH", "./vector_store/faiss_index"))
mcp_server = create_default_tools(MCPToolServer(), long_term_memory)
metrics = AgentMetrics()
graph = None

# 全局 LLM 实例：主链路（Supervisor/子 Agent）与滚动摘要压缩共用，避免重复初始化
llm = ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-4o"), temperature=0)

# ── 滚动摘要（按需注入）配置 ──
RECENT_TURNS = 4          # 注入保留最近几轮原文
COMPACT_THRESHOLD = 6     # 新增超过多少条未压缩消息时触发一次压缩

SUMMARY_COMPACT_PROMPT = """你是一个会话记忆压缩器。请把以下客服对话历史压缩成一段简洁的中文摘要。

要求：
1. 保留：用户的核心诉求、已提供的关键信息（订单号/产品名/金额/身份证号等实体）、客服已答复的结论、尚未完成的待办事项
2. 丢弃：寒暄、重复表述、与业务无关的闲聊
3. 如果已有旧摘要，请基于旧摘要 + 新消息合并更新，不要丢失旧摘要里的待办信息
4. 只输出摘要文本本身，不要任何前后缀

{old_summary_block}新鲜对话:
{transcript}
"""


def _estimate_tokens(*texts: str) -> int:
    """用 tiktoken 估算文本的 token 数（无 tiktoken 时按字符数/2 粗略估算）"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return sum(len(enc.encode(t)) for t in texts if t)
    except Exception:
        return sum(max(len(t) // 2, 0) for t in texts if t)


async def _maybe_compact_history(session_id: str) -> None:
    """
    增量滚动压缩：当会话中新增的未压缩消息超过阈值时，
    把"旧消息"用 LLM 压缩进摘要，只保留最近几轮原文。
    """
    full = await short_term_memory.get_history(session_id)
    summary_text, compacted_upto = await short_term_memory.get_summary(session_id)

    if len(full) - compacted_upto <= COMPACT_THRESHOLD:
        return

    # 需要压缩的消息区间：[compacted_upto, len - RECENT_TURNS)
    cutoff = max(compacted_upto, len(full) - RECENT_TURNS)
    old_msgs = full[compacted_upto:cutoff]
    if not old_msgs:
        return

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in old_msgs)
    old_summary_block = f"旧摘要:\n{summary_text}\n\n" if summary_text else ""

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=SUMMARY_COMPACT_PROMPT.format(
                old_summary_block=old_summary_block,
                transcript=transcript,
            )),
        ])
        new_summary = resp.content.strip()
        if new_summary:
            await short_term_memory.set_summary(session_id, new_summary, cutoff)
            logging.info("[记忆] 会话 %s 滚动压缩: 压缩 %d 条消息 → 摘要", session_id, len(old_msgs))
    except Exception as e:
        logging.warning("[记忆] 会话 %s 压缩失败（跳过，不影响主流程）: %s", session_id, e)


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"


@asynccontextmanager 
#在服务启动时执行（startup）
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global graph

    init_tracer(
        service_name=os.getenv("OTEL_SERVICE_NAME", "smart-cs-multi-agent"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )

    # 初始化数据库（订单、工单等业务数据）
    init_db()
    seed_data()
    logging.info("数据库初始化完成")

    # 初始化多 Agent 系统
    graph = create_supervisor_graph(
        llm=llm,
        working_memory=working_memory,
        short_term_memory=short_term_memory,
        long_term_memory=long_term_memory,
        mcp_server=mcp_server,
    )

    # 知识库数据（FAISS向量存储）
    long_term_memory.add_document(
        content="我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。",
        source="product_faq.md",
    )
    long_term_memory.add_document(
        content="退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。",
        source="refund_policy.md",
    )
    long_term_memory.add_document(
        content="开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。",
        source="account_guide.md",
    )
    logging.info("知识库初始化完成")

    yield

# 在创建 FastAPI 应用，并把你前面的生命周期钩子挂进去
app = FastAPI(
    title="智能客服多Agent系统",
    description="基于LangGraph的Supervisor编排多Agent智能客服系统",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# 解决“前端跨域访问后端”的问题（CORS）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有网站访问（开发用）
    allow_credentials=True,
    allow_methods=["*"], # 允许所有请求方式（GET / POST / DELETE）
    allow_headers=["*"],
)

# 自定义全局异常处理（统一返回格式）
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return UTF8JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return UTF8JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool


# 前端可以用 POST /api/chat 调这个接口，而且返回的数据结构要符合 ChatResponse。
@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """主聊天接口"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())

    await short_term_memory.add_message(session_id, "user", request.message)

    # 先做增量滚动压缩（把旧消息压成摘要，保留最近几轮原文）
    await _maybe_compact_history(session_id)

    # 按需注入：滚动摘要（压缩上下文） + 最近几轮原文 + 当前消息
    summary_text, recent = await short_term_memory.get_injection_context(
        session_id, recent_turns=RECENT_TURNS
    )
    messages: list = []
    if summary_text:
        messages.append(
            SystemMessage(
                content=f"以下是本会话早前对话的滚动摘要，用于保持上下文连贯，无需重复回答其中已答复的内容：\n{summary_text}"
            )
        )
    for msg in recent:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        elif msg["role"] == "assistant":
            messages.append(AIMessage(content=msg["content"]))

    # 确保当前消息在最后
    if not messages or messages[-1].content != request.message:
        messages.append(HumanMessage(content=request.message))

    # Token 计量：全量历史（旧方案） vs 摘要+近轮（新方案） 的 Prompt Token 对比
    full_history = await short_term_memory.get_history(session_id, last_n=10)
    full_tokens = _estimate_tokens(
        *[f"{m['role']}: {m['content']}" for m in full_history]
    )
    injected_tokens = _estimate_tokens(
        *[m.content for m in messages]
    )
    if full_tokens > 0:
        token_saved_ratio = max(0.0, 1.0 - injected_tokens / full_tokens)
    else:
        token_saved_ratio = 0.0
    metrics.record_tokens("prompt_inject", injected_tokens)
    metrics.record_tokens("prompt_full_baseline", full_tokens)
    logging.info(
        "[记忆] 注入方案 token 对比: 全量=%d, 按需注入=%d, 节省 %.1f%% (session=%s)",
        full_tokens, injected_tokens, token_saved_ratio * 100, session_id,
    )

    initial_state = {
        "messages": messages,
        "user_id": request.user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
        "sub_tasks": [],
        "dependencies": [],
        "needs_parallel": False,
        "dispatch_mode": "chain",
        "task_chains": {},
        "current_sub_task_id": "",
        "current_step_index": 0,
        "task_results": {},
        "completed_task_ids": [],
        "_last_dispatched_agent": "",
    }

    # 使用唯一 thread_id，避免状态跨请求持久化
    # completed_ids、task_results 等不应该跨请求保留
    thread_id = f"{session_id}-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        # 触发整个多 Agent 流程执行（异步运行图 graph）
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理失败: {str(e)}")

    final_response = result.get("final_response", "系统处理异常，请稍后重试")

    await short_term_memory.add_message(session_id, "assistant", final_response)

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        compliance_passed=result.get("compliance_passed", True),
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: str):
    """获取对话历史"""
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.get("/api/tools")
async def list_tools():
    """MCP工具发现接口"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: dict):
    """MCP工具调用接口"""
    result = await mcp_server.call_tool(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
    )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/metrics")
async def get_metrics():
    """获取系统指标"""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


# 系统入口，负责启动应用、注册 API、初始化记忆和工具，并把聊天请求交给 `LangGraph` 图执行。
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
