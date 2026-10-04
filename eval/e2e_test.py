"""端到端真实对话验证（含多轮滚动摘要压缩）"""
import sys, os, asyncio
sys.path.insert(0, r"D:\code\smart-cs-multi-agent")
os.chdir(r"D:\code\smart-cs-multi-agent")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from dotenv import load_dotenv
load_dotenv(r"D:\code\smart-cs-multi-agent\.env", override=True)
print("[env] MODEL_NAME =", os.getenv("MODEL_NAME"))
print("[env] BASE_URL =", os.getenv("OPENAI_BASE_URL"))
print("[env] KEY set:", bool(os.getenv("OPENAI_API_KEY")))

import logging
logging.basicConfig(level=logging.WARNING)

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage
from memory.short_term import ShortTermMemory
from memory.long_term import LongTermMemory
from memory.working_memory import WorkingMemory
from mcp.mcp_server import MCPToolServer, create_default_tools
from agents.supervisor import create_supervisor_graph

llm = ChatOpenAI(model=os.getenv("MODEL_NAME", "gpt-4o"), temperature=0)
short_term = ShortTermMemory(redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
long_term = LongTermMemory(index_path=r"D:\code\smart-cs-multi-agent\vector_store\_e2e")
mcp_server = create_default_tools(MCPToolServer(), long_term)

# 知识库初始化（与 api/main.py 保持一致）
for content, source in [
    ("我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。注意：理财非存款，产品有风险，投资须谨慎。", "product_faq.md"),
    ("退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。", "refund_policy.md"),
    ("开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 5.完成风险评估问卷。整个流程约需15-30分钟。", "account_guide.md"),
]:
    long_term.add_document(content, source=source)

graph = create_supervisor_graph(
    llm=llm,
    working_memory=WorkingMemory(),
    short_term_memory=short_term,
    long_term_memory=long_term,
    mcp_server=mcp_server,
)
print("[graph] 节点:", list(graph.get_graph().nodes.keys()))


async def ask(session_id: str, msg: str, idx: int):
    await short_term.add_message(session_id, "user", msg)

    # 按需注入：摘要 + 近轮
    summary_text, recent = await short_term.get_injection_context(session_id, recent_turns=4)
    messages = []
    if summary_text:
        messages.append(AIMessage(content=f"[早前对话摘要] {summary_text}"))
    for m in recent:
        if m["role"] == "user":
            messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            messages.append(AIMessage(content=m["content"]))
    if not messages or messages[-1].content != msg:
        messages.append(HumanMessage(content=msg))

    full_history = await short_term.get_history(session_id, last_n=10)
    full_tokens = sum(len(f"{m['role']}: {m['content']}") // 2 for m in full_history)
    injected_tokens = sum(len(m.content) // 2 for m in messages)

    initial_state = {
        "messages": messages, "user_id": "e2e-user", "session_id": session_id,
        "intent": "", "sub_results": {}, "compliance_passed": True,
        "final_response": "", "current_agent": "", "retry_count": 0,
        "sub_tasks": [], "dependencies": [], "needs_parallel": False,
        "dispatch_mode": "chain", "task_chains": {}, "current_sub_task_id": "",
        "current_step_index": 0, "task_results": {}, "completed_task_ids": [],
        "_last_dispatched_agent": "",
    }
    config = {"configurable": {"thread_id": f"{session_id}-{idx}"}}
    result = await graph.ainvoke(initial_state, config=config)

    response = result.get("final_response", "(无回复)")
    await short_term.add_message(session_id, "assistant", response)

    print(f"\n[轮 {idx}] Q: {msg}")
    print(f"        intent={result.get('intent')} | compliance={result.get('compliance_passed')}")
    print(f"        tokens: 全量={full_tokens} → 注入={injected_tokens}")
    print(f"        A: {response[:220]}")
    return response


async def main():
    session_id = "e2e-session"
    # 多轮对话：覆盖知识检索、工单、补充信息
    await ask(session_id, "理财产品A的收益率是多少？", 1)
    await ask(session_id, "开户需要什么材料？", 2)
    await ask(session_id, "我想申请退款", 3)
    await ask(session_id, "帮我创建一个投诉工单，服务态度很差", 4)
    # 触发滚动摘要压缩的长会话
    for i in range(5, 12):
        await ask(session_id, f"顺便再问一下，第{i}个问题：理财产品A最低投多少钱？", i)

    summary, upto = await short_term.get_summary(session_id)
    print("\n" + "=" * 72)
    print("[滚动摘要] compacted_upto =", upto, "| 摘要:", summary[:200] if summary else "(无)")

asyncio.run(main())