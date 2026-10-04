import asyncio, os, sys
sys.path.insert(0, r"D:\code\smart-cs-multi-agent")
os.chdir(r"D:\code\smart-cs-multi-agent")
from dotenv import load_dotenv
load_dotenv(r"D:\code\smart-cs-multi-agent\.env", override=True)

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from agents.supervisor import SupervisorOutput, SUPERVISOR_DECOMPOSE_PROMPT

llm = ChatOpenAI(model=os.getenv("MODEL_NAME"), temperature=0)

async def t(method):
    try:
        m = llm.with_structured_output(SupervisorOutput, method=method)
        r = await m.ainvoke([
            SystemMessage(content=SUPERVISOR_DECOMPOSE_PROMPT),
            HumanMessage(content="用户消息: 帮我退款"),
        ])
        print(f"[{method}] OK: sub_tasks={[t.description for t in r.sub_tasks]}")
    except Exception as e:
        print(f"[{method}] FAIL:", str(e)[:250])

async def main():
    await t("json_schema")
    await t("function_calling")

asyncio.run(main())